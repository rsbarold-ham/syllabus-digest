# Syllabus Digest (Web MVP)

A multi-user version of the personal syllabus-digest script: anyone can sign
up, upload a syllabus, review the parsed assignments, and get a daily email
to-do list (today / tomorrow / this week).

## What's here

- **FastAPI + SQLite** backend (`app/`), no separate database server needed.
- Email/password accounts (passwords hashed with PBKDF2, no plaintext storage).
- **Every user sends through their own Gmail account.** There is no shared
  sending account -- each person generates their own Gmail app password
  (instructions are on the dashboard) and enters it in Settings. It's
  encrypted at rest (Fernet, key in `APP_ENCRYPTION_KEY`) and never shown
  back to anyone after saving.
- Syllabus upload (PDF/DOCX/TXT) using the same parsing logic validated in
  the personal script -- one-off dated items plus "due every Friday"-style
  recurring rules.
- An editable assignments table per user (add/edit/delete rows by hand).
- **Pause/resume** -- a button to turn your own daily emails off without
  deleting your account or data.
- **Auto-stops when your courses end** -- if every assignment you've ever
  entered is now in the past (semester's over), the digest stops sending on
  its own. No empty "nothing due" emails forever. It picks back up
  automatically the moment you add something with a future date (e.g. next
  semester's syllabus) -- no need to manually resume.
- A per-user daily send hour, an in-process hourly background job
  (APScheduler), *and* a token-protected `/internal/trigger-digest` HTTP
  endpoint an external cron service can hit -- see "Deploying for real"
  below for why both exist.
- A "send me a test email now" button (works even while paused, so you can
  verify setup).

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in SESSION_SECRET / APP_ENCRYPTION_KEY / TRIGGER_SECRET
uvicorn app.main:app --reload --app-dir .
```

Visit http://localhost:8000, sign up, add your Gmail address + app password
in Settings, upload a syllabus, hit "send me a test email now".

## Deploying for real (so it runs even when your computer is off)

This only works while `uvicorn` is running on *some* machine. To get off
your own PC:

1. **Push this folder to a GitHub repo** (private is fine).
2. **Create a free account on [Render](https://render.com)** and create a
   new "Web Service" from that repo. Build command: `pip install -r
   requirements.txt`. Start command: `uvicorn app.main:app --host 0.0.0.0
   --port $PORT`.
3. In Render's environment variables for the service, set `SESSION_SECRET`,
   `APP_ENCRYPTION_KEY`, and `TRIGGER_SECRET` (use the values from your
   local `.env`, or generate fresh ones the same way).
4. **Free-tier catch:** Render's free web services spin down after ~15
   minutes idle and only wake up on an incoming request -- so the
   in-process hourly scheduler won't fire reliably while asleep. Fix: sign
   up free at [cron-job.org](https://cron-job.org), and create a job that
   sends an hourly `POST` to:
   `https://<your-app>.onrender.com/internal/trigger-digest?token=<your TRIGGER_SECRET>`
   That ping wakes the app and runs the same digest check, so emails go out
   on schedule regardless of whether the free tier had gone to sleep.
5. **Data persistence:** Render's free tier disk is *ephemeral* -- the
   SQLite file gets wiped on redeploys/restarts, which is almost certainly
   why signups/settings/assignments have disappeared before. The fix
   (chosen over migrating to Postgres): attach a **persistent disk**, which
   requires Render's paid Starter plan or above.
     - On the service's Render page: **Disks** tab -> **Add Disk**.
     - Mount path: `/var/data` (any path works, this is just what the env
       var below should match).
     - Size: 1 GB is overkill for this app.
     - Then add an environment variable `DB_PATH` = `/var/data/app.db`.
     - Redeploy. The app now reads/writes the database on the persistent
       disk instead of next to its own code, so it survives every future
       redeploy. (The code change for this -- `DB_PATH` becoming
       configurable -- is already in this repo; attaching the disk and
       paying for the plan is the part only you can do.)

None of steps 2-5 can be done on your behalf -- they require an account,
and in step 5's case a paid plan, only you can create/approve. Everything
up through step 1 is already done for you in this repo.

## Other things worth knowing before wider use

- **Security hardening.** File uploads from strangers are a real attack
  surface -- add a file size limit and rate-limit signups/uploads before
  opening this to people you don't know.
- **Privacy/responsibility.** You'd be storing other people's email
  addresses, encrypted app passwords, and syllabus content. Worth a short
  privacy note on the signup page.
- **Time zones.** The "digest hour" is naive server-local time -- fine for
  people in one time zone, wrong for a spread-out audience.

## Project layout

```
app/
  main.py        FastAPI routes (signup/login/dashboard/upload/settings/trigger)
  db.py          SQLite schema + connection helper
  auth.py        Password hashing, session user lookup
  crypto.py      Fernet encryption for stored app passwords
  parsing.py     Syllabus text extraction + date/recurring-rule parsing
  digest.py      Bucketing (today/tomorrow/week) + email building/sending
  scheduler.py   Hourly check: send/skip/auto-stop logic per user
  templates/     Jinja2 HTML (login, signup, dashboard)
  static/        CSS
uploads/         Temp storage for files mid-parse (deleted right after)
app.db           SQLite database (created on first run)
```
