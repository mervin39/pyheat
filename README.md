# PyHeat - AppDaemon Heating Controller

A comprehensive home heating control system for Home Assistant using AppDaemon.

## Overview

PyHeat provides intelligent multi-room heating control with:
- **Per-room temperature management** with individual schedules
- **Smart TRV control** via zigbee2mqtt (TRVZB devices)
- **Boiler management** with safety interlocks and anti-cycling
- **Sensor fusion** with staleness detection
- **Multiple control modes**: Auto (scheduled), Manual, and Off per room
- **Override/boost** functionality with timers
- **Holiday mode** for energy savings

## Architecture

This is a complete rewrite of the original PyScript implementation, migrated to AppDaemon for better reliability and state management.

### Key Components

- **app.py** - Main AppDaemon application class
- **constants.py** - System-wide configuration defaults
- **config/** - YAML configuration files for rooms, schedules, and boiler
- **ha_yaml/** - Home Assistant helper entity definitions

### Heating Logic

1. **Sensor Fusion**: Averages multiple temperature sensors per room with primary/fallback roles
2. **Target Resolution**: Precedence: Off → Manual → Override/Boost → Schedule → Default
3. **Hysteresis**: Asymmetric deadband (on_delta: 0.30°C, off_delta: 0.10°C) prevents oscillation
4. **Valve Control**: Stepped bands (0%, low%, mid%, max%) based on temperature error
5. **Boiler Control**: Simple on/off based on aggregated room demand (full state machine pending)

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

### Override/Boost

Temporarily override the schedule:
1. Set target: `input_number.pyheat_{room}_override_target`
2. Start timer: `timer.pyheat_{room}_override`

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
- Basic TRV control with rate limiting
- Simplified boiler on/off control
- Status entity publishing
- Configuration loading and validation
- Callback registration for all state changes

### 🚧 Pending
- Full 7-state boiler state machine with anti-cycling
- TRV feedback monitoring and retry logic
- Override/boost service handlers
- Enhanced error handling and recovery
- Valve band step hysteresis
- Comprehensive integration testing

See `docs/IMPLEMENTATION_PLAN.md` for detailed roadmap.

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

## Migration from PyScript

This is a complete rewrite of the original PyScript implementation. Key differences:

- **Execution model**: Callback-based vs decorator-based
- **State management**: AppDaemon's persistent state vs PyScript's stateless
- **Entity control**: `call_service()` vs direct state manipulation
- **Imports**: Proper Python package structure

Configuration files (`rooms.yaml`, `schedules.yaml`) are compatible between versions.

## Contributing

See `docs/changelog.md` for recent changes and `docs/todo.md` for planned work.

## License

[Your license here]

## Authors

Originally implemented in PyScript, migrated to AppDaemon in November 2025.
