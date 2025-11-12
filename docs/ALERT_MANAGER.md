# PyHeat Alert Manager

## Overview

The Alert Manager provides intelligent notification management for critical PyHeat issues, creating Home Assistant persistent notifications when problems require user attention.

## Features

- **Debouncing**: Requires multiple consecutive errors before alerting to prevent false positives
- **Rate Limiting**: Limits notifications to one per alert per hour to prevent spam
- **Auto-clearing**: Automatically dismisses notifications when conditions resolve
- **Severity Levels**: Critical and warning levels for appropriate urgency
- **Room Context**: Includes affected room information when applicable

## Alert Types

### Critical Alerts (Immediate Attention Required)

#### 1. Boiler Interlock Failure
**Alert ID**: `ALERT_BOILER_INTERLOCK_FAILURE`

**Trigger**: Boiler was running but valve interlock failed (insufficient valve opening)

**Message Example**:
```
⚠️ PyHeat Critical Alert
Boiler was running but valve interlock failed!

Reason: only 0/3 valves >= 20% (need at least 1)

The boiler has been turned off for safety. Check TRV operation and valve positions.
```

**Action Required**: Check TRV batteries, connections, and mechanical operation

**Auto-clear**: Yes, when valves reopen properly

---

#### 2. TRV Feedback Timeout
**Alert ID**: `ALERT_TRV_FEEDBACK_TIMEOUT_{room_id}`

**Trigger**: TRV valve commanded but feedback doesn't match after multiple retries

**Message Example**:
```
⚠️ PyHeat Critical Alert
Room: Pete

TRV valve feedback mismatch after multiple retries.

Commanded: 75%
Actual: 0%

Check TRV batteries, connection, or mechanical issues.
```

**Action Required**: 
- Check TRV battery level
- Verify TRV connectivity to HA
- Inspect for mechanical blockage or stuck valve

**Auto-clear**: Yes, when valve feedback confirms position

---

#### 3. TRV Unavailable
**Alert ID**: `ALERT_TRV_UNAVAILABLE_{room_id}`

**Trigger**: TRV feedback sensor unavailable after multiple retry attempts

**Message Example**:
```
⚠️ PyHeat Critical Alert
Room: Lounge

TRV feedback sensor unavailable after multiple retries.

Lost communication with TRV. Check TRV connectivity and batteries.
```

**Action Required**:
- Check TRV power/batteries
- Verify TRV is connected to HA
- Check network connectivity

**Auto-clear**: Yes, when feedback sensor becomes available

---

#### 4. Boiler Control Failure
**Alert ID**: `ALERT_BOILER_CONTROL_FAILURE`

**Trigger**: Failed to turn boiler on or off via Home Assistant service call

**Message Example**:
```
⚠️ PyHeat Critical Alert
Failed to turn boiler ON: Service call failed

Check boiler entity (climate.boiler) availability and network connection.
```

**Action Required**:
- Verify boiler entity exists in HA
- Check HA → Boiler communication
- Review network/integration status

**Auto-clear**: Yes, when boiler control succeeds

---

#### 5. Configuration Load Failure
**Alert ID**: `ALERT_CONFIG_LOAD_FAILURE`

**Trigger**: Failed to load pyheat configuration files (YAML syntax errors)

**Message Example**:
```
⚠️ PyHeat Critical Alert
Failed to load PyHeat configuration: YAML syntax error

Please check your YAML files for syntax errors.
```

**Action Required**:
- Check YAML syntax in `rooms.yaml`, `schedules.yaml`, `boiler.yaml`
- Fix syntax errors
- Restart AppDaemon

**Auto-clear**: No (requires manual restart after fix)

---

## Configuration

### Debounce Threshold
```python
self.debounce_threshold = 3  # Require 3 consecutive errors
```

Default: **3 consecutive errors** before creating alert

### Rate Limiting
```python
self.rate_limit_seconds = 3600  # 1 hour
```

Default: **1 hour** between notifications for the same alert

## Usage

### In Boiler Controller

```python
# Report boiler interlock failure
if self.alert_manager:
    from pyheat.alert_manager import AlertManager
    self.alert_manager.report_error(
        AlertManager.ALERT_BOILER_INTERLOCK_FAILURE,
        AlertManager.SEVERITY_CRITICAL,
        "Boiler was running but valve interlock failed!...",
        auto_clear=True
    )
```

### In TRV Controller

```python
# Report TRV feedback timeout
if self.alert_manager:
    from pyheat.alert_manager import AlertManager
    self.alert_manager.report_error(
        f"{AlertManager.ALERT_TRV_FEEDBACK_TIMEOUT}_{room_id}",
        AlertManager.SEVERITY_CRITICAL,
        f"TRV valve feedback mismatch...",
        room_id=room_id,
        auto_clear=True
    )
```

### Clear Alert

```python
# Clear alert when condition resolves
if self.alert_manager:
    from pyheat.alert_manager import AlertManager
    self.alert_manager.clear_error(AlertManager.ALERT_BOILER_CONTROL_FAILURE)
```

## Home Assistant Integration

Alerts appear as **persistent notifications** in Home Assistant:
- Visible in notification panel (bell icon)
- Persist across page reloads
- Dismissable by user
- Auto-dismiss when condition resolves

### Viewing Notifications

1. Click bell icon in HA top bar
2. View notification details
3. Click "Dismiss" to manually clear (if not auto-cleared)

### Notification IDs

Each alert gets a unique notification ID:
```
pyheat_{alert_id}
```

Examples:
- `pyheat_boiler_interlock_failure`
- `pyheat_trv_feedback_timeout_pete`
- `pyheat_config_load_failure`

## API Methods

### report_error()
```python
alert_manager.report_error(
    alert_id: str,           # Unique identifier
    severity: str,           # SEVERITY_CRITICAL or SEVERITY_WARNING
    message: str,            # Human-readable description
    room_id: Optional[str],  # Affected room (optional)
    auto_clear: bool         # Can auto-clear (default True)
)
```

### clear_error()
```python
alert_manager.clear_error(alert_id: str)
```

### get_active_alerts()
```python
alerts = alert_manager.get_active_alerts()
# Returns: Dict[str, Dict] with all active alerts
```

### get_alert_count()
```python
count = alert_manager.get_alert_count(severity="critical")
# Returns: int, count of active alerts (optionally filtered)
```

## Implementation Details

### Alert State Tracking

Each alert stores:
```python
{
    'severity': 'critical',
    'message': 'Description...',
    'timestamp': datetime,
    'room_id': 'pete',
    'auto_clear': True,
    'consecutive_count': 3
}
```

### Notification Format

```
Title: ⚠️ PyHeat Critical Alert
Message:
  **Room:** Pete (if applicable)
  
  Description of the issue
  
  Recommended action
  
  *2025-11-12 14:30:00*
```

## Testing

### Manual Testing

1. **Trigger Alert**: Cause error condition (e.g., disconnect TRV)
2. **Verify Debouncing**: Alert should appear after 3 consecutive errors
3. **Check HA Notification**: Verify persistent notification appears
4. **Resolve Condition**: Reconnect TRV
5. **Verify Auto-Clear**: Notification should dismiss automatically

### Test Commands

```python
# Force alert (in AppDaemon)
alert_manager.error_counts['test_alert'] = 3
alert_manager.report_error(
    'test_alert',
    AlertManager.SEVERITY_CRITICAL,
    'Test alert message',
    auto_clear=True
)

# Clear alert
alert_manager.clear_error('test_alert')
```

## Troubleshooting

### Notifications Not Appearing

1. Check AppDaemon logs for alert manager messages
2. Verify Home Assistant `persistent_notification` service is available
3. Check for rate limiting (1 hour between same alert)
4. Verify debounce threshold reached (3 consecutive errors)

### Notifications Not Auto-Clearing

1. Verify `auto_clear=True` when reporting error
2. Check that `clear_error()` is called when condition resolves
3. Review AppDaemon logs for clear messages

### Too Many Notifications

1. Check debounce threshold - may need to increase
2. Review rate limiting interval - may need to increase
3. Investigate root cause of repeated errors

## Future Enhancements

Potential improvements:
- [ ] Integration with mobile app notifications
- [ ] Email notifications for critical alerts
- [ ] Alert history/logging
- [ ] Configurable debounce thresholds per alert type
- [ ] Alert grouping (combine multiple room alerts)
- [ ] Alert statistics and reporting
