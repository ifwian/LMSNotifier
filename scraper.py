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

# Standard realistic User-Agent (avoid synthetic future versions that trigger bot filters)
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

def log_in(session: requests.Session, username: str, password: str) -> BeautifulSoup:
    try:
        resp = session.get(BASE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to reach LMS homepage (possible IP block or downtime): {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "token_login_form"})
    
    if not token_input or not token_input.get("value"):
        print(f"[debug] Response URL: {resp.url}")
        print(f"[debug] Response Preview: {resp.text[:300]}")
        raise RuntimeError(
            "Could not find token_login_form. The LMS may be blocking GitHub Actions IPs or requiring a CAPTCHA."
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
    
    login_resp = session.post(
        LOGIN_POST_URL, data=payload, headers=post_headers, timeout=30
    )
    login_resp.raise_for_status()

    dash_soup = BeautifulSoup(login_resp.text, "html.parser")

    if dash_soup.find("input", {"name": "password"}):
        raise RuntimeError(
            "LMS Authentication failed. Please verify LMS_USERNAME and LMS_PASSWORD in GitHub Secrets."
        )

    return dash_soup


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
    log_in(session, username, password)

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
