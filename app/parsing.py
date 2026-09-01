import re
from datetime import date, datetime, timedelta
from pathlib import Path

from dateutil import parser as dateparser

_MONTH_NAMES = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"

DATE_HINT = re.compile(
    r"""(
        \b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b
        |\b\d{1,2}-\d{1,2}-\d{2,4}\b
        |\b"""
    + _MONTH_NAMES
    + r"""\s+\d{1,2}(st|nd|rd|th)?\b
        |\b\d{1,2}-"""
    + _MONTH_NAMES
    + r"""\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

RECURRING_HINT = re.compile(
    r"\bevery\s+(?:other\s+)?(?P<weekday>" + "|".join(WEEKDAYS) + r")s?\b",
    re.IGNORECASE,
)

# A "24-Aug" / "31-Aug" style date (day-before-month, no comma) shows up
# almost exclusively in compact weekly itinerary tables (date | week# |
# topic | lab | ...), never in prose -- so a match on that specific shape is
# treated as a strong signal the line is a topic/schedule row, not an
# assignment. Contrast with "August 27: ..." (month-before-day), which is
# how narrative reading-list syllabi write real due dates.
_DAY_MONTH_TABLE_FORMAT = re.compile(r"^\d{1,2}-" + _MONTH_NAMES, re.IGNORECASE)

# A second, independent signal: itinerary tables commonly pack a week number
# right after the date ("24-Aug 0 intuitions", "14-Sep 3 Astronomy: life").
# A bare small number immediately after the date match is very unlikely to
# start a real assignment title.
_LEADING_WEEK_NUMBER = re.compile(r"^\d{1,2}\b")

# Words that indicate a line is actually describing something due, not just
# a lecture/reading topic for that date. Required before a line flagged as
# table-like (above) is allowed through as an assignment.
_ASSIGNMENT_KEYWORDS = re.compile(
    r"\b(due|submit(ted|ting)?|turn(ed)?\s*in|hand(ed)?\s*in|deadline|assignment|"
    r"homework|hw\b|quiz|exam|midterm|final(?:\s+exam)?|paper\b|project\b|essay|"
    r"presentation|problem\s*set|pset\b|prelab|lab\s*report|worksheet|"
    r"response\s*paper|discussion\s*post|upload|complete|finish)\b",
    re.IGNORECASE,
)


# Detects a course-code header line ("MATH 113", "PHYS190", "JWST-219",
# "CS 101A") so one big uploaded document covering several classes can have
# its items auto-tagged per class, the same "[TAG] title" convention used
# everywhere else in this app. This is a pattern heuristic, not real
# document understanding -- it only fires when 2+ distinct course codes
# are found (so an ordinary single-class syllabus is never touched), and
# it's always safe to correct afterward with an "ignore"/"add" instruction
# or by hand-editing the row.
_COURSE_HEADER_RE = re.compile(r"^([A-Z]{2,6}[-\s]\d{2,4}[A-Z]?)\b")


def detect_course_sections(text: str):
    """Returns {line_index: course_label} for each detected header line."""
    sections = {}
    for i, raw_line in enumerate(text.splitlines()):
        m = _COURSE_HEADER_RE.match(raw_line.strip())
        if m:
            label = re.sub(r"\s+", " ", m.group(1).strip())
            sections[i] = label
    return sections


def _looks_like_topic_row(date_text: str, title: str) -> bool:
    """True if this looks like a weekly-schedule/topic row rather than a
    real assignment -- i.e. it has a table-row signal and no due-item
    keyword to override that suspicion."""
    is_table_shaped = bool(_DAY_MONTH_TABLE_FORMAT.match(date_text)) or bool(
        _LEADING_WEEK_NUMBER.match(title)
    )
    return is_table_shaped and not _ASSIGNMENT_KEYWORDS.search(title)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        chunks = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    elif suffix == ".docx":
        import docx

        d = docx.Document(str(path))
        lines = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(lines)
    elif suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def guess_year_for(month: int, day: int, default_year: int) -> int:
    candidate = date(default_year, month, day)
    today = date.today()
    if (today - candidate).days > 120:
        return default_year + 1
    return default_year


def parse_rows(text: str, default_year: int):
    lines = text.splitlines()
    sections = detect_course_sections(text)
    multi_course = len(set(sections.values())) >= 2  # never auto-tag an ordinary single-class syllabus

    rows = []
    current_course = None
    for i, raw_line in enumerate(lines):
        if i in sections:
            current_course = sections[i]

        line = raw_line.strip()
        match = DATE_HINT.search(line)
        if not line or not match:
            continue

        date_text = match.group(0)
        try:
            parsed = dateparser.parse(date_text, default=datetime(default_year, 1, 1)).date()
        except (ValueError, OverflowError):
            continue

        if parsed.year == default_year and not re.search(r"\d{4}", date_text):
            fixed_year = guess_year_for(parsed.month, parsed.day, default_year)
            parsed = parsed.replace(year=fixed_year)

        title = line[: match.start()] + line[match.end() :]
        title = title.strip(" -:|\t.")
        title = re.sub(r"\s+", " ", title).strip() or "(untitled item)"

        if _looks_like_topic_row(date_text, title):
            continue  # a weekly-schedule/lecture-topic row, not a real assignment

        if _LEADING_WEEK_NUMBER.match(title):
            title = _LEADING_WEEK_NUMBER.sub("", title, count=1).strip(" -:|\t.") or title

        if multi_course and current_course and not title.startswith("["):
            title = f"[{current_course}] {title}"

        rows.append({"due_date": parsed.isoformat(), "title": title})
    return rows


def parse_recurring_rules(text: str):
    rules = []
    seen = set()
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            match = RECURRING_HINT.search(sentence)
            if not sentence or not match:
                continue
            weekday = match.group("weekday").lower()
            clean = sentence.strip(" -:|\t.")
            key = (weekday, clean.lower())
            if key in seen:
                continue
            seen.add(key)
            rules.append({"weekday": weekday, "sentence": clean})
    return rules


def expand_recurring_rules(rules, start: date, end: date):
    rows = []
    for rule in rules:
        target_weekday = WEEKDAYS.index(rule["weekday"])
        d = start + timedelta(days=(target_weekday - start.weekday()) % 7)
        while d <= end:
            rows.append({"due_date": d.isoformat(), "title": rule["sentence"]})
            d += timedelta(days=7)
    return rows


def parse_syllabus_file(path: Path, year: int | None = None):
    """Returns a list of {due_date, title} dicts extracted from the file."""
    year = year or date.today().year
    text = extract_text(path)
    rows = parse_rows(text, year)

    recurring_rules = parse_recurring_rules(text)
    if recurring_rules:
        explicit_dates = [date.fromisoformat(r["due_date"]) for r in rows]
        start = min(explicit_dates) if explicit_dates else date.today()
        end = max(explicit_dates) if explicit_dates else date.today() + timedelta(weeks=16)
        rows.extend(expand_recurring_rules(recurring_rules, start, end))

    rows.sort(key=lambda r: r["due_date"])
    return rows


# ---------------------------------------------------------------------------
# Optional per-upload instructions: a small fixed-pattern language so someone
# can steer parsing without hand-editing the table afterward -- for the two
# problems that keep coming up in practice: (1) junk rows the date-regex
# false-positives on (bibliography page ranges, stray table columns), and
# (2) recurring items the syllabus states in prose that doesn't literally say
# "every <weekday>" (e.g. "prelab due Wednesdays" or "homework due Fridays
# and exit tickets due Fridays too"). This is NOT free-form NLP -- it only
# recognizes the two sentence shapes below, one instruction per line. Anything
# else is reported back as "not understood" rather than silently ignored, so
# the manual edit table (which always still works) is the fallback.
# ---------------------------------------------------------------------------

_FILTER_INSTRUCTION = re.compile(
    r"^\s*(?:ignore|remove|filter\s*out|exclude|delete)\b.*?"
    r"(?:containing|with|mentioning|about)\s*[:\-]?\s*[\"']?([^\"'\n]+?)[\"']?\s*$",
    re.IGNORECASE,
)

_ADD_RECURRING_INSTRUCTION = re.compile(
    r"^\s*add\s+(?P<title>.+?)\s+(?:due\s+)?every\s+(?:other\s+)?"
    r"(?P<weekday>" + "|".join(WEEKDAYS) + r")s?"
    r"(?P<time>\s+at\s+[\w:. ]+)?\s*$",
    re.IGNORECASE,
)

_ADD_ONEOFF_INSTRUCTION = re.compile(
    r"^\s*add\s+(?P<title>.+?)\s+(?:due\s+)?on\s+(?P<date>.+?)\s*$",
    re.IGNORECASE,
)


def apply_instructions(rows, instructions_text: str, year: int | None = None):
    """Applies free-text instructions (one per line) to a parsed row list.
    Returns (new_rows, warnings) -- warnings names any line that didn't match
    a recognized instruction shape, so the caller can tell the user to just
    fix it by hand instead (the manual add/edit/delete table is unaffected
    either way)."""
    year = year or date.today().year
    if not instructions_text or not instructions_text.strip():
        return rows, []

    rows = list(rows)
    warnings = []
    filter_phrases = []
    added_rows = []

    explicit_dates = [date.fromisoformat(r["due_date"]) for r in rows]
    range_start = min(explicit_dates) if explicit_dates else date.today()
    range_end = max(explicit_dates) if explicit_dates else date.today() + timedelta(weeks=16)

    for raw_line in instructions_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _FILTER_INSTRUCTION.match(line)
        if m:
            filter_phrases.append(m.group(1).strip().lower())
            continue

        m = _ADD_RECURRING_INSTRUCTION.match(line)
        if m:
            weekday = m.group("weekday").lower()
            title = f"{m.group('title').strip()} due"
            if m.group("time"):
                title = f"{title}{m.group('time').rstrip()}"
            target_weekday = WEEKDAYS.index(weekday)
            d = range_start + timedelta(days=(target_weekday - range_start.weekday()) % 7)
            while d <= range_end:
                added_rows.append({"due_date": d.isoformat(), "title": title})
                d += timedelta(days=7)
            continue

        m = _ADD_ONEOFF_INSTRUCTION.match(line)
        if m:
            try:
                parsed = dateparser.parse(m.group("date"), default=datetime(year, 1, 1)).date()
                added_rows.append({"due_date": parsed.isoformat(), "title": m.group("title").strip()})
            except (ValueError, OverflowError):
                warnings.append(f"Couldn't understand the date in: \"{line}\"")
            continue

        warnings.append(f"Didn't recognize this instruction, so it was skipped: \"{line}\"")

    if filter_phrases:
        rows = [
            r for r in rows
            if not any(phrase in r["title"].lower() for phrase in filter_phrases)
        ]

    rows.extend(added_rows)
    rows.sort(key=lambda r: r["due_date"])
    return rows, warnings
