"""Pure rules for the ESY BEM schedule (no Home Assistant imports).

The rules match the Home Assistant-side export controller's
``regular_minutes_left`` so the two never disagree:

* a period's ``end`` is exclusive, except ``end == 1439`` means 1440 (the app
  stores end-of-day as 1439);
* a period ending at 1440 joins a different period starting at 0, so an
  overnight window stored as 900-1439 plus 0-480 is one window;
* a joined window of 24 h or more covers the whole day and has no end.

Times are local minutes since midnight. Nothing here reads the clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

MODE_REGULAR = "regular"
MODE_SELL = "sell"
MODE_PURCHASE = "purchase"
MODE_NONE = "none"

# Overlap precedence: first match wins.
_PRECEDENCE = (MODE_SELL, MODE_PURCHASE, MODE_REGULAR)

_DAY = 1440
_SEARCH_MINUTES = 48 * 60


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def extract_periods(schedule: dict | None, field: str) -> list[dict]:
    """Return the ``{"start", "end", "soc"}`` list stored under ``field``.

    Items that are not dicts with int (not bool) ``start`` and ``end`` are
    dropped; ``soc`` is the int or ``None``. Anything not a list gives ``[]``.
    """
    if not isinstance(schedule, dict):
        return []
    raw = schedule.get(field)
    if not isinstance(raw, list):
        return []
    periods = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        end = item.get("end")
        if not _is_int(start) or not _is_int(end):
            continue
        soc = item.get("soc")
        periods.append(
            {"start": start, "end": end, "soc": soc if _is_int(soc) else None}
        )
    return periods


def is_enabled(schedule: dict | None, field: str) -> bool:
    """True only for ``True`` or the integer ``1``."""
    if not isinstance(schedule, dict):
        return False
    value = schedule.get(field)
    return value is True or (_is_int(value) and value == 1)


def _joined_windows(periods: list[dict]) -> list[tuple[int, int]]:
    """Windows as (start, end) with overnight pieces joined, as the controller does."""
    norm = [(p["start"], _DAY if p["end"] == 1439 else p["end"]) for p in periods]
    joined = list(norm)
    for i, (s1, e1) in enumerate(norm):
        if e1 != _DAY:
            continue
        for j, (s2, e2) in enumerate(norm):
            if j != i and s2 == 0:
                joined.append((s1, _DAY + e2))
    return joined


def _contains(windows: list[tuple[int, int]], minute: int) -> bool:
    for start, end in windows:
        if end - start >= _DAY:
            return True
        # Test today and "tomorrow" so the early half of an overnight window
        # is found from the other side of midnight.
        if start <= minute < end or start <= minute + _DAY < end:
            return True
    return False


def _compile(enabled: dict[str, list[dict]]) -> list[tuple[str, list[tuple[int, int]]]]:
    return [(mode, _joined_windows(enabled.get(mode) or [])) for mode in _PRECEDENCE]


def _mode_at(compiled, minute: int) -> str:
    for mode, windows in compiled:
        if _contains(windows, minute):
            return mode
    return MODE_NONE


def scheduled_mode(enabled: dict[str, list[dict]], minute: int) -> str:
    """The mode of the enabled window containing local ``minute`` (0..1439).

    ``enabled`` maps a mode name to its period list; omit disabled categories.
    Overlaps resolve sell, then purchase, then regular.
    """
    return _mode_at(_compile(enabled), minute)


def next_transition(
    enabled: dict[str, list[dict]], now: datetime
) -> tuple[datetime, str] | None:
    """First minute boundary strictly after ``now`` where the mode changes.

    Returns ``(boundary, new_mode)`` with seconds and microseconds zero and
    ``now``'s tzinfo, or ``None`` if the mode never changes within 48 h.
    """
    compiled = _compile(enabled)
    start = now.replace(second=0, microsecond=0)
    current = _mode_at(compiled, start.hour * 60 + start.minute)
    for step in range(1, _SEARCH_MINUTES + 1):
        # Wall-clock arithmetic: naive minute counting keeps the local time.
        candidate = start + timedelta(minutes=step)
        mode = _mode_at(compiled, candidate.hour * 60 + candidate.minute)
        if mode != current:
            return candidate, mode
    return None
