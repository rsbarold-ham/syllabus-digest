import os
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

# Every user sends through their own Gmail account + app password (entered
# in Settings) -- there is no shared sending account. Only the server
# hostname/port are global, since nearly everyone is on Gmail; a user could
# still override this per-account later if that changes.
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

# Used to build the "add more assignments" link at the bottom of each
# digest. Set this to the real deployed URL once this is hosted somewhere
# other than localhost (see README).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def bucket_items(items, today: date):
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)

    overdue, due_today, due_tomorrow, due_this_week = [], [], [], []
    for item in items:
        d = item["due_date"]
        if d < today:
            overdue.append(item)
        elif d == today:
            due_today.append(item)
        elif d == tomorrow:
            due_tomorrow.append(item)
        elif tomorrow < d <= week_end:
            due_this_week.append(item)

    for bucket in (overdue, due_today, due_tomorrow, due_this_week):
        bucket.sort(key=lambda i: i["due_date"])

    return overdue, due_today, due_tomorrow, due_this_week


def format_section(title, items):
    lines = [title]
    if not items:
        lines.append("  (nothing)")
    else:
        for item in items:
            lines.append(f"  - [{item['due_date'].strftime('%a %b %d')}] {item['title']}")
    return "\n".join(lines)


def build_email_body(today, overdue, due_today, due_tomorrow, due_this_week):
    sections = []
    if overdue:
        sections.append(format_section("OVERDUE:", overdue))
    sections.append(format_section("DUE TODAY:", due_today))
    sections.append(format_section("DUE TOMORROW:", due_tomorrow))
    sections.append(format_section("DUE THIS WEEK (next 7 days):", due_this_week))
    header = f"To-Do Digest for {today.strftime('%A, %B %d, %Y')}\n"
    footer = f"\nMissing something? Add it here: {APP_BASE_URL}/dashboard\n"
    return header + "\n\n".join(sections) + "\n" + footer


def build_digest_for_assignments(assignment_rows, today: date | None = None):
    """assignment_rows: list of {due_date: date, title: str}. Returns (subject, body)."""
    today = today or date.today()
    overdue, due_today, due_tomorrow, due_this_week = bucket_items(assignment_rows, today)
    body = build_email_body(today, overdue, due_today, due_tomorrow, due_this_week)
    subject = f"To-Do Digest -- {today.strftime('%b %d')}"
    return subject, body


def build_welcome_email():
    """One-time email sent the moment someone finishes connecting their
    sending account -- that's the earliest point the app is actually able
    to send them anything, since digests go out from their own address."""
    subject = "Thanks for signing up for Rachel's Schedule Reminder System!"
    body = """\
Thanks for signing up for Rachel's Schedule Reminder System!

You're set up to start getting a daily email with everything due today,
tomorrow, and in the coming week -- pulled straight from your syllabus.
Here's how to get the most out of it.

1. UPLOAD YOUR SYLLABUS
   Go to your dashboard and use the "Upload a syllabus" box. PDF, DOCX, and
   TXT all work. The parser looks for dated lines (like "Sept 15: Homework 2
   due") and for phrases like "due every Friday" and turns both into a list
   of assignments.

   Parsing a real syllabus is NEVER going to be perfect on the first try --
   syllabi are formatted too inconsistently for that. Treat the result as a
   rough draft, not a final answer.

2. USE PARSING INSTRUCTIONS TO CLEAN UP THE DRAFT WITHOUT EDITING BY HAND
   Right above the upload button is an optional "Parsing instructions" box.
   One instruction per line. Two kinds are understood:

     ignore lines containing "some text"
         Drops anything the parser picked up that isn't really an
         assignment -- for example a bibliography page range it mistook
         for a date, or a line about office hours.
         Example: ignore lines containing "office hours"

     add <title> due every <weekday> [at <time>]
         For recurring items your syllabus states in a way the parser
         can't catch on its own (it only recognizes the literal phrase
         "every <weekday>" in the source text).
         Example: add prelab due every wednesday at 12pm
         Example: add exit ticket due every friday

     add <title> on <date>
         For one-off items you know are missing.
         Example: add final project on december 15

   Anything written outside those exact shapes gets skipped and listed back
   to you after upload, rather than silently ignored or guessed at --
   that's your cue to just fix it by hand instead (see #3).

3. THE ASSIGNMENT TABLE IS ALWAYS YOURS TO EDIT DIRECTLY
   Every row on your dashboard has its own Save and Delete -- click into any
   due date or title and change it right there. There's also an "Add an
   item by hand" form at the bottom for anything you'd rather type in
   directly than describe as an instruction. Uploading a syllabus never
   replaces this -- it only adds to your list.

4. PICK WHEN YOUR DAILY EMAIL ARRIVES
   "Daily email" settings let you choose the hour (24-hour, server local
   time) your digest goes out. Use "Send me a test email now" any time to
   check it looks right -- that works even while paused (see #5).

5. PAUSE ANY TIME, RESUME ANY TIME
   Going on break, or just want quiet for a while? Hit "Pause daily
   emails." Nothing gets deleted -- your assignments and settings are still
   there when you hit "Resume."

6. IT TURNS ITSELF OFF WHEN YOUR COURSES END
   If every assignment on your list is in the past, the daily email stops
   on its own -- no more "nothing due" emails after finals. The moment you
   upload a syllabus with future dates (e.g. next semester), it picks back
   up automatically. No action needed either way.

7. YOUR SENDING PASSWORD
   You're sending through your own Gmail account using an app password --
   not your real Gmail password. That means:
     - It only works for sending email through this app, nothing else.
     - You can revoke it any time at myaccount.google.com/apppasswords
       without touching your real password.
     - You can remove it from this site any time from Settings, or replace
       it with a new one if you ever rotate it.

That's everything. If you run into any questions or concerns, send them to
rbarold@hamilton.edu -- and good luck this semester.
"""
    return subject, body


def send_digest_for_user(user_row, assignment_items, today: date | None = None):
    """user_row must have smtp_email / smtp_app_password_encrypted / email set.
    Raises ValueError if the user hasn't configured their sending account yet."""
    from . import crypto  # local import avoids a hard dependency for callers that don't send

    if not user_row["smtp_email"] or not user_row["smtp_app_password_encrypted"]:
        raise ValueError(
            "No sending email configured yet -- add your Gmail address and app "
            "password in Settings first."
        )
    app_password = crypto.decrypt(user_row["smtp_app_password_encrypted"])
    subject, body = build_digest_for_assignments(assignment_items, today)
    send_email(user_row["smtp_email"], app_password, user_row["email"], subject, body)
    return subject


def send_email(sender_email: str, app_password: str, to_address: str, subject: str, body: str):
    """Sends using the user's own Gmail address + app password (both entered
    in Settings), not a shared site-wide account."""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_address

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(sender_email, app_password)
        server.send_message(msg)
