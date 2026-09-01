import os
import re
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _esc

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


# Same convention the syllabus parser and CSV import both produce: titles
# start with "[COURSE] rest of title". Mirrors the color scheme used on the
# dashboard table (same hue order, same alphabetical assignment) so a class
# looks the same color in your inbox as it does on the site -- though email
# clients can't read CSS custom properties, so these are plain hex, and this
# is a light-mode-only palette since dark-mode email support is too
# inconsistent across clients to rely on.
_COURSE_TAG_RE = re.compile(r"^\[([^\]]+)\]")
_COURSE_HEX = [
    ("#0e7c86", "#e3f2f2"),
    ("#6b4fa0", "#efe9f7"),
    ("#a63b62", "#f7e6ec"),
    ("#2f7d4f", "#e6f3ea"),
    ("#b3551f", "#f8ead9"),
    ("#4a5ba6", "#e7eaf7"),
    ("#8a7213", "#f5f0dc"),
    ("#8a3b72", "#f5e6f0"),
]


def _split_course_tag(title):
    m = _COURSE_TAG_RE.match(title)
    if not m:
        return None, title
    return m.group(1), title[m.end():].strip(" -:")


def _tint_hex(hex_color: str, toward_white: float = 0.85) -> str:
    """Lightens a user-chosen hex color for use as a badge background --
    email clients can't do CSS color-mix(), so this blends in Python."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = (round(c + (255 - c) * toward_white) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _course_color_map(items, overrides: dict | None = None):
    overrides = overrides or {}
    tags = sorted({tag for tag, _ in (_split_course_tag(i["title"]) for i in items) if tag})
    colors = {tag: _COURSE_HEX[i % len(_COURSE_HEX)] for i, tag in enumerate(tags)}
    for tag, hex_color in overrides.items():
        if tag in colors:
            colors[tag] = (hex_color, _tint_hex(hex_color))
    return colors


def _item_row_html(item, course_colors):
    tag, rest_title = _split_course_tag(item["title"])
    chip = ""
    if tag and tag in course_colors:
        fg, bg = course_colors[tag]
        chip = (
            f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'font-family:Arial,sans-serif;font-size:11px;font-weight:600;'
            f'padding:2px 8px;border-radius:999px;margin-right:6px;white-space:nowrap;">'
            f"{_esc(tag)}</span>"
        )
    urgent_badge = ""
    if item.get("urgent"):
        urgent_badge = (
            '<span style="display:inline-block;background:#a13327;color:#ffffff;'
            'font-family:Arial,sans-serif;font-size:10px;font-weight:700;'
            'letter-spacing:0.04em;padding:2px 7px;border-radius:4px;margin-right:6px;">'
            "URGENT</span>"
        )
    date_label = item["due_date"].strftime("%a %b %d")
    return (
        '<tr>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #e5e9f2;'
        f'font-family:\'IBM Plex Mono\',Consolas,monospace;font-size:12px;color:#57607c;'
        f'white-space:nowrap;vertical-align:top;width:78px;">{date_label}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #e5e9f2;'
        f'font-family:Arial,sans-serif;font-size:14px;color:#10192f;line-height:1.4;">'
        f"{urgent_badge}{chip}{_esc(rest_title)}</td>"
        "</tr>"
    )


def _section_html(title, items, course_colors, accent, accent_bg):
    if not items:
        rows_html = (
            '<tr><td colspan="2" style="padding:10px;color:#8891ab;'
            'font-family:Arial,sans-serif;font-size:13px;font-style:italic;">(nothing)</td></tr>'
        )
    else:
        rows_html = "".join(_item_row_html(i, course_colors) for i in items)
    return f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px;">
    <tr><td style="background:{accent_bg};border-left:4px solid {accent};padding:8px 12px;
      font-family:Arial,sans-serif;font-size:12px;font-weight:700;letter-spacing:0.05em;
      text-transform:uppercase;color:{accent};">{_esc(title)}</td></tr>
    <tr><td>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_html}</table>
    </td></tr>
  </table>"""


def build_email_html(today, overdue, due_today, due_tomorrow, due_this_week, dashboard_url, urgent_items, course_color_overrides=None):
    course_colors = _course_color_map((*overdue, *due_today, *due_tomorrow, *due_this_week), course_color_overrides)

    sections = []
    if urgent_items:
        sections.append(_section_html("Urgent", urgent_items, course_colors, "#a13327", "#fbeae7"))
    if overdue:
        sections.append(_section_html("Overdue", overdue, course_colors, "#a13327", "#fbeae7"))
    sections.append(_section_html("Due Today", due_today, course_colors, "#002f86", "#e3e9f8"))
    sections.append(_section_html("Due Tomorrow", due_tomorrow, course_colors, "#4a5ba6", "#eef1fa"))
    sections.append(_section_html("Due This Week", due_this_week, course_colors, "#8f6f37", "#f6eedd"))

    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6fb;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #d7dcec;">
  <tr>
    <td style="background-color:#002f86;background-image:linear-gradient(135deg,#002f86,#001f5c);border-bottom:4px solid #b9924f;padding:20px 24px;">
      <div style="font-family:Georgia,serif;font-size:20px;font-weight:700;color:#ffffff;">Syllabus Digest</div>
      <div style="font-family:Arial,sans-serif;font-size:13px;color:#dfe6fa;margin-top:4px;">{_esc(today.strftime('%A, %B %d, %Y'))}</div>
    </td>
  </tr>
  <tr><td style="padding:20px 20px 4px;">
    {''.join(sections)}
  </td></tr>
  <tr>
    <td style="padding:16px 20px 24px;border-top:1px solid #e5e9f2;">
      <a href="{_esc(dashboard_url)}" style="display:inline-block;background:#b9924f;color:#ffffff;
        font-family:Arial,sans-serif;font-size:13px;font-weight:600;text-decoration:none;
        padding:10px 18px;border-radius:6px;">Add more assignments</a>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def format_section(title, items):
    lines = [title]
    if not items:
        lines.append("  (nothing)")
    else:
        for item in items:
            marker = "*** URGENT *** " if item.get("urgent") else ""
            lines.append(f"  - [{item['due_date'].strftime('%a %b %d')}] {marker}{item['title']}")
    return "\n".join(lines)


def build_email_body(today, overdue, due_today, due_tomorrow, due_this_week, dashboard_url, urgent_items):
    sections = []
    if urgent_items:
        sections.append(format_section("URGENT:", urgent_items))
    if overdue:
        sections.append(format_section("OVERDUE:", overdue))
    sections.append(format_section("DUE TODAY:", due_today))
    sections.append(format_section("DUE TOMORROW:", due_tomorrow))
    sections.append(format_section("DUE THIS WEEK (next 7 days):", due_this_week))
    header = f"To-Do Digest for {today.strftime('%A, %B %d, %Y')}\n"
    footer = f"\nAdd more assignments (goes straight to your account): {dashboard_url}\n"
    return header + "\n\n".join(sections) + "\n" + footer


def build_digest_for_assignments(assignment_rows, dashboard_url, today: date | None = None, course_color_overrides=None):
    """assignment_rows: list of {due_date: date, title: str, urgent: bool}.
    Returns (subject, text_body, html_body) -- html_body is the styled
    version sent alongside the plain-text fallback in the same email."""
    today = today or date.today()
    overdue, due_today, due_tomorrow, due_this_week = bucket_items(assignment_rows, today)
    urgent_items = sorted(
        (i for i in (*overdue, *due_today, *due_tomorrow, *due_this_week) if i.get("urgent")),
        key=lambda i: i["due_date"],
    )
    text_body = build_email_body(today, overdue, due_today, due_tomorrow, due_this_week, dashboard_url, urgent_items)
    html_body = build_email_html(
        today, overdue, due_today, due_tomorrow, due_this_week, dashboard_url, urgent_items, course_color_overrides
    )
    subject = f"To-Do Digest -- {today.strftime('%b %d')}"
    return subject, text_body, html_body


def magic_dashboard_url(user_row, base_url: str | None = None) -> str:
    """The personalized link put in email footers -- clicking it logs the
    user straight in and lands on their dashboard, no password needed. Older
    accounts created before this existed get a token lazily on first use.

    `base_url`, when given, overrides the APP_BASE_URL env var -- callers
    that run inside an actual HTTP request (dashboard view, test-send,
    welcome email at signup) should pass the request's own host, so the
    link is correct even if APP_BASE_URL was never configured. Only the
    background scheduler, which has no request to read a host from, has to
    fall back to the env var."""
    from . import auth  # local import avoids a hard dependency for callers that don't need it

    token = user_row["magic_token"]
    if not token:
        token = auth.regenerate_magic_token(user_row["id"])
    root = (base_url or APP_BASE_URL).rstrip("/")
    return f"{root}/go/{token}"


def build_welcome_email(dashboard_url: str):
    """One-time email sent the moment someone finishes connecting their
    sending account -- that's the earliest point the app is actually able
    to send them anything, since digests go out from their own address."""
    subject = "Thanks for signing up for Rachel's Schedule Reminder System!"
    body = f"""\
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

Your personal link (bookmark it, or just use it from your next digest
email) -- opening it logs you straight in, no password needed:
{dashboard_url}

That's everything. If you run into any questions or concerns, send them to
rbarold@hamilton.edu -- and good luck this semester.
"""

    sections = [
        (
            "Upload your syllabus",
            "Go to your dashboard and use the “Upload a syllabus” box. PDF, DOCX, and TXT "
            "all work. The parser looks for dated lines (like “Sept 15: Homework 2 due”) and "
            "for phrases like “due every Friday” and turns both into a list of assignments."
            "<br><br>Parsing a real syllabus is <strong>never</strong> going to be perfect on the "
            "first try — syllabi are formatted too inconsistently for that. Treat the result as "
            "a rough draft, not a final answer.",
        ),
        (
            "Use parsing instructions to clean up the draft without editing by hand",
            "One instruction per line, right above the upload button. Two kinds are understood:"
            '<br><br><code style="background:#eef1f7;padding:2px 6px;border-radius:4px;">'
            "ignore lines containing \"office hours\"</code> — drops false positives like a "
            "bibliography page range mistaken for a date."
            '<br><br><code style="background:#eef1f7;padding:2px 6px;border-radius:4px;">'
            "add prelab due every wednesday at 12pm</code> or "
            '<code style="background:#eef1f7;padding:2px 6px;border-radius:4px;">'
            "add final project on december 15</code> — for recurring or one-off items the "
            "parser can't catch on its own."
            "<br><br>Anything else gets skipped and reported back to you — that's your cue to "
            "fix it by hand instead (see next).",
        ),
        (
            "The assignment table is always yours to edit directly",
            "Every row saves automatically as you edit it, plus its own Delete. There's also an "
            "“Add an item by hand” form for anything you'd rather type in directly. "
            "Uploading a syllabus never replaces this — it only adds to your list.",
        ),
        (
            "Pick when your daily email arrives",
            "“Daily email” settings let you choose the hour your digest goes out. Use "
            "“Send me a test email now” any time to check it looks right — that works "
            "even while paused.",
        ),
        (
            "Pause any time, resume any time",
            "Hit “Pause daily emails.” Nothing gets deleted — your assignments and "
            "settings are still there when you hit “Resume.”",
        ),
        (
            "It turns itself off when your courses end",
            "If every assignment on your list is in the past, the daily email stops on its own — "
            "no more “nothing due” emails after finals. Uploading a syllabus with future "
            "dates (e.g. next semester) picks it back up automatically.",
        ),
        (
            "Your sending password",
            "You're sending through your own Gmail account using an app password, not your real "
            "Gmail password. Revoke it any time at myaccount.google.com/apppasswords, or remove/"
            "replace it any time from Settings.",
        ),
    ]

    section_html = "".join(
        f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px;">
    <tr><td style="width:32px;vertical-align:top;padding-top:2px;">
      <div style="width:24px;height:24px;border-radius:50%;background:#e3e9f8;color:#002f86;
        font-family:Arial,sans-serif;font-size:12px;font-weight:700;text-align:center;line-height:24px;">{i}</div>
    </td>
    <td style="padding-left:6px;">
      <div style="font-family:Arial,sans-serif;font-size:14px;font-weight:700;color:#10192f;margin-bottom:4px;">{_esc(heading)}</div>
      <div style="font-family:Arial,sans-serif;font-size:13px;color:#3d4661;line-height:1.55;">{content}</div>
    </td></tr>
  </table>"""
        for i, (heading, content) in enumerate(sections, start=1)
    )

    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6fb;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #d7dcec;">
  <tr>
    <td style="background-color:#002f86;background-image:linear-gradient(135deg,#002f86,#001f5c);border-bottom:4px solid #b9924f;padding:24px;">
      <div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#ffffff;">Welcome to Rachel's Schedule Reminder System!</div>
      <div style="font-family:Arial,sans-serif;font-size:13px;color:#dfe6fa;margin-top:6px;">Here's how to get the most out of it</div>
    </td>
  </tr>
  <tr><td style="padding:22px 22px 4px;">{section_html}</td></tr>
  <tr>
    <td style="padding:8px 22px 20px;">
      <a href="{_esc(dashboard_url)}" style="display:inline-block;background:#b9924f;color:#ffffff;
        font-family:Arial,sans-serif;font-size:13px;font-weight:600;text-decoration:none;
        padding:10px 18px;border-radius:6px;">Go to your dashboard</a>
    </td>
  </tr>
  <tr>
    <td style="padding:14px 22px 22px;border-top:1px solid #e5e9f2;font-family:Arial,sans-serif;font-size:12px;color:#8891ab;">
      Questions or concerns? <a href="mailto:rbarold@hamilton.edu" style="color:#4a5ba6;">rbarold@hamilton.edu</a> — good luck this semester.
    </td>
  </tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    return subject, body, html_body


def _fetch_course_color_overrides(user_id: int) -> dict:
    from .db import get_connection

    conn = get_connection()
    try:
        rows = conn.execute("SELECT tag, color_hex FROM course_colors WHERE user_id = ?", (user_id,)).fetchall()
    finally:
        conn.close()
    return {r["tag"]: r["color_hex"] for r in rows}


def send_digest_for_user(user_row, assignment_items, today: date | None = None, base_url: str | None = None):
    """user_row must have smtp_email / smtp_app_password_encrypted / email set.
    Raises ValueError if the user hasn't configured their sending account yet."""
    from . import crypto  # local import avoids a hard dependency for callers that don't send

    if not user_row["smtp_email"] or not user_row["smtp_app_password_encrypted"]:
        raise ValueError(
            "No sending email configured yet -- add your Gmail address and app "
            "password in Settings first."
        )
    app_password = crypto.decrypt(user_row["smtp_app_password_encrypted"])
    dashboard_url = magic_dashboard_url(user_row, base_url)
    overrides = _fetch_course_color_overrides(user_row["id"])
    subject, text_body, html_body = build_digest_for_assignments(assignment_items, dashboard_url, today, overrides)
    send_email(user_row["smtp_email"], app_password, user_row["email"], subject, text_body, html_body)
    return subject


def send_urgent_reminder(user_row, items, base_url: str | None = None):
    """items: everything currently urgent + incomplete for this user (not
    just the one that crossed its 12-hour mark) -- see scheduler.py."""
    from . import crypto

    if not user_row["smtp_email"] or not user_row["smtp_app_password_encrypted"]:
        raise ValueError("No sending email configured -- skipping urgent reminder.")

    app_password = crypto.decrypt(user_row["smtp_app_password_encrypted"])
    dashboard_url = magic_dashboard_url(user_row, base_url)
    items_sorted = sorted(items, key=lambda i: i["due_date"])

    subject = "Urgent Reminder -- items you flagged"
    text_lines = ["You flagged these as urgent 12+ hours ago:\n"]
    for i in items_sorted:
        text_lines.append(f"  - [{i['due_date'].strftime('%a %b %d')}] {i['title']}")
    text_lines.append(f"\nManage these: {dashboard_url}\n")
    text_body = "\n".join(text_lines)

    overrides = _fetch_course_color_overrides(user_row["id"])
    course_colors = _course_color_map(items_sorted, overrides)
    section = _section_html("Still urgent", items_sorted, course_colors, "#a13327", "#fbeae7")
    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6fb;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #d7dcec;">
  <tr>
    <td style="background-color:#a13327;border-bottom:4px solid #b9924f;padding:20px 24px;">
      <div style="font-family:Georgia,serif;font-size:20px;font-weight:700;color:#ffffff;">Urgent Reminder</div>
      <div style="font-family:Arial,sans-serif;font-size:13px;color:#f7d9d4;margin-top:4px;">Flagged urgent 12+ hours ago and still on your list</div>
    </td>
  </tr>
  <tr><td style="padding:20px 20px 4px;">{section}</td></tr>
  <tr>
    <td style="padding:16px 20px 24px;border-top:1px solid #e5e9f2;">
      <a href="{_esc(dashboard_url)}" style="display:inline-block;background:#b9924f;color:#ffffff;
        font-family:Arial,sans-serif;font-size:13px;font-weight:600;text-decoration:none;
        padding:10px 18px;border-radius:6px;">Manage assignments</a>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    send_email(user_row["smtp_email"], app_password, user_row["email"], subject, text_body, html_body)


def verify_smtp_credentials(sender_email: str, app_password: str):
    """Attempts to log in only (no email sent), so Settings can confirm a
    credential works the moment it's entered instead of only finding out at
    the next send attempt. Returns (status, message):
      status "valid"   -- log in succeeded.
      status "invalid" -- Google definitively rejected it; do not save this.
      status "unknown" -- couldn't reach Gmail to check (network blip etc.);
                          safe to save, just unconfirmed.
    """
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(sender_email, app_password)
        return "valid", "Connected successfully."
    except smtplib.SMTPAuthenticationError:
        return "invalid", (
            f"Google rejected that app password for {sender_email}. Double-check "
            "it's an app password (not your real Gmail password) and that it "
            "belongs to this exact address, then try again."
        )
    except (smtplib.SMTPException, OSError) as e:
        return "unknown", f"Saved, but couldn't confirm it with Gmail right now ({e})."


def send_email(sender_email: str, app_password: str, to_address: str, subject: str, body: str, html_body: str | None = None):
    """Sends using the user's own Gmail address + app password (both entered
    in Settings), not a shared site-wide account. Plain `body` is always
    included -- as the whole message when html_body is omitted (e.g. the
    welcome email), or as the multipart/alternative fallback for clients
    that don't render HTML when it's given."""
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_address

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(sender_email, app_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ValueError(
            f"Google rejected the app password for {sender_email}. This means the "
            "email/app-password pair in Settings doesn't work anymore -- it's not "
            "something to retry, it needs fixing there. Most often this is because "
            "a real Gmail password was entered instead of an app password, the "
            "address doesn't match the Google account the app password was made "
            "for, or the app password was revoked. Generate a fresh one at "
            "myaccount.google.com/apppasswords and re-enter it under \"Replace it\" "
            "in Email sending."
        )
