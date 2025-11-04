# TRV Setpoint Locking Strategy

## Problem Statement

The TRVZB valves (via zigbee2mqtt) have a unique control interface:
- `number.{trv}_valve_opening_degree` - controls valve % when TRV wants to OPEN
- `number.{trv}_valve_closing_degree` - controls valve % when TRV wants to CLOSE

The TRV decides whether to open or close based on its internal temperature measurement vs its internal setpoint. This created a problem:
- We needed to send TWO commands for every valve change (opening + closing)
- We never knew which mode the TRV was in (opening or closing)
- Commands took 4 seconds per room (2s per command with feedback)
- Used blocking `time.sleep()` calls that violated AppDaemon best practices

## Solution: Lock Setpoint to 5°C

By locking all TRV setpoints to 5°C (well below any reasonable room temperature):
- TRVs are ALWAYS in "opening" mode
- We only need to control `opening_degree` (not `closing_degree`)
- Single command per valve change (2s instead of 4s)
- Can use non-blocking scheduler callbacks

## Implementation

### Constants (`constants.py`)
```python
TRV_LOCKED_SETPOINT_C = 5.0           # Lock TRV internal setpoint to 5°C
TRV_SETPOINT_CHECK_INTERVAL_S = 300   # Check/correct setpoints every 5 minutes
```

### Entity Patterns
```python
TRV_ENTITY_PATTERNS = {
    "cmd_valve":  "number.{trv_base}_valve_opening_degree",      # Only control opening degree
    "fb_valve":   "sensor.{trv_base}_valve_opening_degree_z2m",  # Only monitor opening degree
    "climate":    "climate.{trv_base}",                          # Climate entity for setpoint control
}
```

### Setpoint Locking Functions

1. **`lock_all_trv_setpoints()`** - Called on startup (3s delay) and periodically (every 5 min)
2. **`lock_trv_setpoint(room_id)`** - Locks a single TRV to 5°C
3. **`check_trv_setpoints()`** - Periodic callback to detect and correct setpoint drift

### Valve Control Flow

1. `set_trv_valve()` - Entry point, checks rate limiting
2. `_start_valve_command()` - Initiates non-blocking command
3. `_execute_valve_command()` - Sends opening_degree command
4. `run_in()` schedules `_check_valve_feedback()` after 2s
5. `_check_valve_feedback()` - Checks feedback, retries if needed (up to 3 attempts)

## Benefits

✅ **50% faster**: 2s per room instead of 4s  
✅ **Non-blocking**: Uses AppDaemon scheduler instead of `time.sleep()`  
✅ **No warnings**: Eliminated AppDaemon callback timeout warnings  
✅ **Simpler code**: Single command path instead of dual sequential logic  
✅ **Robust**: Automatic setpoint correction handles accidental user changes  

## Verification

```bash
# Check TRV setpoints are locked
tail -f /opt/appdata/appdaemon/conf/logs/appdaemon.log | grep "Locking TRV"

# Verify single-command valve control
tail -f /opt/appdata/appdaemon/conf/logs/appdaemon.log | grep "Setting TRV"

# Confirm no blocking warnings
tail -f /opt/appdata/appdaemon/conf/logs/appdaemon.log | grep "Excessive time"
```

## Compatibility

This strategy works with:
- TRVZB thermostatic radiator valves
- Zigbee2MQTT integration
- Home Assistant climate entities

The locked setpoint (5°C) is well below freezing protection thresholds and won't interfere with normal operation.

## Maintenance

The periodic setpoint checker (`check_trv_setpoints()`) runs every 5 minutes to detect and correct:
- User accidentally changing TRV setpoint via UI
- TRV firmware resets
- Power cycle events

If a setpoint drifts more than 0.1°C from 5.0°C, it will be automatically corrected.
