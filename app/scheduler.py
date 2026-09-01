from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import digest
from .db import get_connection

_scheduler = None


def run_hourly_check():
    """Runs every hour on the hour; sends each user their digest if this is
    their configured send hour. Simpler and more resilient than trying to
    schedule a separate per-user cron job for every possible hour."""
    now = datetime.now()
    conn = get_connection()
    try:
        users = conn.execute(
            "SELECT * FROM users WHERE digest_hour = ? AND digest_enabled = 1", (now.hour,)
        ).fetchall()
    finally:
        conn.close()

    for user in users:
        if not user["smtp_email"] or not user["smtp_app_password_encrypted"]:
            continue  # hasn't set up their sending account in Settings yet

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT due_date, title FROM assignments WHERE user_id = ?", (user["id"],)
            ).fetchall()
        finally:
            conn.close()
        items = [
            {"due_date": datetime.strptime(r["due_date"], "%Y-%m-%d").date(), "title": r["title"]}
            for r in rows
        ]

        # If every assignment this user ever entered is now in the past, their
        # course(s) have effectively ended -- stop emailing an empty digest
        # forever. This re-checks live each run, so uploading a new syllabus
        # (e.g. next semester) picks back up automatically, no toggle needed.
        if not items or max(i["due_date"] for i in items) < date.today():
            continue

        try:
            digest.send_digest_for_user(user, items)
        except Exception as e:
            print(f"[scheduler] Failed to send digest to {user['email']}: {e}")


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(run_hourly_check, "cron", minute=0)
    _scheduler.start()
