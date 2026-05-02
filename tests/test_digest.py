"""Unit tests for digest.py helper functions."""
from datetime import datetime, timedelta, timezone

import digest

# ---------- parse_dt ----------


def test_parse_dt_utc_z_suffix():
    result = digest.parse_dt("2026-04-20T13:00:00Z")
    assert result == datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)


def test_parse_dt_none_returns_none():
    assert digest.parse_dt(None) is None


def test_parse_dt_empty_string_returns_none():
    assert digest.parse_dt("") is None


# ---------- status_name ----------


def test_status_name_dict_with_name_key():
    assert digest.status_name({"name": "Done"}) == "Done"


def test_status_name_plain_string():
    assert digest.status_name("Sent") == "Sent"


def test_status_name_none_defaults_to_sent():
    assert digest.status_name(None) == "Sent"


def test_status_name_empty_string_defaults_to_sent():
    assert digest.status_name("") == "Sent"


# ---------- build_prompt ----------


def _make_flywheel_row(record_id, name, active=True, priority=1, phase="Build", pct=0.4):
    return {
        "id": record_id,
        "fields": {
            "Name": name,
            "Active": active,
            "Priority": priority,
            "Phase": phase,
            "Completion %": pct,
        },
    }


def _make_nudge(record_id, project_id, sent_offset_days=0, status="Sent", action="Do X"):
    sent_at = (datetime.now(timezone.utc) - timedelta(days=sent_offset_days)).isoformat()
    return {
        "id": record_id,
        "fields": {
            "Sent At": sent_at,
            "Project": [{"id": project_id}],
            "Status": {"name": status},
            "Action": action,
        },
    }


def test_build_prompt_contains_project_name():
    rows = [_make_flywheel_row("rec1", "Test Project")]
    prompt = digest.build_prompt(rows, [])
    assert "Test Project" in prompt


def test_build_prompt_contains_section_headers():
    rows = [_make_flywheel_row("rec1", "Test Project")]
    prompt = digest.build_prompt(rows, [])
    assert "ACTIVE PROJECTS" in prompt
    assert "INACTIVE PROJECTS" in prompt
    assert "NUDGE LOG" in prompt


def test_build_prompt_counts_recent_nudges():
    rows = [_make_flywheel_row("rec1", "Proj A")]
    nudges = [
        _make_nudge("n1", "rec1", sent_offset_days=1, status="Done"),
        _make_nudge("n2", "rec1", sent_offset_days=2, status="Sent"),
    ]
    prompt = digest.build_prompt(rows, nudges)
    # Both nudges are within 7 days, so sent=2 should appear
    assert "sent=2" in prompt


def test_build_prompt_excludes_old_nudges():
    rows = [_make_flywheel_row("rec1", "Proj A")]
    nudges = [
        _make_nudge("n1", "rec1", sent_offset_days=10),  # older than 7 days
    ]
    prompt = digest.build_prompt(rows, nudges)
    assert "sent=0" in prompt


def test_build_prompt_inactive_projects_listed():
    rows = [
        _make_flywheel_row("rec1", "Active One", active=True),
        _make_flywheel_row("rec2", "Inactive One", active=False),
    ]
    prompt = digest.build_prompt(rows, [])
    assert "Inactive One" in prompt
    assert "INACTIVE PROJECTS" in prompt


def test_build_prompt_forcing_function_included():
    rows = [_make_flywheel_row("rec1", "Proj B")]
    rows[0]["fields"]["Next Step Hint"] = "Ship the MVP by Friday"
    prompt = digest.build_prompt(rows, [])
    assert "Ship the MVP by Friday" in prompt
