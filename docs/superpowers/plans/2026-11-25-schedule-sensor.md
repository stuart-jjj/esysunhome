# ESY schedule sensor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the ESY BEM schedule as `sensor.esy_sunhome_e08cfe52ae74_schedule`, with the scheduled mode now and the next transition, for the charge-match controller (ADO 351).

**Architecture:** A pure module `schedule.py` (no Home Assistant imports) turns ESY's schedule dict into attribute lists, the scheduled mode at a local minute, and the next transition. The coordinator records when the schedule was last fetched. `ScheduleSensor` computes its state and attributes on demand from `coordinator.schedule_data`, so every 15 s coordinator tick refreshes it.

**Tech Stack:** Python 3.12, Home Assistant custom integration, pytest + `pytest-homeassistant-custom-component`, ruff, black.

**Spec:** `docs/superpowers/specs/2026-11-24-schedule-sensor-design.md` (approved 2026-11-25). Read it first; it holds the attribute table and the rules.

## Global Constraints

- Repo: this fork, branch `feat/schedule-sensor`. Do not push, open a PR, tag, bump `manifest.json`, or touch any live Home Assistant. Commit locally only.
- Entity: translation key `schedule`, English name "Schedule", state vocabulary `regular` | `sell` | `purchase` | `none`, `None` before the first fetch. Overlap precedence: sell over purchase over regular.
- Attributes exactly as the spec table: `regular_periods`, `regular_enabled`, `sell_periods`, `sell_enabled`, `purchase_periods`, `purchase_enabled`, `fetched_at`, `updated_at`, `next_transition`, `next_mode`. Periods are `{"start": int, "end": int, "soc": int|None}` as stored. Enabled flags are real `bool`. Timestamps are ISO 8601 strings **with offset**.
- Rules (identical to the controller's `regular_minutes_left`): `end` exclusive; `end == 1439` means 1440; a window ending at 1440 joins a window starting at 0; a joined window with `end - start >= 1440` has no end. Only enabled categories count. A period is never joined with itself.
- ESY field names: `releaseTimeQuantum`/`releaseSwitch` (Regular), `dischargeTimeQuantum`/`dischargeSwitch` (Sell), `chargeTimeQuantum`/`chargeSwitch` (purchase), `updateTime` (epoch seconds).
- Bad or missing ESY fields degrade to an empty list or `False`; the sensor never raises.
- Do not change any existing entity id or behaviour, and do not reformat files you do not otherwise change.
- Dev environment is ready at `/tmp/claude-1000/esy-venv` (installed from `requirements.txt` + `requirements.test.txt`, plus ruff and black). Run tests as `/tmp/claude-1000/esy-venv/bin/pytest`. Format only the files you touch: `ruff check --fix <files>`, `black <files>` (venv `bin/`).
- Commit messages end with the two attribution lines the session was given (`Co-Authored-By: ...` and `Claude-Session: ...`).

## Review Focus

- Regular switched off in the app: `regular_enabled` is `False`, the periods still appear, the state is not `regular`, and `next_transition` ignores them.
- ESY returns `null` for a period list, a missing switch, or a string where a number belongs: empty list / `False`, no exception.
- The exact boundary minute (15:00:00 is already Sell; 14:59:59 is still Regular) and the midnight crossing in both directions of an overnight window.
- A failed refetch after a good one: `fetched_at` stays at the last good time (never advances).
- An all-day window and a schedule with nothing enabled: state `regular`/`none` as appropriate and **no** `next_transition` / `next_mode` attributes.

---

### Task 1: Pure schedule logic (`schedule.py`)

**Files:**
- Create: `custom_components/esy_sunhome/schedule.py`
- Create: `tests/test_schedule_logic.py` (load the module without Home Assistant, as `tests/test_threephase_decode.py` does with its synthetic package)

**Interfaces:**
- Produces (Task 3 uses these exact names):
  - `MODE_REGULAR = "regular"`, `MODE_SELL = "sell"`, `MODE_PURCHASE = "purchase"`, `MODE_NONE = "none"`
  - `extract_periods(schedule: dict | None, field: str) -> list[dict]`: the `{"start","end","soc"}` list for one `...TimeQuantum` field, as stored, dropping any item that is not a dict with int (not bool) `start` and `end`; `soc` is the int or `None`. Anything not a list gives `[]`.
  - `is_enabled(schedule: dict | None, field: str) -> bool`: `True` only for `True` or integer `1` (not other truthy values).
  - `scheduled_mode(enabled: dict[str, list[dict]], minute: int) -> str`: `enabled` maps mode name to its period list (omit disabled categories); `minute` is 0..1439 local minutes since midnight.
  - `next_transition(enabled: dict[str, list[dict]], now: datetime) -> tuple[datetime, str] | None`: `now` is an aware local datetime. Returns the first minute boundary strictly after `now` at which the mode differs from the mode at `now`, and the new mode; searches 48 h; `None` if the mode never changes. The returned datetime has seconds and microseconds zero and the same tzinfo as `now`.

- [ ] **Step 1: Write the failing tests** in `tests/test_schedule_logic.py`. Cover, with exact assertions:
  - The live 2026-11-23 schedule (Regular `[480-900]`, Sell `[900-1439, 0-480]`, purchase empty): `scheduled_mode` at 07:59 is `sell`, 08:00 `regular`, 14:59 `regular`, 15:00 `sell`, 23:59 `sell`, 00:00 `sell`. `next_transition` from 10:00 gives 15:00 same day / `sell`; from 16:00 gives 08:00 next day / `regular`; from 14:59:59 gives 15:00:00; from exactly 15:00:00 gives 08:00 next day.
  - The July sample (Sell `[1020-1200]` only): modes at 16:59/17:00/19:59/20:00 and the next transition from 12:00 (17:00 / `sell`) and from 18:00 (20:00 / `none`).
  - Overnight window stored as 900-1439 + 0-480 behaves as one window; a stored period `(0, 1439)` alone gives the mode all day and `next_transition` is `None`; two periods covering 24 h together give `None`.
  - `end == 1439` is exclusive of 23:59's successor: minute 1439 is inside a `(900, 1439)` period.
  - Nothing enabled gives `none` and `None`; overlapping Sell and Regular gives `sell`; Sell over Purchase over Regular precedence.
  - `extract_periods` with `None`, a non-dict, a `null` list, a string, items that are not dicts, bool or float starts, missing `soc`, and extra keys (`sort`, `power`, `electric`) ignored.
  - `is_enabled` with `1`, `True`, `0`, `False`, `None`, `"1"`, `2` (only `1`/`True` enable).
  - Use `zoneinfo.ZoneInfo("Australia/Sydney")` datetimes and assert the returned value is aware with the same tzinfo.
- [ ] **Step 2: Run to verify they fail:** `/tmp/claude-1000/esy-venv/bin/pytest tests/test_schedule_logic.py -p no:cacheprovider --no-cov -q`. Expected: import error / failures.
- [ ] **Step 3: Implement `schedule.py`.** Normalise `end 1439 -> 1440`, build joined windows exactly as in the controller (join `(s1, 1440)` with `(0, e2)` from a different period into `(s1, 1440 + e2)`), test a minute `m` and `m + 1440` against windows, and treat a window of 1440+ minutes as covering the whole day. For `next_transition`, step whole minutes from `now` floored to the minute up to 48 h, precomputing windows once. No Home Assistant imports; no clock reads. DST-night accuracy is not required (wall-clock minute arithmetic is accepted).
- [ ] **Step 4: Run to verify they pass** (same command, then the whole suite `pytest -q`).
- [ ] **Step 5: ruff + black on both files; commit** `feat: pure schedule logic for the schedule sensor (ADO 351)`.

---

### Task 2: Coordinator records when the schedule was fetched

**Files:**
- Modify: `custom_components/esy_sunhome/coordinator.py` (`__init__` near `self.schedule_data`; `_check_bem_state`)
- Modify: `custom_components/esy_sunhome/number.py` (`ESYSunhomeSOCNumber.async_set_native_value`)
- Create: `tests/test_schedule_fetched_at.py` (use the `entry` / `coordinator` fixture pattern from `tests/test_configid_resync.py`)

**Interfaces:**
- Produces: `ESYSunhomeCoordinator.schedule_fetched_at: datetime | None`, initially `None`, aware UTC (`datetime.now(timezone.utc)`), assigned in the same place `schedule_data` is assigned on success.

- [ ] **Step 1: Write the failing tests.** (a) A fresh coordinator has `schedule_fetched_at is None`. (b) `_check_bem_state` with `api.get_device_info` and `api.get_schedule` mocked: after success `schedule_data` is the dict and `schedule_fetched_at` is an aware datetime (`tzinfo` not None) within a few seconds of now. (c) A second call where `get_schedule` raises leaves both `schedule_data` and `schedule_fetched_at` exactly as after the first call. (d) A first call where `get_schedule` raises leaves both `None`. (e) `ESYSunhomeSOCNumber.async_set_native_value(40)` with a mocked api (`get_schedule` returns a dict, `save_schedule` async mock) sets `coordinator.schedule_data` and an aware `schedule_fetched_at`; build the entity directly as `number.py` does for its other SOC entries (read how the `CONTROLS`/SOC list constructs it), and stub `async_write_ha_state`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the field and the two assignments. Do not change the `get_device_info` handling or logging.
- [ ] **Step 4: Run to verify they pass**, then the whole suite.
- [ ] **Step 5: ruff + black on touched files** (touched files only; the repo is not uniformly formatted, so check `git diff --stat` shows only your lines); commit `feat: coordinator records when the schedule was fetched (ADO 351)`.

---

### Task 3: `ScheduleSensor`, registration, translation, docs

**Files:**
- Modify: `custom_components/esy_sunhome/sensor.py` (new class after `SystemStatusSensor`; add to the entity list under SYSTEM INFO)
- Modify: `custom_components/esy_sunhome/translations/en.json` (`entity.sensor.schedule.name` = "Schedule")
- Modify: `CLAUDE.md` (short paragraph: the sensor, its state vocabulary, the attribute contract, that `system_mode` stays the source of truth)
- Create: `tests/test_schedule_sensor.py`

**Interfaces:**
- Consumes (Task 1): `extract_periods`, `is_enabled`, `scheduled_mode`, `next_transition`, the `MODE_*` constants. (Task 2): `coordinator.schedule_fetched_at`, `coordinator.schedule_data`.
- Produces: `class ScheduleSensor(EsySunhomeEntity, SensorEntity)` with `_attr_translation_key = "schedule"`, icon `mdi:calendar-clock`. Properties `native_value -> str | None` and `extra_state_attributes -> dict | None` computed from the coordinator on each access. Local time from `homeassistant.util.dt.now()` (module-level `from homeassistant.util import dt as dt_util` so tests can patch `custom_components.esy_sunhome.sensor.dt_util.now`).

- [ ] **Step 1: Write the failing tests** (use the `hass` fixture and the coordinator fixture pattern; build `ScheduleSensor(coordinator)` directly; set the local time through the patched `dt_util.now`):
  - Live schedule fixture (`releaseTimeQuantum` 480-900 with extra keys, `dischargeTimeQuantum` 900-1439 and 0-480, `chargeTimeQuantum` `[]`, `releaseSwitch` 1, `dischargeSwitch` 1, `chargeSwitch` 0, `updateTime` 1795405320) at 10:00 Sydney: state `regular`; `regular_periods == [{"start": 480, "end": 900, "soc": ...}]`; `regular_enabled is True`; `sell_enabled is True`; `purchase_enabled is False`; `purchase_periods == []`; `next_mode == "sell"`; `next_transition` parses with `datetime.fromisoformat` to 15:00 with a non-None offset; `fetched_at` parses aware and equals the coordinator's value; `updated_at` is an aware ISO string for that epoch.
  - Same data at 16:00: state `sell`, next `regular` at 08:00 the next day.
  - `releaseSwitch` 0 at 10:00 (Sell 0-480 and 900-1439 do not cover 10:00): state `none`, `regular_enabled is False`, `regular_periods` still listed, `next_mode == "sell"`.
  - All-day Regular `(0, 1439)` enabled: state `regular`; `next_transition` and `next_mode` keys absent.
  - Nothing enabled: state `none`, those two keys absent.
  - `schedule_data = None` (pre-fetch): `native_value is None` and `extra_state_attributes` is `None` or empty.
  - `updateTime` missing or non-numeric: `updated_at` key absent. ESY returning `null` period lists and a string switch: empty lists / `False`, no exception.
  - Attribute `regular_periods` / `regular_enabled` / `fetched_at` pass the controller's shape rules, re-stated inline in the test (ints in 0..1440, not bool, `start < end`, `regular_enabled is True`, `fetched_at` aware).
  - `fetched_at` unchanged after a failed refetch is already covered by Task 2; here assert the attribute follows `coordinator.schedule_fetched_at`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the sensor (never raise from the properties: wrap computation so a bug logs at debug and returns `None`), the translation entry, the list entry, and the `CLAUDE.md` paragraph.
- [ ] **Step 4: Run the whole suite** (`pytest -q`); all existing tests still pass.
- [ ] **Step 5: ruff + black on touched Python files; commit** `feat: schedule sensor exposing the ESY BEM schedule (ADO 351)`.
