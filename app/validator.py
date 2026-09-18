"""Standalone structural validation of iCalendar VEVENTs.

Unlike the expansion endpoints, validation is **total**: any input — even
unparsable iCalendar text — yields a structured diagnostics report that the
HTTP layer returns with status 200, never a 422. Every VEVENT is checked in
document order (``event_index`` is its 0-based position among the
document's VEVENTs) for:

* ``MISSING_UID`` — no UID property;
* ``MISSING_DTSTART`` — no DTSTART property;
* ``DTEND_AND_DURATION`` — DTEND and DURATION declared together
  (RFC 5545 forbids the combination);
* ``END_NOT_AFTER_START`` — the event ends at or before its start (only
  checked when DTSTART plus DTEND or DURATION are present and usable; a
  VEVENT without any explicit end is a valid zero-duration event);
* ``DUPLICATE_MASTER`` — a second (or later) master VEVENT (one without
  RECURRENCE-ID) for a UID that already has one;
* ``ORPHAN_OVERRIDE`` — a RECURRENCE-ID override whose UID has no master
  VEVENT anywhere in the document.

One event may accumulate several issues. An unparsable document produces a
single ``PARSE_ERROR`` issue whose ``event_index`` and ``uid`` are ``None``.
Issues are sorted by ``event_index`` (``None`` first), then ``code``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import ExpansionError, _event_times, _parse_calendar, _to_utc

UTC = dt.timezone.utc

PARSE_ERROR = "PARSE_ERROR"
MISSING_UID = "MISSING_UID"
MISSING_DTSTART = "MISSING_DTSTART"
DTEND_AND_DURATION = "DTEND_AND_DURATION"
END_NOT_AFTER_START = "END_NOT_AFTER_START"
DUPLICATE_MASTER = "DUPLICATE_MASTER"
ORPHAN_OVERRIDE = "ORPHAN_OVERRIDE"


@dataclass(frozen=True)
class Issue:
    """One validation finding, located by event index when possible."""

    code: str
    message: str
    event_index: int | None  # 0-based VEVENT position; None if unlocatable
    uid: str | None  # the event's UID; None when it has none (or unlocatable)


def validate_calendar(ics_text: str) -> list[Issue]:
    """Validate all VEVENTs in ``ics_text`` and return the sorted issues."""
    try:
        calendar = _parse_calendar(ics_text)
    except ExpansionError as exc:
        return [Issue(PARSE_ERROR, str(exc), None, None)]

    issues: list[Issue] = []
    events = list(calendar.walk("VEVENT"))
    for index, event in enumerate(events):
        issues.extend(_check_event(event, index))

    issues.extend(_check_recurrence_structure(events))

    issues.sort(
        key=lambda i: (
            i.event_index is not None,
            i.event_index if i.event_index is not None else 0,
            i.code,
        )
    )
    return issues


# ---------------------------------------------------------------------------
# Per-event checks
# ---------------------------------------------------------------------------


def _check_event(event, index: int) -> list[Issue]:
    """Checks that only need the event itself, in a fixed order."""
    issues: list[Issue] = []
    uid_prop = event.get("UID")
    uid = str(uid_prop) if uid_prop is not None else None

    if uid is None:
        issues.append(
            Issue(MISSING_UID, "VEVENT is missing the required UID property", index, None)
        )
    if "DTSTART" not in event:
        issues.append(
            Issue(
                MISSING_DTSTART,
                "VEVENT is missing the required DTSTART property",
                index,
                uid,
            )
        )
    if "DTEND" in event and "DURATION" in event:
        issues.append(
            Issue(
                DTEND_AND_DURATION,
                "VEVENT declares both DTEND and DURATION; "
                "RFC 5545 allows only one of them",
                index,
                uid,
            )
        )

    times = _checked_times(event)
    if times is not None:
        start, end = times
        if end <= start:
            issues.append(
                Issue(
                    END_NOT_AFTER_START,
                    f"VEVENT end {_iso(end)} is not after its start {_iso(start)}",
                    index,
                    uid,
                )
            )
    return issues


def _checked_times(event) -> tuple[dt.datetime, dt.datetime] | None:
    """UTC (start, end) when DTSTART and an explicit end are usable.

    A VEVENT without DTEND/DURATION is a valid zero-duration event, and
    broken property values leave nothing reliable to compare; both cases
    return ``None`` and skip the END_NOT_AFTER_START check.
    """
    if "DTSTART" not in event:
        return None
    if "DTEND" not in event and "DURATION" not in event:
        return None
    try:
        start, end = _event_times(event)
    except Exception:  # unparsable DTSTART/DTEND/DURATION values
        return None
    return _to_utc(start), _to_utc(end)


# ---------------------------------------------------------------------------
# Recurrence-structure checks (master/override topology per UID)
# ---------------------------------------------------------------------------


def _check_recurrence_structure(events: list) -> list[Issue]:
    """Flag duplicate masters and overrides without a matching master."""
    issues: list[Issue] = []
    masters_by_uid: dict[str, list[int]] = {}
    overrides_by_uid: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        uid_prop = event.get("UID")
        if uid_prop is None:  # ungroupable; already flagged MISSING_UID
            continue
        uid = str(uid_prop)
        if "RECURRENCE-ID" in event:
            overrides_by_uid.setdefault(uid, []).append(index)
        else:
            masters_by_uid.setdefault(uid, []).append(index)

    for uid, master_indexes in masters_by_uid.items():
        first = master_indexes[0]
        for index in master_indexes[1:]:
            issues.append(
                Issue(
                    DUPLICATE_MASTER,
                    f"duplicate master VEVENT for UID {uid!r} "
                    f"(first master at event index {first})",
                    index,
                    uid,
                )
            )
    for uid, override_indexes in overrides_by_uid.items():
        if masters_by_uid.get(uid):
            continue
        for index in override_indexes:
            issues.append(
                Issue(
                    ORPHAN_OVERRIDE,
                    f"override VEVENT for UID {uid!r} has no matching master "
                    "(no VEVENT without RECURRENCE-ID for this UID)",
                    index,
                    uid,
                )
            )
    return issues


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
