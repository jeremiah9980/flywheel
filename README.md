# Flywheel

[![CI](https://github.com/jeremiah9980/flywheel/actions/workflows/ci.yml/badge.svg)](https://github.com/jeremiah9980/flywheel/actions/workflows/ci.yml)

Self-nudge loop + weekly review, grounded in Airtable, delivered to Slack,
orchestrated by Claude. Runs entirely on GitHub Actions cron.

## Developer setup

**Requirements:** Python 3.11+

```bash
# 1. Clone and enter the repo
git clone https://github.com/jeremiah9980/flywheel.git && cd flywheel

# 2. Install developer tooling (ruff + pytest)
make install
# or: pip install ruff pytest

# 3. Lint
make lint      # ruff check .

# 4. Auto-format
make fmt       # ruff format .

# 5. Run tests (no secrets needed — conftest stubs them out)
make test      # pytest
```

All 28 unit tests exercise the pure-function helpers in `flywheel.py` and
`digest.py` (datetime parsing, nudge-ID generation, row-picking logic, and
prompt building). The test suite runs in ~0.1 s with no network access.

The CI workflow (`.github/workflows/ci.yml`) runs `ruff check` + `pytest`
automatically on every push and pull request.

```
┌─────────────┐   ┌─────────────────┐   ┌───────────┐   ┌─────────┐
│  GHA hourly │──▶│  flywheel.py    │──▶│ Anthropic │──▶│  Slack  │
│  (weekday   │   │  (stdlib only)  │   └───────────┘   └─────────┘
│   8am-6pm)  │   │                 │──▶ Airtable Flywheel: update row
└─────────────┘   │                 │──▶ Airtable Nudges:   append row
                  └─────────────────┘

┌─────────────┐   ┌─────────────────┐   ┌───────────┐   ┌─────────┐
│  GHA Monday │──▶│  digest.py      │──▶│ Anthropic │──▶│  Slack  │
│  (weekly)   │   │  reads last 7d  │   └───────────┘   └─────────┘
└─────────────┘   │  of Nudges +    │
                  │  current state  │
                  └─────────────────┘
```

## Two tables

**Flywheel** — current state of each project. One row per active project.
Fields you edit: `Name`, `Priority`, `Phase`, `Context`, `Active`,
`Next Step Hint`. Fields the system updates: `Last Nudged`, `Last Action`,
`Nudge Count`, `Completion %` (via Done button), `Snoozed Until` (via Skip
button).

**Nudges** — append-only log. One row per nudge sent. Field values are
snapshots at send time: `Sent At`, `Phase`, `Completion At Send`, `Action`
(the full message Claude generated), `Status` (`Sent` → `Done`/`Skipped`/
`Stale`). Linked back to Flywheel via the `Project` field, so you can see
a project's entire suggestion history from the Flywheel row too.

## Setup

### 1. Push the repo

```bash
# copy these files into a new repo under your GitHub account
# (the .tar.gz bundle preserves structure)
git init && git add . && git commit -m "init flywheel"
git remote add origin git@github.com:Jeremiah9980/flywheel.git
git push -u origin main
```

### 2. Secrets (Settings → Secrets and variables → Actions → New secret)

| Secret | Where |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ → API Keys |
| `AIRTABLE_PAT` | https://airtable.com/create/tokens → scopes `data.records:read` + `data.records:write`, access: Project Tracker base |
| `SLACK_WEBHOOK_URL` | https://api.slack.com/apps → Create New App → Incoming Webhooks → Add Webhook to Workspace → pick your self-DM channel |

### 3. Button fields on `Flywheel` (one-time, in Airtable UI)

Only **Done** and **Skip 8h** are worth wiring. The reply loop lives on the
project row because that's where you're usually looking after a Slack ping.

**Done** — button field, action: *Run automation*.

Create automation `Flywheel — mark done`:
- Trigger: *When record button clicked*, field `Done`
- Action: *Run script*. Input: `recordId` = Airtable record ID from trigger.

```javascript
let { recordId } = input.config();
let flywheel = base.getTable("Flywheel");
let nudges   = base.getTable("Nudges");

// 1. Bump project completion + note it in Last Action
let r = await flywheel.selectRecordAsync(recordId);
let cur = r.getCellValue("Completion %") || 0;
await flywheel.updateRecordAsync(recordId, {
  "Completion %": Math.min(1, cur + 0.10),
  "Last Action": (r.getCellValue("Last Action") || "") +
    `\n\n✅ DONE ${new Date().toISOString()}`
});

// 2. Mark the most recent Sent nudge for this project as Done
let q = await nudges.selectRecordsAsync({
  sorts: [{ field: "Sent At", direction: "desc" }]
});
let hit = q.records.find(n => {
  let links  = n.getCellValue("Project") || [];
  let status = n.getCellValue("Status");
  return links.some(l => l.id === recordId) && status?.name === "Sent";
});
if (hit) {
  await nudges.updateRecordAsync(hit.id, {
    "Status":      { name: "Done" },
    "Resolved At": new Date().toISOString()
  });
}
```

**Skip 8h** — button field, action: *Run automation*.

Create automation `Flywheel — skip 8h`:
- Trigger: *When record button clicked*, field `Skip 8h`
- Action: *Update record* (same record from trigger):
  - `Snoozed Until` = formula `DATEADD(NOW(), 8, 'hours')`
  - `Last Action` = formula `CONCATENATE({Last Action}, "\n\n⏭ SKIPPED ", DATETIME_FORMAT(NOW(), 'YYYY-MM-DD HH:mm'))`

Note: project-level Skip doesn't mark any Nudge as Skipped — skipping the
project is a different decision from dismissing one suggestion. If you want
to dismiss a specific suggestion, edit `Status` directly in the Nudges row.

### 4. Test

Actions tab → **flywheel** → *Run workflow* → `dry_run=true`. Logs should
show picked project + generated action, nothing sent.

Actions tab → **flywheel-digest** → *Run workflow* → `dry_run=true`. This
bypasses the Monday gate so you can test any day. Logs show the built
prompt + Claude's digest.

Flip both to `dry_run=false` to go live.

## Rotation logic (flywheel.py)

Every hourly run re-sorts Flywheel ascending by `Last Nudged` (empty
first), ties broken by `Priority` ascending. First row that is `Active`
and not currently snoozed wins. After sending, `Last Nudged = now`, which
pushes that row to the back of the queue. This is stateless and robust —
rows added or reactivated mid-week slot in correctly without any
coordination.

## Digest logic (digest.py)

Pulls every row in `Nudges` with `Sent At >= now - 7 days`, groups by
project, counts statuses. Sends a structured prompt to Claude (projects +
nudge log), asks for a synthesized digest with per-project labels and 2-4
forward-looking suggestions. Posts the result to Slack.

The script is deliberately free of hand-rolled stats because the input
structure already surfaces everything Claude needs to notice — stalled
projects, forcing functions not being worked, priority/completion mismatch,
etc.

## Gotchas

**GitHub disables scheduled workflows after 60 days of repo inactivity.**
Commit a trivial change monthly, run a workflow manually, or add a
weekly `git commit --allow-empty` job.

**GitHub Actions cron is best-effort.** 5-15 min delays happen during
load. Fine for this cadence.

**DST:** handled in-script for both workflows. The digest cron fires at
13:00 UTC Monday; during CST (Nov-Mar) that's 7am CT instead of 8am. The
Monday-only gate still passes, so the only effect is a 1hr earlier
delivery half the year. If that annoys you, add a second cron at `0 14 * * 1`
(the script will still only fire once because the gate runs exactly once).

**Airtable rate limits:** 5 req/sec per base. Hourly flywheel makes 3
calls, weekly digest makes ~2 paged calls. Nowhere near the ceiling.

**Anthropic cost:** hourly ~1K in / ~300 out at Opus 4.5 rates. Weekly
digest ~5K in / ~500 out. Rough total under $15/month at the default
cadence.

## Extending

- **Stale cleanup**: add a third workflow on a daily cron that PATCHes any
  `Nudges` row with `Status=Sent` AND `Sent At < now - 3 days` to `Stale`,
  so the weekly digest's "unresolved" count stays meaningful.
- **Retro review**: at the end of a month, feed the full Nudges log for a
  project into Claude and ask for pattern analysis — what kinds of
  suggestions actually get done vs skipped, what's a good Priority signal.
- **Per-project channels**: add a `Slack Channel` field on Flywheel, pass
  a per-row webhook URL, route accordingly.
- **Context auto-refresh**: after N done nudges in a phase, prompt Claude
  to propose an updated `Context` and/or `Phase`.
