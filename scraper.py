"""
e-GURO (CCC LMS) checker.

Logs into the portal, reads the dashboard summary cards (DUE TODAY, ASSIGNED,
MISSED, UNREAD), compares them against the last run, and emails you a
notification if anything changed.

Config comes from environment variables (see .github/workflows/check-lms.yml
and README.md for how these get set as GitHub Secrets):

  LMS_USERNAME        - your portal username
  LMS_PASSWORD        - your portal password
  GMAIL_ADDRESS        - gmail address to send FROM
  GMAIL_APP_PASSWORD   - gmail app password (not your normal password)
  NOTIFY_EMAIL         - where to send the notification (can be same as GMAIL_ADDRESS,
                          or a carrier email-to-SMS gateway address)
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://lms.ccc.edu.ph/"
LOGIN_POST_URL = "https://lms.ccc.edu.ph/app/login.php?formSubmitted=true"
COURSE_FILTER_URL = "https://lms.ccc.edu.ph/app/course_filter.php"
MAIN_STUDENT_URL = "https://lms.ccc.edu.ph/app/main_student.php"
STATE_FILE = "state.json"

# The dashboard tabs (ASSIGNED / DUE TODAY / MISSED / UNREAD)
FILTER_TEXTS = ["ASSIGNED", "DUE_TODAY", "MISSED", "UNREAD"]

# The category icons shown on the to-do page. Some of these are guesses based
# on the legend labels (Assessment, Activity/Quiz, Lesson, Questionnaire,
# Submit Answer, File Lesson, Link) - a wrong guess just returns no data for
# that combination, it won't error out.
TYPE_TEXTS = [
    "LESSON",
    "ACTIVITY_QUIZ",
    "ASSESSMENT",
    "QUESTIONNAIRE",
    "SUBMIT_ANSWER",
    "FILE_LESSON",
    "LINK",
]

# The portal expects a JSON blob describing the browser/OS in the "agents"
# field. Kept in sync with the fake User-Agent below.
AGENTS_VALUE = json.dumps(
    {
        "device": "Chrome",
        "version": "153.0.0.0",
        "layout": "Blink",
        "os": {"architecture": 64, "family": "Windows", "version": "10"},
        "description": "Chrome 153.0.0.0 on Windows 10 64-bit",
    }
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    )
}


def log_in(session: requests.Session, username: str, password: str) -> BeautifulSoup:
    """Load the login page, grab the CSRF token, submit credentials.

    Returns a BeautifulSoup of whatever page we land on after login
    (should be the dashboard if login succeeded).
    """
    # Step 1: load the homepage first (this is what a real browser does) so we
    # pick up the session cookies (PHPSESSID, lms_sys_ccc) AND the fresh token.
    resp = session.get(BASE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    token_input = soup.find("input", {"name": "token_login_form"})
    if not token_input or not token_input.get("value"):
        raise RuntimeError(
            "Could not find token_login_form on the homepage. "
            "The portal's login page structure may have changed."
        )
    token = token_input["value"]

    # Step 2: submit the login form to the actual login endpoint, with headers
    # that mimic the real browser request (Referer/Origin matter here).
    payload = {
        "username": username,
        "password": password,
        "submit": "login",  # lowercase - confirmed from a real browser request
        "token_login_form": token,
        "agents": AGENTS_VALUE,
    }
    post_headers = {
        **HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://lms.ccc.edu.ph",
        "Referer": "https://lms.ccc.edu.ph/index.php",
    }
    login_resp = session.post(
        LOGIN_POST_URL, data=payload, headers=post_headers, timeout=30
    )
    login_resp.raise_for_status()

    dash_soup = BeautifulSoup(login_resp.text, "html.parser")

    # --- Diagnostics: always print these so failed runs are debuggable ---
    print(f"[debug] POST status code: {login_resp.status_code}")
    print(f"[debug] Final URL after redirects: {login_resp.url}")

    attempts_text = dash_soup.find(string=lambda t: t and "Login Attempts" in t)
    if attempts_text:
        print(f"[debug] Page shows: {attempts_text.strip()}")

    # Look for any element that smells like an error/alert message
    for el in dash_soup.select(".alert, .error, .text-danger, [class*='alert']"):
        text = el.get_text(strip=True)
        if text:
            print(f"[debug] Possible error message on page: {text}")

    # Sanity check: if we're still on a page with a password field, login failed.
    if dash_soup.find("input", {"name": "password"}):
        snippet = dash_soup.get_text(" ", strip=True)[:300]
        print(f"[debug] Page text snippet: {snippet}")
        raise RuntimeError(
            "Login appears to have failed (still seeing a password field). "
            "Check the [debug] lines above for clues, and verify "
            "LMS_USERNAME / LMS_PASSWORD secrets, or the agents/token/submit fields."
        )

    return dash_soup


def fetch_items(session: requests.Session, filter_text: str, type_text: str) -> list:
    """Hit the AJAX endpoint behind one to-do tab/category combo."""
    ajax_headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": MAIN_STUDENT_URL,
    }
    resp = session.get(
        COURSE_FILTER_URL,
        params={"filter_text": filter_text, "type_text": type_text},
        headers=ajax_headers,
        timeout=30,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        print(
            f"[debug] Non-JSON response for {filter_text}/{type_text}: "
            f"{resp.text[:200]!r}"
        )
        return []
    return payload.get("data", []) or []


def gather_all_items(session: requests.Session) -> dict:
    """Query every filter/type combo and merge results by item id.

    Each item remembers which filter categories (ASSIGNED/DUE_TODAY/MISSED/
    UNREAD) it currently shows up under, since the same item can appear in
    more than one tab.
    """
    items: dict = {}
    for filt in FILTER_TEXTS:
        for typ in TYPE_TEXTS:
            for raw in fetch_items(session, filt, typ):
                item_id = raw.get("class_exam_id")
                if not item_id:
                    continue
                entry = items.setdefault(
                    item_id,
                    {
                        "title": raw.get("title"),
                        "mark_type": raw.get("mark_type"),
                        "from_date": raw.get("from_date"),
                        "to_date": raw.get("to_date"),
                        "filters": set(),
                    },
                )
                entry["filters"].add(filt)
    return items


def to_serializable(items: dict) -> dict:
    return {
        str(item_id): {
            "title": v["title"],
            "mark_type": v["mark_type"],
            "to_date": v["to_date"],
            "filters": sorted(v["filters"]),
        }
        for item_id, v in items.items()
    }


def load_previous_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def diff_states(previous: dict, current: dict):
    """Return (new_items, urgent_items) as lists of (id, info) tuples.

    new_items: ids that weren't seen last run at all.
    urgent_items: ids currently under DUE_TODAY or MISSED (whether new or not,
    so you keep getting reminded until it's resolved).
    """
    prev_ids = set(previous.keys())
    new_items = [
        (i, v) for i, v in current.items() if i not in prev_ids
    ]
    urgent_items = [
        (i, v)
        for i, v in current.items()
        if "DUE_TODAY" in v["filters"] or "MISSED" in v["filters"]
    ]
    return new_items, urgent_items


def format_item_line(item_id: str, info: dict) -> str:
    status = "MISSED" if "MISSED" in info["filters"] else (
        "DUE TODAY" if "DUE_TODAY" in info["filters"] else "ASSIGNED"
    )
    return f"- [{status}] {info['title']} ({info['mark_type']}) - due {info['to_date']}"


def send_email(subject: str, body: str) -> None:
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    notify_email = os.environ.get("NOTIFY_EMAIL", gmail_address)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = notify_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [notify_email], msg.as_string())


def main():
    username = os.environ["LMS_USERNAME"]
    password = os.environ["LMS_PASSWORD"]

    session = requests.Session()
    log_in(session, username, password)

    items = gather_all_items(session)
    current = to_serializable(items)
    previous = load_previous_state()

    print("Current items:", json.dumps(current, indent=2))

    new_items, urgent_items = diff_states(previous, current)
    # Avoid double-listing something that's both new AND urgent
    new_ids = {i for i, _ in new_items}
    urgent_only = [(i, v) for i, v in urgent_items if i not in new_ids]

    if new_items or urgent_items:
        lines = []
        if new_items:
            lines.append("NEW pending items:")
            lines.extend(format_item_line(i, v) for i, v in sorted(new_items))
        if urgent_only:
            if lines:
                lines.append("")
            lines.append("Still needs attention (due today / missed):")
            lines.extend(format_item_line(i, v) for i, v in sorted(urgent_only))
        lines.append("\nCheck: https://lms.ccc.edu.ph/")
        send_email("LMS: pending items", "\n".join(lines))
        print("Sent notification email.")
    else:
        print("Nothing new or urgent since last run.")

    save_state(current)


if __name__ == "__main__":
    main()
