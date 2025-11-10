# PyHeat Architecture

## Introduction

PyHeat is a sophisticated multi-room heating control system built on AppDaemon for Home Assistant. The system manages per-room temperature control through intelligent TRV (Thermostatic Radiator Valve) management, boiler state control with safety interlocks, and flexible scheduling capabilities.

The architecture is modular and event-driven, with clear separation of concerns across specialized controllers. Each module has a specific responsibility within the heating control pipeline, from sensor fusion through to physical valve commands.

**Key Design Principles:**
- **Rooms as first-class objects** - Each room maintains its own state, sensors, schedule, and heating decisions
- **Event-driven coordination** - Controllers respond to Home Assistant state changes and time triggers
- **Safety-first boiler control** - Multi-state FSM with anti-cycling protection and failure interlocks
- **Deterministic target resolution** - Clear precedence hierarchy for manual/override/scheduled control
- **Hysteresis throughout** - Prevents oscillation in heating decisions, valve commands, and boiler cycling

---

## System Overview

### High-Level Data Flow

PyHeat operates as an event-driven control loop that continuously monitors temperature sensors, evaluates heating requirements, and commands both TRV valves and the central boiler. The data flows through multiple stages of processing:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         EVENT TRIGGERS (Entry Points)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  • Periodic timer (60s)          • Sensor state changes                      │
│  • Room mode changes              • Manual setpoint changes                  │
│  • Override timers                • Service calls (API/HA)                   │
│  • TRV feedback changes           • Config file modifications                │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      SENSOR FUSION (sensor_manager.py)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  Per-room temperature sensors (primary/fallback roles)                       │
│    ├─ Update sensor values and timestamps                                    │
│    ├─ Check staleness (timeout_m threshold)                                  │
│    ├─ Average available sensors by role priority                             │
│    └─ Return: (fused_temp, is_stale)                                         │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    TARGET RESOLUTION (scheduler.py)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  Determine target temperature using precedence hierarchy:                    │
│    1. Off mode          → None (no heating)                                  │
│    2. Manual mode       → manual_setpoint (user-set constant)                │
│    3. Override active   → override_target (absolute temp, from target/delta) │
│    4. Schedule block    → block target for current time/day                  │
│    5. Default           → default_target (outside schedule blocks)           │
│    6. Holiday mode      → 15.0°C (energy saving)                             │
│  Return: target_temp (or None if off)                                        │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   ROOM HEATING LOGIC (room_controller.py)                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  For each room:                                                               │
│    ├─ Calculate error = target - current_temp                                │
│    ├─ Call-for-heat decision (asymmetric hysteresis):                        │
│    │    • error ≥ on_delta (0.30°C)  → start calling                         │
│    │    • error ≤ off_delta (0.10°C) → stop calling                          │
│    │    • Between deltas             → maintain previous state               │
│    ├─ Valve percentage (stepped bands with hysteresis):                      │
│    │    • Band 0 (0%):  error < 0.30°C                                       │
│    │    • Band 1 (35%): 0.30°C ≤ error < 0.80°C                              │
│    │    • Band 2 (65%): 0.80°C ≤ error < 1.50°C                              │
│    │    • Band 3 (100%): error ≥ 1.50°C                                      │
│    │    • Band transitions require step_hysteresis (0.05°C) crossing         │
│    └─ Return: {temp, target, calling, valve_percent, error, mode}            │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      TRV COMMANDS (trv_controller.py)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  For each room with valve change:                                            │
│    ├─ Rate limiting check (min_interval_s, default 30s)                      │
│    ├─ Command valve opening via number.trv_{room}_valve_opening_degree      │
│    ├─ Non-blocking feedback confirmation:                                    │
│    │    • Schedule check after 5s                                            │
│    │    • Compare sensor.trv_{room}_valve_opening_degree_z2m                 │
│    │    • Retry up to 3 times if mismatch                                    │
│    │    • Log error if retries exhausted                                     │
│    ├─ Detect unexpected positions (TRV manual changes)                       │
│    └─ Trigger immediate correction if unexpected change detected             │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  BOILER STATE MACHINE (boiler_controller.py)                 │
├─────────────────────────────────────────────────────────────────────────────┤
│  Aggregate room demand and manage 6-state FSM:                               │
│    ├─ Inputs: any_calling, active_rooms[], room_valve_percents{}            │
│    ├─ Valve interlock: Calculate total valve opening                         │
│    │    • Sum valves for calling rooms                                       │
│    │    • Apply valve persistence for safety                                 │
│    │    • Check min_valve_open_pct threshold (100%)                          │
│    ├─ TRV feedback validation (wait for valves to open)                      │
│    ├─ Anti-cycling protection:                                               │
│    │    • Min ON time: 180s                                                  │
│    │    • Min OFF time: 180s                                                 │
│    │    • Off-delay: 30s (grace period before shutdown)                      │
│    ├─ State transitions:                                                     │
│    │    OFF → PENDING_ON → WAITING_FOR_TRVFB → ON                            │
│    │    ON → PENDING_OFF → PUMP_OVERRUN → ANTICYCLE → OFF                    │
│    │    (Any) → INTERLOCK_BLOCKED if insufficient valve opening              │
│    ├─ Pump overrun handling:                                                 │
│    │    • Save valve positions when demand ceases                            │
│    │    • Keep valves open for 180s after boiler off                         │
│    │    • Ensures residual heat circulation                                  │
│    └─ Return: (state, reason, persisted_valves{}, must_stay_open)            │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    VALVE PERSISTENCE LOGIC (app.py)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  Critical safety handling of persisted valve positions:                      │
│    ├─ If persisted_valves exist (pump overrun or interlock):                 │
│    │    • Apply persisted positions to those rooms FIRST                     │
│    │    • Apply normal calculated positions to other rooms                   │
│    ├─ Else: Apply all normal calculated positions                            │
│    └─ Ensures safety overrides take precedence over heating logic            │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   STATUS PUBLICATION (status_publisher.py)                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Publish state to Home Assistant entities:                                   │
│    ├─ Per-room entities (sensor.pyheat_room_{room}):                         │
│    │    • State: current temperature                                         │
│    │    • Attributes: target, mode, calling, valve_percent, error            │
│    │    • formatted_status with schedule/override information                │
│    │    • next_change, override_end_time (for UI countdowns)                 │
│    ├─ System status (sensor.pyheat_status):                                  │
│    │    • State: "heating", "idle", or "master_off"                          │
│    │    • Attributes: boiler_state, calling_rooms[], all room_data           │
│    │    • last_recompute, recompute_count                                    │
│    └─ Format status text per STATUS_FORMAT_SPEC.md                           │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                 EXTERNAL INTERFACES (service/api handlers)                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Service calls (pyheat.*):          HTTP API (/api/appdaemon/pyheat_*):     │
│    • set_room_mode                    • GET /pyheat_get_status               │
│    • set_room_target                  • GET /pyheat_get_schedules            │
│    • override                         • POST /pyheat_override                │
│    • cancel_override                  • POST /pyheat_cancel_override         │
│    • reload_schedules                 • POST /pyheat_replace_schedules       │
│    • reload_rooms                     • POST /pyheat_set_mode                │
│  All trigger recompute cycle          • Used by pyheat-web UI                │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key Flow Characteristics:**

1. **Event-Driven**: Any state change (sensor, mode, timer, manual) triggers immediate recompute
2. **Periodic Baseline**: 60-second timer ensures regular evaluation even without events
3. **Synchronous Processing**: Recompute runs synchronously to prevent race conditions
4. **Safety Priority**: Boiler safety logic can override room heating decisions via valve persistence
5. **Stateful**: Previous states (calling, band) maintained for hysteresis calculations
6. **Non-Blocking**: TRV commands use async feedback confirmation to avoid blocking control loop

### Core Components

- **app.py** - Main AppDaemon application and orchestration
- **sensor_manager.py** - Temperature sensor fusion with staleness detection
- **scheduler.py** - Schedule parsing and time-based target calculation
- **room_controller.py** - Per-room heating logic and target resolution
- **trv_controller.py** - TRV valve commands and setpoint locking
- **boiler_controller.py** - 6-state FSM boiler control with safety interlocks
- **service_handler.py** - Home Assistant service registration and handling
- **status_publisher.py** - Entity creation and status publication to HA
- **config_loader.py** - YAML configuration validation and loading
- **api_handler.py** - REST API endpoints for external control
- **constants.py** - System-wide configuration defaults

---

## Temperature Sensing and Fusion

### Overview

The `SensorManager` class (`sensor_manager.py`) implements a robust sensor fusion system that combines multiple temperature sensors per room with staleness detection and role-based prioritization. This provides reliable temperature readings even when individual sensors fail or become temporarily unavailable.

**Key Features:**
- Multiple sensors per room with primary/fallback roles
- Automatic averaging of available sensors
- Staleness detection with configurable timeouts
- Graceful degradation when sensors fail
- State restoration on AppDaemon restart

### Sensor Configuration

Each room can have multiple temperature sensors defined in `rooms.yaml`:

```yaml
rooms:
  - id: pete
    name: "Pete's Room"
    sensors:
      - entity_id: sensor.roomtemp_pete
        role: primary              # "primary" or "fallback"
        timeout_m: 180             # Minutes before considered stale
      - entity_id: sensor.pete_snzb02_temperature
        role: primary              # Multiple primaries are averaged
        timeout_m: 180
```

**Configuration Parameters:**
- `entity_id`: Home Assistant sensor entity (must report numeric temperature in °C)
- `role`: Either `"primary"` or `"fallback"` (determines priority)
- `timeout_m`: Staleness timeout in minutes (default: 180 minutes / 3 hours)

### Sensor Roles and Prioritization

The system uses a **two-tier hierarchy** for sensor selection:

1. **Primary Sensors** (role: `primary`)
   - Used by default when available
   - Multiple primary sensors are averaged together
   - Provides sensor redundancy within the same priority level
   
2. **Fallback Sensors** (role: `fallback`)
   - Only used when ALL primary sensors are stale/unavailable
   - Useful for less accurate sensors (e.g., TRV internal sensors)
   - Also averaged if multiple fallback sensors available

**Fusion Algorithm:**
```
1. Collect all PRIMARY sensors that are fresh (age ≤ timeout_m)
2. If any primary sensors available:
     → Return average of primary sensor values
3. Else, collect all FALLBACK sensors that are fresh
4. If any fallback sensors available:
     → Return average of fallback sensor values
5. Else:
     → Return None (all sensors stale), mark room as stale
```

### Data Storage and State Management

The `SensorManager` maintains an in-memory cache of sensor readings:

```python
self.sensor_last_values = {
    'sensor.roomtemp_pete': (21.5, datetime(2025, 11, 10, 14, 30, 15)),
    'sensor.roomtemp_lounge': (19.2, datetime(2025, 11, 10, 14, 30, 18)),
    # entity_id: (temperature_value, timestamp)
}
```

**State Structure:**
- **Key**: Home Assistant sensor entity ID
- **Value**: Tuple of `(temperature: float, timestamp: datetime)`

**Initialization on Startup:**
- On AppDaemon restart, `initialize_from_ha()` reads current sensor states from Home Assistant
- Only sensors with valid numeric values (not 'unknown' or 'unavailable') are loaded
- All sensors are timestamped with the initialization time
- Failed initializations log warnings but don't prevent startup

### Staleness Detection

Sensors are considered **stale** when their age exceeds the configured timeout:

```python
age_minutes = (now - sensor_timestamp).total_seconds() / 60
is_stale = age_minutes > timeout_m
```

**Staleness Behavior:**
- **Individual Sensor**: Excluded from averaging if stale
- **All Sensors Stale**: Room returns `(None, True)` - heating disabled for safety
- **Timeout Default**: 180 minutes (3 hours) if not specified
- **Minimum Timeout**: 1 minute (enforced by constants.py)

**Safety Implications:**
- Stale sensors prevent heating to avoid runaway scenarios
- Manual mode ALSO requires valid sensors (override for safety)
- Room heating decisions cannot proceed without temperature data

### Sensor Averaging

When multiple sensors in the same role are available and fresh, they are **averaged arithmetically**:

```python
temps = [21.5, 21.8, 21.3]  # Three fresh primary sensors
avg_temp = sum(temps) / len(temps)  # → 21.53°C
```

**Averaging Properties:**
- Equal weighting for all sensors in the same role
- Simple arithmetic mean (no weighted averaging)
- Precision determined by room's `precision` setting (applied later in processing)
- Outlier rejection: NOT implemented (assumes sensor reliability)

**Example Scenarios:**

| Primary Sensors | Fallback Sensors | Result |
|----------------|------------------|--------|
| 21.5°C (fresh) | 20.0°C (fresh) | **21.5°C** (primary only) |
| 21.5°C, 21.8°C (both fresh) | - | **21.65°C** (average primaries) |
| 21.5°C (stale) | 20.0°C (fresh) | **20.0°C** (fallback) |
| 21.5°C (stale) | 20.0°C (stale) | **None** (all stale) |

### Entity State Tracking

Temperature sensors are monitored via **AppDaemon state listeners** registered in `app.py`:

```python
# Registered for each sensor in each room
self.listen_state(self.sensor_changed, entity_id, room_id=room_id)
```

**State Change Callback Flow:**
1. Home Assistant sensor changes state (new temperature reading)
2. AppDaemon triggers `sensor_changed()` callback
3. Validate new value (numeric, not 'unknown'/'unavailable')
4. Update `SensorManager` with new value and current timestamp
5. Trigger immediate system recompute

**Update Characteristics:**
- **Debouncing**: None - every sensor update triggers recompute
- **Validation**: Non-numeric values rejected with warning log
- **Timestamp**: Uses `datetime.now()` at callback time (not sensor timestamp)
- **Synchronous**: Recompute runs immediately to prevent race conditions

### Temperature Retrieval API

The main interface used by `RoomController`:

```python
temp, is_stale = sensor_manager.get_room_temperature(room_id, now)
```

**Return Values:**
- `temp: Optional[float]` - Fused temperature in °C, or None if all sensors stale
- `is_stale: bool` - True if no fresh sensors available, False otherwise

**Usage in Heating Logic:**
```python
if temp is None or is_stale:
    # Cannot heat safely - disable room heating
    # Exception: Manual mode still checks for valid temp
    return stop_heating()
```

### Sensor Failure Modes and Recovery

**Failure Scenarios:**

1. **Single Sensor Fails** (multiple configured)
   - Other sensors in same role continue providing data
   - System continues normal operation
   - No user notification required

2. **All Primary Sensors Fail**
   - System automatically switches to fallback sensors
   - Logged at WARNING level for monitoring
   - Heating continues with fallback data

3. **All Sensors Fail**
   - Room marked as stale (`is_stale = True`)
   - Heating disabled for that room
   - Valve commanded to 0%
   - Logged at WARNING level
   - Other rooms continue operating normally

4. **Sensor Recovery**
   - Automatic recovery when sensor returns to service
   - Fresh reading immediately reintegrates into fusion
   - Triggers recompute to resume heating if needed

**No Manual Intervention Required** - System handles all failure and recovery automatically.

### Performance Considerations

**Memory Usage:**
- Minimal: Only latest value + timestamp per sensor
- Typical: ~50 bytes per sensor × 10 sensors = 500 bytes total
- No historical data storage

**CPU Usage:**
- Sensor fusion: O(n) where n = sensors per room (typically 1-3)
- Called once per recompute cycle (60s) plus on-demand updates
- Negligible computational cost

**Update Frequency:**
- Driven by sensor update rate (typically 30-300 seconds)
- No artificial throttling or rate limiting
- Each update triggers full system recompute

---

## Scheduling System

### Overview

The `Scheduler` class (`scheduler.py`) is responsible for determining target temperatures based on time-based schedules, user overrides, and system modes. It implements a sophisticated precedence system that allows temporary overrides while maintaining underlying schedule logic for automatic resumption.

**Key Features:**
- Weekly schedule blocks with start/end times per room
- Default target for gaps between scheduled blocks
- Override mode with flexible parameters (absolute target OR relative delta, duration OR end time)
- Holiday mode (energy-saving override)
- Manual mode (constant user setpoint)
- Next schedule change calculation with gap detection

### Schedule Configuration

Schedules are defined per-room in `config/schedules.yaml`:

```yaml
rooms:
  - id: pete
    default_target: 14.0        # Used outside scheduled blocks
    week:
      mon:
        - start: "06:30"        # HH:MM format, 24-hour
          end: "07:00"          # Block end (exclusive)
          target: 17.0          # Target temp during block (°C)
        - start: "19:00"
          end: "21:00"
          target: 18.0
      tue:
        - start: "06:30"
          end: "07:00"
          target: 17.0
      wed: []                   # Empty array = use default_target all day
      # ... etc for thu, fri, sat, sun
```

**Configuration Rules:**
- **start/end times**: 24-hour format "HH:MM", start inclusive, end exclusive
- **Blocks**: Can overlap days if needed (e.g., "23:00" to "01:00")
- **Gaps**: Time between blocks uses `default_target`
- **Empty days**: `[]` means entire day uses `default_target`
- **Precision**: Targets rounded to room's configured precision (typically 1 decimal place)

**Schedule Block Behavior:**
- `start: "07:00", end: "09:00"` → active from 07:00:00 to 08:59:59
- Blocks ending at "23:59" are treated as "24:00" (midnight)
- Multiple blocks on same day must not overlap (validation occurs at load time)
- Blocks are checked in definition order (first match wins)

### Target Precedence Hierarchy

The `resolve_room_target()` method implements a strict precedence hierarchy:

```
Priority (highest to lowest):
1. OFF mode          → None (no heating)
2. MANUAL mode       → manual setpoint (ignores schedule/override)
3. OVERRIDE active   → override target (absolute, calculated from target or delta at creation)
4. SCHEDULE block    → block target for current time
5. DEFAULT           → default_target (gap between blocks)
6. HOLIDAY mode      → 15.0°C (if no schedule/override/manual)
```

**Precedence Examples:**

| Mode | Override | Schedule | Result |
|------|----------|----------|--------|
| Off | - | 18.0°C | **None** (off wins) |
| Manual | 21.0°C | 18.0°C | **20.0°C** (manual setpoint, ignores override) |
| Auto | 21.0°C | 18.0°C | **21.0°C** (override wins) |
| Auto | 20.0°C (from delta +2) | 18.0°C | **20.0°C** (override calculated from delta) |
| Auto | - | 18.0°C | **18.0°C** (scheduled block) |
| Auto | - | (gap) | **14.0°C** (default_target) |
| Auto | - | (gap, holiday) | **15.0°C** (holiday mode) |

**Key Behaviors:**
- **Off mode** always wins - even if override active
- **Manual mode** ignores ALL overrides and schedules
- **Override** is stored as absolute temperature (delta only used at creation time)
- **Holiday mode** only applies when no override active
- **Stale sensors** prevent heating EXCEPT in manual mode

### Schedule Resolution Algorithm

The `get_scheduled_target()` method determines the scheduled target for the current time:

```python
Algorithm:
1. Check holiday_mode → return 15.0°C immediately
2. Determine current day (0=Monday, 6=Sunday)
3. Get blocks for current day from week_schedule
4. Iterate through blocks:
   - Convert current time to "HH:MM" format
   - Check if current_time in [start, end)
   - If match found → return block['target']
5. No block matched → return default_target
```

**Time Comparison:**
- Uses string comparison: `"06:30" <= "07:15" < "09:00"` → True
- Works correctly for 24-hour format
- End time "23:59" normalized to "24:00" for midnight handling
- Simple and efficient (no datetime parsing per check)

**Gap Handling:**
```
06:00 ┌────────┐            ┌──────────┐
      │ 17.0°C │   14.0°C   │  18.0°C  │  14.0°C
      └────────┘ (default)  └──────────┘ (default)
      06:30 - 07:00         19:00 - 21:00
      
Timeline: [gap] → [block 1] → [gap] → [block 2] → [gap]
```

### Override Mode

**Unified temporary temperature override system** with flexible parameters:

#### Concept

Override is a single mechanism that allows temporary temperature adjustments in two ways:
1. **Absolute mode**: Set an explicit target temperature
2. **Delta mode**: Adjust by a relative amount from the current schedule

Both modes support flexible duration specification (relative minutes or absolute end time).

#### Helper Entities

```yaml
timer.pyheat_{room}_override            # Controls duration (absolute end time)
input_number.pyheat_{room}_override_target  # Stores absolute target temperature
```

**Note**: No metadata tracking needed - override type (absolute vs delta) is only used at creation time to calculate the absolute target.

#### Service Interface

**Single unified service with mutually exclusive parameter pairs:**

```python
service: pyheat.override
data:
  room: str                    # Required - room identifier
  
  # Temperature mode (exactly one required):
  target: float               # Absolute temperature (°C)
  delta: float                # Relative adjustment (°C, can be negative)
  
  # Duration mode (exactly one required):
  minutes: int                # Duration in minutes
  end_time: str               # ISO datetime string (e.g., "2025-11-10T17:30:00")
```

#### Examples

**Absolute temperature with relative duration:**
```python
service: pyheat.override
data:
  room: pete
  target: 21.0     # Set to exactly 21.0°C
  minutes: 120     # For 2 hours
```

**Delta adjustment with relative duration:**
```python
service: pyheat.override
data:
  room: pete
  delta: 2.0       # Increase by 2°C from current schedule
  minutes: 180     # For 3 hours
```

**Absolute temperature with absolute end time:**
```python
service: pyheat.override
data:
  room: pete
  target: 20.0                        # Set to 20°C
  end_time: "2025-11-10T23:00:00"    # Until 23:00 tonight
```

**Delta adjustment with absolute end time:**
```python
service: pyheat.override
data:
  room: pete
  delta: -1.5                         # Decrease by 1.5°C
  end_time: "2025-11-11T07:00:00"    # Until 7am tomorrow
```

#### Behavior

**Calculation at Service Call Time:**
- **Absolute mode** (`target`): Directly stores the provided temperature
- **Delta mode** (`delta`): 
  1. Reads current scheduled target (without any existing override)
  2. Calculates absolute target: `scheduled_target + delta`
  3. Stores the calculated absolute target
  4. Delta is NOT stored - it was only used for calculation

**Duration Handling:**
- **Relative** (`minutes`): Calculates absolute end time from current time + duration
- **Absolute** (`end_time`): Parses ISO datetime and validates it's in the future
- Timer is started with calculated duration in seconds

**Important**: The override target is calculated **once** at creation time and does not change:
- If schedule changes during an override, the override target stays constant
- Example: Set delta=+2°C at 13:00 (schedule: 18°C → override: 20°C)
  - At 14:00 schedule changes to 16°C
  - Override target remains 20°C (implied delta is now +4°C)
- This ensures user intent is preserved - they requested a specific temperature

**Cancellation:**
- Manual cancellation via `pyheat.cancel_override` service
- Automatic expiration when timer reaches end time
- On cancellation/expiration, system reverts to current scheduled target

**Validation Rules:**
- Exactly one of `target` or `delta` must be provided
- Exactly one of `minutes` or `end_time` must be provided
- Delta range: -10.0°C to +10.0°C
- Calculated target clamped to 10.0-35.0°C range
- Duration must be positive (minutes > 0)
- End time must be in the future

#### Status Display

Override status is formatted based on available information:

**In Home Assistant:**
```
Override: 20.0° (+2.0°) until 17:30
```
- Shows absolute target
- Shows calculated delta if scheduled temperature is known
- Shows end time from timer

**Without scheduled temp:**
```
Override: 20.0° until 17:30
```
- Just shows absolute target and end time

#### Timer Management

Home Assistant timer entity controls the override duration:

```python
# Timer started automatically by service
entity_id: timer.pyheat_{room}_override
duration: 7200  # seconds (calculated from minutes or end_time)

# Timer states
- "idle"     → no override active
- "active"   → counting down
- "paused"   → paused (still active)

# Timer expiration
- Timer transitions to "idle"
- Callback clears override_target (set to 0 sentinel)
- Triggers recompute → reverts to schedule
```

**Sentinel Value:**
- `override_target = 0` indicates cleared (entity min is 5°C)
- Checked in `resolve_room_target()`: `if override_target >= C.TARGET_MIN_C`

### Manual Mode

**Constant user-set temperature**, ignoring all schedules and overrides:

```yaml
Helper Entities:
  input_select.pyheat_{room}_mode           # "auto", "manual", "off"
  input_number.pyheat_{room}_manual_setpoint  # Manual target
```

**Behavior:**
- Reads `manual_setpoint` helper entity
- Returns setpoint rounded to room precision
- Ignores schedule, override, holiday mode
- **Exception:** Still requires valid temperature sensors (safety)
- Used for rooms needing constant temperature (e.g., nursery, office)

**Service Call:**
```python
service: pyheat.set_mode
data:
  room: pete
  mode: manual
  target: 19.5  # Optional, sets manual_setpoint
```

### Holiday Mode

**System-wide energy-saving mode**:

```yaml
Helper Entities:
  input_boolean.pyheat_holiday_mode  # System-wide toggle
```

**Behavior:**
- When enabled, returns `HOLIDAY_TARGET_C = 15.0°C` for all rooms in auto mode
- Overrides schedule blocks and default_target
- Does NOT override manual mode or override
- Useful when away from home for extended periods
- Can still use override to heat individual rooms temporarily

**Precedence:**
```
Manual mode > Override > Holiday mode > Schedule
```

### Next Schedule Change Calculation

The `get_next_schedule_change()` method calculates when the target will next change and to what value. This is **complex** because it must:

1. Detect gap starts/ends (transitions to/from default_target)
2. Skip blocks with same temperature as current
3. Handle day wraparound
4. Search up to 7 days ahead

**Algorithm Overview:**
```python
1. Get current target temperature
2. Track "scanning_target" as we move forward in time
3. For each future time point (block start/end, gaps, days):
   - If temperature different from scanning_target:
       → Return (time, new_target, day_offset)
   - Else: Update scanning_target and continue
4. If searched full week with no change:
   → Return None ("forever" - no change coming)
```

**Complex Scenarios:**

| Schedule | Current | Next Change | Result |
|----------|---------|-------------|--------|
| 18.0° until 23:00 (then 14.0°) | 18.0° | 23:00 (14.0°) | `("23:00", 14.0, 0)` |
| Block ends 10:30 → gap → block at 16:00 | 10.0° | 10:30 (14.0°) | `("10:30", 14.0, 0)` |
| Same temp all week | 18.0° | - | `None` (forever) |
| 18.0° until 07:00 tomorrow | 18.0° (at 22:00) | 07:00 (20.0°) | `("07:00", 20.0, 1)` |

**Return Format:**
```python
(time_string, target_temp, day_offset)
# time_string: "HH:MM" (24-hour)
# target_temp: Next target (°C)
# day_offset: 0=today, 1=tomorrow, 2=day after, etc.
```

**Used By:**
- Status publisher for "Auto: 18.0° until 23:00 (14.0°)" formatting
- Web UI for showing next temperature change
- Does NOT affect actual heating logic (only informational)

### Configuration Reloading

Schedules can be reloaded at runtime without restarting AppDaemon:

**Methods:**
1. **Automatic file monitoring**: Checks config files every 30 seconds
2. **Service call**: `pyheat.reload_config` forces immediate reload
3. **API endpoint**: `/pyheat_reload_config` (used by pyheat-web)
4. **Full schedule replacement**: `/pyheat_replace_schedules` (web editor)

**Reload Process:**
```python
1. config_loader.reload() reads schedules.yaml
2. Validates YAML structure and values
3. Updates self.config.schedules dict in-memory
4. Triggers immediate recompute
5. New schedule active immediately (no restart)
```

**Validation:**
- Block times must be valid "HH:MM" format
- start < end (unless spanning midnight)
- Targets within valid range (5-35°C)
- No overlapping blocks on same day
- Room IDs match rooms.yaml

**Error Handling:**
- Invalid schedule → logs error, keeps old schedule
- Syntax errors → detailed line number in log
- Service returns success/failure status

### Precision Handling

Target temperatures are rounded to room's configured precision:

```python
precision = room_config.get('precision', 1)  # Default: 1 decimal place
target = round(calculated_target, precision)
```

**Examples:**
- `precision: 0` → 18°C (whole numbers)
- `precision: 1` → 18.5°C (typical, matches most sensors)
- `precision: 2` → 18.25°C (high precision)

Applied at target resolution time, not in schedule config (schedules can have any precision).

### Performance Considerations

**Schedule Resolution:**
- O(n) where n = blocks per day (typically 2-4)
- String comparison only (no datetime parsing)
- Called once per recompute per room
- Negligible CPU cost

**Next Change Calculation:**
- O(blocks × days) worst case
- Typically O(blocks) for near-term changes
- Cached in status publication (not recalculated per query)
- Complex but infrequent (only for status display)

**Memory:**
- Full week schedule: ~500 bytes per room
- 10 rooms: ~5KB total
- Override types JSON: <1KB
- Minimal memory footprint

---

## Room Control Logic

### Overview

The `RoomController` class (`room_controller.py`) is the core decision-making engine that determines whether each room should heat and at what intensity. It implements sophisticated control algorithms with **asymmetric hysteresis** to prevent oscillation and **stepped valve bands** for proportional control without complex PID tuning.

**Key Responsibilities:**
- Coordinate sensor fusion, scheduling, and TRV control
- Determine call-for-heat status with hysteresis
- Calculate valve opening percentages using stepped bands
- Maintain per-room state across recompute cycles
- Handle startup initialization from existing valve positions

### Room State Management

Each room maintains stateful information used for hysteresis calculations:

```python
self.room_call_for_heat = {}   # {room_id: bool} - Current calling status
self.room_current_band = {}    # {room_id: 0-3} - Current valve band
self.room_last_valve = {}      # {room_id: 0-100} - Last commanded valve %
self.room_last_target = {}     # {room_id: float} - Previous target for change detection
```

**State Persistence:**
- State survives across recompute cycles (essential for hysteresis)
- Lost on AppDaemon restart, but restored from TRV valve positions and current targets
- Updated every recompute cycle (60s + event-driven)

**Initialization on Restart:**

Critical safety feature - on AppDaemon restart, rooms are initialized based on **current valve positions** and **current targets**:

```python
if fb_valve > 0:
    room_call_for_heat[room_id] = True  # Assume was calling

# Initialize target tracking to prevent false "changed" on first recompute
room_last_target[room_id] = current_target
```

**Why This Matters:**
- Prevents sudden valve closures in hysteresis deadband on restart
- Avoids boiler running with all valves closed after restart
- Maintains heating continuity during AppDaemon updates/restarts
- Rooms gradually transition to correct state rather than abrupt changes

### The compute_room() Pipeline

The main entry point that orchestrates all room-level logic:

```python
def compute_room(room_id: str, now: datetime) -> Dict:
    """Returns room state with heating decisions."""
```

**Processing Steps:**

```
1. Read room mode (auto/manual/off) from helper entity
2. Read holiday mode (system-wide) from helper entity
3. Get fused temperature from SensorManager
4. Resolve target temperature from Scheduler
5. Validate inputs:
   - If target is None → calling=False, valve=0%
   - If temp is None (sensors stale) → calling=False, valve=0%
   - Exception: Manual mode still requires valid temp
6. Calculate error = target - temp
7. Compute call-for-heat using hysteresis
8. Compute valve percentage using stepped bands
9. Return room state dict
```

**Return Value:**
```python
{
    'temp': 21.3,              # Current temperature (°C) or None
    'target': 22.0,            # Target temperature (°C) or None
    'is_stale': False,         # True if sensors stale
    'mode': 'auto',            # Room mode
    'calling': True,           # Whether room calls for heat
    'valve_percent': 65,       # Commanded valve opening (0-100)
    'error': 0.7,              # target - temp (°C)
    'manual_setpoint': None    # For manual mode status display
}
```

**Safety Checks:**
- No target → no heating (off mode or invalid schedule)
- Stale sensors → no heating (safety, prevents runaway)
- Manual mode with stale sensors → still no heating (safety override)

### Asymmetric Hysteresis (Call-for-Heat Decision)

Prevents rapid on/off cycling by using different thresholds for turning on vs. turning off:

**Configuration (per room):**
```yaml
hysteresis:
  on_delta_c: 0.30    # Start heating when 0.3°C below target
  off_delta_c: 0.10   # Stop heating when 0.1°C below target
```

**Target Change Detection:**
The hysteresis deadband is **bypassed when the target temperature changes** (override, schedule transition, or mode change). This ensures immediate response to user actions while preserving anti-flapping protection during temperature drift.

```python
# Constants for target change detection
TARGET_CHANGE_EPSILON = 0.01°C      # Floating-point tolerance
FRESH_DECISION_THRESHOLD = 0.05°C   # Min error to heat on target change
```

**Algorithm:**
```python
error = target - temp  # Positive = below target

# Check if target changed
if abs(target - prev_target) > TARGET_CHANGE_EPSILON:
    # Target changed → bypass deadband, make fresh decision
    return error >= FRESH_DECISION_THRESHOLD
else:
    # Target unchanged → use normal hysteresis
    if error >= on_delta:
        return True         # Clearly below target → start calling
    elif error <= off_delta:
        return False        # At or above target → stop calling
    else:
        return prev_calling  # In deadband → maintain previous state
```

**Graphical Representation:**
```
Temperature (relative to target)
    +1.0°C ─────────────────────────────────────
           │                                    
     0.0°C ├─────────────┐  OFF               
           │  DEADBAND   │  (stop calling)     
    -0.1°C ├─────────────┤ ← off_delta         
           │  DEADBAND   │  (keep prev state)  
           │             │  *BYPASSED if target changed*
    -0.3°C ├─────────────┤ ← on_delta          
           │             │  ON                  
    -1.0°C ─────────────────────────────────────

Transitions:
  • NOT calling + error ≥ 0.3°C → START calling
  • Calling + error ≤ 0.1°C → STOP calling  
  • Target changed + error ≥ 0.05°C → START calling (bypass deadband)  
  • In deadband (0.1-0.3°C) → NO CHANGE
```

**State Machine:**
```
┌──────────────┐  error ≥ 0.3°C   ┌──────────────┐
│ NOT CALLING  │ ─────────────────→│  CALLING     │
│ (valve 0%)   │                   │ (valve open) │
└──────────────┘ ←────────────────┘──────────────┘
                   error ≤ 0.1°C

Deadband (0.1-0.3°C): No state change
```

**Why Asymmetric?**
- Prevents "flapping" where heating turns on/off repeatedly
- Larger gap between on/off thresholds = more stability
- Room allowed to slightly overshoot target before stopping
- Trade-off: ±0.2°C accuracy for system stability

**Tuning Guidance:**
- **Tight control**: `on_delta=0.2`, `off_delta=0.1` (risks more cycling)
- **Default**: `on_delta=0.3`, `off_delta=0.1` (balanced)
- **Stable/slow**: `on_delta=0.4`, `off_delta=0.1` (very stable, less precise)
- **Rule**: Always maintain `on_delta ≥ off_delta + 0.1°C` for deadband

### Known Issue: Override Hysteresis Trap

**Problem:** When override is set with target only 0.1-0.3°C above current temp, and room was not previously calling, the room may fail to start heating immediately due to deadband logic maintaining previous state.

**Example:**
```
Current: 17.3°C, not calling
Set override: 17.5°C
Error: 0.2°C (in deadband)
Result: Does NOT call for heat (maintains prev_calling=False)
Expected: Should call for heat to reach override
```

**Workaround:** Set override at least 0.3°C above current temperature for immediate effect.

**See:** `docs/BUG_OVERRIDE_HYSTERESIS_TRAP.md` for full analysis and potential fixes.

### Stepped Valve Bands (Proportional Control)

Instead of simple on/off, valve opening is calculated using **4 discrete bands** based on temperature error:

**Configuration (per room):**
```yaml
valve_bands:
  t_low: 0.30           # Band threshold (°C below target)
  t_mid: 0.80
  t_max: 1.50
  low_percent: 35.0     # Valve opening for each band
  mid_percent: 65.0
  max_percent: 100.0
  step_hysteresis_c: 0.05  # Band transition hysteresis
```

**Band Mapping:**
```
Error (target - temp)     Band    Valve Opening
─────────────────────────────────────────────────
< 0.30°C                  0       0%     (not calling)
0.30 - 0.80°C             1       35%    (low heat)
0.80 - 1.50°C             2       65%    (medium heat)
≥ 1.50°C                  3       100%   (max heat)
```

**Visual Representation:**
```
Error (°C below target)
    3.0 ████████████████████████████ 100% (Band 3)
        │
    1.5 ├────────────────────────────── t_max threshold
        │
    2.0 ████████████████████ 65% (Band 2)
        │
    0.8 ├────────────────────────────── t_mid threshold
        │
    0.5 ███████ 35% (Band 1)
        │
    0.3 ├────────────────────────────── t_low threshold
        │
    0.1 ░░░░░░░ 0% (Band 0)
    0.0 ─────────────────────────────────
```

**Why Stepped Bands?**
- **Proportional response** without PID complexity
- **Fast response** to large errors (100% immediately)
- **Gentle approach** near target (35% prevents overshoot)
- **Simple tuning** (4 thresholds + 3 percentages)
- **Predictable behavior** (discrete states easier to debug)

### Band Transition Hysteresis

Band changes also use hysteresis to prevent rapid switching between bands:

**Algorithm:**
```python
# Determine target_band based on error (no hysteresis)
if error < t_low:      target_band = 0
elif error < t_mid:    target_band = 1
elif error < t_max:    target_band = 2
else:                  target_band = 3

# Apply hysteresis when changing bands
if target_band > current_band:
    # Increasing (need to cross threshold + hysteresis)
    if error >= threshold + step_hysteresis_c:
        new_band = target_band
        
elif target_band < current_band:
    # Decreasing (drop one band at a time)
    if error < threshold - step_hysteresis_c:
        new_band = current_band - 1  # Only drop one band
```

**Key Rules:**
1. **Increasing demand**: Must exceed threshold + 0.05°C to jump bands
2. **Decreasing demand**: Drop only ONE band at a time (gradual)
3. **Prevents oscillation**: 0.05°C hysteresis on each threshold

**Example Transition:**

```
Current band: 1 (35%), error = 0.75°C

Temperature drops, error increases to 0.86°C:
  • t_mid (0.80) + step_hyst (0.05) = 0.85°C
  • 0.86 > 0.85 → transition to band 2 (65%)

Temperature rises, error decreases to 0.74°C:
  • t_mid (0.80) - step_hyst (0.05) = 0.75°C
  • 0.74 < 0.75 → transition to band 1 (35%)
```

**Hysteresis Gap:** Each threshold has a 0.1°C gap (±0.05°C) where band won't change.

### Multi-Band Jump Optimization

**Special Case:** When error is very large, system can jump directly to max band:

```python
# Example: Room at 16.0°C, target 19.5°C
error = 3.5°C

# Without multi-band jump:
Band 0 → Band 1 (35%) → Band 2 (65%) → Band 3 (100%)
  Takes 3 recompute cycles to reach full heat

# With multi-band jump:
Band 0 → Band 3 (100%) immediately
  Reaches full heat in 1 cycle
```

**Implementation:**
- System calculates target_band based on current error
- If `target_band == 3` (error ≥ 1.5°C), jumps directly
- Provides fast response to large temperature errors
- Gradually reduces as temperature approaches target

### Valve Command Coordination

The `set_room_valve()` method bridges room logic with TRV controller:

```python
def set_room_valve(room_id: str, valve_percent: int, now: datetime):
    # Check for unexpected valve position (TRV manual change)
    if room_id in trvs.unexpected_valve_positions:
        # Force correction to expected position
        expected = trvs.unexpected_valve_positions[room_id]['expected']
        trvs.set_valve(room_id, expected, now, is_correction=True)
    else:
        # Normal valve command
        trvs.set_valve(room_id, valve_percent, now, is_correction=False)
```

**Handles:**
- Normal valve commands from room logic
- Corrections when TRV position changed manually
- Rate limiting (delegated to TRVController)
- Non-blocking feedback confirmation (delegated to TRVController)

### Interaction with Boiler Safety

Room controller does NOT directly command valves in all scenarios:

**Normal Operation:**
```python
# In app.py recompute_all():
for room_id in config.rooms:
    data = rooms.compute_room(room_id, now)  # Calculate valve %
    rooms.set_room_valve(room_id, data['valve_percent'], now)
```

**Pump Overrun / Boiler Safety:**
```python
# Boiler controller returns persisted_valves
if persisted_valves:
    # Safety takes priority - use persisted positions
    for room_id, valve_pct in persisted_valves.items():
        rooms.set_room_valve(room_id, valve_pct, now)
    
    # Normal calculations still run but may be overridden
    for room_id in other_rooms:
        rooms.set_room_valve(room_id, calculated_valve, now)
```

**Key Point:** Room controller computes desired valve position, but boiler safety logic can override it via valve persistence.

### Configuration Defaults vs. Overrides

All rooms inherit defaults from `constants.py` unless overridden in `rooms.yaml`:

**Defaults:**
```python
# constants.py
HYSTERESIS_DEFAULT = {
    "on_delta_c": 0.30,
    "off_delta_c": 0.10,
}

VALVE_BANDS_DEFAULT = {
    "t_low": 0.30,
    "t_mid": 0.80,
    "t_max": 1.50,
    "low_percent": 40,
    "mid_percent": 70,
    "max_percent": 100,
    "step_hysteresis_c": 0.05,
}
```

**Per-Room Override:**
```yaml
# rooms.yaml
rooms:
  - id: nursery
    hysteresis:
      on_delta_c: 0.20    # Tighter control for nursery
      off_delta_c: 0.10
    valve_bands:
      max_percent: 80     # Limit max heat
      # Other params inherit defaults
```

**Merging Logic:** Room-specific values override defaults, others inherited.

### Error Handling and Edge Cases

**No Target Temperature:**
```python
if target is None:
    calling = False
    valve_percent = 0
    # Room mode is "off" or schedule invalid
```

**Stale Sensors:**
```python
if temp is None and mode != "manual":
    calling = False
    valve_percent = 0
    # Safety: don't heat without temperature feedback
```

**Manual Mode with Stale Sensors:**
```python
if temp is None and mode == "manual":
    calling = False  # Safety override
    valve_percent = 0
    # Even manual mode requires sensor data
```

**Disabled Room:**
```python
if room_cfg.get('disabled'):
    # Skip processing entirely
    # No valve commands, no status updates
```

### Performance Characteristics

**Computation Cost:**
- O(1) per room (constant time operations)
- Hysteresis: 3 float comparisons
- Valve bands: ~10 float comparisons + 1 dict lookup
- Negligible CPU usage (<0.1ms per room)

**State Memory:**
- 3 dicts × 10 rooms × 20 bytes = ~600 bytes total
- Minimal memory footprint

**Execution Frequency:**
- Called every 60s (periodic recompute)
- Called on any relevant state change (sensors, modes, overrides)
- Typically 60-600 times per hour per room

**Latency:**
- Hysteresis response: 1-2 recompute cycles (60-120s)
- Band transitions: Immediate (next recompute)
- Overall: Heating system is slow by nature, latency is acceptable

---

## TRV Control

### Overview

The `TRVController` class (`trv_controller.py`) manages all interactions with Thermostatic Radiator Valves (TRVs), implementing sophisticated command/feedback logic with automatic retry, position verification, and setpoint locking. The system is designed specifically for **TRVZB valves** via Zigbee2MQTT.

**Key Features:**
- Non-blocking command execution with feedback confirmation
- Automatic retry on command failures (up to 3 attempts)
- Rate limiting to prevent excessive TRV commands
- Unexpected position detection and correction
- TRV setpoint locking at 35°C (bypasses internal control)
- Tolerance-based feedback matching

### TRV Entity Structure

Each TRV uses three Home Assistant entities:

```yaml
# Configuration in rooms.yaml
trv:
  entity_id: climate.trv_pete  # Base climate entity

# Derived entities (automatic):
cmd_valve:  number.trv_pete_valve_opening_degree        # Command entity
fb_valve:   sensor.trv_pete_valve_opening_degree_z2m   # Feedback sensor
climate:    climate.trv_pete                            # Setpoint control
```

**Entity Derivation:**
- Base pattern: `climate.{trv_base}` (e.g., `climate.trv_pete`)
- Extract `trv_base` from climate entity ID
- Apply patterns from `TRV_ENTITY_PATTERNS` constant
- All three entities must exist in Home Assistant

**Entity Purposes:**
- **cmd_valve**: Send valve opening commands (0-100%)
- **fb_valve**: Read actual valve position from TRV
- **climate**: Control TRV internal setpoint (locked to 35°C)

### TRV Setpoint Locking Strategy

**Problem:** TRVZB valves have two control modes:
- `valve_opening_degree` - used when TRV wants to open (room below setpoint)
- `valve_closing_degree` - used when TRV wants to close (room above setpoint)

**Original Approach Issues:**
- Needed TWO commands per valve change (opening + closing)
- Never knew which mode TRV was in
- Commands took 4 seconds per room (2s × 2 with blocking sleep)
- Used blocking `time.sleep()` that violated AppDaemon best practices

**Solution: Lock Setpoint to 35°C**

By forcing all TRV internal setpoints to **35°C** (maximum):
```python
TRV_LOCKED_SETPOINT_C = 35.0  # Well above any reasonable room temperature
```

**Result:**
- TRVs ALWAYS think room is "cold" (actual temp << 35°C setpoint)
- TRVs permanently in "opening" mode
- Only need to control `opening_degree` (not `closing_degree`)
- Single command per valve change (2s instead of 4s)
- Can use non-blocking AppDaemon scheduler

**Implementation Details:**

```python
# On startup (3s delay):
lock_all_trv_setpoints()

# Periodic check (every 5 minutes):
check_all_setpoints()

# On detected setpoint change (state listener):
lock_setpoint(room_id)
```

**Setpoint Locking Process:**
```python
1. Read current setpoint from climate.trv_{room}.temperature attribute
2. If setpoint ≠ 35.0°C:
   - Call climate.set_temperature(temperature=35.0)
   - Log correction at WARNING level
3. Else: Already locked, skip (DEBUG log)
```

**Robustness:**
- Automatic correction if user manually changes TRV setpoint
- Survives TRV power cycles and firmware resets
- Monitored by state listener for immediate correction
- Periodic checks every 5 minutes as backup

### Valve Command Flow (Non-Blocking)

Commands use an **asynchronous state machine** to avoid blocking the control loop:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. set_valve(room_id, percent, now)                         │
│    - Entry point from RoomController                         │
│    - Check rate limiting (min_interval_s)                    │
│    - Check if value changed (skip if same)                   │
│    - Check is_correction flag (bypass checks if correcting)  │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. _start_valve_command(room_id, percent, now)              │
│    - Cancel any existing command for this room              │
│    - Initialize command state dict:                          │
│      {room_id, target_percent, attempt=0, start_time, handle}│
│    - Call _execute_valve_command() immediately              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. _execute_valve_command(state_key)                        │
│    - Read command state from _valve_command_state dict      │
│    - Send number.set_value to cmd_valve entity              │
│    - Schedule _check_valve_feedback() in 2 seconds          │
│    - Store timer handle in state dict                        │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼ (after 2 second delay)
┌─────────────────────────────────────────────────────────────┐
│ 4. _check_valve_feedback(state_key)                         │
│    - Read actual valve position from fb_valve sensor        │
│    - Compare: abs(actual - target) <= tolerance (5%)        │
│    - If match: SUCCESS → update tracking, clean up state    │
│    - If mismatch and attempt < 3: RETRY → increment attempt │
│    - If mismatch and attempt == 3: FAIL → log error         │
└─────────────────────────────────────────────────────────────┘
```

**State Management:**
```python
_valve_command_state = {
    "valve_cmd_pete": {
        'room_id': 'pete',
        'target_percent': 65,
        'attempt': 1,
        'start_time': datetime(...),
        'handle': <timer_handle>
    }
}
```

**Key Features:**
- **Non-blocking**: Uses AppDaemon `run_in()` scheduler
- **Cancellable**: New commands cancel pending commands for same room
- **Retryable**: Automatic retry up to 3 times on feedback mismatch
- **Tolerant**: ±5% tolerance for feedback matching
- **Cleanup**: State dict cleaned up on success or final failure

### Rate Limiting

Prevents excessive TRV commands that can cause valve wear and communication issues:

**Configuration:**
```yaml
valve_update:
  min_interval_s: 30  # Minimum seconds between commands
```

**Implementation:**
```python
last_update = trv_last_update.get(room_id)
if last_update:
    elapsed = (now - last_update).total_seconds()
    if elapsed < min_interval_s:
        return  # Skip command, log at DEBUG level
```

**Tracking:**
```python
trv_last_update = {
    'pete': datetime(2025, 11, 10, 14, 30, 00),
    'lounge': datetime(2025, 11, 10, 14, 29, 45),
}
```

**Bypass:** Rate limiting skipped when `is_correction=True` (unexpected position corrections)

**Typical Behavior:**
- Normal operation: Valve changes every 1-10 minutes (driven by room logic)
- Rate limit rarely hit (30s is conservative)
- Protects against rapid oscillation if room logic misbehaves

### Feedback Confirmation and Retry Logic

**Tolerance Matching:**
```python
TRV_COMMAND_FEEDBACK_TOLERANCE = 5  # ±5%

if abs(actual_percent - target_percent) <= tolerance:
    # Success
else:
    # Mismatch - retry
```

**Why 5% Tolerance?**
- TRV valve positions not always exact (mechanical limitations)
- Zigbee2MQTT may round or interpolate values
- 5% = acceptable margin while detecting real failures
- Example: 65% target, 60-70% actual = OK

**Retry Strategy:**
```python
max_retries = 3  # Total attempts (initial + 2 retries)
retry_interval = 2  # Seconds between attempts

Attempt 1: Send command → wait 2s → check feedback
Attempt 2: Resend command → wait 2s → check feedback
Attempt 3: Resend command → wait 2s → check feedback
Final: Log ERROR if still mismatch, give up
```

**Failure Handling:**
- **Retry exhaustion**: Update tracking to actual value (accept reality)
- **Feedback unavailable**: Retry if attempts remain, else give up
- **Exception during check**: Clean up state, log error
- System continues operating (one failed valve doesn't halt heating)

### Unexpected Position Detection

Monitors for **manual TRV changes** or **TRV malfunctions**:

**Mechanism:**
```python
# Called on every TRV feedback sensor update
check_feedback_for_unexpected_position(room_id, feedback_percent, now, boiler_state)
```

**Detection Logic:**
```python
1. Skip if command in progress (expected to change)
2. Skip if boiler in PENDING_OFF or PUMP_OVERRUN (valve persistence active)
3. Compare feedback to last_commanded
4. If abs(feedback - commanded) > tolerance:
   - Flag as unexpected
   - Store: {actual, expected, detected_at}
   - Trigger immediate recompute
```

**Correction Flow:**
```python
# In set_room_valve():
if room_id in unexpected_valve_positions:
    expected = unexpected_valve_positions[room_id]['expected']
    set_valve(room_id, expected, now, is_correction=True)
```

**Use Cases:**
- User manually adjusts TRV using physical buttons
- TRV firmware glitch changes position
- Zigbee communication error causes position drift
- Power cycle causes TRV to reset to last known position

**Safety Integration:**
Critical check during pump overrun - do NOT trigger corrections when boiler is deliberately holding valves open:

```python
if boiler_state in (STATE_PENDING_OFF, STATE_PUMP_OVERRUN):
    return  # Valve persistence is expected
```

### Command State Tracking

Three dictionaries maintain TRV state:

```python
trv_last_commanded = {
    'pete': 65,      # Last successfully commanded position
    'lounge': 35,
}

trv_last_update = {
    'pete': datetime(2025, 11, 10, 14, 30, 00),  # Last command timestamp
    'lounge': datetime(2025, 11, 10, 14, 29, 45),
}

unexpected_valve_positions = {
    'abby': {
        'actual': 45,     # Position from feedback
        'expected': 65,   # Position we commanded
        'detected_at': datetime(2025, 11, 10, 14, 31, 00)
    }
}
```

**State Persistence:**
- Survives across recompute cycles (essential for tracking)
- Lost on AppDaemon restart, but restored from feedback sensors
- Updated only on successful command confirmation
- Cleaned up on correction or successful retry

### Initialization from Home Assistant

On AppDaemon startup, TRV state is restored:

```python
def initialize_from_ha():
    for room_id, room_cfg in config.rooms.items():
        fb_valve_entity = room_cfg['trv']['fb_valve']
        current_percent = int(float(get_state(fb_valve_entity)))
        trv_last_commanded[room_id] = current_percent
```

**Why Important:**
- Prevents unnecessary commands on restart
- Maintains rate limiting continuity
- Avoids unexpected position false positives
- Ensures smooth operation across AppDaemon restarts

### Entity Callbacks

**TRV Feedback Listener:**
```python
# Registered in app.py for each room
listen_state(trv_feedback_changed, trv['fb_valve'], room_id=room_id)

def trv_feedback_changed(entity, attribute, old, new, kwargs):
    feedback_percent = int(float(new))
    check_feedback_for_unexpected_position(room_id, feedback_percent, now, boiler_state)
    if unexpected position detected:
        trigger_recompute("trv_unexpected_position")
```

**TRV Setpoint Monitor:**
```python
# Monitors climate entity temperature attribute
listen_state(trv_setpoint_changed, trv['climate'], room_id=room_id, attribute='temperature')

def trv_setpoint_changed(entity, attribute, old, new, kwargs):
    if new != TRV_LOCKED_SETPOINT_C:
        log WARNING
        run_in(lambda: lock_setpoint(room_id), 1)  # Correct after 1s
```

**Periodic Checks:**
```python
# Every 5 minutes
run_every(check_trv_setpoints, "now+10", 300)
```

### Integration with Room Controller

Room controller calls TRV controller via thin wrapper:

```python
# In room_controller.py
def set_room_valve(room_id, valve_percent, now):
    if room_id in trvs.unexpected_valve_positions:
        # Correction path
        expected = trvs.unexpected_valve_positions[room_id]['expected']
        trvs.set_valve(room_id, expected, now, is_correction=True)
    else:
        # Normal path
        trvs.set_valve(room_id, valve_percent, now, is_correction=False)
```

**Key Points:**
- Room controller doesn't know about retry logic
- TRV controller handles all command complexity
- Corrections prioritized over new commands
- Rate limiting transparent to room controller

### Configuration Constants

**Timing:**
```python
TRV_COMMAND_RETRY_INTERVAL_S = 2    # Feedback check delay
TRV_COMMAND_MAX_RETRIES = 3         # Total attempts
TRV_SETPOINT_CHECK_INTERVAL_S = 300 # Periodic setpoint check
```

**Tolerances:**
```python
TRV_COMMAND_FEEDBACK_TOLERANCE = 5  # ±5% for position match
TRV_LOCKED_SETPOINT_C = 35.0        # Setpoint lock value
```

**Patterns:**
```python
TRV_ENTITY_PATTERNS = {
    "cmd_valve":  "number.{trv_base}_valve_opening_degree",
    "fb_valve":   "sensor.{trv_base}_valve_opening_degree_z2m",
    "climate":    "climate.{trv_base}",
}
```

### Error Handling

**Command Send Failure:**
```python
try:
    call_service("number/set_value", entity_id=cmd_valve, value=percent)
except Exception as e:
    log ERROR
    clean_up_state()
    # System continues, room valve command failed
```

**Feedback Check Failure:**
```python
try:
    actual_percent = int(float(get_state(fb_valve)))
except Exception as e:
    log ERROR
    clean_up_state()
    # Retry if attempts remain
```

**Invalid Configuration:**
```python
if not room_config or room_config.get('disabled'):
    return  # Skip silently
```

**Entity Missing:**
- Command fails (exception logged)
- System continues with other rooms
- No cascade failures

### Performance Characteristics

**Command Latency:**
- Command to feedback check: 2 seconds
- Successful single command: 2 seconds total
- With 1 retry: 4 seconds total
- With 2 retries: 6 seconds total
- Max time (3 retries, all fail): 6 seconds + failure handling

**Concurrency:**
- All room commands can run in parallel
- Non-blocking scheduler allows simultaneous feedback checks
- 10 rooms × 2 seconds = 2 seconds total (not 20 seconds)
- Independent state tracking per room

**Memory Usage:**
- Command state: ~200 bytes per active command
- Tracking dicts: ~50 bytes per room × 10 rooms = 500 bytes
- Minimal overhead

**Network Load:**
- One Zigbee command per valve change
- Typical: 1-5 commands per minute system-wide
- Retry overhead: Additional 1-2 commands per failure
- Rate limiting prevents command storms

### Compatibility Notes

**TRVZB-Specific:**
- Designed for Sonoff TRVZB valves
- Requires Zigbee2MQTT integration
- Entity patterns specific to z2m naming conventions

**Potential Adaptations:**
- Other TRV models may need different entity patterns
- Setpoint locking concept portable to similar TRVs
- Feedback confirmation logic generally applicable
- Rate limiting and retry logic generic

---

## Boiler Control

### Overview

The `BoilerController` class (`boiler_controller.py`) implements a sophisticated **6-state finite state machine (FSM)** that manages central boiler operation with comprehensive safety features. The system prevents dangerous conditions like no-flow heating, excessive cycling, and residual heat buildup.

**Critical Safety Features:**
- **Valve interlock**: Prevents boiler running without sufficient water flow
- **Anti-cycling protection**: Enforces minimum on/off times
- **TRV feedback confirmation**: Waits for valves to physically open
- **Pump overrun**: Keeps valves open after shutdown to dissipate heat
- **Safety room failsafe**: Emergency flow path if control logic fails
- **Off-delay grace period**: Prevents cycling from brief demand drops

**Design Philosophy:** Multiple layers of safety ensure the boiler never runs in a dangerous state, even if control logic fails.

### 6-State Finite State Machine

```
                      ┌─────────────────────────────────────┐
                      │          STATE_OFF                  │
                      │   Boiler off, no demand             │
                      └────────┬────────────────────────────┘
                               │ Demand + Interlock OK
                               │ (but TRVs not confirmed)
                               ▼
                      ┌─────────────────────────────────────┐
                      │       STATE_PENDING_ON              │
                      │   Waiting for TRV feedback          │
                      └────────┬────────────────────────────┘
                               │ TRV feedback confirmed
                               ▼
                      ┌─────────────────────────────────────┐
          ┌──────────▶│          STATE_ON                   │◀──────────┐
          │           │   Boiler actively heating            │           │
          │           └────────┬────────────────────────────┘           │
          │                    │ Demand ceased                          │
          │                    ▼                                         │
          │           ┌─────────────────────────────────────┐           │
          │           │      STATE_PENDING_OFF              │           │
          │           │   Off-delay timer active            │           │
          │           │   Boiler still ON, waiting          │           │
          │           └────────┬────────────────────────────┘           │
          │                    │ Off-delay elapsed                      │
          │                    ▼                                         │
          │           ┌─────────────────────────────────────┐           │
          │           │     STATE_PUMP_OVERRUN              │           │
          │           │   Boiler commanded OFF              │           │
          │           │   Valves forced open (persistence)  │           │
          │           └────────┬────────────────────────────┘           │
          │                    │ Pump overrun complete                  │
          │                    │ (back to OFF)                          │
          │                    │                                         │
          │                    └──────────────┐                         │
          │                                   ▼                         │
          │                          ┌─────────────────────────────────┐│
          │                          │      STATE_OFF                  ││
          │                          └─────────────────────────────────┘│
          │                                                              │
          │           ┌─────────────────────────────────────┐           │
          └───────────│   STATE_INTERLOCK_BLOCKED           │───────────┘
                      │   Insufficient valve opening         │
                      │   Cannot turn on (safety)            │
                      └─────────────────────────────────────┘

Special transitions (from any state):
  • Interlock failure while ON → immediate PUMP_OVERRUN
  • Demand returns during PUMP_OVERRUN → back to ON (if min_off_time elapsed)
  • Demand returns during PENDING_OFF → back to ON (cancels off-delay)
```

### State Descriptions

#### STATE_OFF
**Condition:** Boiler off, no heating demand from any room.

**Entry Actions:**
- None (boiler already off from previous state)

**While In State:**
- Monitor for heating demand
- Check valve interlock if demand appears
- Check TRV feedback readiness

**Exit Transitions:**
- Demand + Interlock OK + TRV not ready → **PENDING_ON**
- Demand + Interlock OK + TRV ready → **ON** (direct)
- Demand + Interlock failed → **INTERLOCK_BLOCKED**

**Restrictions:**
- Must wait for `min_off_time` timer to expire before turning on (anti-cycling)

#### STATE_PENDING_ON
**Condition:** Rooms calling for heat, interlock satisfied, but TRV feedback not yet confirmed.

**Entry Actions:**
- Log transition reason
- DO NOT turn boiler on yet (waiting for valves)

**While In State:**
- Monitor TRV feedback sensors
- Check for demand changes
- Recheck interlock continuously
- Log WARNING if stuck >5 minutes

**Exit Transitions:**
- TRV feedback confirmed → **ON**
- Demand ceased → **OFF**
- Interlock failed → **INTERLOCK_BLOCKED**

**Purpose:** Ensures valves are physically open before firing boiler (safety).

**Typical Duration:** 2-10 seconds (time for TRV commands to execute and feedback to update)

#### STATE_ON
**Condition:** Boiler actively heating, valves confirmed open, demand exists.

**Entry Actions:**
- `set_boiler_on()` - Set HVAC mode to 'heat' with 30°C setpoint
- Start `min_on_time` timer (180s)
- Log reason and room list

**While In State:**
- Continuously save valve positions (for pump overrun)
- Monitor interlock status
- Monitor demand from rooms
- Track time in state

**Exit Transitions:**
- Demand ceased → **PENDING_OFF** (enter off-delay)
- Interlock failed → **PUMP_OVERRUN** (emergency shutdown)

**Restrictions:**
- Must run for at least `min_on_time` (180s) before off-delay timer can complete
- Interlock failure causes immediate emergency shutdown

**Valve Position Saving:**
```python
# Continuously update while ON
if has_demand and all_valve_positions:
    boiler_last_valve_positions = all_valve_positions.copy()
```

This ensures we have recent positions for pump overrun even if demand suddenly drops.

#### STATE_PENDING_OFF
**Condition:** Demand ceased, waiting for off-delay timer before turning off. Boiler still physically ON.

**Entry Actions:**
- Start `off_delay_timer` (30s default)
- Preserve last valve positions
- Set `valves_must_stay_open = True` (critical!)
- Log transition

**While In State:**
- Keep valves at saved positions (valve persistence)
- Monitor for demand returning
- Wait for off-delay timer
- Check if `min_on_time` satisfied

**Exit Transitions:**
- Demand returns + interlock OK → **ON** (cancel off-delay)
- Off-delay elapsed + min_on_time satisfied → **PUMP_OVERRUN**
- Off-delay elapsed but min_on_time not satisfied → stay in PENDING_OFF

**Purpose:** 
- Grace period prevents cycling from brief demand drops
- User adjusting thermostat doesn't cause immediate off/on cycle
- Rooms briefly dipping below target don't cycle boiler

**Critical Safety:** Valves MUST stay open because boiler is still physically heating. Closing valves during PENDING_OFF would trap hot water in heat exchanger.

#### STATE_PUMP_OVERRUN
**Condition:** Boiler commanded off, pump running, valves forced open to dissipate residual heat.

**Entry Actions:**
- `set_boiler_off()` - Set HVAC mode to 'off'
- Start `pump_overrun_timer` (180s default)
- Start `min_off_time` timer (180s)
- Save valve positions to helper entity (survives restarts)
- Set `valves_must_stay_open = True`
- Cancel `min_on_time` timer

**While In State:**
- Keep valves at saved positions (valve persistence)
- Monitor for new demand
- Wait for pump overrun timer
- Enforce `min_off_time` if demand returns

**Exit Transitions:**
- Pump overrun complete → **OFF** (clear valve persistence)
- New demand + min_off_time elapsed → **ON** (early exit)
- New demand but min_off_time not elapsed → stay in PUMP_OVERRUN

**Purpose:**
- Allows boiler's internal pump to circulate water
- Dissipates residual heat from heat exchanger
- Prevents water hammer and thermal stress
- Protects boiler components from overheating

**Typical Duration:** 180 seconds (3 minutes)

**Why Valve Persistence:**
```
Without persistence:
  • Demand stops → valves close to 0%
  • Hot water trapped in heat exchanger
  • No flow path for pump
  • Potential damage from overheating

With persistence:
  • Demand stops → valves stay at last positions
  • Pump can circulate water
  • Heat dissipated safely
  • Boiler protected
```

#### STATE_INTERLOCK_BLOCKED
**Condition:** Heating demand exists but total valve opening insufficient for safe operation.

**Entry Actions:**
- Log interlock failure reason
- DO NOT turn boiler on (safety)

**While In State:**
- Monitor valve positions
- Monitor demand
- Log WARNING if blocked >5 minutes
- Wait for interlock to be satisfied

**Exit Transitions:**
- Interlock satisfied + TRV feedback ready + min_off_time elapsed → **ON**
- Demand ceased → **OFF**

**Purpose:** Prevents boiler running with insufficient water flow path (catastrophic damage prevention).

**Typical Causes:**
- All rooms using low valve percentages (<100% total)
- Configuration error (min_valve_open_percent too high)
- TRVs not responding to commands
- Rooms not calling for heat at sufficient intensity

**Example:**
```
3 rooms calling:
  • pete: 35% (band 1)
  • lounge: 35% (band 1)
  • abby: 0% (not calling)
  • Total: 70%

Min required: 100%
Result: INTERLOCK_BLOCKED (70 < 100)
```

### Valve Interlock System

**Purpose:** Ensure boiler never runs without sufficient water flow path.

**Configuration:**
```yaml
interlock:
  min_valve_open_percent: 100  # Total valve opening required
```

**Algorithm:**
```python
def _calculate_valve_persistence(rooms_calling, room_valve_percents):
    # Sum calculated valve percentages
    total_from_bands = sum(room_valve_percents[room] for room in rooms_calling)
    
    if total_from_bands >= min_valve_open:
        # Sufficient flow - use normal valve bands
        return room_valve_percents, True, "OK"
    
    # Insufficient - calculate persistence percentage
    n_rooms = len(rooms_calling)
    persist_percent = ceil(min_valve_open / n_rooms)  # Distribute evenly
    persist_percent = min(100, persist_percent)  # Clamp to 100%
    
    # Override ALL calling rooms to persistence percentage
    persisted = {room: persist_percent for room in rooms_calling}
    new_total = persist_percent * n_rooms
    
    return persisted, True, f"Persistence: {n_rooms}×{persist_percent}% = {new_total}%"
```

**Scenarios:**

| Rooms Calling | Valve Bands | Total | Min Required | Action |
|---------------|-------------|-------|--------------|--------|
| pete: 65%, lounge: 35% | - | 100% | 100% | ✅ Use bands (100 ≥ 100) |
| pete: 35%, lounge: 35%, abby: 35% | - | 105% | 100% | ✅ Use bands (105 ≥ 100) |
| pete: 35%, lounge: 35% | - | 70% | 100% | ⚠️ Persist: 2×50% = 100% |
| pete: 35% | - | 35% | 100% | ⚠️ Persist: 1×100% = 100% |

**Valve Persistence Priority:**
- Persisted valves override room controller valve bands
- Applied in app.py before sending valve commands
- Ensures safety even if room logic has bugs
- Logged at INFO level for visibility

**Emergency Shutdown:**
If interlock fails WHILE boiler is running:
```python
if boiler_state == STATE_ON and not interlock_ok:
    # CRITICAL: Turn off immediately
    transition_to(STATE_PUMP_OVERRUN, now)
    set_boiler_off()
    # Valves stay open for pump overrun
```

### TRV Feedback Confirmation

**Purpose:** Ensure TRV valves are physically open before firing boiler.

**Implementation:**
```python
def _check_trv_feedback_confirmed(rooms_calling, valve_persistence):
    for room_id in rooms_calling:
        commanded = valve_persistence[room_id]
        feedback = get_state(trv['fb_valve'])
        
        if abs(feedback - commanded) > TOLERANCE:
            return False  # Not confirmed
    
    return True  # All confirmed
```

**Checks:**
- All calling rooms must have TRV feedback matching commanded position
- Uses ±5% tolerance (same as TRV controller)
- Feedback sensor must be available (not 'unknown' or 'unavailable')
- Validates AFTER valve persistence is applied

**State Flow:**
```
OFF → (demand) → PENDING_ON → (feedback OK) → ON
                     ↓
                (waiting for valves)
                     ↓
                (timeout >5min: WARNING log)
```

**Protection:**
- Prevents firing boiler before valves open
- Avoids no-flow condition
- Provides time for TRV commands to execute (typically 2-10s)

### Anti-Cycling Protection

**Three-Layer Protection:**

1. **Minimum ON Time** (180s default)
   ```python
   # Boiler must run at least 3 minutes once started
   # Enforced by: min_on_time timer must expire before PENDING_OFF → PUMP_OVERRUN
   ```

2. **Minimum OFF Time** (180s default)
   ```python
   # Boiler must stay off at least 3 minutes after stopping
   # Enforced by: min_off_time timer must expire before OFF → ON transition
   ```

3. **Off-Delay Grace Period** (30s default)
   ```python
   # Waits 30 seconds after demand stops before turning off
   # Allows brief demand fluctuations without cycling
   ```

**Timer Management:**
```
Startup:
  OFF → ON: Start min_on_timer (180s)

Shutdown:
  ON → PENDING_OFF: Start off_delay_timer (30s)
  PENDING_OFF → PUMP_OVERRUN: Start min_off_timer (180s)

Checks:
  OFF → ON: Blocked if min_off_timer still active
  PENDING_OFF → PUMP_OVERRUN: Blocked if min_on_timer still active
```

**Example Timeline:**
```
00:00 - Demand appears
00:00 - ON (min_on_timer starts: 00:00-03:00)
01:30 - Demand ceases
01:30 - PENDING_OFF (off_delay: 01:30-02:00)
02:00 - off_delay expires, BUT min_on not satisfied (need to wait until 03:00)
02:00 - Stay in PENDING_OFF (waiting for min_on)
03:00 - min_on satisfied
03:00 - PUMP_OVERRUN (min_off_timer starts: 03:00-06:00)
03:00 - (pump_overrun_timer starts: 03:00-06:00)
04:30 - New demand appears, but min_off not satisfied
04:30 - Stay in PUMP_OVERRUN (blocked by min_off)
06:00 - Both timers expired
06:00 - ON (can start heating again)
```

**Configuration:**
```yaml
anti_cycling:
  min_on_time_s: 180   # 3 minutes minimum on
  min_off_time_s: 180  # 3 minutes minimum off
  off_delay_s: 30      # 30 second grace period
```

### Pump Overrun Management

**Purpose:** Dissipate residual heat from boiler heat exchanger after shutdown.

**Mechanism:**
```python
1. Before entering PUMP_OVERRUN:
   - Save all current valve positions: boiler_last_valve_positions

2. During PUMP_OVERRUN:
   - Boiler commanded off (HVAC mode = 'off')
   - Valves forced to stay at saved positions (valve persistence)
   - Pump continues running (inherent in boiler hardware)
   - Water circulates through open TRVs

3. After PUMP_OVERRUN (timer expires):
   - Clear valve persistence
   - Valves can close normally
   - Transition to STATE_OFF
```

**Valve Position Persistence:**
```python
# Saved during STATE_ON (continuously)
boiler_last_valve_positions = {
    'pete': 65,
    'lounge': 35,
    'abby': 100,
    'games': 0
}

# Applied during PUMP_OVERRUN and PENDING_OFF
persisted_valves = boiler_last_valve_positions.copy()
valves_must_stay_open = True
```

**Why This Works:**
- Boiler internal pump runs even in 'off' mode for short duration
- Open TRVs provide flow path for circulating water
- Heat transferred from heat exchanger to radiators
- Prevents water hammer and thermal stress
- Protects boiler longevity

**Persistence Across Restarts:**
```python
# Saved to Home Assistant helper entity
input_text.pyheat_pump_overrun_valves = "{\"pete\": 65, \"lounge\": 35}"

# Restored on AppDaemon restart
# Ensures pump overrun continues even if AppDaemon reloads
```

**Duration:** 180 seconds (3 minutes) default, configurable per boiler type.

### Safety Room Failsafe

**Purpose:** Emergency flow path if control logic fails and boiler is on with no demand.

**Configuration:**
```yaml
safety_room: games  # Dining room - centrally located
```

**Activation Condition:**
```python
if (boiler_is_heating() and 
    len(active_rooms) == 0 and 
    not in_pump_overrun_or_pending_off):
    # EMERGENCY!
    persisted_valves[safety_room] = 100
    log ERROR: "Boiler ON with no demand!"
```

**Why Needed:**
- Last-resort protection against control logic bugs
- Ensures hot water always has somewhere to go
- Prevents no-flow damage to boiler
- Rare activation (indicates software failure)

**Choice of Safety Room:**
- Centrally located (good flow path)
- Can handle unexpected heat
- Not a bedroom (won't disturb sleeping)
- Typically a common area (dining, living room)

### Integration with Room Control

**Valve Command Priority:**
```python
# In app.py recompute_all():
room_data = rooms.compute_room(room_id, now)  # Calculate desired valve

# Get boiler state and persistence
state, reason, persisted_valves, must_stay_open = boiler.update_state(...)

# Apply commands with persistence priority
if persisted_valves:
    for room_id, valve_pct in persisted_valves.items():
        rooms.set_room_valve(room_id, valve_pct, now)  # OVERRIDE
    
    for room_id in other_rooms:
        rooms.set_room_valve(room_id, calculated_valve, now)  # NORMAL
```

**Key Point:** Boiler safety logic can override room heating logic for safety.

### Boiler Control Commands

**Turn On:**
```python
call_service('climate/set_hvac_mode', entity_id=boiler, hvac_mode='heat')
call_service('climate/set_temperature', entity_id=boiler, temperature=30.0)
```

**Turn Off:**
```python
call_service('climate/set_hvac_mode', entity_id=boiler, hvac_mode='off')
```

**Why 30°C Setpoint?**
- Binary on/off control via setpoint manipulation
- Nest thermostat doesn't support true modulation
- 30°C > any reasonable room temp → always calls for heat
- 5°C < any room temp → never calls for heat
- Future: Full OpenTherm modulation (different approach)

**HVAC Action Monitoring:**
```python
hvac_action = boiler.attributes.hvac_action
# Values: 'heating', 'idle', 'off'
# Used for safety room failsafe detection
```

### Configuration

**Complete Example:**
```yaml
boiler:
  entity_id: climate.boiler
  
  binary_control:
    on_setpoint_c: 30.0   # Setpoint for "heat" mode
    off_setpoint_c: 5.0   # Not used (mode='off')
  
  pump_overrun_s: 180     # 3 minutes
  
  anti_cycling:
    min_on_time_s: 180    # 3 minutes
    min_off_time_s: 180   # 3 minutes
    off_delay_s: 30       # 30 seconds
  
  interlock:
    min_valve_open_percent: 100  # Sum of all valves
  
  safety_room: games      # Emergency flow path
```

### Performance Characteristics

**State Update Frequency:**
- Called every recompute cycle (60s minimum)
- Called on any relevant state change
- Typical: 60-600 times per hour

**Computation Cost:**
- O(n) where n = number of rooms
- Valve interlock calculation: sum of n values
- TRV feedback check: n entity reads
- Negligible CPU (<1ms per update)

**State Transitions:**
- Average: 2-4 transitions per heating cycle
- Typical cycle: OFF → PENDING_ON → ON → PENDING_OFF → PUMP_OVERRUN → OFF
- Duration: ~7-10 minutes per cycle

**Timer Overhead:**
- 4 Home Assistant timer entities
- Managed via HA services (non-blocking)
- State checked each recompute cycle
- Minimal overhead

### Error Handling

**Missing Boiler Entity:**
```python
if not boiler_entity:
    log ERROR: "No boiler entity configured"
    return STATE_OFF  # Safe default
```

**Timer Service Failure:**
```python
try:
    call_service("timer/start", entity_id=timer, duration=duration)
except Exception as e:
    log WARNING: "Failed to start timer"
    # Continue operation (timers are safety enhancements, not critical)
```

**TRV Feedback Unavailable:**
```python
if feedback in [None, 'unknown', 'unavailable']:
    # Stay in PENDING_ON until feedback available
    # Log WARNING if stuck >5 minutes
```

**Interlock Calculation Error:**
```python
try:
    total = sum(...)
except Exception as e:
    log ERROR
    # Assume interlock failed (safe default)
    return {}, False, "calculation error"
```

### Logging and Diagnostics

**State Transitions:**
```
INFO: Boiler: off → pending_on (demand and conditions met)
INFO: Boiler: pending_on → on (TRV feedback confirmed)
INFO: Boiler: on → pending_off (demand ceased, entering off-delay)
INFO: Boiler: pending_off → pump_overrun (off-delay elapsed, turning off)
INFO: Boiler: pump_overrun → off (pump overrun complete)
```

**Warnings:**
```
WARNING: Boiler has been waiting for TRV feedback for 6 minutes
WARNING: Boiler interlock has been blocked for 8 minutes
WARNING: Boiler: interlock failed while ON, turning off immediately
```

**Errors:**
```
ERROR: 🔴 CRITICAL: Boiler interlock failed while running!
ERROR: 🔴 EMERGENCY: Boiler ON with no demand! Forcing games valve to 100%
```

**Debug Information:**
```
DEBUG: Boiler: saved valve positions: {'pete': 65, 'lounge': 35}
DEBUG: Boiler: STATE_PENDING_OFF using saved positions: {...}
DEBUG: Boiler: total valve opening 120% >= min 100%, using valve bands
```

---

## Service Interface

### AppDaemon Services

PyHeat registers services with **AppDaemon** (not Home Assistant). These services are callable from Home Assistant automations and scripts using the `appdaemon` domain.

**Service Registration:**
```python
# In service_handler.py
ad.register_service("pyheat/override", svc_override)
ad.register_service("pyheat/cancel_override", svc_cancel_override)
# ... etc
```

**Available Services:**
- `appdaemon.pyheat_override` - Set temperature override (absolute target OR relative delta, duration OR end time)
- `appdaemon.pyheat_cancel_override` - Cancel active override
- `appdaemon.pyheat_set_mode` - Change room mode (auto/manual/off)
- `appdaemon.pyheat_set_default_target` - Update schedule default target
- `appdaemon.pyheat_reload_config` - Reload configuration files
- `appdaemon.pyheat_get_schedules` - Retrieve schedule configuration
- `appdaemon.pyheat_get_rooms` - Retrieve room configuration
- `appdaemon.pyheat_replace_schedules` - Replace entire schedule config

**Calling from Home Assistant:**
```yaml
# Absolute target override for 2 hours
service: appdaemon.pyheat_override
data:
  room: pete
  target: 21.0
  minutes: 120

# Relative delta override until 22:30
service: appdaemon.pyheat_override
data:
  room: pete
  delta: 2.0
  end_time: "22:30"
```

### Service Handler Implementation

The `ServiceHandler` class manages service registration and execution with parameter validation and error handling. Services are synchronous (blocking) to ensure state consistency.

---

## REST API

PyHeat exposes HTTP endpoints via AppDaemon's `register_endpoint()` mechanism for external access (primarily pyheat-web).

### Endpoints

All endpoints are registered in `api_handler.py`:

- **GET** `/api/appdaemon/pyheat_get_rooms` - Room configurations and current state
- **GET** `/api/appdaemon/pyheat_get_schedules` - Schedule data for all rooms
- **GET** `/api/appdaemon/pyheat_get_status` - System status (rooms, boiler, timers)
- **GET** `/api/appdaemon/pyheat_get_history` - Historical data (if enabled)
- **POST** `/api/appdaemon/pyheat_set_mode` - Change room mode
- **POST** `/api/appdaemon/pyheat_override` - Set override (target/delta, minutes/end_time)
- **POST** `/api/appdaemon/pyheat_cancel_override` - Cancel override
- **POST** `/api/appdaemon/pyheat_set_default_target` - Update default setpoint
- **POST** `/api/appdaemon/pyheat_replace_schedules` - Update schedule data
- **POST** `/api/appdaemon/pyheat_reload_config` - Reload YAML configurations

Endpoints bridge to internal service handlers (`ServiceHandler`) and return JSON responses. No authentication is required (handled by AppDaemon's HTTP layer and reverse proxy).

---

## Status Publication

PyHeat publishes system state to Home Assistant helper entities for display in dashboards and consumption by pyheat-web.

### Entity Updates

Per-room status is published to `input_text.pyheat_{room}_status` with attributes:
- `formatted_status` - Human-readable status text (see STATUS_FORMAT_SPEC.md)
- `current_temp` - Fused temperature
- `target_temp` - Resolved target
- `call_for_heat` - Boolean heating demand
- `valve_position` - Current commanded valve %
- `override_end_time` - ISO8601 timestamp (if active)
- `next_change` - Next schedule change timestamp
- `mode` - Current room mode

Boiler status published to `input_text.pyheat_boiler_status`:
- `state` - FSM state name
- `setpoint` - Commanded temperature
- `valve_interlock` - Total valve demand %
- `timer_state` - Active timer name

### Update Frequency

- **Immediate**: State changes trigger instant status update via `recompute_all()`
- **Periodic**: 60-second timer ensures status refresh even without state changes
- **Throttling**: No debouncing - each state change triggers one recompute

See `docs/STATUS_FORMAT_SPEC.md` for detailed formatting rules.

---

## Configuration Management

PyHeat uses YAML files for declarative configuration, loaded at startup and reloadable at runtime.

### Configuration Files

Located in `config/` directory:

**`rooms.yaml`** - Room definitions, sensors, TRV entities, hysteresis, valve bands
```yaml
rooms:
  - id: living_room
    name: "Living Room"
    trv:
      entity_id: climate.living_room_trvzb
    sensors:
      - entity_id: sensor.living_room_temp
        role: primary
```

**`schedules.yaml`** - Per-room weekly schedules with time blocks
```yaml
rooms:
  - id: living_room
    default_target: 16.0
    week:
      monday:
        - start: "07:00"
          setpoint: 20.0
```

**`boiler.yaml`** - Boiler entity, anti-cycling, valve interlock thresholds
```yaml
boiler:
  entity_id: climate.boiler
  anti_cycling:
    min_on_time_s: 180
    min_off_time_s: 300
```

### Validation and Reload

- **Startup**: `config_loader.py` loads and validates all YAML files
- **Runtime Reload**: `pyheat.reload_config` service re-reads files without restart
- **Change Detection**: Periodic check (30s) monitors file modification times
- **Error Handling**: Invalid YAML logs warning, previous config retained

Configuration changes trigger full `recompute_all()` to apply new settings.

---

## Event-Driven Architecture

PyHeat operates as an event-driven system using AppDaemon's state listeners and time triggers.

### Trigger Types

**State Listeners** (`listen_state()`) - Respond to Home Assistant entity changes:
- Temperature sensor updates → per-room recompute
- TRV feedback changes → valve tracking, boiler FSM update
- Helper entity changes (mode, setpoint, timers) → immediate recompute
- Master enable/holiday mode toggles → system-wide recalculation

**Time Triggers** (`run_every()`):
- 60s periodic recompute → ensures consistency even without state changes
- 60s TRV setpoint check → enforces 35°C locking
- 30s config file monitoring → hot-reload detection

**HTTP Requests** (`register_endpoint()`):
- External API calls from pyheat-web → service execution + recompute

### Callback Flow

All events funnel through `app.py` orchestrator:
1. Callback receives state change or timer tick
2. Updates internal state (sensor values, timers, config)
3. Calls `recompute_all()` to recalculate heating decisions
4. Modules process in order: sensors → scheduler → room_controller → trv_controller → boiler_controller
5. Commands issued to Home Assistant via service calls
6. Status published back to HA entities

**No debouncing** - Each state change triggers one full recompute. The 60-second interval is slow enough to prevent event storms. TRV commands use non-blocking execution to avoid blocking the event loop.

---

## Error Handling and Recovery

PyHeat implements graceful degradation for common failure scenarios.

### Sensor Failures

- **Staleness Detection**: Sensors older than `timeout_m` (default 10 min) ignored
- **Role Fallback**: If primary sensors stale, fallback sensors used
- **Total Failure**: If all sensors stale, room call-for-heat set `False` (safe default)
- **Recovery**: Fresh sensor data immediately re-enables heating decisions

### TRV Communication Failures

- **Feedback Timeout**: If valve feedback doesn't update within expected time, command retried
- **Setpoint Lock**: 35°C setpoint ensures manual valve changes don't conflict
- **Non-blocking Commands**: Valve commands don't block event loop, failure logged but system continues

### Boiler Safety

- **Valve Interlock**: Boiler won't turn on if total valve demand below threshold (protects against dry firing)
- **Feedback Confirmation**: Boiler waits for TRV feedback before entering FIRING state
- **Anti-cycling**: Min on/off timers prevent rapid cycling even on valve changes
- **Safe Default**: On error or restart, boiler defaults to OFF state

### Configuration Errors

- **Invalid YAML**: Parse errors logged, previous valid config retained
- **Missing Entities**: If helper entities don't exist, functionality degrades gracefully (logged warning)
- **Schedule Gaps**: Default target used when no schedule blocks match time

All errors logged via AppDaemon's logging facility (`self.ad.log()`) with appropriate severity levels.

---

## Integration with Home Assistant

### Entity Dependencies

PyHeat requires specific Home Assistant helper entities to be created. These are defined in `ha_yaml/pyheat_package.yaml`.

**Required Helpers:**
- `input_boolean.pyheat_master_enable` - Master system on/off
- `input_boolean.pyheat_holiday_mode` - Holiday mode toggle
- Per-room helpers (format: `pyheat_{room}_*`):
  - `input_select.pyheat_{room}_mode` - Room mode selection
  - `input_number.pyheat_{room}_manual_setpoint` - Manual temperature
  - `input_number.pyheat_{room}_override_target` - Override target
  - `timer.pyheat_{room}_override` - Override timer
- Boiler control timers:
  - `timer.pyheat_boiler_min_on_timer`
  - `timer.pyheat_boiler_min_off_timer`
  - `timer.pyheat_boiler_off_delay_timer`
  - `timer.pyheat_boiler_pump_overrun_timer`
- State persistence:
  - `input_text.pyheat_pump_overrun_valves` - Saved valve positions

### Home Assistant Services Consumed

PyHeat calls various Home Assistant services to control devices:

**Climate Control:**
- `climate.set_hvac_mode` - Turn boiler on/off
- `climate.set_temperature` - Set boiler setpoint

**Number Entities (TRV Valves):**
- `number.set_value` - Command TRV valve positions

**Helper Entity Updates:**
- `input_boolean.turn_on/turn_off` - Toggle boolean helpers
- `input_number.set_value` - Update numeric helpers
- `input_text.set_value` - Store JSON state
- `timer.start` - Start countdown timers
- `timer.cancel` - Cancel active timers

**State Queries:**
All entity states are read via AppDaemon's `get_state()` method which queries Home Assistant's state machine.

### State Management

**AppDaemon State Cache:**
- AppDaemon maintains a local cache of HA entity states
- Updates via WebSocket connection to Home Assistant
- State listeners trigger on changes (near real-time)

**PyHeat Internal State:**
- Room call-for-heat status (not persisted to HA)
- Valve band tracking (ephemeral)
- Boiler FSM state (ephemeral)
- TRV command state (non-blocking execution tracking)

**State Restoration on Restart:**
- TRV positions read from feedback sensors
- Room call-for-heat inferred from valve positions
- Boiler state defaults to OFF (safe restart)
- Configuration reloaded from YAML files

**Synchronization:**
- PyHeat writes to HA via service calls
- PyHeat reads from HA via state listeners and get_state()
- All heating decisions are one-way (PyHeat → HA)
- No feedback loops or circular dependencies

---

## Appendices

### Glossary

- **TRV** - Thermostatic Radiator Valve
- **FSM** - Finite State Machine
- **TRVZB** - Sonoff Zigbee TRV model
- **Hysteresis** - Intentional deadband to prevent oscillation
- **Setpoint Locking** - Fixing TRV internal target to bypass its control logic
- **AppDaemon** - Python automation framework for Home Assistant
- **Call-for-heat** - Boolean demand signal indicating room needs heating
- **Valve Interlock** - Safety mechanism preventing boiler operation without sufficient valve demand

### Related Documentation

- [README.md](../README.md) - Installation and setup
- [STATUS_FORMAT_SPEC.md](STATUS_FORMAT_SPEC.md) - Status attribute format
- [BOILER_SAFETY_STATUS.md](BOILER_SAFETY_STATUS.md) - Boiler control details
- [TRV_SETPOINT_LOCKING.md](TRV_SETPOINT_LOCKING.md) - TRV locking rationale
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) - Development roadmap
- [BUG_OVERRIDE_HYSTERESIS_TRAP.md](BUG_OVERRIDE_HYSTERESIS_TRAP.md) - Known issue documentation

### Configuration Examples

- [config/examples/](../config/examples/) - Sample YAML configuration files

---

**Document Version**: 1.0  
**Last Updated**: 2025-11-10  
**Author**: PyHeat Development Team
