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
`systemRunMode` is Sell/Export mode, since the inverter latches those registers on mode entry and
ignores live writes while selling. Confirmed on real hardware: enabling Battery Energy Management
with a defined schedule can itself drive the MQTT-reported `systemRunMode` to Sell (3) — so Export
Power Limit, Max Output Power, On-Grid SOC Limit, and Off-Grid SOC Limit all go unavailable purely
as a side effect of BEM being active/scheduled, not just from a user manually selecting Sell mode.
This is a separate mechanism from the Operating Mode select's own unavailability, which is gated
directly on `coordinator.bem_active` (`select.py`) rather than on `systemRunMode`.

Writes to these power-control numbers are **optimistic and unconfirmed**: `async_set_native_value`
publishes the MQTT write and immediately shows the requested value via `self._optimistic`, before
the inverter has necessarily applied it — unlike the mode select, which has an explicit
pending/confirm/retry cycle (`select.py::_schedule_confirmation_timeout`), power-control numbers
have no equivalent. Confirmed on real hardware: if you change a power-control value (e.g. Max
Output Power) and then switch straight into Sell/Export mode, the inverter can latch the *previous*
value instead of the one you just set, since the mode-entry latch can occur before the write has
actually been committed device-side. The reliable workaround is to wait for confirmation that the
new value has round-tripped back through telemetry — visible as a `Received EVENT message
(... bytes) - full data dump` log line (or the entity settling to the new value from live data
rather than the optimistic one) — before switching to Sell mode.

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
