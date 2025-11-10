# PyHeat - AppDaemon Heating Controller

A comprehensive home heating control system for Home Assistant using AppDaemon.

## Overview

PyHeat provides intelligent multi-room heating control with:
- **Per-room temperature management** with individual schedules
- **Smart TRV control** via zigbee2mqtt (TRVZB devices)
- **Boiler management** with safety interlocks and anti-cycling
- **Sensor fusion** with staleness detection
- **Multiple control modes**: Auto (scheduled), Manual, and Off per room
- **Override** functionality with flexible parameters (absolute/delta temp, duration/end time)
- **Holiday mode** for energy savings

## Architecture

This is a complete rewrite of the original PyScript implementation, migrated to AppDaemon for better reliability and state management. The codebase has been fully modularized for maintainability.

### Key Components

- **app.py** - Main AppDaemon application orchestration
- **boiler_controller.py** - 6-state FSM boiler control with safety interlocks
- **room_controller.py** - Per-room heating logic and target resolution
- **trv_controller.py** - TRV valve command and setpoint locking
- **sensor_manager.py** - Temperature sensor fusion and staleness detection
- **scheduler.py** - Schedule parsing and time-based target calculation
- **service_handler.py** - Home Assistant service handlers for programmatic control
- **status_publisher.py** - Entity creation and status publication
- **config_loader.py** - YAML configuration validation and loading
- **constants.py** - System-wide configuration defaults
- **config/** - YAML configuration files for rooms, schedules, and boiler
- **ha_yaml/** - Home Assistant helper entity definitions

### Heating Logic

1. **Sensor Fusion**: Averages multiple temperature sensors per room with primary/fallback roles and staleness detection
2. **Target Resolution**: Precedence: Off → Manual → Override → Schedule → Default
3. **Hysteresis**: Asymmetric deadband (on_delta: 0.30°C, off_delta: 0.10°C) prevents oscillation; bypassed on target changes for immediate override response
4. **Valve Control**: Stepped bands (0%, low%, mid%, max%) based on temperature error with multi-band jump optimization
5. **TRV Setpoint Locking**: All TRVs locked to 35°C with immediate correction via state listener
6. **Boiler Control**: Full 6-state FSM with anti-cycling timers, TRV feedback validation, and pump overrun

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
   
   Add the PyHeat package to your Home Assistant configuration:
   
   ```bash
   # From your Home Assistant config directory
   ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_package.yaml packages/pyheat_package.yaml
   ```
   
   Then ensure your `configuration.yaml` has packages enabled:
   
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
   
   See `ha_yaml/README.md` for alternative installation methods.

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
- **sensors**: Array of temperature sensors with roles (primary/fallback) and timeouts
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

## Development Status

### ✅ Completed
- Core heating logic (sensor fusion, target resolution, hysteresis)
- Full modular architecture with 10 specialized modules
- TRV control with setpoint locking (35°C), rate limiting, and feedback confirmation
- Complete 6-state boiler FSM with anti-cycling protection
- TRV-open interlock validation and safety override
- Pump overrun timer with valve position persistence
- Status entity publishing with comprehensive diagnostics
- Per-room sensor entities (temperature, target, valve_percent, calling_for_heat)
- Configuration loading and validation
- Callback registration for all state changes
- **HTTP API endpoints** for external access (pyheat-web integration)
- Debug monitoring tool for system testing

## HTTP API Endpoints

PyHeat exposes HTTP API endpoints via AppDaemon's `register_endpoint()` for external control and monitoring:

**Base URL**: `http://<appdaemon-host>:5050/api/appdaemon/`

### Available Endpoints

- **`pyheat_override`** - Set temperature override with flexible parameters
  ```json
  // Absolute target for 60 minutes
  POST /api/appdaemon/pyheat_override
  {"room": "lounge", "target": 21.0, "minutes": 60}
  
  // Delta from schedule until 22:30
  POST /api/appdaemon/pyheat_override
  {"room": "lounge", "delta": 2.0, "end_time": "22:30"}
  ```

- **`pyheat_cancel_override`** - Cancel active override
  ```json
  POST /api/appdaemon/pyheat_cancel_override
  {"room": "lounge"}
  ```

- **`pyheat_set_mode`** - Set room operating mode
  ```json
  POST /api/appdaemon/pyheat_set_mode
  {"room": "lounge", "mode": "auto"}
  // mode: "auto", "manual", "off"
  // optional: "manual_setpoint": 20.0 for manual mode
  ```

- **`pyheat_get_status`** - Get complete system and room status
  ```json
  POST /api/appdaemon/pyheat_get_status
  {}
  // Returns: {rooms: [...], system: {...}}
  ```

- **`pyheat_get_schedules`** - Get current schedules
  ```json
  POST /api/appdaemon/pyheat_get_schedules
  {}
  // Returns: {rooms: [{id, default_target, week: {...}}, ...]}
  ```

- **`pyheat_replace_schedules`** - Update schedules (used by pyheat-web)
  ```json
  POST /api/appdaemon/pyheat_replace_schedules
  {"schedule": {"rooms": [...]}}
  // Atomically replaces schedules.yaml
  ```

- **`pyheat_reload_config`** - Reload configuration from YAML files
  ```json
  POST /api/appdaemon/pyheat_reload_config
  {}
  ```

All endpoints return JSON with `{"success": true/false, ...}` format.

See `api_handler.py` for implementation details and response formats.

## Troubleshooting

### App won't load
- Check AppDaemon logs for import errors
- Verify `__init__.py` exists in the pyheat directory
- Ensure `apps.yaml` has `module: pyheat.app` (not `pyheat.pyheat`)

### TRVs not responding
- Verify TRV entity IDs in `config/rooms.yaml`
- Check that zigbee2mqtt is running
- Ensure TRV command entities exist (derived from climate entity)

### Temperature not reading
- Check sensor entity IDs in `config/rooms.yaml`
- Verify sensors are updating (check timeout_m settings)
- Look for "stale" messages in logs

### Boiler not turning on
- Ensure `input_boolean.pyheat_master_enable` is on
- Check that at least one room is calling for heat
- Verify boiler actor entity exists

### API endpoints not working
- Verify AppDaemon is running and accessible on port 5050
- Check logs for endpoint registration messages
- Ensure JSON body is properly formatted
- Note: Endpoints are synchronous, not async

## Integration with pyheat-web

PyHeat can be controlled via the [pyheat-web](https://github.com/yourusername/pyheat-web) mobile-first web interface:
- Real-time status monitoring via WebSocket
- Room control (boost, override, mode switching)
- Visual schedule editor with drag-and-drop
- Secure token custody (HA token never exposed to browser)

See pyheat-web documentation for setup instructions.

## Migration from PyScript

This is a complete rewrite of the original PyScript implementation. Key differences:

- **Execution model**: Callback-based vs decorator-based
- **State management**: AppDaemon's persistent state vs PyScript's stateless
- **Entity control**: `call_service()` vs direct state manipulation
- **Imports**: Proper Python package structure
- **API access**: HTTP endpoints via `register_endpoint()` (new in AppDaemon version)

Configuration files (`rooms.yaml`, `schedules.yaml`) are compatible between versions.

## Contributing

See `docs/changelog.md` for recent changes and `docs/TODO.md` for planned work.

## License

[Your license here]

## Authors

Originally implemented in PyScript, migrated to AppDaemon in November 2025.
HTTP API endpoints and pyheat-web integration added November 2025.
