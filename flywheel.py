#!/usr/bin/env python3
"""Flywheel nudger.

Runs hourly via GitHub Actions. Picks the active, non-snoozed project in
Airtable that was nudged the longest ago (ties broken by Priority), asks
Claude for a 45-minute action, posts it to Slack, logs the nudge to the
Nudges table, and updates the Flywheel row.

Business-hours gate (8 AM - 6 PM CT, Mon-Fri) is enforced in Python so DST
transitions don't shift the schedule.

Env vars (all required except the last three, which have sensible defaults):
    ANTHROPIC_API_KEY   - from console.anthropic.com
    AIRTABLE_PAT        - Personal access token with data.records:read+write
                          scoped to Project Tracker
    SLACK_WEBHOOK_URL   - Incoming webhook for the target DM channel
    AIRTABLE_BASE_ID    - default: appVilJUh8vIlDFr8 (Project Tracker)
    AIRTABLE_TABLE_ID   - default: tbl4KjCdTacVTV784 (Flywheel)
    NUDGES_TABLE_ID     - default: tblhLdW0lJhTfFDV4 (Nudges)
    DRY_RUN             - if "1", print what would be sent but don't send
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------- config ----------

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_PAT   = os.environ["AIRTABLE_PAT"]
SLACK_WEBHOOK  = os.environ["SLACK_WEBHOOK_URL"]
BASE_ID        = os.environ.get("AIRTABLE_BASE_ID",  "appVilJUh8vIlDFr8")
FLYWHEEL_TABLE = os.environ.get("AIRTABLE_TABLE_ID", "tbl4KjCdTacVTV784")
NUDGES_TABLE   = os.environ.get("NUDGES_TABLE_ID",   "tblhLdW0lJhTfFDV4")
DRY_RUN        = os.environ.get("DRY_RUN") == "1"

CT    = ZoneInfo("America/Chicago")
MODEL = "claude-opus-4-5"

SYSTEM_PROMPT = (
    "You are Jeremiah's infra/career ops co-pilot. Each hour pick ONE concrete "
    "action completable in 45 minutes on the specified project. Format: "
    "*ACTION* (one sentence, imperative) then 3-5 numbered micro-steps then "
    "one line starting 'Why now:'. No preamble, no sign-off. Slack markdown "
    "only (*bold*, _italic_, `code`, >quote - NEVER **bold**). Never repeat "
    "the last action verbatim. If Next Step Hint is non-empty, treat it as a "
    "forcing function."
)

# ---------- helpers ----------

def http(method: str, url: str, headers: dict | None = None,
         data: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {method} {url}\n{e.read().decode(errors='replace')}",
              file=sys.stderr)
        raise

def within_business_hours() -> bool:
    now = datetime.now(CT)
    return now.weekday() < 5 and 8 <= now.hour <= 18

def parse_airtable_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def make_nudge_id(project_name: str, sent_at: datetime) -> str:
    """e.g. 'ERCOT-20260420-0830' — project's first word + timestamp."""
    first_word = project_name.split()[0] if project_name else "NUDGE"
    slug = re.sub(r"[^A-Z0-9]", "", first_word.upper())[:10] or "NUDGE"
    return f"{slug}-{sent_at.astimezone(CT).strftime('%Y%m%d-%H%M')}"

# ---------- airtable ----------

def fetch_candidates() -> list[dict]:
    qs = urllib.parse.urlencode({
        "sort[0][field]": "Last Nudged",
        "sort[0][direction]": "asc",
        "sort[1][field]": "Priority",
        "sort[1][direction]": "asc",
        "pageSize": 100,
    })
    url = f"https://api.airtable.com/v0/{BASE_ID}/{FLYWHEEL_TABLE}?{qs}"
    data = http("GET", url, {"Authorization": f"Bearer {AIRTABLE_PAT}"})
    return data.get("records", [])

def pick_row(records: list[dict]) -> dict | None:
    now = datetime.now(timezone.utc)
    for rec in records:
        f = rec.get("fields", {})
        if not f.get("Active"):
            continue
        snoozed = parse_airtable_dt(f.get("Snoozed Until"))
        if snoozed and snoozed > now:
            continue
        return rec
    return None

def update_flywheel_row(record_id: str, fields: dict) -> None:
    url = f"https://api.airtable.com/v0/{BASE_ID}/{FLYWHEEL_TABLE}/{record_id}"
    http("PATCH", url,
         {"Authorization": f"Bearer {AIRTABLE_PAT}",
          "Content-Type": "application/json"},
         {"fields": fields})

def log_nudge(project_record_id: str, project_name: str, phase: str,
              completion: float, action: str, sent_at: datetime) -> str:
    """Create a row in the Nudges table. Returns the new record ID."""
    url = f"https://api.airtable.com/v0/{BASE_ID}/{NUDGES_TABLE}"
    payload = {
        "fields": {
            "ID":                 make_nudge_id(project_name, sent_at),
            "Project":            [project_record_id],
            "Sent At":            sent_at.isoformat(),
            "Phase":              phase or "",
            "Completion At Send": completion,
            "Action":             action,
            "Status":             "Sent",
        }
    }
    resp = http("POST", url,
        {"Authorization": f"Bearer {AIRTABLE_PAT}",
         "Content-Type": "application/json"},
        payload)
    return resp["id"]

# ---------- claude ----------

def ask_claude(fields: dict) -> str:
    pct = int(round((fields.get("Completion %") or 0) * 100))
    user_msg = (
        f"Project: {fields.get('Name')}\n"
        f"Phase: {fields.get('Phase') or '(none)'}\n"
        f"Completion: {pct}%\n"
        f"Context:\n{fields.get('Context') or ''}\n\n"
        f"Last action sent:\n{fields.get('Last Action') or '(none yet)'}\n\n"
        f"Forcing function (may be empty):\n{fields.get('Next Step Hint') or ''}\n\n"
        "Generate the next 45-minute action."
    )
    resp = http("POST", "https://api.anthropic.com/v1/messages",
        {"x-api-key": ANTHROPIC_KEY,
         "anthropic-version": "2023-06-01",
         "Content-Type": "application/json"},
        {"model": MODEL,
         "max_tokens": 600,
         "system": SYSTEM_PROMPT,
         "messages": [{"role": "user", "content": user_msg}]})
    return "\n".join(b["text"] for b in resp.get("content", [])
                     if b.get("type") == "text").strip()

# ---------- slack ----------

def send_slack(project: str, phase: str, message: str,
               flywheel_url: str, nudge_url: str) -> None:
    text = (
        f"⚡ *Flywheel* · {project} · _{phase}_\n\n"
        f"{message}\n\n"
        f"→ <{flywheel_url}|Project> · <{nudge_url}|This nudge> · "
        f"tap *Done* or *Skip 8h* on the project record"
    )
    http("POST", SLACK_WEBHOOK,
         {"Content-Type": "application/json"},
         {"text": text})

# ---------- main ----------

def main() -> int:
    if not within_business_hours():
        print(f"[skip] outside business hours "
              f"({datetime.now(CT).strftime('%a %H:%M %Z')})")
        return 0

    candidates = fetch_candidates()
    pick = pick_row(candidates)
    if not pick:
        print("[skip] no eligible rows")
        return 0

    f     = pick["fields"]
    name  = f.get("Name", "(unnamed)")
    phase = f.get("Phase", "")
    pct   = f.get("Completion %") or 0
    print(f"[pick] {name} — last nudged: {f.get('Last Nudged', 'never')}")

    message = ask_claude(f)
    if not message:
        print("[error] empty response from Claude", file=sys.stderr)
        return 1

    now_utc      = datetime.now(timezone.utc)
    flywheel_url = f"https://airtable.com/{BASE_ID}/{FLYWHEEL_TABLE}/{pick['id']}"

    if DRY_RUN:
        print("=== DRY RUN ===")
        print(f"Project: {name} · {phase}")
        print(f"Flywheel URL: {flywheel_url}")
        print(f"Nudge ID would be: {make_nudge_id(name, now_utc)}")
        print("---")
        print(message)
        return 0

    # 1. Log to Nudges first so we have a record even if Slack fails
    nudge_id  = log_nudge(pick["id"], name, phase, pct, message, now_utc)
    nudge_url = f"https://airtable.com/{BASE_ID}/{NUDGES_TABLE}/{nudge_id}"
    print(f"[log] nudge {nudge_id}")

    # 2. Slack
    send_slack(name, phase, message, flywheel_url, nudge_url)
    print("[slack] sent")

    # 3. Update Flywheel row
    # Note: nudge counts come from the back-linked Nudges field (auto-counted
    # by Airtable). No separate Nudge Count field needed.
    update_flywheel_row(pick["id"], {
        "Last Nudged": now_utc.isoformat(),
        "Last Action": message,
    })
    print("[ok] nudge delivered end-to-end")
    return 0

if __name__ == "__main__":
    sys.exit(main())
