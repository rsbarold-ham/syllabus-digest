import os
import secrets
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, Request, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, parsing, digest, crypto
from .db import init_db, get_connection
from .scheduler import start_scheduler, run_hourly_check

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR.parent / "uploads"

# A missing SESSION_SECRET falls back to a random key generated at process
# start -- fine for local dev, but it means everyone is logged out on
# restart. Set SESSION_SECRET in .env for anything longer-lived.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def on_startup():
    init_db()
    UPLOAD_DIR.mkdir(exist_ok=True)
    start_scheduler()


def current_user_or_none(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user_or_none(request)
    return RedirectResponse("/dashboard" if user else "/login")


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not email or "@" not in email or len(password) < 8:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "Enter a valid email and a password of at least 8 characters."},
        )
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()
    if existing:
        return templates.TemplateResponse(
            request, "signup.html", {"error": "An account with that email already exists."}
        )
    user_id = auth.create_user(email, password)
    request.session["user_id"] = user_id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(email, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect email or password."}
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def require_user(request: Request):
    user = current_user_or_none(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(require_user)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM assignments WHERE user_id = ? ORDER BY due_date", (user["id"],)
        ).fetchall()
    finally:
        conn.close()
    today_iso = date.today().isoformat()
    has_upcoming = any(r["due_date"] >= today_iso for r in rows)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "assignments": rows,
            "message": request.session.pop("message", None),
            "current_year": date.today().year,
            "has_upcoming": has_upcoming,
        },
    )


@app.post("/upload")
def upload(
    request: Request,
    syllabus: UploadFile = File(...),
    year: int = Form(default=date.today().year),
    instructions: str = Form(default=""),
    user=Depends(require_user),
):
    suffix = Path(syllabus.filename).suffix.lower()
    if suffix not in (".pdf", ".docx", ".txt"):
        request.session["message"] = "Unsupported file type. Please upload a .pdf, .docx, or .txt file."
        return RedirectResponse("/dashboard", status_code=303)

    tmp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with tmp_path.open("wb") as f:
        shutil.copyfileobj(syllabus.file, f)

    warnings = []
    try:
        rows = parsing.parse_syllabus_file(tmp_path, year=year)
        rows, warnings = parsing.apply_instructions(rows, instructions, year=year)
    except Exception:
        rows = []
    finally:
        tmp_path.unlink(missing_ok=True)  # don't keep the uploaded file after parsing

    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO assignments (user_id, due_date, title) VALUES (?, ?, ?)",
            [(user["id"], r["due_date"], r["title"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()

    warning_note = f" Note: {' | '.join(warnings)}" if warnings else ""
    request.session["message"] = (
        f"Parsed {len(rows)} candidate item(s) from {syllabus.filename}.{warning_note} "
        "Review them below -- this is a first draft, not a final list."
    )
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/import-csv")
def import_csv(request: Request, csv_file: UploadFile = File(...), user=Depends(require_user)):
    """Bulk-import rows from a due_date,title CSV -- e.g. an assignments.csv
    already built and hand-corrected by the personal (local) version of this
    tool, so that work doesn't need to be redone through the syllabus parser."""
    import csv
    import io

    filename = csv_file.filename or ""
    if not filename.lower().endswith(".csv"):
        request.session["message"] = "Please upload a .csv file (columns: due_date, title)."
        return RedirectResponse("/dashboard", status_code=303)

    raw = csv_file.file.read().decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(raw))

    rows = []
    skipped = 0
    for row in reader:
        due = (row.get("due_date") or "").strip()
        title = (row.get("title") or "").strip()
        if not due or not title:
            skipped += 1
            continue
        try:
            date.fromisoformat(due)
        except ValueError:
            skipped += 1
            continue
        rows.append((user["id"], due, title))

    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO assignments (user_id, due_date, title) VALUES (?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()

    skip_note = f" ({skipped} row(s) skipped -- missing or malformed due_date/title.)" if skipped else ""
    request.session["message"] = f"Imported {len(rows)} row(s) from {filename}.{skip_note}"
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/assignments/add")
def add_assignment(request: Request, due_date: str = Form(...), title: str = Form(...), user=Depends(require_user)):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO assignments (user_id, due_date, title) VALUES (?, ?, ?)",
            (user["id"], due_date, title.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/assignments/{assignment_id}/edit")
def edit_assignment(assignment_id: int, request: Request, due_date: str = Form(...), title: str = Form(...), user=Depends(require_user)):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE assignments SET due_date = ?, title = ? WHERE id = ? AND user_id = ?",
            (due_date, title.strip(), assignment_id, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/assignments/{assignment_id}/delete")
def delete_assignment(assignment_id: int, user=Depends(require_user)):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM assignments WHERE id = ? AND user_id = ?", (assignment_id, user["id"])
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings/digest-hour")
def set_digest_hour(request: Request, digest_hour: int = Form(...), user=Depends(require_user)):
    digest_hour = max(0, min(23, digest_hour))
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET digest_hour = ? WHERE id = ?", (digest_hour, user["id"]))
        conn.commit()
    finally:
        conn.close()
    request.session["message"] = f"Daily digest time set to {digest_hour}:00."
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings/digest-toggle")
def toggle_digest(request: Request, enabled: int = Form(...), user=Depends(require_user)):
    enabled = 1 if enabled else 0
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET digest_enabled = ? WHERE id = ?", (enabled, user["id"]))
        conn.commit()
    finally:
        conn.close()
    request.session["message"] = "Daily emails paused." if not enabled else "Daily emails resumed."
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings/email")
def set_email_settings(request: Request, smtp_email: str = Form(...), app_password: str = Form(...), user=Depends(require_user)):
    smtp_email = smtp_email.strip()
    app_password = app_password.strip()
    if not smtp_email or not app_password:
        request.session["message"] = "Enter both your Gmail address and an app password."
        return RedirectResponse("/dashboard", status_code=303)

    is_first_time = not user["smtp_email"]

    encrypted = crypto.encrypt(app_password)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET smtp_email = ?, smtp_app_password_encrypted = ? WHERE id = ?",
            (smtp_email, encrypted, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    if is_first_time:
        try:
            subject, body = digest.build_welcome_email()
            digest.send_email(smtp_email, app_password, user["email"], subject, body)
        except Exception as e:
            print(f"[settings/email] Failed to send welcome email to {user['email']}: {e}")

    request.session["message"] = f"Sending account set to {smtp_email}."
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings/email/clear")
def clear_email_settings(request: Request, user=Depends(require_user)):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET smtp_email = NULL, smtp_app_password_encrypted = NULL WHERE id = ?",
            (user["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    request.session["message"] = "Sending account removed."
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/send-test")
def send_test(request: Request, user=Depends(require_user)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT due_date, title FROM assignments WHERE user_id = ?", (user["id"],)
        ).fetchall()
    finally:
        conn.close()
    items = [{"due_date": datetime.strptime(r["due_date"], "%Y-%m-%d").date(), "title": r["title"]} for r in rows]
    try:
        digest.send_digest_for_user(user, items)
        request.session["message"] = f"Test email sent to {user['email']}."
    except ValueError as e:
        request.session["message"] = str(e)
    except Exception as e:
        request.session["message"] = f"Failed to send: {e}"
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/internal/trigger-digest")
def trigger_digest(token: str):
    """Called by an external cron service (e.g. cron-job.org) on an hourly
    schedule, so digests still go out even if this app's own process was
    asleep (as free hosting tiers often are) and its in-process scheduler
    didn't fire. Not linked from the UI -- token is the only guard."""
    expected = os.environ.get("TRIGGER_SECRET")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid token")
    run_hourly_check()
    return {"status": "ok"}
