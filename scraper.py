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

LOGIN_PAGE_URL = "https://lms.ccc.edu.ph/app/login.php?formSubmitted=true"
STATE_FILE = "state.json"

# The portal expects a JSON blob describing the browser/OS in the "agents"
# field. Kept in sync with the fake User-Agent below.
AGENTS_VALUE = json.dumps(
    {
        "device": "Chrome",
        "version": "124.0.0.0",
        "layout": "Blink",
        "os": {"architecture": 64, "family": "Windows", "version": "10"},
        "description": "Chrome 124.0.0.0 on Windows 10 64-bit",
    }
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def log_in(session: requests.Session, username: str, password: str) -> BeautifulSoup:
    """Load the login page, grab the CSRF token, submit credentials.

    Returns a BeautifulSoup of whatever page we land on after login
    (should be the dashboard if login succeeded).
    """
    # Step 1: load the login page to get the CSRF token
    resp = session.get(LOGIN_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    token_input = soup.find("input", {"name": "token_login_form"})
    if not token_input or not token_input.get("value"):
        raise RuntimeError(
            "Could not find token_login_form on the login page. "
            "The portal's login page structure may have changed."
        )
    token = token_input["value"]

    # Step 2: submit the login form
    payload = {
        "username": knrsvlcs,
        "password": Mariannerose_14,
        "submit": "Login",  # adjust if the real button value differs
        "token_login_form": token,
        "agents": AGENTS_VALUE,
    }
    login_resp = session.post(
        LOGIN_PAGE_URL, data=payload, headers=HEADERS, timeout=30
    )
    login_resp.raise_for_status()

    dash_soup = BeautifulSoup(login_resp.text, "html.parser")

    # Sanity check: if we're still on a page with a password field, login failed.
    if dash_soup.find("input", {"name": "password"}):
        raise RuntimeError(
            "Login appears to have failed (still seeing a password field). "
            "Check LMS_USERNAME / LMS_PASSWORD secrets, or the agents/token fields."
        )

    return dash_soup


def parse_dashboard_cards(soup: BeautifulSoup) -> dict:
    """Pull out each summary card's label -> (count, sub-label, link)."""
    results = {}

    for card in soup.select(".single_crm.card"):
        head = card.select_one(".crm_head span")
        count_el = card.select_one(".crm_body h4")
        sub_el = card.select_one(".crm_body p")
        link_el = card.select_one("a.click_me")

        if not head or not count_el:
            continue

        label = head.get_text(strip=True)
        try:
            count = int(count_el.get_text(strip=True))
        except ValueError:
            count = count_el.get_text(strip=True)
        sub_label = sub_el.get_text(strip=True) if sub_el else ""
        link = link_el["href"] if link_el and link_el.has_attr("href") else None

        # Cards repeat labels (DUE TODAY appears twice: Activity&Quiz, Assessment)
        key = f"{label} - {sub_label}" if sub_label else label
        results[key] = {"count": count, "link": link}

    return results


def load_previous_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def diff_states(old: dict, new: dict) -> list[str]:
    """Return a list of human-readable change lines."""
    changes = []
    for key, new_info in new.items():
        old_info = old.get(key)
        old_count = old_info["count"] if old_info else 0
        new_count = new_info["count"]

        if isinstance(new_count, int) and isinstance(old_count, int):
            if new_count > old_count:
                changes.append(
                    f"{key}: {old_count} -> {new_count} "
                    f"({new_info['link']})" if new_info["link"] else
                    f"{key}: {old_count} -> {new_count}"
                )
    return changes


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
    dash_soup = log_in(session, username, password)
    current = parse_dashboard_cards(dash_soup)

    if not current:
        print("Warning: no dashboard cards found. Portal layout may have changed.")
        sys.exit(1)

    previous = load_previous_state()
    changes = diff_states(previous, current)

    print("Current state:", json.dumps(current, indent=2))

    if changes:
        body_lines = ["Your LMS dashboard has new pending items:\n"]
        body_lines.extend(f"- {c}" for c in changes)
        body_lines.append("\nCheck: https://lms.ccc.edu.ph/")
        send_email("LMS: new pending items", "\n".join(body_lines))
        print("Sent notification email.")
    else:
        print("No changes since last run.")

    save_state(current)


if __name__ == "__main__":
    main()
