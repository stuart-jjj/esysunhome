# ESY schedule sensor: expose the BEM schedule to Home Assistant

Status: draft for approval, 2026-11-24. Owner: Stuart. Repo: `stuart-jjj/esysunhome` (this fork), branch `feat/schedule-sensor`.
Tracked as ADO 351 (home-assistant project). Consumer: the ESY charge-match loop in the `homeassistant-config` repo
(ADO 353), which reads this sensor to learn when the battery next leaves Regular mode. Optional follow-on: ADO 352.

## Intent and why

The integration already fetches the user's BEM schedule from ESY's cloud API about every 3 minutes
(`coordinator.schedule_data`, every 12th 15 s poll in `_check_bem_state`) but exposes only the three SOC cutoffs
as entities. Home Assistant automations and pyscript therefore cannot see when the schedule switches between
Regular and Sell mode. The end user edits the schedule in the ESY app to the minute, so nothing can be hard-coded.

This change publishes the schedule as a read-only sensor, plus two derived facts: what mode the schedule says
applies now, and when it next changes. **The raw `system_mode` sensor stays the source of truth for what the
inverter is actually doing** (decided by the user). The schedule is extra context and never overrides it.

## What gets built

### The sensor (`sensor.esy_sunhome_e08cfe52ae74_schedule`)

One new sensor, `ScheduleSensor`, built like the existing ones on `EsySunhomeEntity` + `SensorEntity`, with
translation key `schedule` and English name "Schedule" (so the entity id matches the one the controller reads).

- **State:** what the schedule says the mode should be now: `regular`, `sell`, `purchase` or `none`
  (my call; the consumer ignores the state). If periods overlap, `sell` wins over `purchase` over `regular`
  (my call; the real device's precedence is unknown). `none` when no enabled period contains the current time.
  `unknown` (state `None`) until the first schedule fetch.
- **Attributes** (the consumer contract, recorded on ADO 351; values are plain JSON types):

| Attribute | Type | Meaning |
|---|---|---|
| `regular_periods` | list of `{start, end, soc}` | `releaseTimeQuantum` periods **as stored** (minutes since midnight, ints; an overnight window stays split at midnight), whether or not enabled |
| `regular_enabled` | `bool` (exactly `True`/`False`) | `releaseSwitch` |
| `sell_periods`, `sell_enabled` | same shapes | `dischargeTimeQuantum`, `dischargeSwitch` |
| `purchase_periods`, `purchase_enabled` | same shapes | `chargeTimeQuantum`, `chargeSwitch` |
| `fetched_at` | ISO 8601 string **with offset** | when the schedule was last fetched successfully |
| `updated_at` | ISO 8601 string with offset, or absent | ESY's `updateTime` (epoch seconds) converted |
| `next_transition` | ISO 8601 string with offset, or absent | the local time the scheduled mode next changes |
| `next_mode` | string | the mode it changes to |

Missing or malformed fields in ESY's response degrade to an empty list or `False`, never an exception.

### Derived values (pure functions, no Home Assistant imports)

New module `custom_components/esy_sunhome/schedule.py`, tested in isolation. Rules (identical to the controller's,
so the two never disagree):

- A period's `end` is exclusive, except `end == 1439` is treated as 1440 (the app stores end-of-day as 1439).
- A period ending at 1440 joins a period starting at 0, so an overnight window (stored as 900-1439 and 0-480)
  is one window running 15:00 to 08:00 the next day.
- A joined window that covers the whole day (`end - start >= 1440`) has no end, so it yields no transition.
- Only periods of enabled categories count toward `scheduled_mode` and `next_transition`.
- `scheduled_mode(...)` returns the mode of the enabled window containing the given local minute; `next_transition(...)`
  returns the first moment strictly after now at which the scheduled mode differs from the mode now, searching
  up to 48 hours ahead, together with that new mode; it returns nothing when the mode never changes (no enabled
  periods, or a window covering the whole day). Both take the local time as an argument (no clock reads inside).
- Local time is Home Assistant's configured time zone (`homeassistant.util.dt.now()`), not the process TZ.

### Coordinator

One new field, `schedule_fetched_at: datetime | None` (aware, UTC), set wherever `schedule_data` is set: in
`_check_bem_state` after a successful `get_schedule()`, and in `number.py` after a SOC-cutoff save refreshes
`coordinator.schedule_data`. A failed fetch leaves `schedule_data` and `schedule_fetched_at` unchanged, so
`fetched_at` stops advancing and consumers can tell the data is stale (the controller treats more than 15 minutes
as stale).

### Update cadence

The coordinator ticks every 15 s and the sensor recomputes its state and `next_transition` on each tick, so the
state flips at the right minute without waiting for the 3-minute schedule fetch. Home Assistant only records a
state change when the state or attributes change.

## Goals, non-goals, anti-goals

Goals: the sensor reports the schedule exactly as ESY stores it, the scheduled mode now, and the next
transition, with the contract above, so the controller can go live; a schedule edit in the app appears within one or
two fetch cycles (about 3 to 6 minutes).

Non-goals: writing the schedule (the SOC cutoff numbers already do that); changing any existing entity, the mode
select, or the `system_mode` sensor; the optional deviation-reason sensor (ADO 352); exposing the sensor through the
diagnostics download; any HA restart or HACS action from code (see Delivery).

Anti-goals (failure even if it works): an exception in the sensor breaking other entities; the sensor claiming a
fresh `fetched_at` when the fetch failed; publishing naive timestamps; any change to existing entity ids or
behaviour; the derived state disagreeing with the controller's reading of the same periods.

## Testing

- **Pure-logic tests** (follow `tests/test_threephase_decode.py`, which loads a module without the full Home
  Assistant stack): the live 2026-11-23 schedule (Regular 480-900; Sell 900-1439 and 0-480; purchase empty), the July
  sample (Sell 1020-1200), a window spanning midnight in both directions, an all-day window (no transition),
  `end == 1439` handling, disabled categories, empty and malformed input, overlapping periods, and transitions at
  and just either side of a boundary minute.
- **Sensor test** with the `hass` fixture (follow `tests/test_init.py`): set `coordinator.schedule_data` and
  `schedule_fetched_at`, assert the state and every attribute in the table (including that `regular_enabled` is a real
  `bool`, ints stay ints, and timestamps carry an offset); assert the pre-fetch state is `unknown` with no attributes;
  assert a failed refetch leaves `fetched_at` unchanged.
- **Cross-check with the consumer:** a test (or a documented manual check) that the attributes pass the controller's
  `parse_regular_periods` shape rules: ints in 0..1440, `start < end`, `regular_enabled is True`, aware `fetched_at`.
- The dev environment needs `pip install -r requirements.txt -r requirements.test.txt` (syrupy and
  pytest-homeassistant-custom-component are not installed on this machine); use a throwaway virtualenv outside the
  repo. Run with `pytest`; format with `ruff` and `black` as `CLAUDE.md` says.

## Delivery

- Work on branch `feat/schedule-sensor` (this fork uses feature branches and pull requests). Opening the PR, merging,
  tagging a release (version bump in `manifest.json`, next tag after `2026.11.1`) and pushing are the owner's calls.
- Install on `ha-birchgrove` through HACS and restart Home Assistant there. That is a customer site and the restart
  briefly pauses the Birchgrove export controller's polling, so the owner chooses the moment. Nothing in this change
  touches a live instance.
- Verify live: after the update, read the sensor on `ha-birchgrove` and compare it to the schedule in the ESY app
  (Regular 08:00-15:00, Sell 15:00-08:00 as of 2026-11-23); edit the schedule in the app and confirm the sensor
  follows within two fetch cycles.
- `CLAUDE.md` in the fork gets a short paragraph describing the sensor and its attributes.

## Left to the builder

Names of private helpers and test fixtures; whether the period dictionaries reuse one small helper to build the
attribute lists; log wording (debug level only); the sensor's icon (follow the neighbouring sensors); handling of unexpected extra keys in ESY's period objects (ignore them).

## Open items

None. Calls I made, for the owner to check while reading: the state vocabulary and its overlap precedence; publishing
all three categories (not only Regular); `updated_at` as an extra attribute; the sensor not appearing in diagnostics.
