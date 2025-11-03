# Pyheat
> **About this document**
>
> This is the **functional & architectural specification** for *Pyheat*, a Home Assistant PyScript app.
> It defines what the system does, how components interact, and the contracts between files/modules.
> It is **planning-focused**: no implementation code

## 1) High-Level Overview (What It Does)

### Description
Pyheat is a home heating controller written in Home Assistant Pyscript. It manages heating schedules, boiler control, smart TRV control, per-room temperature monitoring, and heating boosts. It allows external apps to interact with it via the Home Assistant API. **Rooms are first-class domain objects:** each room owns its sensors, TRV, desired/target temperature, fused current temperature, and call-for-heat/valve decisions. The app coordinates rooms; decisions are made at the room level, not per sensor or per radiator. It is event driven, reacting on HA state changes on its helper entities, other defined HA entities, and service calls. Frost protection, window/vent detection are not handled.

### Execution Model
- **Event triggers:**  
  - `homeassistant_started`  
  - Helpers: state changes of `input_select.pyheat_*_mode`, `input_number.pyheat_*_manual_setpoint`, `input_boolean.pyheat_master_enable`, `input_boolean.pyheat_holiday_mode`  
  - Timers: `timer.pyheat_*_override` (`started|paused|cancelled`) and `timer.finished`  
  - Sensors: room temperature sensors defined in `rooms.yaml`  
  - TRV feedback (interlock): `sensor.<trv_base>_valve_opening_degree_z2m`, `sensor.<trv_base>_valve_closing_degree_z2m`
  - Services: any `pyheat.*` call  
  - Config applied: successful `pyheat.reload_config` / `pyheat.replace_schedules`
- **Time trigger:** 1-minute cron for schedule boundaries and stale-sensor checks.
- **Debounce:** coalesce bursts into one recompute per tick.
- **No busy loops:** logic runs only on triggers.
- **Immediate recompute on commands:** user actions via services re-evaluate instantly.


### Startup behavior 
- **Initial state:** Keep last-known HA states (boiler/TRVs) until recompute.
- **Recompute:** Run an immediate full recompute **and** a second recompute after a short delay to catch late-restoring sensors.
- **Master enable:** Honour `input_boolean.pyheat_master_enable` immediately.
- **Sensor freshness:** Use last-known temps from HA if present; otherwise mark sensors stale until readings arrive.
- **Overrides:** With `restore: true` timers, continue any `timer.pyheat_<room>_override` that is `active` or `paused`; if `idle`, treat it as ended.
- **First boiler ON guard:** Add a short grace before the first boiler **ON** after startup to allow TRV feedback for the interlock.
- **Logging:** Emit a one-line startup summary (mode, active overrides, rooms detected).

### Core Logic Layers
1. **Per-room state machine**  
   Inputs: sensors, mode, override/boost, schedule  
   Outputs: target, `call_for_heat`, valve %
2. **Global controller**  
   Inputs: rooms’ `call_for_heat` + safety  
   Outputs: boiler on/off and, if OpenTherm, CH flow setpoint

### State Model & Precedence
**Purpose:** specify how a room’s target is chosen, when a room calls for heat, and how those room demands turn the boiler on/off safely.

**Scope:** two levels—(1) per-room state machine, (2) global controller.

**Room states**
- `auto` — follow schedule/default, apply overrides (including boosts).
- `manual` — target = state(input_number.pyheat_<room>_manual_setpoint) (rounded to room precision); overrides/boosts ignored; if all sensors are stale → no call-for-heat and valve 0%.
- `off` — never heat; valve 0%, no `call_for_heat`.
- `stale` — all room temperature sensors unavailable/stale → safe mode: valve 0%, no `call_for_heat`.

**Global states**
- `holiday_on` (boolean) with `holiday_target` (= `constants.HOLIDAY_TARGET_C`). Holiday only swaps the base schedule target; overrides/boosts still apply.

**Precedence & Target Resolution**
Order (highest wins): room off → manual → override → schedule block → `default_target`.

Targets:
- room off: valve 0%, no `call_for_heat`, `target = None`.
- manual: `target = user_setpoint`.
- override: `target = override_target`.
- auto: `target = base_target`, where  
  `base_target = (holiday_on ? holiday_target : (schedule_block_target or default_target))`.

**Call-for-heat (per room)**
- Start calling when clearly below target; use hysteresis with a lower “turn-on” threshold.
- Keep calling until clearly above target; use a higher “turn-off” threshold.
- Near target → keep previous state to avoid flicker.
- If room is `off` or `stale`, never call and keep the valve closed.
- If no valid target, do not call.
- Boost is treated the same as an override (same priority).
- Valve % decided separately with stepped bands and step hysteresis; not part of call-for-heat.

**Asymmetric hysteresis (per-room configurable with defaults):**  
Let `e = target − temp` (°C; positive means below target).

- Start calling when `e ≥ on_delta_c(room)` (**turn-on** threshold).
- Stop calling when `e ≤ off_delta_c(room)` (**turn-off** threshold).
- If `off_delta_c(room) < e < on_delta_c(room)`, keep the previous call state (no flip).
- `on_delta_c(room) ≥ off_delta_c(room)` must hold.

Defaults live in `constants.py`; rooms may override via `rooms.yaml`.


**Global boiler decision**
- Turn boiler on only when at least one enabled room is calling for heat and at least one TRV is confirmed open above a safety minimum (feedback matches command within a short timeout).
- Keep it on until no rooms are calling for heat, then turn it off.
- Enforce anti short-cycling: minimum on time, minimum off time.
- If master enable is off, or all rooms are stale/unavailable, force the boiler off.

---

## 2) Architecture & Contracts (How Parts Talk)

### Domain Objects
- **Room** — owns sensors, TRV, target resolution, valve %, and call-for-heat logic.
- **Global Controller** — aggregates room demand, applies safety, drives boiler.
- **Scheduler** — resolves schedule/defaults/overrides/boosts.
- **Sensors** — fuses multiple sensors into a room temperature; handles stale.
- **TRV** — issues valve % commands and checks feedback.
- **Boiler** — on/off or OpenTherm setpoint control.
- **Config Loader** — reads/writes YAML, validation, atomic I/O.
- **Constants** — centralizes fixed config and default parameters.

### Events
- HA state changes on helper entities and defined entities.
- Service calls (`pyheat.*`).
- 1-minute cron tick.
- `homeassistant_started`.

### Data Flow
Sensors → Room (target resolve, hysteresis) → TRV valve % + room `call_for_heat` → Global controller (safety, anti short-cycle) → Boiler.

---

## 3) File Layout (Single Responsibility per File)

### `__init__.py` 
**Responsibilities**
- Bootstrap only: create a single `PyHeatApp` and hand it to the HA adapters.
- Instantiate and initialize `HaTriggers` and `HaServices` with references to the orchestrator.
- Ensure startup ordering (create orchestrator → init triggers/services).
- No business logic, no direct trigger/service registration.

**Public surface**
- None (internal bootstrap only).

### `core.py`

#### Responsibilities 
- Central orchestrator for all app logic and data flow.
- Owns room registry and coordinates modules (`scheduler`, `sensors`, `room_controller`, `trv`, `boiler`).
- Applies state precedence, runs recomputes, and enforces safety/interlocks via `boiler.py`

#### Public entry points (called by `__init__.py`)
- `recompute_all()` — resolve targets, call-for-heat, valve %, boiler state.
- `handle_state_change(entity_id, old, new)` — react to HA state updates.
- `handle_event(evt_type, data)` — generic event dispatcher (incl. cron tick).
- `handle_timer(room_id)` — react to `timer.pyheat_<room>_override` ticks/finish.
- Service handlers:  
  - `svc_override(room, target, minutes)`  
  - `svc_boost(room, delta, minutes)`  
  - `svc_cancel_override(room)`  
  - `svc_set_mode(room, mode)`  
  - `svc_set_default_target(room, target)`  
  - `svc_reload_config()`  
  - `svc_replace_schedules(schedule_dict)`

#### `ha_triggers.py` 
**Role:** HA/PyScript adapter for **state**, **event**, and **time** triggers.

**Responsibilities**
- Register all event sources:
  - Core: `homeassistant_started`
  - Helpers: `input_select.pyheat_*_mode`, `input_number.pyheat_*_manual_setpoint`,
    `input_boolean.pyheat_master_enable`, `input_boolean.pyheat_holiday_mode`
  - Timers: `timer.pyheat_*_override` (`started|paused|cancelled`) and `timer.finished`
  - Sensors: room temperature sensors from `rooms.yaml`
  - TRV feedback: `sensor.<trv_base>_valve_opening_degree_z2m`, `sensor.<trv_base>_valve_closing_degree_z2m`
  - 1-minute cron tick
- Debounce/coalesce bursts to one recompute per tick.
- On helper changes (mode/setpoint), request `orchestrator.recompute_all()`.
- Callbacks: route individual events to orchestrator:
  - `handle_state_change(entity_id, old, new)`
  - `handle_event(evt_type, data)`
  - `handle_timer(room_id)`
- Startup:
  - Trigger immediate recompute and a delayed second recompute (late sensor restore guard).
  - Respect master enable immediately.
  - Apply “first boiler ON” grace for TRV interlock (via orchestrator flag).

**Public entry points**
- `init(orchestrator) -> None` — register all triggers.
- `shutdown() -> None` — (optional) unregister/cleanup if needed.

---

#### `ha_services.py` 
**Role:** HA/PyScript adapter for **service registration & argument validation**.

**Responsibilities**
- Register all `pyheat.*` services and map to orchestrator methods.
- Validate/normalize service args (types, ranges) before forwarding.
- Trigger immediate recompute after each successful service call.
- Handle and log errors; return user-visible failure where applicable.

**Mapped services**
- `pyheat.override(room, target, minutes)` → `orchestrator.svc_override(...)`
- `pyheat.boost(room, delta, minutes)` → `orchestrator.svc_boost(...)`
- `pyheat.cancel_override(room)` → `orchestrator.svc_cancel_override(...)`
- `pyheat.set_mode(room, mode)` → `orchestrator.svc_set_mode(...)`
- `pyheat.set_default_target(room, target)` → `orchestrator.svc_set_default_target(...)`
- `pyheat.reload_config()` → `orchestrator.svc_reload_config()`
- `pyheat.replace_schedules(schedule_dict)` → `orchestrator.svc_replace_schedules(...)`

**Public entry points**
- `init(orchestrator) -> None` — register all services.
- `shutdown() -> None` — (optional) unregister/cleanup if needed.

---

### `config_loader.py`
Loads YAML from disk and passes it to the app.  
Creates room objects using `schedules.yaml` and `rooms.yaml` by matching IDs. Logs a warning if there is a room without a schedule or a schedule without a room. Saves YAML to disk.  
YAML I/O notes (PyScript + Home Assistant)

- Paths: always resolve with hass.config.path("config/<file>.yaml") so it works in the HA container.
- Non-blocking: perform ALL disk I/O via await task.executor(...). Never block the event loop.
- Read pattern: executor-> read text -> yaml.safe_load(text) -> normalize None to {} -> validate -> return dict.
- Write pattern: validate in-memory first -> yaml.safe_dump(data) -> executor writes to temp file in same dir -> os.replace(temp, final) for atomic swap.
- Concurrency: guard reads/writes with a simple lock around the critical section (used inside executor too).
- Error handling: on parse/validation failure, KEEP last good config; log one clear error; return (ok=False, err=...).
- Existence/permissions: ensure parent directory exists (create once in executor); treat missing file as empty YAML.
- No extra deps: use stdlib + PyYAML shipped with HA; no pip installs at runtime.

[See file I/O notes](#file-io-notes).

#### Responsibilities
- Load `rooms.yaml` and `schedules.yaml` from `hass.config.path(...)`.
- Validate both files per **§4 Validation & File I/O**; on failure, **keep last good config** and return an error (or log).
- Build the in-memory room registry by matching room IDs across `rooms.yaml` and `schedules.yaml`; **warn** for rooms without schedules and schedules referencing unknown rooms.
- Provide current configs to the orchestrator; perform **atomic writes** for updates (temp file → `os.replace`).
- Implement read/write patterns using `task.executor(...)` (no blocking in the event loop).
- Normalize trv.entity_id (climate.*) → derive number.*/sensor.* IDs using patterns in constants.py; verify all four exist or disable the room with a warning.

#### Public entry points
- `load_all() -> (ok: bool, err: str|None)` — load & validate both YAMLs; prepare room registry.
- `load_rooms() -> (rooms_dict, ok: bool, err: str|None)` — parse & validate `rooms.yaml`.
- `load_schedules() -> (sched_dict, ok: bool, err: str|None)` — parse & validate `schedules.yaml`.
- `write_schedules(new_sched: dict) -> (ok: bool, err: str|None)` — validate in-memory, **atomic write**, or keep last good on error.
- `reload_configs() -> (ok: bool, err: str|None)` — re-read from disk and re-validate; non-destructive on failure.


### `room_controller.py`
Responsible for individual rooms. Each room is a self-contained object that owns its temperature, TRV control, and call-for-heat logic; the app coordinates the rooms. A room, rather than a radiator, will call for heat, have a temperature, contain temperature sensors, TRVs, etc.

#### Responsibilities
- Model a **Room** as a first-class object with state: mode (`auto|manual|off|stale`), target, fused temperature, call-for-heat, and valve %.
- Resolve the **effective target** using precedence (off → manual → override/boost → schedule/default).
- Compute **call-for-heat** using per-room **asymmetric hysteresis** (see §State Model).
- Compute **valve percent** using **stepped bands** + **step hysteresis** and **rate limiting** (see §Smart TRV Control).
- **Multi-band jump optimization**: When transitioning across 2+ valve bands (e.g., 0→3 during boost), skip intermediate bands and jump directly. Prevents slow ramp-up (0→40%→70%→100% becomes direct 0→100%). Only apply step hysteresis for adjacent single-band transitions.
- Track per-room override/boost (remaining time comes from HA timer), and expose a concise **status** string.
- Publish room-derived entities (`sensor.pyheat_<room>_*`, `binary_sensor.pyheat_<room>_calling_for_heat`, `number.pyheat_<room>_valve_percent`).
- **State persistence**: Restore active overrides/boosts from timer state and persisted targets on initialization.

#### Public entry points
- `update_inputs(*, temp: float|None, mode: str, schedule_target: float|None, manual_setpoint: float|None)` — set the latest inputs from sensors/scheduler/UI.
- `apply_override(target: float, minutes: int)` — start/extend an absolute target override.
- `apply_boost(delta: float, minutes: int)` — start/extend a delta-based boost.
- `clear_override()` — cancel any active override/boost.
- `set_mode(mode: Literal["auto","manual","off"])` — change room mode.
- `compute(now: datetime) -> dict` — recompute `target`, `call_for_heat`, `valve_percent`, `state`, and `status`; returns a summary for the orchestrator.
- `get_status() -> dict` — read-only snapshot for `pyheat_status` (reason string, active flags, remaining override time).


### `constants.py`
Centralizes all fixed config and default parameters so other modules import them rather than hardcoding values.

#### Responsibilities
- Single source of truth for **defaults**, **limits**, and **tuning knobs** used across modules.
- Define namespaced constants (no hardcoded magic numbers elsewhere).
- **Read-only at runtime** (no mutation).

#### Public surface
- Constant groups:
  - `TIMEZONE`
  - `HOLIDAY_TARGET_C`  # °C used when holiday mode is on
  - `HYSTERESIS_DEFAULT = { on_delta_c: ..., off_delta_c: ... }`
  - `VALVE_BANDS_DEFAULT = { t_low: ..., t_mid: ..., t_max: ..., low_percent: ..., mid_percent: ..., max_percent: ..., step_hysteresis_c: ... }`
  - `VALVE_UPDATE_DEFAULT = { min_interval_s: ... }`
  - `SAFETY_DEFAULT = { min_on_s: ..., min_off_s: ..., min_open_percent: ..., feedback_timeout_s: ... }`
  - **TRV entity derivation (from `climate.<trv_base>`):**
    - `TRV_ENTITY_PATTERNS = {`
      - `"cmd_open":  "number.{trv_base}_valve_opening_degree",`
      - `"cmd_close": "number.{trv_base}_valve_closing_degree",`
      - `"fb_open":   "sensor.{trv_base}_valve_opening_degree_z2m",`
      - `"fb_close":  "sensor.{trv_base}_valve_closing_degree_z2m"`
    - `}`
    - `VALVE_PERCENT_INTEGER = True`  *(commands rounded to nearest 0–100 int)*
  - Numeric bounds: `TARGET_MIN_C`, `TARGET_MAX_C`, `PRECISION_ALLOWED = {0,1,2}`, `TIMEOUT_MIN_M = 1`


### `scheduler.py`
Deals with the schedule, overrides, and boosts.

#### Responsibilities
- Resolve the **scheduled target** for a room at a given local time (default target + active block).
- Apply **holiday mode** substitution of the base/default target when `holiday_on` is true, using `constants.HOLIDAY_TARGET_C`.
- Combine schedule with an optional **override/boost** to yield a resolved target (precedence with manual/off is handled in `room_controller.py`).
- Expose block metadata (for status strings) without owning any file I/O or HA wiring.
- React to in-memory schedule reloads provided by `config_loader.py` (no disk writes here).

#### Public entry points
- `get_scheduled_target(room_id: str, ts: datetime) -> float|None` — default or block target (holiday already applied).
- `with_override(base_target: float|None, *, kind: Literal["override","boost",None], target: float|None = None, delta: float|None = None) -> float|None` — apply override/boost to a base target.
- `current_block(room_id: str, ts: datetime) -> dict|None` — return the active block info (start, end, target) for status/debug.
- `reload(schedules: dict) -> None` — replace in-memory schedules after validation by `config_loader.py`.


### `sensors.py`
Ingests raw sensor values and turns them into a single value that is the room temperature, which is also published to HA as `sensor.pyheat_<room>_temperature`.  
Multiple sensors in a room are handled in different ways based on preferences in `rooms.yaml`. Temperature sensors are marked in `rooms.yaml` as either primary or fallback. `sensors.py` will average the primary sensors, but if no primary sensors are available it will average the fallback sensors. Responsible for marking sensors as stale (`timeout_m`) or unavailable. A sensor is stale if it has not provided a value in `timeout_m` minutes.

#### Responsibilities
- Maintain per-room temperature readings from one or more sensors (as defined in `rooms.yaml` with `primary`/`fallback` roles).
- Compute the **fused room temperature**: average all available **primary** sensors; if none available, average **fallback** sensors; if none available, mark room **stale**.
- Apply room `precision` when publishing `sensor.pyheat_<room>_temperature`.
- Track **staleness** per temperature sensor and per room using `timeout_m` (minutes since last update). Unavailable or stale temp sensors are excluded from the average; if none remain, the **room becomes stale**.
- On startup, accept last-known HA state values if present (otherwise treat as stale) and republish on recompute.
- No file I/O or HA wiring here; just fusion logic and status for the orchestrator.

#### Public entry points
- `update_sensor(entity_id: str, value: float, ts: datetime) -> None` — record a new reading for a specific sensor.
- `get_room_temp(room_id: str) -> tuple[float|None, bool]` — return `(fused_temp, is_stale_room)`.
- `is_sensor_stale(entity_id: str, now: datetime) -> bool` — helper used internally and for diagnostics.
- `get_room_sensor_status(room_id: str, now: datetime) -> list[dict]` — per-sensor availability/stale info for status/debug.
- `reload_rooms(rooms_cfg: dict) -> None` — refresh sensor mappings/roles/precision after `rooms.yaml` changes.


### `trv.py`
Controls smart TRVs on radiators. All TRVs are Sonoff TRVZB controlled via zigbee2mqtt (z2m).  
Instead of controlling the valve using its heating setpoint, it controls two values:

- `number.trv_<room>_valve_opening_degree`  
- `number.trv_<room>_valve_closing_degree`

Both are percentages. To set the valve to **P%** open: set `opening_degree = P` and `closing_degree = (100 - P)`
These entities are created by zigbee2mqtt and are the authoritative write targets for valve control.


Since setting these values is optimistic, sensors in HA check the response from z2m using MQTT sensors:
- `sensor.<trv_base>_valve_closing_degree_z2m`
- `sensor.<trv_base>_valve_opening_degree_z2m`

Once these agree exactly with the corresponding `number.trv_<room>_valve_...`, the valve is in the correct state.

#### Responsibilities
- Thin adapter for TRV control via HA: write commanded valve opening to
  `number.trv_<room>_valve_opening_degree` and `number.trv_<room>_valve_closing_degree`
  (set `opening = P`, `closing = 100 - P`).
- Read back **feedback** from MQTT sensors
  `sensor.<trv_base>_valve_opening_degree_z2m` and
  `sensor.<trv_base>_valve_closing_degree_z2m`.
- Report the **effective feedback opening %** and whether it **exactly equals** the commanded value
  (used by the TRV-open interlock).
- Be stateless with respect to policy: banding/rate limits are decided in `room_controller.py`;
  this module just sends commands and reads feedback.
- rooms.yaml provides a `climate.<trv_base>` per TRV. Pyheat derives the command/feedback entities from `trv_base` and never writes to the climate entity.

#### Sequential Command Execution
- Commands sent sequentially: opening degree → wait for feedback → closing degree → wait for feedback
- Retry logic: 10s retry interval, up to 6 max retries per command
- Feedback tolerance: ±5% deviation acceptable
- Prevents valve thrashing and inconsistent states during transitions

#### Anti-Thrashing Protection
Multiple layers prevent unnecessary TRV commands:
1. **Command in progress lock**: Prevents concurrent commands to same TRV
2. **Last commanded check**: Skip if target matches `last_commanded_percent`
3. **Entity value check**: Double-check current HA entity values before sending
4. **Sequential execution**: Wait for feedback confirmation before next command

#### Public entry points
- `set_valve_percent(room_id: str, percent: int) -> None` — issue the opening/closing degree commands with sequential execution and feedback confirmation.
- `get_feedback_percent(room_id: str) -> int|None` — return current feedback opening % (or `None` if no valid feedback is available or inconsistent - open% + close% != 100% ±5%).
- `matches_command(room_id: str) -> bool` — `True` iff feedback **exactly equals** the last commanded opening.
- All functions operate on derived entity IDs (number.*, sensor.*) computed from the room's trv.entity_id

### `boiler.py`
Responsible for boiler control with comprehensive state machine and safety interlocks. Supports simple on/off boiler control and OpenTherm boilers (OpenTherm currently stubbed).

#### State Machine
- **STATE_OFF**: Boiler idle, no demand
- **STATE_PENDING_ON**: Waiting for TRV feedback confirmation before turning on (max 90s timeout)
- **STATE_ON**: Boiler running, heating active
- **STATE_PENDING_OFF**: Off-delay period (30s) to handle brief demand interruptions
- **STATE_PUMP_OVERRUN**: Post-shutdown circulation (180s) to dissipate heat
- **STATE_INTERLOCK_BLOCKED**: Safety lockout due to insufficient valve opening
- **STATE_INTERLOCK_FAILED**: Permanent failure state requiring manual intervention

#### Responsibilities
- Abstract the boiler actuator:
  - **Binary mode:** drive `input_boolean.pyheat_boiler_actor` on/off.
  - **OpenTherm (stub):** accept a CH flow setpoint; no policy logic here.
- Enforce **anti short-cycling** (`min_on_s`, `min_off_s`): queue requested flips and apply only when timers allow.
- Honour **force-off**: if `pyheat_master_enable` is off, turn boiler **off immediately** and clear any queued ON.
- Apply the **TRV-open interlock** by requiring an `interlock_ok` flag from the orchestrator before turning ON.
- Track timestamps (`last_on_utc`, `last_off_utc`) and expose **short-cycle time remaining** for status.

#### Public entry points
- `request_heat(on: bool, *, interlock_ok: bool, master_enable: bool, now: datetime) -> dict`  
  Process a demand change with safety rules. Returns:
  `{ applied_on: bool, queued: bool, reason: "ok|short_cycle|interlock|disabled", short_cycle_remaining_s: int }`.
- `get_state() -> dict`  
  Snapshot: `{ is_on: bool, last_on_utc: str|None, last_off_utc: str|None, queued: "on|off|none", short_cycle_remaining_s: int }`.
- `set_opentherm_setpoint(flow_temp_c: float|None) -> None`  
  Set CH flow target when in OpenTherm mode (noop in binary mode).

### `status.py`

#### Responsibilities
- Compose the `sensor.pyheat_status` **state** and **attributes** from module snapshots
  (rooms, boiler, scheduler/holiday, any_call_for_heat).
- Include per-room **mode** and, when `mode = manual`, the **user_setpoint** in both attributes and the short reason.
- Keep formatting rules and short reason strings in one place (e.g., `at_target`, `boost(+2.0) 23m left`, `manual(20.0°C)`, `manual(stale_sensors)`, `stale_sensors`).
- No HA I/O: returns a payload; `core.py` publishes it.

#### Public entry points
- `build_status(*, mode: str, holiday: bool, rooms: list[dict], boiler: dict) -> tuple[str, dict]`  
  - Returns `(state_str, attributes_dict)`, e.g. `("heating (2 rooms)", { ... })`.  
  - Attributes **must** include a per-room summary with at least: `id`, `mode`, `temp`, `target`, and `user_setpoint` (only when `mode=manual`), plus the `reason` string.
- `short_reason_for_room(room: dict) -> str`  
  - Produce the per-room reason used in `sensor.pyheat_<room>_status`, incorporating `mode` and `user_setpoint` when manual and handling stale/override/boost cases.

---

## 4) Configuration

### Config Files

#### `config/schedules.yaml`
Defines each room’s heating targets over the week. It holds the default temperature for a room and a set of timed “blocks” per weekday that temporarily override that default.

**Rules and defaults**
- Times are `"HH:MM"` between 00:00 and 23:59 (no 24:00).
- Block edges: start is inclusive, end is exclusive (clean handoff at 08:00).
- A block with no end runs until midnight; 23:59 is treated as midnight.
- Gaps: outside any block, `default_target` is always active.
- Overlaps: overlapping blocks on the same day are invalid (fail load with one clear error).
- Ordering: blocks are sorted by start time on load (YAML order doesn’t matter).
- All keys `mon…sun` must be present. Use empty lists (`[]`) for no blocks.
- Reloads: on file change, validate then apply immediately; if invalid, keep last good config and expose one status message.
- Unknown rooms: if a schedule references a room not in `rooms.yaml`, log a warning (don’t fail).
- Same-day blocks only: end must be > start (no wrap to next day; no zero-length blocks).

**Structure**
```yaml
# schedules.yaml — structure
rooms:                         # array
  - id: <string>               # required; must match a room in rooms.yaml
    default_target: <float>    # required; °C used outside blocks
    week:                      # required; keys for mon..sun
      mon:                     # list of blocks; use [] for no blocks
        - start: "HH:MM"       # required; 00:00–23:59
          end: "HH:MM"         # optional; omit = until midnight
          target: <float>      # required; °C
      tue:
        - start: "HH:MM"
          end: "HH:MM"
          target: <float>
      wed: []
      thu: []
      fri: []
      sat: []
      sun: []

```
**Time & DST rules**
- All schedule times are interpreted in **Home Assistant’s configured timezone** (local wall clock).
- Evaluation happens on each 1-minute tick against local time.
- **DST spring forward:** if a scheduled boundary falls inside the skipped hour, it is applied at the first valid minute after the clock change.
- **DST fall back:** when an hour repeats, a boundary at that time triggers once at the first occurrence; the repeated hour does not cause a second trigger.
- Midnight rollover and weekday selection use local time. No cross-day blocks.

#### `config/rooms.yaml`
Defines each room, its sensors, its TRV, and per-room preferences.

- Each room must have a unique `id`, at least one temperature sensor, and exactly one `trv.entity_id`.
- `precision`: decimals for `sensor.pyheat_<room>_temperature`. Default: 1.
- `timeout_m`: minutes before a sensor is considered stale. Default: 180.
- `role`: `primary` or `fallback`. If any primary sensors are available, Pyheat averages primaries; otherwise it averages fallbacks.
- `entity_id` values are lowercase Home Assistant entity IDs.
- **TRV climate entity:** `trv.entity_id` **must be** a zigbee2mqtt-provided `climate.*` (e.g., `climate.living_trv`). Pyheat **does not** control this entity directly. It is used **only** to derive the valve control/feedback entities:
  - `number.<trv_base>_valve_opening_degree`
  - `number.<trv_base>_valve_closing_degree`
  - `sensor.<trv_base>_valve_opening_degree_z2m`
  - `sensor.<trv_base>_valve_closing_degree_z2m`  
  where `<trv_base>` is the part after `climate.` (e.g., `living_trv`). All four derived entities must exist; if any are missing, the room is disabled and a warning is logged.
- **Per-room optional tuning** (all have defaults in `constants.py`):
  - `hysteresis.on_delta_c`, `hysteresis.off_delta_c`
  - `valve_bands.t_low`, `t_mid`, `t_max`; `valve_bands.low_percent`, `mid_percent`, `max_percent`; `valve_bands.step_hysteresis_c`
  - `valve_update.min_interval_s`

**Example**
```yaml
rooms:
  - id: living
    name: Living Room
    precision: 1
    sensors:
      - entity_id: sensor.living_temp_main
        role: primary
        timeout_m: 180
      - entity_id: sensor.living_temp_backup
        role: fallback
    trv:
      entity_id: climate.living_trv   # used only to derive number./sensor. valve entities
    hysteresis:
      on_delta_c: 0.30
      off_delta_c: 0.10
    valve_bands:
      t_low: 0.30
      t_mid: 0.80
      t_max: 1.50
      low_percent: 40
      mid_percent: 70
      max_percent: 100
      step_hysteresis_c: 0.05
    valve_update:
      min_interval_s: 30

  - id: bedroom
    name: Bedroom
    sensors:
      - entity_id: sensor.bedroom_temp
        role: primary
        # timeout_m defaults to 180
    trv:
      entity_id: climate.bedroom_trv   # used only to derive number./sensor. valve entities
    # Optional per-room tuning omitted here; defaults from constants.py apply
```

### Validation & File I/O

#### File I/O Notes
- **Paths:** Build an absolute path under Home Assistant’s config directory with `hass.config.path("relative/path.yaml")`. This ensures it works inside the HA container.
- **Why executors:** File I/O blocks. In PyScript you should offload any blocking work using `await task.executor(func, *args)` so you don’t freeze triggers.
- **Read YAML (pattern):**
  1. In an async PyScript function, resolve the path with `hass.config.path(...)`.
  2. Use `await task.executor` to run a small function that opens the file and returns its text/bytes.
  3. Parse that string with a safe loader (PyYAML is bundled with HA) via `yaml.safe_load(...)`.
  4. Handle `None` (empty file) and parse errors; return a dict (or `{}`) either way.
- **Write YAML (pattern):**
  1. Convert your Python dict back to a string with `yaml.safe_dump(...)`.
  2. Use `await task.executor` to do the disk write so it’s off the event loop.
  3. Prefer an **atomic write**: write to a temp file in the same directory, then `os.replace(temp, final)`—also done inside the executor.
- **Concurrency:** If reads and writes might overlap, guard the critical section with a simple lock (e.g., a module-level `Lock`) that you also use inside the executor.
- **Permissions & existence:** Ensure the directory exists before writing (create it once, via executor). If the file doesn’t exist on first run, treat that as “empty YAML”.
- **No pip installs:** PyScript runs in HA’s environment; use only standard library + what HA already ships (PyYAML is available). If you need other libs, they must be in the container image, not installed at runtime.

**Validation rules & error handling**

**`rooms.yaml`**
- **Schema:** each room requires `id`, at least one `sensors[].entity_id` (with `role`), and exactly one `trv.entity_id`.
- **Duplicates:** duplicate room `id` → **fail validation (keep last good config) and notify**.
- **Unknown HA entities:** if any listed sensor/TRV entity IDs don’t exist, **warn and disable the room** (no heat) until fixed.
- **Roles:** schema-only check; no availability requirement for primaries at load (fallbacks handle absence).
- **Bounds:** enforce `precision ∈ {0,1,2}` and `timeout_m ≥ 1`.
- **IDs:** entity IDs must be lowercase HA IDs.

**`schedules.yaml`**
- **Schema:** each room entry requires `id`, `default_target`, and `week.mon..sun` keys (lists).
- **Targets:** enforce numeric °C in a sane range (e.g., **5–35**).
- **Times:** `"HH:MM"` 00:00–23:59; **start < end**; same-day only; no 24:00.
- **Overlaps:** overlapping blocks for a day → **fail** with one clear error.
- **Ordering:** blocks are sorted by start time on load (YAML order ignored).
- **Unknown rooms:** schedule references to rooms not in `rooms.yaml` → **warn** (do not fail).

**Writes & errors**
- **Atomic writes:** validate in-memory; if valid, write via temp file then `os.replace`; if invalid, **keep last good file** and return an error.
- **Service errors:** log clear messages; services return generic failure (no structured error codes).
---

## 5) Services & Entities (Public API)

### Entities Created by Pyheat
- `sensor.pyheat_<room>_temperature` (float): fused room temp from sensors.
- `sensor.pyheat_<room>_target` (float): resolved target after schedule/override/boost/holiday.
- `number.pyheat_<room>_valve_percent` (0–100): desired TRV opening from controller.
- `binary_sensor.pyheat_<room>_calling_for_heat` (on/off): room demand after deadband/hysteresis.
- `sensor.pyheat_<room>_state` (string): `off | manual | auto | stale`.
- `sensor.pyheat_<room>_status` (string): short reason (“at_target”, “boost(+2.0) 23m left”, “manual(20.0°C)”, “manual(stale_sensors)”, “stale_sensors”, etc).
- `binary_sensor.pyheat_any_call_for_heat` (on/off): OR of room demands (filtered by safety).

#### `pyheat_status`
`sensor.pyheat_status` shows a short human summary in state and more detail in attributes.  
**State:** short summary, e.g., `heating (2 rooms)`, `idle`, `disabled`, `error`  
**Attributes:**
- `mode`: `enabled`, `disabled`, `holiday`
- `any_call_for_heat`: true/false
- `active_rooms`: `['lounge', 'pete']`
- `room_calling_count`: int

### Helpers Defined in `configuration.yaml`

#### `input_boolean` Helpers
- `input_boolean.pyheat_master_enable` — If on, pyheat is enabled; otherwise disabled.
- `input_boolean.pyheat_holiday_mode` — Used to change to holiday mode.

#### `input_select` Helpers
- `input_select.pyheat_<room>_mode` options: `auto`, `manual`, `off`  
  - **auto:** managed by Pyheat via schedule/override/boost
  - **manual:** target = input_number.pyheat_<room>_manual_setpoint; overrides/boosts ignored.
  - **off:** never heat; valve 0%, no call-for-heat.
- `input_number.pyheat_<room>_manual_setpoint` — °C; range 5–35; step = room precision; unit °C.
  - Note: UI writes only; Pyheat reads these and remains sole actuator for TRVs/boiler.

#### Manually defined MQTT sensors (per TRV)
- `sensor.<trv_base>_valve_closing_degree_z2m` — percentage that the TRV valve is closed when the TRV is closed
- `sensor.<trv_base>_valve_opening_degree_z2m` — percentage that the TRV valve is open when the TRV is open
These are not auto-discovered and must be manually created

#### Timer Helpers
- `timer.pyheat_<room>_override` tracks how long remains on an override for `<room>`.
- Persistence: `timer.pyheat_<room>_override` is created with restore: true so it resumes across HA restarts. If a timer would have expired while HA was down, it won’t emit timer.finished on startup; Pyheat must recompute on startup and clear/continue overrides based on the timer’s state/remaining time.

### Services
> **All services trigger an immediate recompute.**

- `pyheat.override`  
  **Args:** `room: str`, `target: float`, `minutes: int`  
  **Behavior:** Start/extend a fixed target for `minutes`. **Ignored if room mode = `manual` or `off`.**

- `pyheat.boost`  
  **Args:** `room: str`, `delta: float`, `minutes: int`  
  **Behavior:** Raise the current resolved target by `delta` for `minutes`. **Ignored if room mode = `manual` or `off`.**

- `pyheat.cancel_override`  
  **Args:** `room: str`  
  **Behavior:** Clear any active override/boost for the room (return to schedule/manual). Safe no-op if none active.

- `pyheat.set_mode`  
  **Args:** `room: str`, `mode: "auto|manual|off"`  
  **Behavior:** Write `input_select.pyheat_<room>_mode`.

- `pyheat.set_default_target`  
  **Args:** `room: str`, `target: float`  
  **Behavior:** Update the room’s `default_target` in `schedules.yaml` and save.

- `pyheat.reload_config`  
  **Args:** *(none)*  
  **Behavior:** Re-load `rooms.yaml` and `schedules.yaml`; validate; apply if valid, otherwise keep last good and report error.

- `pyheat.replace_schedules`  
  **Args:** `schedule: dict` (full `schedules.yaml` contents)  
  **Behavior:** Validate; atomically replace `config/schedules.yaml` if valid, then reload. On validation failure, do nothing and return an error.

#### External entities (required, not created by Pyheat)
These must already exist in Home Assistant; Pyheat reads/writes them but does not create them.

- **Per-TRV (from zigbee2mqtt, derived from `climate.<trv_base>`):**
  - Commands: `number.<trv_base>_valve_opening_degree`, `number.<trv_base>_valve_closing_degree`
  - Feedback: `sensor.<trv_base>_valve_opening_degree_z2m`, `sensor.<trv_base>_valve_closing_degree_z2m`
  - Climate (UI/derivation only): `climate.<trv_base>`

- **Room temperature sensors (from your integrations):**
  - `sensor.<...>` listed in `rooms.yaml` (primary/fallback). Read-only inputs.


---

## 6) Control Logic Details

### Schedules
Schedules are stored in a single local YAML file (`config/schedules.yaml`) read by PyScript. Changes to the schedule are written back to the YAML and immediately picked up by Pyheat.

### Boiler Control
- Supports both binary and OpenTherm boilers.
- Binary boilers are simply on when heat is needed.
- OpenTherm boilers have temperature control - need more here.
- Pyheat evaluates room demand; if any room calls for heat, it adjusts the relevant TRV(s) as needed and turns the boiler on **only when** the TRV-open interlock confirms at least one valve is open (≥ `min_open_percent`).



#### Safety Features

**Anti short-cycling (app-level):**
- Enforce minimum durations `min_on_s` and `min_off_s` between boiler state changes.
- **Deferral:** If a state flip is requested inside an active window, **queue** the change and apply it when the window expires.
- **Force-off:** If `input_boolean.pyheat_master_enable` is `off`, force the boiler **off immediately** (ignore timers).
- **Pump overrun:** Handled by the boiler; Pyheat does not add extra post-run behavior.

**TRV-open interlock:**
- Boiler may turn **on** only if **at least one room** is calling for heat **and** its TRV feedback confirms an opening **exactly equal** to the commanded value and **≥ `min_open_percent`** **within** `feedback_timeout_s`.
- If no TRV meets this within `feedback_timeout_s`, treat all as **closed** and keep the boiler **off**.
- If TRV feedback sensors are **unavailable**, treat that TRV as **closed**.
- When a queued **ON** becomes eligible (anti-cycle window expires), it is applied **only if** the interlock passes at that moment; otherwise remain **off** and continue evaluating on subsequent recomputes.

**Defaults & tuning:**
- `min_on_s`, `min_off_s`, `min_open_percent`, and `feedback_timeout_s` are defined in `constants.py`.


### Smart TRV Control
Decides how open each room’s TRV valve should be (0–100%) based on how far the room is from its target temperature. Decisions made by `room_controller.py`; TRV hardware handled by `trv.py`.

- Uses stepped valve percentages based on how far a room is from its target (bands defined elsewhere).
- When a room is at or above target, the valve is closed (0%).
- A small deadband around the target avoids micro-adjustments (width configurable).
- Hysteresis is temperature-based to prevent flapping (thresholds configurable).
- Valve updates are event-driven (sensor/target/override changes) and rate-limited.
- If a room’s temperature becomes unavailable/stale, close the valve and don’t call for heat.

**Control style:** stepped bands with four levels (0% / low / mid / max).

- Error term: `e = target − temp`.
- **Bands (per-room configurable with defaults):**  
  Define three ascending error thresholds `t_low < t_mid < t_max` (°C).
  - `e < t_low` → valve = 0%
  - `t_low ≤ e < t_mid` → valve = low%
  - `t_mid ≤ e < t_max` → valve = mid%
  - `e ≥ t_max` → valve = max%
  Thresholds (`t_low`, `t_mid`, `t_max`) and outputs (`low%`, `mid%`, `max%`) have defaults in `constants.py` and may be overridden per room.

**Step hysteresis (band change damping):**
- A band change requires crossing the relevant threshold by at least `dh_c(room)` (°C) **in the direction of change**; otherwise remain in the current band.
- `dh_c(room)` has a default; per-room override allowed.

**Rate limiting:**
- Apply valve updates no more frequently than `min_interval_s(room)`; additional changes within that window are coalesced.
- Default in `constants.py`; per-room override allowed.

**Feedback / safety for “TRV open” checks:**
- Count a TRV as “open” **only when feedback exactly equals the commanded opening** (no tolerance).
- Use this rule wherever the global controller requires “at least one TRV open”.

### Per-Room Temperature Monitoring
A single YAML file (`config/rooms.yaml`) defines each room in the house, specifying which temperature sensors it contains, which TRV it contains, and user configuration options for this hardware. Rooms are first-class domain objects and drive decisions at the room level.

### Overrides
Pyheat allows the user to override the scheduled target temperature in two ways:

#### Override
A room is targeted and a new target temperature overrides the scheduled target temperature for a specified time. This is a service that takes target temperature (10-35°C) and time (minutes) as arguments. The target and expiry time are persisted across pyscript reloads using Home Assistant input_number entities and timer attributes.

#### Boost
Boost is a special case of override, which instead of taking an absolute target temperature, takes a delta (-10 to +10°C) as an argument, increasing the target temperature by that delta for the duration of the override. The computed target (schedule + delta) is persisted after first calculation.

### State Persistence Across Pyscript Reload

Pyheat implements comprehensive state persistence to maintain operation during pyscript reloads:

#### Override/Boost Persistence
- **Timer Tracking**: Active overrides/boosts tracked via `timer.pyheat_{room}_override` entities
- **Target Persistence**: Target temperature stored in `input_number.pyheat_{room}_override_target`
- **Restoration Logic**:
  1. On initialization, check if override timer is active
  2. If active, read persisted target from input_number
  3. Parse timer's `finishes_at` attribute to restore expiry datetime
  4. Only restore if target ≥ 10°C (values <10°C indicate cleared/invalid state)
- **Clearing**: When override expires or is cancelled, target set to 0 (clamped to 5°C minimum by entity definition)
- **Benefit**: User comfort maintained during pyscript updates; no manual re-application needed

#### Pump Overrun Persistence
- **Purpose**: Maintain valve positions during 180-second pump overrun safety period
- **Valve Position Storage**: Saved as JSON to `input_text.pyheat_pump_overrun_valves`
- **State Restoration**:
  1. On initialization, check if `timer.pyheat_boiler_pump_overrun_timer` is active
  2. If active, restore boiler state to `STATE_PUMP_OVERRUN`
  3. Parse JSON from input_text to restore `last_valve_positions` dict
  4. Continue pump overrun until timer completes
- **Clearing**: Persisted positions cleared when pump overrun completes normally
- **Safety**: Prevents premature valve closure if pyscript reloads mid-cycle, protecting boiler from running with closed valves

#### Implementation Details
- **Entity Minimums**: Override target entities use min=5°C (below service minimum) to detect edge cases
- **Service Validation**: Services enforce min=10°C for override, -10 to +10°C for boost delta
- **Boost Computation**: Delta stored initially, absolute target computed in `_resolve_target()` and persisted on first calculation
- **Timer Coordination**: HA timers persist independently; pyscript restoration logic synchronizes with timer state

---

## 7) Appendix

### Pyscript Notes
Pyscript documentation: https://hacs-pyscript.readthedocs.io/en/latest/reference.html

### Persistent State
Pyscript can persist state variables in the `pyscript.` domain; values and attributes are preserved across HASS restarts. To request persistence, call `state.persist` at startup, e.g.:

```python
state.persist('pyscript.last_light_on')

@state_trigger('binary_sensor.motion == "on"')
def turn_on_lights():
  light.turn_on('light.overhead')
  pyscript.last_light_on = "light.overhead"
```

With this in place, `state.persist()` will be called every time the script is parsed, ensuring the `pyscript.last_light_on` variable is preserved. If `state.persist` is not called on a particular variable before HASS stops, that variable will not be preserved on the next start.

### Language Limitations
Pyscript implements a Python interpreter in a fully-async manner to run safely in the main HASS event loop.

Areas where PyScript differs from Python:
- Pyscript-specific function names and state names that contain a period are treated as plain identifiers, not attributes. Assigning to `pyscript` or `state` can shadow built-ins.
- Since PyScript is async, it detects whether functions are real or async and calls them correctly; `async/await` are optional. However, `async def` in PyScript doesn’t behave like Python coroutines.
- All PyScript functions are async. Python modules expecting sync callbacks can’t call PyScript functions unless they support async.
- Special methods (e.g., `__eq__`) in classes created in PyScript will not work since they are async; use `@pyscript_compile` or native Python modules if needed.
- The `import` function in PyScript fails for certain complex packages; consider shims.
- PyScript and HASS primitives are not thread-safe; use `task.executor()` for regular Python code in threads.

Unsupported features:
- Generators and `yield`.
- `match-case`.
- Built-in functions that do I/O (e.g., `open`) are not supported; `print` only logs.
- Built-in decorators can’t be used inline like real Python decorators.

Workarounds include moving code to native Python modules and importing into PyScript or using `@pyscript_compile`.
