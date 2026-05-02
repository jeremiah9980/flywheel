"""Unit tests for flywheel.py pure-function helpers."""
from datetime import datetime, timedelta, timezone

import flywheel as fw

# ---------- parse_airtable_dt ----------


def test_parse_airtable_dt_valid():
    result = fw.parse_airtable_dt("2026-04-20T08:30:00Z")
    assert result == datetime(2026, 4, 20, 8, 30, 0, tzinfo=timezone.utc)


def test_parse_airtable_dt_none_returns_none():
    assert fw.parse_airtable_dt(None) is None


def test_parse_airtable_dt_empty_string_returns_none():
    assert fw.parse_airtable_dt("") is None


def test_parse_airtable_dt_with_offset():
    result = fw.parse_airtable_dt("2026-04-20T08:30:00+00:00")
    assert result == datetime(2026, 4, 20, 8, 30, 0, tzinfo=timezone.utc)


# ---------- make_nudge_id ----------


def test_make_nudge_id_basic():
    dt = datetime(2026, 4, 20, 14, 30, tzinfo=timezone.utc)  # 09:30 CDT
    nid = fw.make_nudge_id("ERCOT Pipeline", dt)
    assert nid.startswith("ERCOT-")
    assert "20260420" in nid


def test_make_nudge_id_empty_project_falls_back_to_nudge():
    dt = datetime(2026, 4, 20, 14, 0, tzinfo=timezone.utc)
    nid = fw.make_nudge_id("", dt)
    assert nid.startswith("NUDGE-")


def test_make_nudge_id_strips_non_alphanum():
    dt = datetime(2026, 4, 20, 14, 0, tzinfo=timezone.utc)
    nid = fw.make_nudge_id("My-Project!", dt)
    # first word is "My-Project!" → slug "MYPROJECT"
    assert "MYPROJECT" in nid


def test_make_nudge_id_truncates_slug_at_10_chars():
    dt = datetime(2026, 4, 20, 14, 0, tzinfo=timezone.utc)
    nid = fw.make_nudge_id("AVERYLONGPROJECTNAME extra", dt)
    slug = nid.split("-")[0]
    assert len(slug) <= 10


# ---------- pick_row ----------


def test_pick_row_active_record_is_returned():
    records = [
        {"id": "rec1", "fields": {"Active": True, "Name": "A"}},
    ]
    pick = fw.pick_row(records)
    assert pick is not None
    assert pick["id"] == "rec1"


def test_pick_row_inactive_record_is_skipped():
    records = [
        {"id": "rec1", "fields": {"Active": False, "Name": "A"}},
    ]
    assert fw.pick_row(records) is None


def test_pick_row_empty_list_returns_none():
    assert fw.pick_row([]) is None


def test_pick_row_future_snooze_is_skipped():
    future = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    records = [
        {"id": "rec1", "fields": {"Active": True, "Snoozed Until": future}},
    ]
    assert fw.pick_row(records) is None


def test_pick_row_expired_snooze_is_picked():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    records = [
        {"id": "rec1", "fields": {"Active": True, "Snoozed Until": past}},
    ]
    pick = fw.pick_row(records)
    assert pick is not None
    assert pick["id"] == "rec1"


def test_pick_row_returns_first_eligible_record():
    """When multiple active records exist, the first one (already sorted by
    Airtable on Last Nudged asc) should be returned."""
    records = [
        {"id": "rec1", "fields": {"Active": True, "Name": "First"}},
        {"id": "rec2", "fields": {"Active": True, "Name": "Second"}},
    ]
    pick = fw.pick_row(records)
    assert pick["id"] == "rec1"


def test_pick_row_skips_inactive_finds_active():
    records = [
        {"id": "rec1", "fields": {"Active": False, "Name": "Inactive"}},
        {"id": "rec2", "fields": {"Active": True, "Name": "Active"}},
    ]
    pick = fw.pick_row(records)
    assert pick["id"] == "rec2"
