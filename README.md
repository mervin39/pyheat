# PyHeat - AppDaemon Heating Controller

Home heating control system for Home Assistant using AppDaemon.

## Overview

PyHeat provides multi-room heating control with:
- **Per-room temperature management** with individual schedules
- **Smart TRV control** via zigbee2mqtt (TRVZB devices)
- **Boiler management** with safety interlocks and anti-cycling
- **Short-cycling protection** via return temperature monitoring and setpoint manipulation
- **Load sharing** - intelligent multi-room heating to prevent boiler short-cycling
- **Sensor fusion** with staleness detection and optional EMA smoothing
- **Multiple control modes**: Auto (scheduled), Manual, and Off per room
- **Override** functionality with flexible parameters (absolute/delta temp, duration/end time)
- **Holiday mode** for energy savings

## Architecture

### Key Components

- **app.py** - Main AppDaemon application orchestration
- **boiler_controller.py** - 6-state FSM boiler control with safety interlocks
- **cycling_protection.py** - Automatic short-cycling prevention via return temperature monitoring
- **load_sharing_manager.py** - Intelligent load balancing using schedule-aware pre-warming
- **room_controller.py** - Per-room heating logic and target resolution
- **trv_controller.py** - TRV valve command and setpoint locking
- **valve_coordinator.py** - Single authority for valve commands with priority system
- **sensor_manager.py** - Temperature sensor fusion and staleness detection
- **scheduler.py** - Schedule parsing and time-based target calculation
- **service_handler.py** - Home Assistant service handlers for programmatic control
- **status_publisher.py** - Entity creation and status publication
- **config_loader.py** - YAML configuration validation and loading
- **constants.py** - System-wide configuration defaults
- **config/** - YAML configuration files for rooms, schedules, and boiler
- **ha_yaml/** - Home Assistant helper entity definitions

### Heating Logic

1. **Sensor Fusion**: Averages multiple temperature sensors per room with primary/fallback roles and staleness detection. Optional EMA smoothing reduces display noise for rooms with sensors in different locations.
2. **Target Resolution**: Precedence: Off → Manual → Override → Schedule → Default
3. **Hysteresis**: Asymmetric deadband (on_delta: 0.30°C, off_delta: 0.10°C) prevents oscillation; bypassed on target changes for immediate override response
4. **Valve Control**: 3 stepped heating bands (Band 1: 40%, Band 2: 70%, Band Max: 100%) based on temperature error with hysteresis
5. **Load Sharing**: Intelligently opens additional room valves when primary calling rooms have insufficient capacity to prevent boiler short-cycling. Uses three-tier cascade: schedule-aware pre-warming → extended lookahead → fallback priority list
6. **TRV Setpoint Locking**: All TRVs locked to 35°C with immediate correction via state listener
7. **Boiler Control**: Full 6-state FSM with anti-cycling timers, TRV feedback validation, and pump overrun
8. **Short-Cycling Protection**: Monitors return temperature on flame OFF events; triggers cooldown when efficiency degrades (return temp ≥ setpoint - 10°C); uses setpoint manipulation to enforce cooldown; recovers when return temp drops below dynamic threshold

## Installation

### Prerequisites

- Home Assistant with AppDaemon add-on or standalone installation
- MQTT broker (if using MQTT sensors)
- Zigbee2MQTT for TRV control
- Temperature sensors for each room

### Setup Steps

1. **Clone or copy this repository** to your AppDaemon apps directory:
   ```bash
   cd /opt/appdata/appdaemon/conf/apps/
   git clone <repo-url> pyheat
   ```

2. **Configure rooms and schedules**:
   Edit `config/rooms.yaml` and `config/schedules.yaml` to match your setup.

3. **Install Home Assistant entities**:
   
   Copy the PyHeat package to your Home Assistant configuration:
   
   ```bash
   # From your Home Assistant config directory
   cp /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_package.yaml packages/pyheat_package.yaml
   ```
   
   Then ensure your `configuration.yaml` has packages enabled:
   
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

4. **Register the app** in `apps.yaml`:
   ```yaml
   pyheat:
     module: pyheat.app
     class: PyHeat
     log: main_log
   ```

5. **Restart AppDaemon** to load the app.

## Configuration

### Rooms Configuration (`config/rooms.yaml`)

Each room requires:
- **id**: Unique identifier (used in entity names)
- **name**: Display name
- **sensors**: Array of temperature sensors with roles (primary/fallback), timeouts, and optional temperature_attribute
  - `entity_id`: Home Assistant entity ID
  - `role`: `primary` or `fallback`
  - `timeout_m`: Minutes before sensor is considered stale
  - `temperature_attribute`: (optional) Read from attribute instead of state (e.g., `current_temperature` for climate entities)
- **trv**: TRV climate entity
- **hysteresis**: Optional override for on/off delta temperatures
- **valve_bands**: Optional override for valve percentage bands
- **valve_update**: Optional min_interval_s for rate limiting

Example:
```yaml
rooms:
  - id: pete
    name: "Pete's Room"
    precision: 1
    sensors:
      - entity_id: sensor.roomtemp_pete
        role: primary
        timeout_m: 180
      - entity_id: climate.trv_pete  # Use TRV's internal sensor as fallback
        role: fallback
        timeout_m: 180
        temperature_attribute: current_temperature
    trv:
      entity_id: climate.trv_pete
    hysteresis:
      on_delta_c: 0.4
      off_delta_c: 0.1
```

### Schedules Configuration (`config/schedules.yaml`)

Define weekly schedules per room:
```yaml
rooms:
  - id: pete
    default_target: 14.0
    week:
      mon:
        - start: "06:30"
          end: "07:00"
          target: 17.0
        - start: "19:00"
          end: "21:00"
          target: 18.0
      # ... other days
```

## Usage

### Control Modes

Each room has three modes:
- **Auto**: Follows the configured schedule
- **Manual**: Uses the manual setpoint (ignores schedule)
- **Off**: No heating, TRV closes

Set via `input_select.pyheat_{room}_mode`

### Manual Control

When in Manual mode, set temperature via:
`input_number.pyheat_{room}_manual_setpoint`

### Override

Temporarily override the schedule with flexible parameters:
- **Temperature mode**: Absolute target OR relative delta
- **Duration mode**: Minutes OR end time (HH:MM)

Uses entities:
- `input_number.pyheat_{room}_override_target` - Stores calculated absolute target
- `timer.pyheat_{room}_override` - Controls duration

Call via service `appdaemon.pyheat_override` with parameters:
```yaml
# Absolute target for 2 hours
service: appdaemon.pyheat_override
data:
  room: lounge
  target: 21.0
  minutes: 120

# Delta from schedule until 22:30
service: appdaemon.pyheat_override
data:
  room: lounge
  delta: 2.0
  end_time: "22:30"
```

When timer expires, room returns to scheduled target.

### Load Sharing

**Intelligent load balancing** to prevent boiler short-cycling when primary calling rooms have insufficient radiator capacity.

**How it works:**
1. Detects when calling rooms cannot dissipate boiler output (low capacity + cycling evidence)
2. Opens additional room valves using three-tier cascading strategy:
   - **Tier 1**: Rooms with schedules starting soon (schedule-aware pre-warming)
   - **Tier 2**: Rooms with schedules in extended window (2× lookahead)
   - **Tier 3**: Fallback priority list for off-schedule periods
3. Persists until calling pattern changes (not arbitrary timers)

**Control:**
- Master switch: `input_boolean.pyheat_load_sharing_enable`
- Status: `sensor.pyheat_load_sharing_status` (state, active rooms, reason)
- Per-room status visible in room sensor attributes

**Configuration:**
```yaml
# rooms.yaml - optional per-room tuning
rooms:
  - id: lounge
    load_sharing:
      schedule_lookahead_m: 60    # Check schedules within 60 min (default)
      fallback_priority: 1         # Lower = higher priority for fallback

# boiler.yaml - system thresholds
boiler:
  load_sharing:
    min_calling_capacity_w: 3500  # Activation threshold
    target_capacity_w: 4000       # Target to reach
```

See [docs/load_sharing_proposal.md](docs/load_sharing_proposal.md) for complete design details.

### Holiday Mode

Enable `input_boolean.pyheat_holiday_mode` to set all rooms to a low temperature (12°C default).

### Master Enable/Disable

Toggle `input_boolean.pyheat_master_enable` to turn entire system on/off.

## Monitoring

### Status Entity

`sensor.pyheat_status` provides real-time system status:
- **State**: "idle" or "heating (N rooms)"
- **Attributes**:
  - Per-room details (mode, temp, target, call_for_heat)
  - Boiler state and reason
  - Total valve percentage
  - Last recompute timestamp

### Logs

AppDaemon logs are available in the AppDaemon log directory (usually `/conf/logs/`).

The app logs:
- Room mode/setpoint changes
- TRV commands sent
- Boiler state changes
- Configuration loading
- Errors and warnings

## REST API Reference

PyHeat exposes a REST API for external applications like pyheat-web. All endpoints are available at `http://<appdaemon-host>:5050/api/appdaemon/<endpoint>`.

### Common Patterns

**Request Format:**
- Method: POST (all endpoints)
- Content-Type: `application/json`
- Body: JSON object with parameters

**Response Format:**
```json
{
  "success": true,  // or false on error
  "error": "error message"  // only present if success=false
}
```

**Error Codes:**
- `200 OK` - Success
- `400 Bad Request` - Invalid parameters or validation error
- `500 Internal Server Error` - Server-side error

### Control Endpoints

#### `pyheat_override` - Set Temperature Override

Set a temporary temperature target for a room with flexible parameters.

**Parameters:**
- `room` (string, required) - Room ID
- Temperature (one required):
  - `target` (float) - Absolute temperature in °C (mutually exclusive with delta)
  - `delta` (float) - Temperature adjustment from current schedule (mutually exclusive with target)
- Duration (one required):
  - `minutes` (integer) - Duration in minutes (mutually exclusive with end_time)
  - `end_time` (string) - End time in HH:MM format (mutually exclusive with minutes)

**Examples:**
```bash
# Set absolute target for 120 minutes
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_override \
  -H "Content-Type: application/json" \
  -d '{"room": "lounge", "target": 21.5, "minutes": 120}'

# Adjust +2°C from schedule until 22:30
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_override \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "delta": 2.0, "end_time": "22:30"}'

# Set target until specific time
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_override \
  -H "Content-Type: application/json" \
  -d '{"room": "abby", "target": 20.0, "end_time": "18:00"}'
```

**Response:**
```json
{"success": true}
```

---

#### `pyheat_cancel_override` - Cancel Active Override

Cancel any active override for a room, returning it to scheduled/manual control.

**Parameters:**
- `room` (string, required) - Room ID

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_cancel_override \
  -H "Content-Type: application/json" \
  -d '{"room": "lounge"}'
```

**Response:**
```json
{"success": true}
```

---

#### `pyheat_set_mode` - Set Room Operating Mode

Change a room's operating mode between auto (scheduled), manual, or off.

**Parameters:**
- `room` (string, required) - Room ID
- `mode` (string, required) - Operating mode: `"auto"`, `"manual"`, or `"off"`
- `manual_setpoint` (float, optional) - Target temperature for manual mode (defaults to 20.0°C)

**Examples:**
```bash
# Switch to auto mode (uses schedule)
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_set_mode \
  -H "Content-Type: application/json" \
  -d '{"room": "lounge", "mode": "auto"}'

# Switch to manual mode with specific setpoint
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_set_mode \
  -H "Content-Type: application/json" \
  -d '{"room": "office", "mode": "manual", "manual_setpoint": 22.5}'

# Turn off heating for room
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_set_mode \
  -H "Content-Type: application/json" \
  -d '{"room": "guest", "mode": "off"}'
```

**Response:**
```json
{"success": true}
```

---

#### `pyheat_set_default_target` - Update Default Temperature

Update a room's default target temperature in schedules.yaml.

**Parameters:**
- `room` (string, required) - Room ID
- `target` (float, required) - New default temperature in °C

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_set_default_target \
  -H "Content-Type: application/json" \
  -d '{"room": "lounge", "target": 19.5}'
```

**Response:**
```json
{"success": true}
```

---

### Status Endpoints

#### `pyheat_get_status` - Get Complete System Status

Get comprehensive status for all rooms and system state. This is the primary endpoint for monitoring.

**Parameters:** None (empty object)

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_get_status \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{
  "rooms": [
    {
      "id": "lounge",
      "name": "Lounge",
      "temp": 19.8,
      "target": 21.0,
      "mode": "auto",
      "calling_for_heat": true,
      "valve_percent": 70,
      "is_stale": false,
      "status_text": "Auto: 21.0° until 22:00 on Mon (19.5°)",
      "formatted_status": "Auto: 21.0° until 22:00 on Mon (19.5°)",
      "manual_setpoint": null,
      "valve_feedback_consistent": true,
      "override_end_time": null,
      "override_remaining_minutes": null,
      "override_target": null,
      "scheduled_temp": 19.5
    },
    {
      "id": "pete",
      "name": "Pete's Room",
      "temp": 18.2,
      "target": 20.5,
      "mode": "auto",
      "calling_for_heat": true,
      "valve_percent": 100,
      "is_stale": false,
      "status_text": "Override: 20.5° (+2.0°)",
      "formatted_status": "Override: 20.5° (+2.0°)",
      "manual_setpoint": null,
      "valve_feedback_consistent": true,
      "override_end_time": "2025-11-13T22:30:00+00:00",
      "override_remaining_minutes": 47,
      "override_target": 20.5,
      "scheduled_temp": 18.5
    }
  ],
  "system": {
    "master_enabled": true,
    "holiday_mode": false,
    "any_call_for_heat": true,
    "boiler_state": "on",
    "boiler_actual_state": "on",
    "last_recompute": "2025-11-13T21:43:15+00:00",
    "boiler_off_delay_end_time": null,
    "boiler_min_on_end_time": "2025-11-13T21:48:00+00:00",
    "boiler_min_off_end_time": null,
    "boiler_pump_overrun_end_time": null
  }
}
```

**Field Descriptions:**

*Room Fields:*
- `id` - Room identifier (used in API calls)
- `name` - Display name
- `temp` - Current temperature (°C) or null if stale
- `target` - Current target temperature (°C) or null if off
- `mode` - Operating mode: "auto", "manual", or "off"
- `calling_for_heat` - Whether room is actively demanding heat
- `valve_percent` - TRV valve opening (0-100%)
- `is_stale` - Whether temperature sensors are outdated
- `status_text` - Human-readable status summary
- `formatted_status` - Formatted status with schedule context
- `manual_setpoint` - Manual mode setpoint (°C) or null
- `valve_feedback_consistent` - Whether commanded valve matches actual
- `override_end_time` - ISO 8601 timestamp when override expires (null if no override)
- `override_remaining_minutes` - Minutes remaining on override (null if no override)
- `override_target` - Override target temperature (null if no override)
- `scheduled_temp` - Temperature from schedule (before any override)

*System Fields:*
- `master_enabled` - Whether PyHeat heating control is enabled
- `holiday_mode` - Whether holiday mode is active
- `any_call_for_heat` - Whether any room is calling for heat
- `boiler_state` - Boiler state machine state: "off", "pending_on", "on", "pending_off", "pump_overrun", "interlock_blocked"
- `boiler_actual_state` - Actual boiler status: "on" or "off"
- `last_recompute` - ISO 8601 timestamp of last system update
- `boiler_*_end_time` - ISO 8601 timestamps for active boiler timers (null if inactive)

---

#### `pyheat_get_history` - Get Room Temperature History

Get historical temperature, setpoint, and heating activity data for a room.

**Parameters:**
- `room` (string, required) - Room ID
- `period` (string, required) - Time period: `"today"`, `"yesterday"`, or `"recent_Nh"` (e.g., "recent_4h")

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_get_history \
  -H "Content-Type: application/json" \
  -d '{"room": "lounge", "period": "today"}'
```

**Response:**
```json
{
  "temperature": [
    {"time": "2025-11-13T00:00:00+00:00", "value": 18.5},
    {"time": "2025-11-13T00:05:00+00:00", "value": 18.6},
    ...
  ],
  "setpoint": [
    {"time": "2025-11-13T00:00:00+00:00", "value": 19.0},
    {"time": "2025-11-13T06:30:00+00:00", "value": 21.0},
    ...
  ],
  "calling_for_heat": [
    ["2025-11-13T06:30:00+00:00", "2025-11-13T08:15:00+00:00"],
    ["2025-11-13T18:00:00+00:00", "2025-11-13T22:00:00+00:00"]
  ]
}
```

---

#### `pyheat_get_boiler_history` - Get Boiler Operation History

Get historical boiler on/off periods for a specific day.

**Parameters:**
- `days_ago` (integer, required) - Days ago (0 = today, 1 = yesterday, max 7)

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_get_boiler_history \
  -H "Content-Type: application/json" \
  -d '{"days_ago": 0}'
```

**Response:**
```json
{
  "periods": [
    {"start": "2025-11-13T06:30:00+00:00", "end": "2025-11-13T08:15:00+00:00", "state": "on"},
    {"start": "2025-11-13T08:15:00+00:00", "end": "2025-11-13T18:00:00+00:00", "state": "off"},
    {"start": "2025-11-13T18:00:00+00:00", "end": "2025-11-13T22:00:00+00:00", "state": "on"}
  ],
  "start_time": "2025-11-13T00:00:00+00:00",
  "end_time": "2025-11-13T23:59:59+00:00"
}
```

---

### Configuration Endpoints

#### `pyheat_get_schedules` - Get Current Schedules

Retrieve the complete schedules configuration.

**Parameters:** None (empty object)

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_get_schedules \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{
  "rooms": [
    {
      "id": "lounge",
      "default_target": 19.5,
      "week": {
        "mon": [
          {"start": "06:30", "target": 21.0},
          {"start": "22:00", "target": 19.5}
        ],
        "tue": [...],
        ...
      }
    },
    ...
  ]
}
```

---

#### `pyheat_get_rooms` - Get Rooms Configuration

Retrieve the complete rooms configuration.

**Parameters:** None (empty object)

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_get_rooms \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{
  "lounge": {
    "name": "Lounge",
    "sensors": [...],
    "trv": {...},
    "hysteresis": {...},
    "valve_bands": {...},
    ...
  },
  ...
}
```

---

#### `pyheat_replace_schedules` - Replace Schedules Configuration

Atomically replace the entire schedules.yaml file. Used by pyheat-web's schedule editor.

**Parameters:**
- `schedule` (object, required) - Complete schedules configuration

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_replace_schedules \
  -H "Content-Type: application/json" \
  -d '{
    "schedule": {
      "lounge": {
        "default_target": 19.5,
        "week": {
          "mon": [{"start": "06:30", "target": 21.0}],
          ...
        }
      }
    }
  }'
```

**Response:**
```json
{"success": true}
```

**Note:** Changes take effect immediately without requiring a restart.

---

#### `pyheat_reload_config` - Reload Configuration

Reload all PyHeat configuration from YAML files (rooms.yaml, schedules.yaml, boiler.yaml).

**Parameters:** None (empty object)

**Example:**
```bash
curl -X POST http://appdaemon:5050/api/appdaemon/pyheat_reload_config \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{"success": true}
```

---

### Integration Example (Python)

pyheat-web uses the API via an async HTTP client. See `pyheat-web/server/appdaemon_client.py` for a complete implementation example:

```python
import httpx

class PyHeatClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=10.0)
    
    async def override(self, room: str, target: float, minutes: int):
        response = await self.client.post(
            f"{self.base_url}/api/appdaemon/pyheat_override",
            json={"room": room, "target": target, "minutes": minutes}
        )
        response.raise_for_status()
        return response.json()
    
    async def get_status(self):
        response = await self.client.post(
            f"{self.base_url}/api/appdaemon/pyheat_get_status",
            json={}
        )
        response.raise_for_status()
        return response.json()
```

---

### Home Assistant Services

All API endpoints are also available as Home Assistant services via AppDaemon integration:

```yaml
# Example automation
service: appdaemon.pyheat_override
data:
  room: lounge
  target: 21.0
  minutes: 120
```

Service names match API endpoint names with the `appdaemon.` prefix.

## License

MIT License - see LICENSE file for details.
