# Pyheat - Home Assistant Heating Control

A Home Assistant heating controller written in Pyscript that manages smart TRV control, boiler operation, per-room temperature monitoring, and flexible scheduling.

## Overview

Pyheat treats **rooms as first-class domain objects** - each room owns its sensors, TRV, desired target temperature, fused current temperature, and heating decisions. The system is event-driven, reacting to Home Assistant state changes, service calls, and time-based triggers.

### Key Features

- **Smart TRV Control**: Stepped valve control with hysteresis to prevent flapping
- **Flexible Scheduling**: Per-room weekly schedules with timed blocks
- **Temperature Sensor Fusion**: Primary/fallback sensor averaging with staleness detection
- **Override & Boost**: Temporary temperature adjustments (absolute or delta-based)
- **Multiple Room Modes**: Auto (schedule-based), Manual (user setpoint), Off
- **Boiler Integration**: Simple on/off control based on room demand
- **Holiday Mode**: Substitute schedule targets when away
- **REST API**: Full control via Home Assistant services

## Architecture

### Core Modules

- **`core.py`**: Central orchestrator coordinating all modules
- **`sensors.py`**: Temperature sensor fusion with staleness detection
- **`scheduler.py`**: Weekly schedule resolution with override/boost support
- **`room_controller.py`**: Per-room state machine with hysteresis and valve control
- **`trv.py`**: TRV adapter for Sonoff TRVZB via Zigbee2MQTT
- **`boiler.py`**: Boiler on/off control based on aggregated room demand
- **`status.py`**: Status composition for Home Assistant entities
- **`config_loader.py`**: YAML configuration management with atomic writes
- **`ha_triggers.py`**: Event trigger registration and debouncing
- **`ha_services.py`**: Home Assistant service registration

## Configuration

### Rooms Configuration (`config/rooms.yaml`)

Defines each room's sensors, TRV, and control parameters.

```yaml
rooms:
  - id: <room_id>
    name: "<Room Name>"
    precision: 1  # Temperature decimal places
    sensors:
      - entity_id: sensor.roomtemp_<room_id>
        role: primary  # primary or fallback
        timeout_m: 180  # Minutes before considered stale
    trv:
      entity_id: climate.trv_<room_id>  # Used to derive command entities
    hysteresis:
      on_delta_c: 0.40   # Start heating when target - temp >= this
      off_delta_c: 0.10  # Stop heating when target - temp <= this
    valve_bands:
      t_low: 0.30       # Error thresholds (°C below target)
      t_mid: 0.80
      t_max: 1.50
      low_percent: 35.0  # Valve opening percentages
      mid_percent: 65.0
      max_percent: 100.0
      step_hysteresis_c: 0.05  # Band change damping
    valve_update:
      min_interval_s: 30  # Minimum seconds between valve commands
```

**TRV Control Notes:**
- `climate.trv_<room_id>` is used **only** to derive the actual control entities
- Pyheat controls via `number.trv_<room_id>_valve_opening_degree` and `number.trv_<room_id>_valve_closing_degree`
- Feedback via `sensor.trv_<room_id>_valve_opening_degree_z2m` and `sensor.trv_<room_id>_valve_closing_degree_z2m`
- This bypasses the TRV's internal logic for precise control

### Schedules Configuration (`config/schedules.yaml`)

Defines per-room heating schedules with default targets and timed blocks.

```yaml
rooms:
  - id: <room_id>
    default_target: 19.5  # °C used outside scheduled blocks
    week:
      mon:
        - start: "07:00"
          end: "09:00"
          target: 21.0
        - start: "18:00"
          end: "22:00"
          target: 22.0
      tue:
        - start: "07:00"
          end: "09:00"
          target: 21.0
      wed: []  # No blocks = use default_target all day
      thu: []
      fri: []
      sat: []
      sun: []
```

**Schedule Rules:**
- Times are `HH:MM` in 24-hour format (00:00–23:59)
- Block start is inclusive, end is exclusive
- Blocks with no `end` run until midnight
- Gaps between blocks use `default_target`
- No overlapping blocks allowed on the same day
- All weekdays (mon–sun) must be present (use `[]` for no blocks)

## Home Assistant Entities

### Created by Pyheat

**Per-Room Entities:**
- `sensor.pyheat_<room>_temperature` - Fused temperature from sensors
- `sensor.pyheat_<room>_target` - Resolved target (schedule + overrides)
- `sensor.pyheat_<room>_status` - Short status string (e.g., "heating", "boost(+2.0) 15m")
- `sensor.pyheat_<room>_state` - Room state (auto/manual/off/stale)
- `binary_sensor.pyheat_<room>_calling_for_heat` - Heat demand (on/off)
- `number.pyheat_<room>_valve_percent` - Commanded valve opening (0-100%)

**Global Entities:**
- `sensor.pyheat_status` - System status with room summary
- `binary_sensor.pyheat_any_call_for_heat` - Any room calling for heat

### Required External Entities

**Per-Room Helpers (create in `configuration.yaml`):**
- `input_select.pyheat_<room>_mode` - Options: Auto, Manual, Off
- `input_number.pyheat_<room>_manual_setpoint` - Manual mode target (°C)
- `timer.pyheat_<room>_override` - Override/boost countdown (restore: true)

**Global Helpers:**
- `input_boolean.pyheat_master_enable` - System enable/disable
- `input_boolean.pyheat_holiday_mode` - Holiday mode toggle
- `input_boolean.pyheat_boiler_actor` - Boiler control (dummy implementation)

**TRV Entities (from Zigbee2MQTT):**
- `climate.trv_<room>` - TRV climate entity (for derivation only)
- `number.trv_<room>_valve_opening_degree` - Command: valve open %
- `number.trv_<room>_valve_closing_degree` - Command: valve close %
- `sensor.trv_<room>_valve_opening_degree_z2m` - Feedback: actual open %
- `sensor.trv_<room>_valve_closing_degree_z2m` - Feedback: actual close %

## Services

All services are in the `pyheat` domain and trigger immediate recomputation.

**Service Responses:** Some services support the `?return_response=true` parameter (HA 2023.7+) to retrieve data or diagnostic information. These services are documented with response examples below.

### `pyheat.override`

Set an absolute temperature override for a room.

**Arguments:**
- `room` (string, required): Room ID
- `target` (float, required): Target temperature in °C
- `minutes` (int, required): Duration in minutes

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/override \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "living_room", "target": 22.0, "minutes": 60}'
```

**Notes:**
- Ignored if room mode is Manual or Off
- Overrides schedule target
- Can be extended by calling again
- Timer persists across HA restarts

### `pyheat.boost`

Apply a delta-based temperature boost.

**Arguments:**
- `room` (string, required): Room ID
- `delta` (float, required): Temperature increase in °C (can be negative)
- `minutes` (int, required): Duration in minutes

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/boost \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "bedroom", "delta": 2.0, "minutes": 45}'
```

**Notes:**
- Adds delta to current scheduled/default target
- Ignored if room mode is Manual or Off
- Useful for temporary comfort without knowing exact target

### `pyheat.cancel_override`

Cancel active override or boost for a room.

**Arguments:**
- `room` (string, required): Room ID

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/cancel_override \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "living_room"}'
```

### `pyheat.set_mode`

Change a room's operating mode.

**Arguments:**
- `room` (string, required): Room ID
- `mode` (string, required): One of: `auto`, `manual`, `off`

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/set_mode \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "bedroom", "mode": "manual"}'
```

**Modes:**
- `auto`: Follow schedule with overrides/boosts
- `manual`: Use manual setpoint from `input_number.pyheat_<room>_manual_setpoint`
- `off`: No heating, valve closed

### `pyheat.set_default_target`

Update a room's default target temperature in the schedule.

**Arguments:**
- `room` (string, required): Room ID
- `target` (float, required): New default target in °C

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/set_default_target \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "living_room", "target": 20.0}'
```

**Notes:**
- Writes to `config/schedules.yaml`
- Does NOT modify scheduled blocks
- Reloads scheduler after update
- File written with 644 permissions (world-readable)

### `pyheat.reload_config`

Reload all configurations from disk.

**Arguments:** None

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/reload_config \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

**With response:**
```bash
curl -X POST "https://YOUR_HA_URL/api/services/pyheat/reload_config?return_response=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

Returns:
```json
{
  "service_response": {
    "room_count": 3,
    "schedule_count": 3
  }
}
```

**Notes:**
- Re-reads `rooms.yaml` and `schedules.yaml`
- Validates before applying
- Keeps last good config on validation failure
- Reloads all modules (sensors, scheduler, rooms, TRV)
- Optionally returns reload statistics with `?return_response=true`

### `pyheat.get_schedules`

Get the current schedule configuration.

**Arguments:** None

**Example:**
```bash
curl -X POST "https://YOUR_HA_URL/api/services/pyheat/get_schedules?return_response=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

Returns:
```json
{
  "service_response": {
    "rooms": [
      {
        "id": "living_room",
        "default_target": 20.0,
        "week": {
          "mon": [{"start": "07:00", "end": "09:00", "target": 21.0}],
          "tue": [],
          ...
        }
      }
    ]
  }
}
```

**Notes:**
- Returns current `schedules.yaml` contents
- MUST use `?return_response=true` parameter
- Use before modifying schedules with `replace_schedules`
- Does NOT reload from disk (returns in-memory version)

### `pyheat.get_rooms`

Get the current rooms configuration.

**Arguments:** None

**Example:**
```bash
curl -X POST "https://YOUR_HA_URL/api/services/pyheat/get_rooms?return_response=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

Returns:
```json
{
  "service_response": {
    "rooms": [
      {
        "id": "living_room",
        "name": "Living Room",
        "precision": 1,
        "sensors": [
          {
            "entity_id": "sensor.living_room_temperature",
            "role": "primary",
            "timeout_m": 180
          }
        ],
        "trv": {
          "entity_id": "climate.trv_living_room"
        },
        "hysteresis": {
          "on_delta_c": 0.4,
          "off_delta_c": 0.1
        },
        "valve_bands": {
          "t_low": 0.3,
          "t_mid": 0.8,
          "t_max": 1.5,
          "low_percent": 35.0,
          "mid_percent": 65.0,
          "max_percent": 100.0,
          "step_hysteresis_c": 0.05
        },
        "valve_update": {
          "min_interval_s": 30
        }
      }
    ]
  }
}
```

**Notes:**
- Returns current `rooms.yaml` contents
- MUST use `?return_response=true` parameter
- Shows full room configuration including sensors, TRVs, and tuning parameters
- Does NOT reload from disk (returns in-memory version)

### `pyheat.replace_schedules`

Atomically replace the entire schedules configuration.

**Arguments:**
- `schedule` (dict, required): Complete schedules.yaml structure

**Example:**
```bash
curl -X POST https://YOUR_HA_URL/api/services/pyheat/replace_schedules \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
  "schedule": {
    "rooms": [
      {
        "id": "living_room",
        "default_target": 19.5,
        "week": {
          "mon": [
            {"start": "07:00", "end": "09:00", "target": 21.0},
            {"start": "18:00", "end": "22:00", "target": 22.0}
          ],
          "tue": [],
          "wed": [],
          "thu": [],
          "fri": [],
          "sat": [],
          "sun": []
        }
      }
    ]
  }
}'
```

**Notes:**
- Validates before writing
- Atomic write (temp file → replace)
- Reloads scheduler on success
- Returns error if validation fails
- File written with 644 permissions

## Reading Configuration via REST API

### Get Current Schedule

Read the current schedule from the status entity attributes:

```bash
curl -s https://YOUR_HA_URL/api/states/sensor.pyheat_<room_id>_status \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.attributes'
```

### Get All Room States

```bash
curl -s https://YOUR_HA_URL/api/states \
  -H "Authorization: Bearer YOUR_TOKEN" | \
  jq '.[] | select(.entity_id | startswith("sensor.pyheat_"))'
```

### Get Room Target Temperature

```bash
curl -s https://YOUR_HA_URL/api/states/sensor.pyheat_<room_id>_target \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.state'
```

### Get Room Temperature

```bash
curl -s https://YOUR_HA_URL/api/states/sensor.pyheat_<room_id>_temperature \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.state'
```

### Get Global Status

```bash
curl -s https://YOUR_HA_URL/api/states/sensor.pyheat_status \
  -H "Authorization: Bearer YOUR_TOKEN" | \
  jq '{state: .state, mode: .attributes.mode, active_rooms: .attributes.active_rooms}'
```

## Control Logic

### Temperature Target Resolution

Precedence (highest wins):
1. Room Off → No heating, valve closed
2. Manual Mode → `input_number.pyheat_<room>_manual_setpoint`
3. Override → Absolute target from `pyheat.override`
4. Boost → Schedule target + delta from `pyheat.boost`
5. Schedule Block → Current time block target
6. Default Target → `default_target` from schedules.yaml

Holiday mode substitutes the schedule/default target with a lower value but still allows overrides/boosts.

### Call for Heat (Asymmetric Hysteresis)

Let `e = target - temp` (°C; positive means below target).

- **Start heating** when `e ≥ on_delta_c` (turn-on threshold)
- **Stop heating** when `e ≤ off_delta_c` (turn-off threshold)
- If `off_delta_c < e < on_delta_c`, keep previous state (deadband prevents flapping)
- Requires `on_delta_c ≥ off_delta_c`

**Defaults:**
- `on_delta_c`: 0.40°C
- `off_delta_c`: 0.10°C

### Valve Control (Stepped Bands)

Valve opening percentage based on error `e = target - temp`:

| Error Range | Valve Opening |
|-------------|---------------|
| `e < t_low` | 0% (closed) |
| `t_low ≤ e < t_mid` | low_percent (35%) |
| `t_mid ≤ e < t_max` | mid_percent (65%) |
| `e ≥ t_max` | max_percent (100%) |

**Step Hysteresis:**
- Band changes require crossing threshold by `step_hysteresis_c` (default 0.05°C)
- Prevents rapid band switching

**Rate Limiting:**
- Valve commands no more than once per `min_interval_s` (default 30s)
- Protects TRV hardware from excessive commands

### Boiler Control

**Current Implementation (Dummy):**
- Boiler ON when any room calls for heat
- Boiler OFF when no rooms call for heat
- Uses `input_boolean.pyheat_boiler_actor`

**Planned Enhancements:**
- Anti short-cycling (minimum on/off times)
- TRV-open interlock (verify at least one valve open before starting)
- OpenTherm support with flow temperature control

## Status Strings

Room status entity shows concise heating state:

- `"at_target"` - Temperature within deadband
- `"heating"` - Calling for heat
- `"cooling"` - Above target, not calling for heat
- `"override(22.0) 15m"` - Override active with remaining time
- `"boost(+2.0) 30m"` - Boost active with delta and remaining time
- `"manual(20.5°C)"` - Manual mode with setpoint
- `"manual(stale_sensors)"` - Manual mode but no valid temperature
- `"stale_sensors"` - All sensors unavailable
- `"off"` - Room mode set to off

## Debugging

### Check Pyscript Logs

```bash
tail -f /opt/appdata/hass/homeassistant/home-assistant.log | grep pyheat
```

### Reload Pyscript Module

```bash
touch /opt/appdata/hass/homeassistant/pyscript/apps/pyheat/__init__.py
```

This triggers automatic reload within seconds.

### Check TRV Commands

```bash
# Check commanded valve position (example for "bedroom" room)
curl -s https://YOUR_HA_URL/api/states/number.trv_bedroom_valve_opening_degree \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.state'

# Check feedback (actual position)
curl -s https://YOUR_HA_URL/api/states/sensor.trv_bedroom_valve_opening_degree_z2m \
  -H "Authorization: Bearer YOUR_TOKEN" | jq '.state'
```

### Verify Schedule Loading

Check logs after reload for:
```
ScheduleManager: room <room> loaded with default=<temp>°C, <n> block(s)
```

### Monitor Recompute Triggers

Recomputes happen on:
- State changes (sensor readings, mode changes, manual setpoints)
- 1-minute cron tick
- Service calls
- Timer events (override/boost finish)
- System startup

## Implementation Notes

### File Permissions

Configuration files written by services are owned by `root:root` (because Home Assistant runs as root) with 644 permissions (world-readable). This allows:
- Services to write atomically
- User to read files
- Git to track changes
- Manual edits require sudo (by design for service-managed files)

### Atomic Writes

Configuration updates use atomic write pattern:
1. Write to temporary file in same directory
2. Set permissions to 644
3. `os.replace()` temp file over target
4. No partial writes or corruption on failure

### Event-Driven Execution

No busy loops - system runs only on:
- HA state change triggers
- Time-based cron triggers (1-minute)
- Service call triggers
- Debounced to coalesce rapid changes

### Sensor Fusion

Multiple sensors per room:
1. Average all available **primary** sensors
2. If no primaries available, average **fallback** sensors  
3. If none available, mark room **stale**
4. Apply room `precision` when publishing

### TRV Control Method

Instead of using climate entity setpoints, Pyheat directly controls valve opening/closing degrees. This ensures precise control regardless of the TRV's internal logic:

```
To set valve to P% open:
  number.trv_<room>_valve_opening_degree = P
  number.trv_<room>_valve_closing_degree = 100 - P
```

Feedback confirms actual position via Zigbee2MQTT MQTT sensors.

## Project Status

**Version:** 1.0 (Fully Operational)

**Completed:**
- ✅ All core modules (sensors, scheduler, room_controller, trv, boiler, status)
- ✅ Full orchestrator with debouncing and entity publishing
- ✅ All 7 services tested and working
- ✅ TRV control with physical confirmation (audible valve changes)
- ✅ Boiler integration with dummy control
- ✅ Temperature sensor fusion with staleness detection
- ✅ Schedule management with atomic writes
- ✅ Override/boost with timer persistence
- ✅ REST API access

**Future Enhancements:**
- Advanced boiler safety (anti short-cycling, TRV interlock)
- OpenTherm integration
- Multi-room testing
- Edge case hardening (sensor failures, network issues)
- Performance monitoring

## Specification

Full architectural specification: [`docs/pyheat-spec.md`](docs/pyheat-spec.md)

Implementation tracking: [`docs/todo.md`](docs/todo.md)
