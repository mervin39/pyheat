# Status Text Formatting Specification

**Date**: 2025-11-08 (Updated)  
**Purpose**: Define the exact format for `formatted_status` text displayed in pyheat-web UI and Home Assistant entities.

## Design Principles

1. **Server-side formatting**: All status text formatting happens in AppDaemon (status_publisher.py)
2. **Auto mode shows times**: Both HA and Web show full status with "until HH:MM on Day (T°)"
3. **Override/Boost strip times**: Web receives without ". Until HH:MM" and adds live countdown
4. **Client adds countdown**: Web UI appends live countdown from `override_end_time` for Override/Boost only

## Status Formats

### Auto Mode (no boost/override)

#### With Schedule Change Coming
**Format**: `Auto: T° until HH:MM on $DAY (S°)`
- **T**: Current scheduled setpoint
- **S**: Next scheduled setpoint (accounting for gaps)
- **HH:MM**: Start time of next setpoint (24-hour format)
- **on $DAY**: Only included if next change is not today

**Examples**:
- Current: 18.0°, Next: 14.0° at 23:00 today  
  → `Auto: 18.0° until 23:00 (14.0°)`

- Current: 18.0°, Next: 20.0° at 07:00 tomorrow  
  → `Auto: 18.0° until 07:00 on Friday (20.0°)`

- Current: 10.0° (block ends 10:30), Gap (default 14.0°), Next block: 16:00 @ 12.0°  
  → `Auto: 10.0° until 10:30 (14.0°)`  
  *(Shows gap temperature, not next block)*

#### Forever (No Schedule Changes)
**Format**: `Auto: T° forever`
- Room has no schedule blocks defined across all 7 days
- **Detection**: `not any(schedule['week'].values())`

**Example**:
- Room with only default target, no blocks  
  → `Auto: 18.0° forever`

### Override Mode

**HA Format**: `Override: T° → S°. Until HH:MM`  
**Web Format**: `Override: T° → S°`  
**Client Display**: `Override: T° → S°. 2h 29m left` (live countdown)

- **T**: Current scheduled setpoint (may be default_target)
- **S**: Override target temperature
- **HH:MM**: End time (24-hour format) - only in HA version
- Client calculates countdown from `override_end_time` and appends

**Examples**:
- Scheduled: 14.0°, Override: 21.0°, ends 17:30  
  - HA: `Override: 14.0° → 21.0°. Until 17:30`
  - Web receives: `Override: 14.0° → 21.0°`
  - Client shows: `Override: 14.0° → 21.0°. 2h 29m left`

### Boost Mode

**HA Format**: `Boost +D°: T° → S°. Until HH:MM`  
**Web Format**: `Boost +D°: T° → S°`  
**Client Display**: `Boost +D°: T° → S°. 3h 15m left` (live countdown)

- **D**: Temperature delta (with sign)
- **T**: Current scheduled setpoint
- **S**: Boosted temperature (T + D)
- **HH:MM**: End time (24-hour format) - only in HA version

**Examples**:
- Scheduled: 18.0°, Boost: +2.0°, ends 19:00  
  - HA: `Boost +2.0°: 18.0° → 20.0°. Until 19:00`
  - Web receives: `Boost +2.0°: 18.0° → 20.0°`
  - Client shows: `Boost +2.0°: 18.0° → 20.0°. 3h 15m left`

- Scheduled: 14.0°, Boost: -1.0°, ends 21:30  
  - HA: `Boost -1.0°: 14.0° → 13.0°. Until 21:30`
  - Web receives: `Boost -1.0°: 14.0° → 13.0°`
  - Client shows: `Boost -1.0°: 14.0° → 13.0°. 1h 5m left`

### Manual Mode

**Format**: `Manual: T°`
- **T**: Manual setpoint

**Example**:
- Manual setpoint: 19.5°  
  → `Manual: 19.5°`

### Off Mode

**Format**: `Heating Off`

**Example**: `Heating Off`

## Implementation Details

### Server (status_publisher.py)

1. **Format with static times**: All status includes "until HH:MM" or "Until HH:MM"
2. **Published to HA entities**: `sensor.pyheat_{room}_state` attribute `formatted_status`
3. **Passed to API handler**: Full formatted string with times

### API Handler (api_handler.py)

1. **Auto mode**: Keep full status with times (same as HA)
2. **Override/Boost**: Strip `. Until \d{2}:\d{2}` only
3. **Result**: Web receives Auto with times, Override/Boost without times

### Client (room-card.tsx, embed-room-card.tsx)

1. **Auto mode**: Display `formatted_status` as-is (includes time and next temp)
2. **Override/Boost**: Add live countdown from `override_end_time`
3. **Append**: `. {countdown} left` to Override/Boost status
4. **Update**: Every second for smooth countdown

### Forever Detection Algorithm

```python
def is_forever(schedule: dict) -> bool:
    """Check if schedule has no blocks (forever at default temp)."""
    week_schedule = schedule.get('week', {})
    # If any day has any blocks, it's not forever
    return not any(week_schedule.values())
```

### Gap Detection Algorithm

When finding next schedule change:
1. Get current block's end time
2. Get next block's start time
3. If next block starts > 1 minute after current block ends:
   - It's a gap
   - Show default_target as next temp, not next block's temp
4. Show end time of current block as "until" time

### Day Name Logic

```python
if next_change_date.date() == today.date():
    # Today - no day name
    return f"Auto: {current}° until {time} ({next}°)"
else:
    day_name = next_change_date.strftime("%A")  # "Monday", "Friday", etc.
    return f"Auto: {current}° until {time} on {day_name} ({next}°)"
```

## Testing Checklist

- [ ] Auto mode with next change today
- [ ] Auto mode with next change tomorrow
- [ ] Auto mode with gap showing default_target
- [ ] Auto mode with no schedule (forever)
- [ ] Override mode with countdown
- [ ] Boost mode positive delta with countdown
- [ ] Boost mode negative delta with countdown
- [ ] Manual mode
- [ ] Off mode
- [ ] HA entity shows "Until HH:MM"
- [ ] Web UI shows live countdown
- [ ] Countdown updates every second
- [ ] Countdown expires correctly

## Performance Notes

- Forever check is O(1): just check if week dict values are all empty
- Next schedule lookup is O(n) where n = blocks in week (typically < 20)
- Gap detection adds minimal overhead (one time comparison)
- No time calculation on server (static "until HH:MM" only)
- Client handles all live countdown logic
