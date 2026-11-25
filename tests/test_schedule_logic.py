"""Tests for the pure schedule rules (ADO 351).

schedule.py has no Home Assistant imports, so it is loaded through a synthetic
package, as tests/test_threephase_decode.py does for protocol.py.

The rules match the Home Assistant-side controller's regular_minutes_left:
end is exclusive, end == 1439 means 1440, a window ending at midnight joins one
starting at 0, and a joined window of 24 h or more has no end.
"""

import importlib
import pathlib
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_PKG_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "esy_sunhome"
)
if "esyx" not in sys.modules:
    _pkg = types.ModuleType("esyx")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["esyx"] = _pkg
schedule = importlib.import_module("esyx.schedule")

SYD = ZoneInfo("Australia/Sydney")


def p(start, end, soc=None):
    return {"start": start, "end": end, "soc": soc}


def hm(h, m=0):
    return h * 60 + m


def at(day, h, m=0, s=0):
    return datetime(2026, 11, day, h, m, s, tzinfo=SYD)


# Live 2026-11-23 schedule: Regular 08:00-15:00, Sell 15:00-08:00 overnight.
LIVE = {
    "regular": [p(480, 900)],
    "sell": [p(900, 1439), p(0, 480)],
}
# July sample: Sell 17:00-20:00 only.
JULY = {"sell": [p(1020, 1200)]}


# --- constants ---------------------------------------------------------------


def test_mode_constants():
    assert schedule.MODE_REGULAR == "regular"
    assert schedule.MODE_SELL == "sell"
    assert schedule.MODE_PURCHASE == "purchase"
    assert schedule.MODE_NONE == "none"


# --- scheduled_mode ----------------------------------------------------------


@pytest.mark.parametrize(
    ("minute", "expected"),
    [
        (hm(7, 59), "sell"),
        (hm(8, 0), "regular"),
        (hm(14, 59), "regular"),
        (hm(15, 0), "sell"),
        (hm(23, 59), "sell"),
        (hm(0, 0), "sell"),
    ],
)
def test_live_schedule_modes(minute, expected):
    assert schedule.scheduled_mode(LIVE, minute) == expected


@pytest.mark.parametrize(
    ("minute", "expected"),
    [
        (hm(16, 59), "none"),
        (hm(17, 0), "sell"),
        (hm(19, 59), "sell"),
        (hm(20, 0), "none"),
    ],
)
def test_july_schedule_modes(minute, expected):
    assert schedule.scheduled_mode(JULY, minute) == expected


def test_end_1439_includes_last_minute():
    enabled = {"sell": [p(900, 1439)]}
    assert schedule.scheduled_mode(enabled, 1439) == "sell"
    assert schedule.scheduled_mode(enabled, 899) == "none"


def test_overnight_window_is_one_window():
    enabled = {"sell": [p(900, 1439), p(0, 480)]}
    for minute in (900, 1200, 1439, 0, 240, 479):
        assert schedule.scheduled_mode(enabled, minute) == "sell"
    for minute in (480, 600, 899):
        assert schedule.scheduled_mode(enabled, minute) == "none"


def test_overnight_pieces_in_either_order():
    enabled = {"sell": [p(0, 480), p(900, 1439)]}
    assert schedule.scheduled_mode(enabled, 100) == "sell"
    assert schedule.scheduled_mode(enabled, 1000) == "sell"
    assert schedule.scheduled_mode(enabled, 600) == "none"


def test_all_day_period_covers_every_minute():
    enabled = {"regular": [p(0, 1439)]}
    for minute in (0, 1, 720, 1438, 1439):
        assert schedule.scheduled_mode(enabled, minute) == "regular"


def test_nothing_enabled_is_none():
    assert schedule.scheduled_mode({}, 600) == "none"
    assert schedule.scheduled_mode({"sell": [], "regular": []}, 600) == "none"


def test_overlap_precedence_sell_over_regular():
    enabled = {"regular": [p(0, 1439)], "sell": [p(600, 700)]}
    assert schedule.scheduled_mode(enabled, 650) == "sell"
    assert schedule.scheduled_mode(enabled, 800) == "regular"


def test_overlap_precedence_sell_purchase_regular():
    enabled = {
        "regular": [p(0, 1000)],
        "purchase": [p(100, 900)],
        "sell": [p(200, 800)],
    }
    assert schedule.scheduled_mode(enabled, 500) == "sell"
    assert schedule.scheduled_mode(enabled, 150) == "purchase"
    assert schedule.scheduled_mode(enabled, 850) == "purchase"
    assert schedule.scheduled_mode(enabled, 50) == "regular"
    assert schedule.scheduled_mode(enabled, 950) == "regular"
    assert schedule.scheduled_mode(enabled, 1100) == "none"


def test_purchase_alone():
    enabled = {"purchase": [p(60, 120)]}
    assert schedule.scheduled_mode(enabled, 60) == "purchase"
    assert schedule.scheduled_mode(enabled, 120) == "none"


def test_period_is_never_joined_with_itself():
    # (0, 1439) alone must not become a 38 h window; it is simply all day.
    enabled = {"sell": [p(0, 1439)]}
    assert schedule.scheduled_mode(enabled, 600) == "sell"
    assert schedule.next_transition(enabled, at(23, 10)) is None


# --- next_transition ---------------------------------------------------------


def test_next_transition_live_from_10am():
    got = schedule.next_transition(LIVE, at(23, 10, 0))
    assert got == (at(23, 15, 0), "sell")


def test_next_transition_live_from_4pm_crosses_midnight():
    got = schedule.next_transition(LIVE, at(23, 16, 0))
    assert got == (at(24, 8, 0), "regular")


def test_next_transition_just_before_boundary():
    got = schedule.next_transition(LIVE, at(23, 14, 59, 59))
    assert got == (at(23, 15, 0, 0), "sell")


def test_next_transition_exactly_on_boundary_looks_to_the_next():
    got = schedule.next_transition(LIVE, at(23, 15, 0, 0))
    assert got == (at(24, 8, 0), "regular")


def test_next_transition_ignores_seconds_and_microseconds_of_now():
    now = datetime(2026, 11, 23, 10, 0, 30, 500000, tzinfo=SYD)
    assert schedule.next_transition(LIVE, now) == (at(23, 15, 0), "sell")


def test_next_transition_july():
    assert schedule.next_transition(JULY, at(23, 12, 0)) == (at(23, 17, 0), "sell")
    assert schedule.next_transition(JULY, at(23, 18, 0)) == (at(23, 20, 0), "none")


def test_next_transition_after_window_ends_wraps_to_tomorrow():
    assert schedule.next_transition(JULY, at(23, 21, 0)) == (at(24, 17, 0), "sell")


def test_next_transition_result_is_aware_with_same_tzinfo():
    when, _mode = schedule.next_transition(LIVE, at(23, 10, 0))
    assert when.tzinfo is SYD
    assert when.utcoffset() is not None
    assert when.second == 0
    assert when.microsecond == 0


def test_next_transition_all_day_window_is_none():
    assert schedule.next_transition({"regular": [p(0, 1439)]}, at(23, 10)) is None


def test_next_transition_two_periods_covering_24h_is_none():
    enabled = {"sell": [p(0, 720), p(720, 1439)]}
    assert schedule.next_transition(enabled, at(23, 10)) is None


def test_next_transition_nothing_enabled_is_none():
    assert schedule.next_transition({}, at(23, 10)) is None
    assert schedule.next_transition({"sell": []}, at(23, 10)) is None


def test_next_transition_between_different_modes_back_to_back():
    enabled = {"regular": [p(480, 900)], "sell": [p(900, 1200)]}
    assert schedule.next_transition(enabled, at(23, 10)) == (at(23, 15), "sell")
    assert schedule.next_transition(enabled, at(23, 16)) == (at(23, 20), "none")


def test_next_transition_from_none_into_window_at_midnight():
    got = schedule.next_transition({"sell": [p(0, 60)]}, at(23, 1, 0))
    assert got == (at(24, 0, 0), "sell")


# --- extract_periods ---------------------------------------------------------


def test_extract_periods_as_stored():
    sched = {"dischargeTimeQuantum": [{"start": 900, "end": 1439, "soc": 20}]}
    assert schedule.extract_periods(sched, "dischargeTimeQuantum") == [
        {"start": 900, "end": 1439, "soc": 20}
    ]


@pytest.mark.parametrize("sched", [None, "x", 5, [], [{"start": 1, "end": 2}]])
def test_extract_periods_non_dict_schedule(sched):
    assert schedule.extract_periods(sched, "dischargeTimeQuantum") == []


@pytest.mark.parametrize("value", [None, "900-1439", 7, {"start": 1, "end": 2}, True])
def test_extract_periods_non_list_field(value):
    assert schedule.extract_periods({"f": value}, "f") == []


def test_extract_periods_missing_field():
    assert schedule.extract_periods({}, "f") == []


def test_extract_periods_drops_bad_items():
    items = [
        "oops",
        None,
        5,
        {"start": 1, "end": 2, "soc": 10},
        {"start": True, "end": 2},
        {"start": 1, "end": False},
        {"start": 1.0, "end": 2},
        {"start": 1, "end": 2.5},
        {"start": "1", "end": 2},
        {"start": 1},
        {"end": 2},
        {"start": 30, "end": 60, "soc": 50},
    ]
    assert schedule.extract_periods({"f": items}, "f") == [
        {"start": 1, "end": 2, "soc": 10},
        {"start": 30, "end": 60, "soc": 50},
    ]


def test_extract_periods_missing_soc_is_none():
    assert schedule.extract_periods({"f": [{"start": 1, "end": 2}]}, "f") == [
        {"start": 1, "end": 2, "soc": None}
    ]


@pytest.mark.parametrize("soc", [None, "20", 20.5, True])
def test_extract_periods_non_int_soc_is_none(soc):
    got = schedule.extract_periods({"f": [{"start": 1, "end": 2, "soc": soc}]}, "f")
    assert got == [{"start": 1, "end": 2, "soc": None}]


def test_extract_periods_ignores_extra_keys():
    item = {"start": 1, "end": 2, "soc": 5, "sort": 0, "power": 3000, "electric": 9}
    assert schedule.extract_periods({"f": [item]}, "f") == [
        {"start": 1, "end": 2, "soc": 5}
    ]


# --- is_enabled --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, True),
        (True, True),
        (0, False),
        (False, False),
        (None, False),
        ("1", False),
        (2, False),
        (1.0, False),
        ([1], False),
    ],
)
def test_is_enabled(value, expected):
    result = schedule.is_enabled({"sw": value}, "sw")
    assert result is expected


def test_is_enabled_missing_or_bad_schedule():
    assert schedule.is_enabled({}, "sw") is False
    assert schedule.is_enabled(None, "sw") is False
    assert schedule.is_enabled("x", "sw") is False
