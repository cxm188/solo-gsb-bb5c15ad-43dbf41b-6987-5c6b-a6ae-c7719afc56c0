"""Key tests for the /validate endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//validate-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def validate(ics: str):
    return client.post("/validate", json={"ics": ics})


def codes(resp) -> list[str]:
    return [issue["code"] for issue in resp.json()["issues"]]


# ---------------------------------------------------------------------------
# Valid calendars
# ---------------------------------------------------------------------------

def test_valid_one_off_event_has_no_issues():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:Weekly sync",
    ))
    resp = validate(ics)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True, "error_count": 0, "issues": []}


def test_recurring_series_with_override_is_valid():
    ics = cal(
        event(
            "UID:series-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
        ),
    )
    resp = validate(ics)
    assert resp.json() == {"valid": True, "error_count": 0, "issues": []}


def test_event_without_dtend_or_duration_is_a_valid_zero_duration_event():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    assert validate(ics).json()["valid"] is True


def test_empty_calendar_has_no_events_to_flag():
    resp = validate(cal())
    assert resp.json() == {"valid": True, "error_count": 0, "issues": []}


# ---------------------------------------------------------------------------
# Missing required properties
# ---------------------------------------------------------------------------

def test_missing_uid_is_flagged_with_null_uid():
    ics = cal(event("DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"))
    resp = validate(ics)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 1
    assert body["issues"] == [
        {
            "code": "MISSING_UID",
            "message": "VEVENT is missing the required UID property",
            "event_index": 0,
            "uid": None,
        }
    ]


def test_missing_dtstart_is_flagged_and_uid_reported():
    ics = cal(event("UID:evt-1", "DTEND:20260105T100000Z"))
    resp = validate(ics)
    assert resp.json()["issues"] == [
        {
            "code": "MISSING_DTSTART",
            "message": "VEVENT is missing the required DTSTART property",
            "event_index": 0,
            "uid": "evt-1",
        }
    ]


def test_missing_uid_and_dtstart_accumulate_on_one_event():
    ics = cal(event("SUMMARY:no identity, no start"))
    resp = validate(ics)
    body = resp.json()
    assert body["error_count"] == 2
    # Sorted by code within the same event_index.
    assert codes(resp) == ["MISSING_DTSTART", "MISSING_UID"]
    assert all(issue["uid"] is None for issue in body["issues"])
    assert all(issue["event_index"] == 0 for issue in body["issues"])


# ---------------------------------------------------------------------------
# DTEND / DURATION and the end-after-start check
# ---------------------------------------------------------------------------

def test_dtend_and_duration_together_are_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "DURATION:PT1H",
    ))
    resp = validate(ics)
    assert codes(resp) == ["DTEND_AND_DURATION"]
    issue = resp.json()["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] == "evt-1"


def test_end_equal_to_start_is_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T090000Z",
    ))
    resp = validate(ics)
    assert codes(resp) == ["END_NOT_AFTER_START"]
    assert "not after its start" in resp.json()["issues"][0]["message"]


def test_end_before_start_is_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T090000Z",
    ))
    assert codes(validate(ics)) == ["END_NOT_AFTER_START"]


def test_zero_duration_property_is_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DURATION:PT0S",
    ))
    assert codes(validate(ics)) == ["END_NOT_AFTER_START"]


def test_negative_duration_is_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DURATION:-PT1H",
    ))
    assert codes(validate(ics)) == ["END_NOT_AFTER_START"]


def test_end_compare_normalizes_to_utc():
    # 10:00 in Europe/Berlin (UTC+1) is the same instant as 09:00Z.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260301T090000Z",
        "DTEND;TZID=Europe/Berlin:20260301T100000",
    ))
    assert codes(validate(ics)) == ["END_NOT_AFTER_START"]


def test_all_day_event_with_dtend_equal_to_dtstart_is_flagged():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;VALUE=DATE:20260301",
        "DTEND;VALUE=DATE:20260301",
    ))
    assert codes(validate(ics)) == ["END_NOT_AFTER_START"]


def test_all_day_event_with_exclusive_dtend_is_valid():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;VALUE=DATE:20260301",
        "DTEND;VALUE=DATE:20260302",
    ))
    assert validate(ics).json()["valid"] is True


def test_unparsable_time_values_skip_the_end_check_without_crashing():
    # DTSTART is present but broken: no MISSING_DTSTART, and the end check
    # has nothing reliable to compare, so the event reports no issues.
    ics = cal(event("UID:evt-1", "DTSTART:foobar", "DTEND:20260105T100000Z"))
    resp = validate(ics)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "error_count": 0, "issues": []}


# ---------------------------------------------------------------------------
# Recurrence structure: duplicate masters and orphan overrides
# ---------------------------------------------------------------------------

def test_second_master_for_same_uid_is_flagged():
    ics = cal(
        event("UID:dup", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:dup", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
    )
    resp = validate(ics)
    body = resp.json()
    assert body["error_count"] == 1
    issue = body["issues"][0]
    assert issue["code"] == "DUPLICATE_MASTER"
    assert issue["event_index"] == 1  # the later master is the duplicate
    assert issue["uid"] == "dup"
    assert "first master at event index 0" in issue["message"]


def test_three_masters_flag_the_two_later_ones():
    ics = cal(
        event("UID:dup", "DTSTART:20260105T090000Z"),
        event("UID:dup", "DTSTART:20260106T090000Z"),
        event("UID:dup", "DTSTART:20260107T090000Z"),
    )
    resp = validate(ics)
    issues = resp.json()["issues"]
    assert codes(resp) == ["DUPLICATE_MASTER", "DUPLICATE_MASTER"]
    assert [i["event_index"] for i in issues] == [1, 2]


def test_override_without_master_is_flagged():
    ics = cal(event(
        "UID:ghost",
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260112T110000Z",
        "DTEND:20260112T120000Z",
    ))
    resp = validate(ics)
    issue = resp.json()["issues"][0]
    assert issue["code"] == "ORPHAN_OVERRIDE"
    assert issue["event_index"] == 0
    assert issue["uid"] == "ghost"


def test_every_orphan_override_of_one_uid_is_flagged():
    ics = cal(
        event(
            "UID:ghost",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
        ),
        event(
            "UID:ghost",
            "RECURRENCE-ID:20260119T090000Z",
            "DTSTART:20260119T110000Z",
        ),
    )
    resp = validate(ics)
    assert codes(resp) == ["ORPHAN_OVERRIDE", "ORPHAN_OVERRIDE"]
    assert [i["event_index"] for i in resp.json()["issues"]] == [0, 1]


def test_master_appearing_after_its_override_still_counts():
    # Orphan detection considers the whole document, not just earlier events.
    ics = cal(
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
        ),
        event("UID:series-1", "DTSTART:20260105T090000Z", "RRULE:FREQ=WEEKLY;COUNT=3"),
    )
    assert validate(ics).json()["valid"] is True


# ---------------------------------------------------------------------------
# Ordering, accumulation, and response shape
# ---------------------------------------------------------------------------

def test_issues_sorted_by_event_index_then_code():
    # Event 0 is an orphan override (found in the structure phase, after the
    # per-event phase), event 1 lacks a UID; document order must win.
    ics = cal(
        event(
            "UID:ghost",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
        ),
        event("DTSTART:20260105T090000Z"),
    )
    resp = validate(ics)
    assert codes(resp) == ["ORPHAN_OVERRIDE", "MISSING_UID"]
    assert [i["event_index"] for i in resp.json()["issues"]] == [0, 1]


def test_one_event_accumulates_issues_from_several_checks():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T090000Z",
        "DURATION:PT1H",
    ))
    resp = validate(ics)
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 2
    # Same event_index, so the sort falls back to code order.
    assert codes(resp) == ["DTEND_AND_DURATION", "END_NOT_AFTER_START"]
    assert all(i["event_index"] == 0 and i["uid"] == "evt-1" for i in body["issues"])


def test_error_count_always_matches_the_issue_list():
    ics = cal(
        event("SUMMARY:no uid, no start"),
        event("UID:dup", "DTSTART:20260105T090000Z"),
        event("UID:dup", "DTSTART:20260106T090000Z"),
    )
    body = validate(ics).json()
    assert body["error_count"] == len(body["issues"]) == 3
    assert body["valid"] is False


# ---------------------------------------------------------------------------
# Parse failures still return 200
# ---------------------------------------------------------------------------

def test_unparsable_ics_returns_200_with_parse_error():
    resp = validate("not an iCalendar document")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 1
    issue = body["issues"][0]
    assert issue["code"] == "PARSE_ERROR"
    assert issue["event_index"] is None
    assert issue["uid"] is None
    assert "invalid iCalendar" in issue["message"]


def test_empty_body_text_returns_parse_error():
    resp = validate("")
    assert resp.status_code == 200
    assert resp.json()["issues"][0]["code"] == "PARSE_ERROR"


def test_unclosed_component_returns_parse_error():
    resp = validate("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert resp.status_code == 200
    assert resp.json()["issues"][0]["code"] == "PARSE_ERROR"


def test_missing_ics_field_is_a_request_error():
    resp = client.post("/validate", json={})
    assert resp.status_code == 422
