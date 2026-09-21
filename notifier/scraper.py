"""
e-GURO (CCC LMS) checker.

Logs into the portal, reads the dashboard, compares against last run, and
emails you when something's new or urgent.
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

FILTER_TEXTS = ["ASSIGNED", "DUE_TODAY", "MISSED", "UNREAD"]
TYPE_TEXTS = [
    "LESSON",
    "ACTIVITY_QUIZ",
    "ASSESSMENT",
    "QUESTIONNAIRE",
    "SUBMIT_ANSWER",
    "FILE_LESSON",
    "LINK",
]

AGENTS_VALUE = json.dumps(
    {
        "device": "Chrome",
        "version": "122.0.0.0",
        "layout": "Blink",
        "os": {"architecture": 64, "family": "Windows", "version": "10"},
        "description": "Chrome 122.0.0.0 on Windows 10 64-bit",
    }
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}


# --- Distinct error types, so a failure actually tells you what kind of
# problem it is instead of one generic "authentication failed" message ---


class InvalidCredentialsError(Exception):
    """The portal's own Login Attempts counter went up - this is a real
    wrong username/password, not something a code fix can solve."""


class PortalStructureError(Exception):
    """Login was rejected but Login Attempts did NOT increase - this means
    the request itself was rejected before the portal even checked the
    password. Common causes: a stale CSRF token, a missing/wrong header, or
    (importantly) the portal blocking the request based on WHERE it came
    from - e.g. some school firewalls block traffic from cloud data center
    IP ranges (which is exactly what GitHub Actions runners use) even
    though the exact same request from a home internet connection works
    fine. If this keeps happening despite everything else checking out,
    that's the most likely explanation, and no header/token tweak fixes it -
    the local Windows notifier (running from your own home IP) becomes the
    reliable option instead.
    """


def _extract_login_attempts(soup: BeautifulSoup):
    import re

    text_node = soup.find(string=lambda t: t and "Login Attempts" in t)
    if not text_node:
        return None
    match = re.search(r"Login Attempts:\s*(\d+)", text_node)
    return int(match.group(1)) if match else None


def _attempt_login_once(session: requests.Session, username: str, password: str):
    try:
        resp = session.get(BASE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to reach LMS homepage: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    pre_attempts = _extract_login_attempts(soup)

    token_input = soup.find("input", {"name": "token_login_form"})
    if not token_input or not token_input.get("value"):
        raise PortalStructureError(
            "Could not find token_login_form on the homepage."
        )
    token = token_input["value"]

    payload = {
        "username": username,
        "password": password,
        "submit": "login",
        "token_login_form": token,
        "agents": AGENTS_VALUE,
    }
    post_headers = {
        **HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://lms.ccc.edu.ph",
        "Referer": "https://lms.ccc.edu.ph/index.php",
    }
    try:
        login_resp = session.post(
            LOGIN_POST_URL, data=payload, headers=post_headers, timeout=30
        )
        login_resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to submit login: {e}")

    dash_soup = BeautifulSoup(login_resp.text, "html.parser")

    print(f"[debug] POST status code: {login_resp.status_code}")
    print(f"[debug] Final URL after redirects: {login_resp.url}")

    if dash_soup.find("input", {"name": "password"}):
        post_attempts = _extract_login_attempts(dash_soup)
        print(f"[debug] Login Attempts before: {pre_attempts}, after: {post_attempts}")

        for el in dash_soup.select(".alert, .error, .text-danger, [class*='alert']"):
            text = el.get_text(strip=True)
            if text:
                print(f"[debug] Possible error message on page: {text}")

        snippet = dash_soup.get_text(" ", strip=True)[:300]
        print(f"[debug] Page text snippet: {snippet}")

        if (
            pre_attempts is not None
            and post_attempts is not None
            and post_attempts > pre_attempts
        ):
            raise InvalidCredentialsError(
                "Portal's Login Attempts counter increased - this is a real "
                "wrong username/password. Double-check LMS_USERNAME / "
                "LMS_PASSWORD in GitHub Secrets."
            )

        raise PortalStructureError(
            "Login was rejected but Login Attempts did not increase. See "
            "the [debug] lines above - this is NOT necessarily a wrong "
            "password."
        )

    return dash_soup


def log_in(session: requests.Session, username: str, password: str) -> BeautifulSoup:
    """Logs in, retrying once with a completely fresh token if the first
    failure looks structural rather than a genuine wrong password."""
    try:
        return _attempt_login_once(session, username, password)
    except PortalStructureError as first_error:
        print("[debug] First attempt looked structural - retrying once with a fresh token.")
        try:
            return _attempt_login_once(session, username, password)
        except PortalStructureError:
            raise first_error


def fetch_items(session: requests.Session, filter_text: str, type_text: str) -> list:
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
        return []
    return payload.get("data", []) or []


def gather_all_items(session: requests.Session) -> dict:
    items: dict = {}
    combo_hits = {}
    for filt in FILTER_TEXTS:
        for typ in TYPE_TEXTS:
            raw_items = fetch_items(session, filt, typ)
            if raw_items:
                combo_hits[f"{filt}/{typ}"] = len(raw_items)
            for raw in raw_items:
                item_id = raw.get("class_exam_id")
                if not item_id:
                    continue
                entry = items.setdefault(
                    item_id,
                    {
                        "title": raw.get("title") or "Untitled item",
                        "mark_type": raw.get("mark_type") or typ,
                        "from_date": raw.get("from_date"),
                        "to_date": raw.get("to_date"),
                        "filters": set(),
                    },
                )
                entry["filters"].add(filt)
    print(f"[debug] Combos with data this run: {combo_hits or 'none'}")
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
    prev_ids = set(previous.keys())
    new_items = [(i, v) for i, v in current.items() if i not in prev_ids]
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
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL", gmail_address)

    if not gmail_address or not gmail_app_password:
        raise ValueError("Missing GMAIL_ADDRESS or GMAIL_APP_PASSWORD environment variables.")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = notify_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [notify_email], msg.as_string())


def main():
    username = os.getenv("LMS_USERNAME")
    password = os.getenv("LMS_PASSWORD")
    if not username or not password:
        sys.exit("Error: LMS_USERNAME or LMS_PASSWORD environment variable is missing.")

    session = requests.Session()

    try:
        log_in(session, username, password)
    except InvalidCredentialsError as e:
        print(f"LOGIN FAILED (wrong credentials): {e}")
        sys.exit(1)
    except PortalStructureError as e:
        print(f"LOGIN FAILED (not a password problem - see debug lines above): {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"LOGIN FAILED (network/portal issue): {e}")
        sys.exit(1)

    items = gather_all_items(session)
    current = to_serializable(items)
    previous = load_previous_state()

    new_items, urgent_items = diff_states(previous, current)
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
