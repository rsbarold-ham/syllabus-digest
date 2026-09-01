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
                "SELECT due_date, title, urgent FROM assignments WHERE user_id = ? AND completed = 0",
                (user["id"],),
            ).fetchall()
        finally:
            conn.close()
        items = [
            {"due_date": datetime.strptime(r["due_date"], "%Y-%m-%d").date(), "title": r["title"], "urgent": bool(r["urgent"])}
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

    run_urgent_reminders()


def run_urgent_reminders():
    """Marking an item urgent starts its own 12-hour countdown (stored in
    the DB as urgent_marked_at, not an in-memory timer) so it survives a
    restart/redeploy mid-countdown. Checked on the same hourly heartbeat as
    the daily digest -- and therefore also covered by the external cron
    trigger that keeps a sleeping free-tier host honest."""
    conn = get_connection()
    try:
        due_rows = conn.execute(
            """SELECT id, user_id FROM assignments
               WHERE urgent = 1 AND urgent_reminder_sent = 0 AND completed = 0
                 AND urgent_marked_at IS NOT NULL
                 AND datetime(urgent_marked_at, '+12 hours') <= datetime('now')"""
        ).fetchall()
    finally:
        conn.close()

    for user_id in sorted({r["user_id"] for r in due_rows}):
        conn = get_connection()
        try:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        if not user or not user["smtp_email"] or not user["smtp_app_password_encrypted"]:
            continue

        conn = get_connection()
        try:
            urgent_rows = conn.execute(
                "SELECT due_date, title FROM assignments WHERE user_id = ? AND urgent = 1 AND completed = 0",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
        if not urgent_rows:
            continue
        items = [
            {"due_date": datetime.strptime(r["due_date"], "%Y-%m-%d").date(), "title": r["title"]}
            for r in urgent_rows
        ]

        try:
            digest.send_urgent_reminder(user, items)
        except Exception as e:
            print(f"[scheduler] Failed to send urgent reminder to {user['email']}: {e}")
            continue

        ids_triggered = [r["id"] for r in due_rows if r["user_id"] == user_id]
        conn = get_connection()
        try:
            conn.executemany(
                "UPDATE assignments SET urgent_reminder_sent = 1 WHERE id = ?",
                [(i,) for i in ids_triggered],
            )
            conn.commit()
        finally:
            conn.close()


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(run_hourly_check, "cron", minute=0)
    _scheduler.start()
