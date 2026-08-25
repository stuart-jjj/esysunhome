# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (`custom_components/esy_sunhome`) for ESY Sunhome
battery/inverter systems. It authenticates against ESY's cloud REST API, then talks to the
inverter directly over MQTT (mTLS) using a reverse-engineered binary Modbus-like protocol,
matching what the official ESY mobile app does on the wire.

`esy_inverter_protocol.py` at the repo root is a standalone reverse-engineering reference
document (not imported by the integration) — it captures the raw protocol notes independent of
the HA-integrated dynamic parser in `custom_components/esy_sunhome/protocol.py`. Don't treat it
as live code; treat the module under `custom_components/esy_sunhome/` as the source of truth.

## Commands

Install deps:
```
pip install -r requirements.txt -r requirements.test.txt
```

Run tests:
```
pytest
```
(config lives in `setup.cfg` under `[tool:pytest]`; runs with `--cov=custom_components` and the
`syrupy` snapshot plugin, testpaths = `tests/`)

Run a single test:
```
pytest tests/test_threephase_decode.py::test_three_phase_inflow_is_import_and_ct1_excluded
```

Lint/format (mirrors `.pre-commit-config.yaml`, run individually or via `pre-commit run --all-files`):
```
ruff check --fix custom_components tests
ruff format custom_components tests
black custom_components tests
mypy custom_components
codespell
```

## Architecture

**Auth + dynamic protocol bootstrap** (`__init__.py`): on `async_setup_entry`, the integration
logs into the ESY REST API (`esysunhome.py::ESYSunhomeAPI`), then re-detects protocol parameters
(`pvPower`, `tpType`, `versionMcu`) from `/api/lsydevice/info` — the authoritative source, since
stored config-entry values or the device LIST endpoint can be stale/wrong and cause telemetry to
decode at the wrong register addresses. Those parameters are fed to `protocol_api.py` to fetch a
per-model `ProtocolDefinition` (register map + polling segments) from ESY's protocol API, cached
24h. Everything downstream (parsing, writes) resolves registers dynamically through this map
rather than hardcoding addresses, because **register addresses vary by model** (e.g.
`systemRunMode` is register 57 on single-phase units but 72 on three-phase; 57 is a different
register, `clearMeterEnergy`, on three-phase).

**Coordinator** (`coordinator.py::ESYSunhomeCoordinator`, a `DataUpdateCoordinator`): owns the
MQTT connection lifecycle (`abroadtcp.esysunhome.com:8883`, mTLS, client cert fetched via
`get_mqtt_credentials`) and polls by publishing a segment-read request to the device's `DOWN`
topic; the device replies on `UP` (and duplicates full dumps on `EVENT`). Falls back to
REST-triggered updates when MQTT is disconnected. Also polls BEM (Battery Energy Management, a
server-side scheduling mode) state and SOC schedule data via REST on a slower cadence, since BEM
is invisible on the MQTT register that reports the base mode.

**Binary protocol parsing** (`protocol.py`): `MsgHeader` (24-byte header) + `PayloadParser`
(segmented register values) + `DynamicTelemetryParser`, which decodes raw register values using
the `ProtocolDefinition` register map and then applies `_compute_derived_values` — phase-count-
dependent corrections (see `tests/test_threephase_decode.py` for the specifics: 3-phase sites
exclude the single-phase `ct1Power` CT clamp, treat `totalPowerOfGridInFlow` as already
sign-correct import power, prefer AC-side energy-flow figures over DC registers, and normalize
per-source flows to conserve with load). `ESYCommandBuilder` builds outbound write commands
(single/multi register writes, poll requests).

**Root-caused and fixed 2026-08-25: near-total telemetry corruption + all writes rejected, traced
to a `configId` mismatch, not a missing/renamed register.** Symptoms included SOC, load power, grid power, grid voltage, grid frequency,
and total generation all silently wrong (not missing/zero in the usual way — `gridVoltage` read 0.8V, `gridFrequency`
read 111.05Hz, `battery_status` said "Standby" while `batteryPower` reported 6515W) while MQTT
stayed connected, EVENT messages kept arriving every few seconds, and `coordinator.last_exception`
was `null` the whole time — i.e. decode wasn't crashing or falling back to defaults, it was
confidently producing garbage. Power-control writes (`number.py`) were being published
successfully (`Published command to .../DOWN` logged fine) but always failed to confirm, and the
ESY app itself popped "the requested parameter is out of range or invalid" when a write was
attempted from the integration.

Root cause: every MQTT message header carries a `configId` (`MsgHeader.config_id`,
`protocol.py`'s `_configId` raw key), and `ESYSunhomeCoordinator`/`number.py` resolve register
addresses from a `ProtocolDefinition` fetched from ESY's `/sys/protocol/list` +
`/sys/protocol/segment` endpoints, keyed by `(pvPower, tpType, mcuVersion)` — that response also
carries its own `configId` (`protocol_api.py`'s `ProtocolDefinition.config_id`, from the segment
list's `configId` field). If the fetched definition's `config_id` doesn't match what the physical
device is actually broadcasting, every register address means something different: reads silently
decode the wrong bytes (explaining the garbage-but-not-crashing values above), and writes land on
whatever register that address actually is under the *device's* real config — which is why the app
reported "out of range or invalid" rather than the write just quietly doing nothing.

Confirmed live on this device (E08CFE52AE74): the device's own wire `_configId` is **23**
(read directly off a live message header via the `dump_debug` service's raw-values dump — sort by
key, `_configId` is set before any register keys in `_build_telemetry_data`). But
`/api/lsydevice/info` (previously trusted as the "authoritative" source for `pvPower`/`tpType`/
`mcuVersion`, see `async_migrate_entry`/the old re-detect block this replaced) reports
`pvPower=10` for this device — likely because this site's PV is partly AC-coupled via a separate
meter/channel, so `pvPower=10` reflects only the DC-coupled capacity the inverter itself sees, not
whatever total the protocol-selection API keys off. At `pvPower=10`, the protocol API returns
`configId=13` **regardless of `mcuVersion`** (1137 and 1146 both resolve to the same wrong
`configId=13`), which is what made this so confusing to diagnose — the wrong `pvPower` doesn't
just pick a wrong config, it masks `mcuVersion`'s real effect entirely. A saved debug dump from
2026-07-05 (a working session, before the breakage) showed `mcuVersion=1137` and wire
`_configId=20` — proving the device's own `configId` genuinely changes over time as ESY pushes
firmware updates (1137 → 1146 by 2026-08-25), which is what actually broke this integration a few
weeks prior to this fix, not a generic "vendor changed the MQTT format" issue. The correct
combination for this device, confirmed by brute-force testing `pvPower ∈ {0, 10, 20}` ×
`mcuVersion ∈ {1137, 1146}` (`tpType=3` fixed) against the live protocol API and checking each
result's `configId`, is **pvPower=20, tpType=3, mcuVersion=1146 → configId=23** — exact match to
the wire. `pvPower=0` returns a near-empty fallback config (`configId=6`, ~36 registers) and isn't
a real option.

Fix, in `__init__.py`'s `async_setup_entry`: `pvPower` and `tpType` are now **config-flow-only**
(read from `entry.data`, never re-detected/overridden from `/api/lsydevice/info` at runtime) —
`pvPower` because it's proven unreliable for this purpose, `tpType` because it's a fixed physical
property (phase count) that has no reason to change. `mcuVersion` **is still re-detected from
`/api/lsydevice/info` and overrides the stored value whenever it differs** (self-healing, scoped to
just this one field now) — unlike `pvPower`, it's confirmed reliable, and a real firmware update is
exactly the kind of thing expected to recur, so it needs to keep tracking automatically rather than
silently going stale. If this breaks again after a future ESY firmware push, check
`dump_debug`'s `_configId` against what got auto-detected — if `mcuVersion` alone doesn't produce
a matching `configId`, `pvPower` may need re-deriving the same way (see below), since a firmware
update could in principle also change which `pvPower` value ESY's protocol API expects, though
that wasn't observed to be the case here.

**Diagnostic service added and kept permanently: `esy_sunhome.test_protocol_params`** (in
`__init__.py`, alongside `dump_debug`/`write_raw_register`). Takes `pv_power`/`tp_type`/
`mcu_version`, force-fetches (`get_protocol_definition(..., force_refresh=True)`) the protocol
definition ESY's API resolves for that combination, and logs the resulting `configId` and register
counts — without touching the running coordinator, config entry, or device. This is how the
`pvPower=20` combination above was actually found: call it for each candidate combination, then
compare each `test_protocol_params RESULT` log line's `configId` against `dump_debug`'s live wire
`_configId`. Use this before committing to a config-flow reconfigure.

**Gotcha confirmed live while diagnosing this: `get_protocol_api()` (`protocol_api.py`) is a
process-wide singleton** (`_protocol_api_instance` is a bare module-level global), so its
`_protocol_cache` dict persists across integration reloads and config-entry remove/re-add — only a
full Home Assistant restart actually clears it. Don't assume a reload gives you a clean cache when
testing; if a fetch behaves unexpectedly right after a reload/re-add, a full restart rules out
stale cache as the cause (this is also why `test_protocol_params` always passes
`force_refresh=True` rather than relying on cache invalidation).

**Gotcha: the config flow's device auto-select (`config_flow.py`'s `extract_protocol_params`,
sourced from `/api/lsydevice/page`/`/api/lsydevice/detail`) can report a materially different
`pvPower`/`mcuVersion` than `/api/lsydevice/info`** (observed live: `pvPower=6, mcuVersion=1049`
from the LIST/detail endpoints vs. `pvPower=10, mcuVersion=1146` from device-info, for the same
device at the same time) — the `async_step_protocol` form pre-fills from whichever of these ran,
which the user must actively overwrite if it's wrong; it's easy to submit the form without
noticing the pre-filled value is stale (happened twice while fixing this).

**Known pre-existing bug, not fixed as part of this (out of scope): `dump_debug`'s "Parsed values"
section always crashes** — `coordinator.data.data` is always `None` (`TelemetryData.__getattr__`
never raises, so `hasattr(coordinator.data, 'data')` is always `True` and resolves to `None`, then
`None.items()` raises), so the service dies with a 500 partway through, after the "Raw values"
section (which is what actually matters for `_configId`) has already logged successfully.
`diagnostics.py`'s own downloadable diagnostics dump already has a comment noting this exact
`TelemetryData` gotcha and correctly uses `coordinator.data._data` instead — `dump_debug` in
`__init__.py` was never updated to match.


**Entities**: all platform entities (`sensor.py`, `binary_sensor.py`, `select.py`, `switch.py`,
`number.py`) subclass `EsySunhomeEntity` (`entity.py`), a `CoordinatorEntity` that reads from
`coordinator.data` (a `TelemetryData` — an attribute-accessible dict wrapper) and builds its
`unique_id`/`DeviceInfo` from the coordinator's device id. `number.py` distinguishes two write
paths: power-control registers (charge/discharge %, export/output/SOC limits) are resolved
per-model from the register map and written via MQTT register writes
(`coordinator.write_register`/`write_registers`); BEM SOC cutoffs are written via the schedule
REST API instead, since BEM is server-side. Charge/discharge sliders are exposed as a friendly %
but written as watts (against `ESY_PER_PHASE_RATED_W = 5000` × phase count), matching what the
ESY app's normal-user controls actually send. Power-control sliders are disabled while
`systemRunMode` is Sell/Export mode, since `antiBackflowPowerPercentage` (Export Power Limit) is
confirmed on real hardware to latch on mode entry and ignore live writes while selling — but this
gating is a *blanket* rule applied to all `CONTROLS` entries via one shared `available` property,
originally generalised from that one confirmed case, not verified per-register. `maxOutputPowerPercent`
(Max Output Power) is confirmed on real hardware to function as a live power setpoint *while already
in Sell Mode* — `PowerControlDescriptor.available_in_sell` (both `max_output_power_percent` and
`max_output_power_watts`) opts a control out of the Sell-mode gating so this can be tested/relied on
per-register rather than assumed for all four controls uniformly. Confirmed on real hardware
specifically during a **BEM-scheduled** Sell window (not just a manually-entered one): Max Output
Power stayed settable and writes were dispatched/confirmed normally, exactly as in a manual Sell
Mode — BEM-driven Sell doesn't add any extra lockout beyond what `systemRunMode` already implies.
Confirmed on real hardware:
enabling Battery Energy Management with a defined schedule can itself drive the MQTT-reported
`systemRunMode` to Sell (3) — so Export Power Limit, On-Grid SOC Limit, and Off-Grid SOC Limit go
unavailable purely as a side effect of BEM being active/scheduled, not just from a user manually
selecting Sell mode. This is a separate mechanism from the Operating Mode select's own
unavailability, which is gated directly on `coordinator.bem_active` (`select.py`) rather than on
`systemRunMode`. Confirmed on real hardware: when BEM's own schedule puts the inverter back into
Regular Mode, the power-control entities become available again exactly as they would from a
manual mode change — BEM introduces no additional/separate locking mechanism of its own. BEM is
purely a `systemRunMode` scheduler (cycling Regular/Sell/Emergency server-side on a timer); all
availability behaviour is fully explained by whatever mode it's currently driven to, nothing BEM-
specific beyond that.

Power-control writes are tracked through a pending/confirm/retry cycle mirroring the mode select's
(`select.py::_schedule_confirmation_timeout`): `async_set_native_value` writes the register,
`native_value` shows the pending value until telemetry confirms it within tolerance (step-based) or
up to `NUMBER_MAX_RETRIES` retries time out, and `esy_sunhome_number_change_requested` /
`esy_sunhome_number_changed` / `esy_sunhome_number_change_retry` / `esy_sunhome_number_change_timeout`
events fire at each stage — mirroring the mode select's own `esy_sunhome_mode_change_*` events — so
external automations (e.g. a pyscript controller) can react to a real confirmation instead of a
fixed sleep. Before this, `native_value`'s fallback to `self._optimistic` was effectively dead code:
telemetry for a given key is basically never `None` once the device has reported it once
(`coordinator._last_data` accumulates and is never cleared), so it kept showing the stale pre-write
value until confirmed rather than what was just requested — fixed by having `native_value`
prioritise the pending value while a write is unconfirmed.

An earlier note here claimed you must wait for a full `EVENT` telemetry dump after writing a
power-control value before switching to Sell mode, or the inverter would latch the previous value —
a follow-up test on real hardware disproved this (the register was written, then mode was switched
to Sell ~90s later with no intervening `EVENT` dump, and the new value latched correctly). The new
confirm/event mechanism above supersedes the need to guess at this: wait for
`esy_sunhome_number_changed` rather than any specific log line or fixed delay.

**Mode control** (`battery.py::BatteryState`): holds the MQTT-register-value ↔ display-name maps.
API and MQTT use *different* numeric codes for the same modes (documented in comments in
`battery.py` and `coordinator.py::set_mode_mqtt`) — don't assume a code from one surface applies
to the other. Mode changes go through either the REST API or a direct MQTT register-57/72 write,
selectable via `CONF_MODE_CHANGE_METHOD` in config flow options.

**Config flow** (`config_flow.py`): username/password login, device selection, and protocol
parameter detection at setup time; `async_migrate_entry` in `__init__.py` handles upgrading
older config entries (pre-v2, missing protocol params) in place.

## Known limitations

**No real-time/VPP-grade dispatch.** BEM and the mode select/power-limit controls this integration
exposes are all built from reverse-engineering the ESY *consumer* mobile app (per
`esy_inverter_protocol.py`'s own APK/smali analysis notes) — they inherit that app's UX
constraints, not a dispatch API: BEM is a coarse schedule (time windows + SOC cutoffs), the mode
select locks out while BEM is active, and the export/output-limit registers latch on Sell-mode
entry (see the Entities section above). None of this is designed for low-latency setpoint control.

The `device_info` REST response includes two fields this integration has never read or acted on:
`vppJoin` and `ausBatteryConnection`. `ausBatteryConnection` strongly suggests **CSIP-AUS**
(Common Smart Inverter Profile - Australia, built on IEEE 2030.5) — the standardized DER dispatch
interface Australian DNSPs/VPP aggregators use for dynamic connection agreements on newer
solar/battery inverters. If that's what it is, real VPP operators almost certainly control the
inverter through that channel — a separate control plane run by the inverter's own firmware
talking to a DNSP/aggregator-hosted IEEE 2030.5 server — not through ESY's cloud API or MQTT
broker at all. `vppJoin` is likely just an enrollment/consent flag for that program on ESY's side,
not a second cloud API endpoint. This is out of reach for this integration to add: it isn't
something dialed into via the app-facing API this integration mirrors, so don't go looking for a
"hidden endpoint" that unlocks unrestricted control — if it exists, it's a different protocol
entirely, requiring DNSP/retailer VPP enrollment, not just different credentials.

## Testing notes

- `tests/test_threephase_decode.py` loads `protocol.py` through a synthetic `esyx` package
  (`sys.modules` shim) so its relative imports resolve without pulling in Home Assistant itself —
  follow that pattern for other pure-logic unit tests on this module that shouldn't need the full
  `pytest-homeassistant-custom-component` fixture stack.
- Full-integration tests (e.g. `test_init.py`) use the `hass` fixture from
  `pytest-homeassistant-custom-component` with `enable_custom_integrations` (autoused in
  `tests/conftest.py`).
