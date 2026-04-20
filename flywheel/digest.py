#!/usr/bin/env python3
"""Flywheel weekly digest.

Runs Monday mornings via GitHub Actions. Pulls the past 7 days of nudges
plus current project states, sends a structured summary to Claude, and
posts Claude's synthesized review to Slack.

Env vars (required unless noted):
    ANTHROPIC_API_KEY
    AIRTABLE_PAT
    SLACK_WEBHOOK_URL
    AIRTABLE_BASE_ID     default: appVilJUh8vIlDFr8
    AIRTABLE_TABLE_ID    default: tbl4KjCdTacVTV784 (Flywheel)
    NUDGES_TABLE_ID      default: tblhLdW0lJhTfFDV4 (Nudges)
    DRY_RUN              if "1", print without sending
    GITHUB_EVENT_NAME    set automatically by Actions — bypasses Monday gate
                         on manual (workflow_dispatch) runs
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_PAT   = os.environ["AIRTABLE_PAT"]
SLACK_WEBHOOK  = os.environ["SLACK_WEBHOOK_URL"]
BASE_ID        = os.environ.get("AIRTABLE_BASE_ID",  "appVilJUh8vIlDFr8")
FLYWHEEL_TABLE = os.environ.get("AIRTABLE_TABLE_ID", "tbl4KjCdTacVTV784")
NUDGES_TABLE   = os.environ.get("NUDGES_TABLE_ID",   "tblhLdW0lJhTfFDV4")
DRY_RUN        = os.environ.get("DRY_RUN") == "1"
EVENT_NAME     = os.environ.get("GITHUB_EVENT_NAME", "manual")

CT    = ZoneInfo("America/Chicago")
MODEL = "claude-opus-4-5"

SYSTEM_PROMPT = """You are Jeremiah's weekly ops reviewer. Given the past 7 days of nudges and current project states, produce a brief weekly digest in Slack markdown.

Structure:
1. *Top line* — one sentence on the week's overall momentum.
2. *Per project* (only Active projects) — one line each: "ProjectName: N sent · M done · X% complete · LABEL" where LABEL is exactly one of: 🔥 hot (2+ done), 🟢 steady (1 done), ⚠️ stalled (0 done and 3+ sent), 💤 quiet (0–2 sent).
3. *What to change next week* — 2-4 specific suggestions. May recommend: deactivate a stalled project, change a project's Phase, add a forcing function to Next Step Hint, raise/lower a Priority, or reactivate something.

No preamble, no sign-off. Slack markdown only (*bold*, _italic_, `code`, >quote — NEVER **bold**). Keep total under 25 lines. Be honest — if the week was weak, say so."""


def http(method, url, headers=None, data=None):
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


def fetch_all(table_id, params=None):
    records = []
    cursor = None
    while True:
        q = dict(params or {})
        q["pageSize"] = 100
        if cursor:
            q["offset"] = cursor
        qs = urllib.parse.urlencode(q)
        url = f"https://api.airtable.com/v0/{BASE_ID}/{table_id}?{qs}"
        data = http("GET", url, {"Authorization": f"Bearer {AIRTABLE_PAT}"})
        records.extend(data.get("records", []))
        cursor = data.get("offset")
        if not cursor:
            break
    return records


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def status_name(val):
    if isinstance(val, dict):
        return val.get("name")
    return val or "Sent"


def build_prompt(flywheel_rows, nudges):
    now_ct   = datetime.now(CT)
    now_utc  = datetime.now(timezone.utc)
    week_ago = now_utc - timedelta(days=7)

    # Filter nudges to past 7 days
    recent = []
    for n in nudges:
        sent = parse_dt(n.get("fields", {}).get("Sent At"))
        if sent and sent >= week_ago:
            recent.append(n)

    # Index: project_record_id -> list of nudges
    by_project = defaultdict(list)
    for n in recent:
        for link in n.get("fields", {}).get("Project", []):
            by_project[link["id"]].append(n)

    # Build project-name lookup
    name_by_id = {r["id"]: r.get("fields", {}).get("Name", "?") for r in flywheel_rows}

    lines = [
        f"Week ending {now_ct.strftime('%A, %b %d, %Y')} (past 7 days).",
        f"Total nudges sent this week: {len(recent)}",
        "",
        "=== ACTIVE PROJECTS (current state) ===",
    ]
    for row in sorted(flywheel_rows,
                      key=lambda r: r.get("fields", {}).get("Priority") or 99):
        f = row.get("fields", {})
        if not f.get("Active"):
            continue
        pn     = by_project.get(row["id"], [])
        counts = defaultdict(int)
        for n in pn:
            counts[status_name(n.get("fields", {}).get("Status"))] += 1
        pct = int(round((f.get("Completion %") or 0) * 100))
        lines.append(
            f"- {f.get('Name')} (Priority {f.get('Priority', '?')}) | "
            f"Phase: {f.get('Phase') or '(none)'} | "
            f"Completion: {pct}% | "
            f"Week: sent={len(pn)} done={counts['Done']} "
            f"skipped={counts['Skipped']} unresolved={counts['Sent']}"
        )
        hint = f.get("Next Step Hint")
        if hint:
            lines.append(f"  Forcing function: {hint[:200]}")

    lines.append("")
    lines.append("=== INACTIVE PROJECTS ===")
    inactive = [r for r in flywheel_rows if not r.get("fields", {}).get("Active")]
    if inactive:
        for row in inactive:
            f = row.get("fields", {})
            lines.append(f"- {f.get('Name')} (inactive)")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("=== NUDGE LOG (past 7 days, oldest → newest) ===")
    recent_sorted = sorted(recent, key=lambda x: x["fields"].get("Sent At", ""))
    for n in recent_sorted[-40:]:  # cap to avoid giant prompts
        f       = n["fields"]
        st      = status_name(f.get("Status"))
        sent    = (f.get("Sent At") or "")[:16].replace("T", " ")
        links   = f.get("Project", [])
        proj    = name_by_id.get(links[0]["id"], "?") if links else "?"
        snippet = (f.get("Action") or "").split("\n", 1)[0][:100]
        lines.append(f"  [{st:8}] {sent} | {proj} | {snippet}")

    return "\n".join(lines)


def ask_claude(prompt):
    resp = http("POST", "https://api.anthropic.com/v1/messages",
        {"x-api-key": ANTHROPIC_KEY,
         "anthropic-version": "2023-06-01",
         "Content-Type": "application/json"},
        {"model": MODEL,
         "max_tokens": 1200,
         "system": SYSTEM_PROMPT,
         "messages": [{"role": "user", "content": prompt}]})
    return "\n".join(b["text"] for b in resp.get("content", [])
                     if b.get("type") == "text").strip()


def main():
    now_ct = datetime.now(CT)

    # Gate: only run on Monday for scheduled events; bypass on manual dispatch
    if EVENT_NAME == "schedule" and now_ct.weekday() != 0:
        print(f"[skip] scheduled run but not Monday ({now_ct.strftime('%A')})")
        return 0

    print(f"[start] digest for week ending {now_ct.strftime('%Y-%m-%d')} "
          f"(event: {EVENT_NAME})")

    flywheel = fetch_all(FLYWHEEL_TABLE)
    nudges   = fetch_all(NUDGES_TABLE)
    print(f"[data] {len(flywheel)} projects, {len(nudges)} total nudges in log")

    prompt = build_prompt(flywheel, nudges)
    print(f"[prompt] {len(prompt)} chars")

    digest = ask_claude(prompt)
    if not digest:
        print("[error] empty response from Claude", file=sys.stderr)
        return 1

    slack_text = (
        f"📊 *Flywheel Weekly Digest* — week ending "
        f"{now_ct.strftime('%b %d, %Y')}\n\n{digest}"
    )

    if DRY_RUN:
        print("=== DRY RUN — would send to Slack ===")
        print(slack_text)
        print("\n\n=== PROMPT SENT TO CLAUDE (for debugging) ===")
        print(prompt)
        return 0

    http("POST", SLACK_WEBHOOK,
         {"Content-Type": "application/json"},
         {"text": slack_text})
    print("[ok] digest delivered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
