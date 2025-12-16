# PyHeat - AppDaemon Heating Controller

Home heating control system for Home Assistant using AppDaemon.

## Overview

PyHeat provides multi-room heating control with:
- **Per-room temperature management** with individual schedules
- **Smart TRV control** via zigbee2mqtt (TRVZB devices)
- **Boiler management** with safety interlocks and anti-cycling
- **Short-cycling protection** via return temperature monitoring and setpoint manipulation
- **Setpoint ramping** - optional physics-aware headroom-based ramping to prevent premature flame-off
- **Load sharing** - intelligent multi-room heating to prevent boiler short-cycling
- **Sensor fusion** with staleness detection and optional EMA smoothing
- **Multiple control modes**: Auto (scheduled), Manual, Passive, and Off per room
- **Passive mode** - opportunistic heating without calling for heat (valves open when other rooms heating)
- **Override** functionality with flexible parameters (absolute/delta temp, duration/end time)
- **Holiday mode** for energy savings
- **Frost protection** - automatic emergency heating when rooms drop below safety threshold

## Architecture

### Key Components

- **app.py** - Main AppDaemon application orchestration
- **boiler_controller.py** - 6-state FSM boiler control with safety interlocks
- **cycling_protection.py** - Automatic short-cycling prevention via return temperature monitoring
- **setpoint_ramp.py** - Optional headroom-based setpoint ramping to reduce flame cycling
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
2. **Target Resolution**: Precedence: Off → Manual → Passive → Override → Schedule → Default
3. **Hysteresis**: Asymmetric deadband (on_delta: 0.30°C, off_delta: 0.10°C) prevents oscillation; bypassed on target changes for immediate override response
4. **Valve Control**: 3 stepped heating bands (Band 1: 40%, Band 2: 70%, Band Max: 100%) based on temperature error with hysteresis. Passive mode uses binary threshold control (open/closed based on max temp).
5. **Load Sharing**: Intelligently opens additional room valves when primary calling rooms have insufficient capacity to prevent boiler short-cycling. Uses three-tier cascade: schedule-aware pre-warming (60min) → extended lookahead (120min) → passive rooms + fallback priority list. Passive rooms participate with higher valve percentages (50-100%) while respecting temperature ceilings.
6. **TRV Setpoint Locking**: All TRVs locked to 35°C with immediate correction via state listener
7. **Boiler Control**: Full 6-state FSM with anti-cycling timers, TRV feedback validation, and pump overrun
8. **Short-Cycling Protection**: Dual-temperature overheat detection with automatic cooldown enforcement
   - **Detection**: Triggers on flame-off if flow temp ≥ setpoint+2°C (overheat) OR return temp ≥ setpoint-5°C (fallback)
   - **DHW filtering**: Ignores flame-offs during hot water demand (multi-sensor check with 60s history buffer)
   - **Cooldown enforcement**: Drops boiler setpoint to 30°C to prevent re-ignition
   - **Recovery**: Exits when max(flow, return) ≤ dynamic threshold (setpoint-15°C, min 45°C)
   - **Monitoring**: 10s interval checks with 30min timeout protection and excessive cycling alerts
9. **Setpoint Ramping** (optional): Physics-aware dynamic setpoint ramping to prevent short-cycling within heating cycles
   - **Algorithm**: Monitors headroom to shutoff (setpoint + hysteresis - flow), jumps setpoint when headroom ≤ buffer
   - **Configuration**: Enable via `input_boolean.pyheat_setpoint_ramp_enable`, configure buffer_c and setpoint_offset_c in `boiler.yaml`
   - **DHW detection**: Skips ramping during hot water events (prevents incorrect readings from DHW flow temps)
   - **Reset strategy**: On flame-OFF (DHW, cooldown, loss of demand), reset to user's desired baseline setpoint
   - **Coordination**: Works alongside cycling protection - uses baseline setpoint for cooldown recovery threshold
   - **Validation**: Enforces stability constraint (buffer + offset + 1 ≤ hysteresis) to prevent oscillation

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
   cp /opt/appdata/appdaemon/conf/apps/pyheat/config/ha_yaml/pyheat_package.yaml packages/pyheat_package.yaml
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

Each room has four modes:
- **Auto**: Follows the configured schedule
- **Manual**: Uses the manual setpoint (ignores schedule)
- **Passive**: Opens valve opportunistically without calling for heat
- **Off**: No heating, TRV closes

Set via `input_select.pyheat_{room}_mode`

### Manual Control

When in Manual mode, set temperature via:
`input_number.pyheat_{room}_manual_setpoint`

### Passive Mode

**Opportunistic heating** that opens valves when temperature is below a maximum threshold, but never calls for heat from the boiler. Useful for:
- Rooms that benefit from heat when other rooms are heating (hallways, bathrooms)
- Gentle morning pre-warming before scheduled active periods
- Maintaining minimum temperature without triggering boiler cycles

**How it works:**
1. Room never calls for heat (demand = 0W)
2. Valve opens to configured percentage when temp < max_temp
3. Valve closes when temp ≥ max_temp
4. Room benefits from heat circulation when other rooms are actively heating

**Configuration (for manual passive mode):**
- `input_number.pyheat_{room}_passive_mode_max_temp` - Maximum temperature ceiling (10-30°C, default 18°C)
  - Valve closes when room reaches this temperature
- `input_number.pyheat_{room}_passive_mode_min_temp` - Minimum temperature/comfort floor (8-20°C, default 8°C)
  - Room actively calls for heat if temperature drops below this threshold
  - This is the actual heating target in passive mode
- `input_number.pyheat_{room}_passive_mode_valve_percent` - Valve opening percentage (0-100%, default 30%)
  - Applied when temp is between min and max

Note: These entities are only used when the room mode selector is set to "passive". For scheduled passive blocks (in auto mode), use the schedule's `default_valve_percent`, `default_min_temp`, etc.

**Important Semantic Note:**
In passive mode, the "target" displayed in status and stored in `sensor.pyheat_{room}_target` is the minimum temperature (comfort floor), not the maximum. This reflects the actual heating target - the temperature below which the system will actively call for heat. The maximum temperature (valve-close threshold) is stored separately in `sensor.pyheat_{room}_passive_max_temp`. Both values have full state history for reliable graph visualization.

**Comfort Floor (Minimum Temperature):**
Optionally configure a comfort floor to prevent passive rooms from getting too cold:
- When temp drops below min_temp, room automatically switches to comfort mode
- Comfort mode: Calls for heat with 100% valve for rapid recovery
- Returns to normal passive behavior when temp recovers above min_temp
- Default is 8°C (equals frost protection) - set higher (e.g., 12-15°C) for comfort
- Uses same hysteresis as other modes (on_delta, off_delta)

**Three temperature zones:**
1. **Normal passive** (temp ≥ min_temp): No heat call, valve opens opportunistically
2. **Comfort mode** (frost_temp < temp < min_temp): Calls for heat, 100% valve
3. **Frost protection** (temp < frost_temp): Emergency heating (applies to all modes)

**Scheduled Passive Mode:**
Schedules can specify passive periods in auto mode:
```yaml
rooms:
  - id: bathroom
    week:
      mon:
        - start: "06:30"
          end: "08:00"
          mode: passive       # Passive period
          target: 18.0        # Max temp (not setpoint)
          valve_percent: 30   # Valve opening when below max
          min_target: 12.0    # Optional: Comfort floor (activates heating if temp drops below)
        - start: "08:00"
          end: "22:00"
          target: 19.0        # Active heating (mode defaults to active)
```

**min_target** field (optional):
- Specifies comfort floor for this passive block
- Takes precedence over `passive_mode_min_temp` entity when in auto mode
- Must be >= frost_protection_temp_c (8°C by default)
- Omit to use entity value or frost protection temp only

**Important:**
- Passive rooms participate in load sharing with valve override (50-100% vs normal 10-30%)
- Override always forces active heating (not passive)
- Passive valves count toward boiler interlock (contribute to system capacity)

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
2. Opens additional room valves using two-tier cascading strategy:
   - **Tier 1**: Rooms with schedules starting soon (schedule-aware pre-warming, 60 min default)
   - **Tier 2 Phase A**: Passive mode rooms (opportunistic heating with valve override)
   - **Tier 2 Phase B**: Fallback priority list for off-schedule periods
3. Persists until calling pattern changes (not arbitrary timers)

**Passive Room Participation:**
- Passive mode rooms participate in Tier 2 Phase A with valve override
- Normal passive: 10-30% valve (gentle opportunistic heating)
- Load sharing active: 50-100% valve (effective heat dumping)
- Respects max_temp ceiling (exits at max_temp + off_delta)
- Context matters: passive mode = "I want opportunistic heating", load sharing provides it effectively

**Control:**

`input_select.pyheat_load_sharing_mode` - Granular aggressiveness control:
- **Off**: Load sharing completely disabled
- **Conservative**: Tier 1 only (schedule pre-warming within lookahead window)
  - Less intrusive, good for spring/summer when cycling is rare
  - No emergency fallback (reduced cycling protection)
- **Balanced**: Tier 1 + Tier 2 Phase A (schedule pre-warming + passive rooms)
  - Includes passive rooms with opportunistic heating
  - Excludes fallback priority list (no surprise heating in off-schedule rooms)
  - Good balance of cycling protection and privacy
- **Aggressive**: All tiers (Tier 1 + Tier 2A + Tier 2B)
  - Maximum cycling protection with full fallback capability
  - Includes fallback priority list for emergency heat dumping
  - Recommended for winter heating season
  - Default mode

`sensor.pyheat_load_sharing_status` - Real-time status:
- State shows current tier and reason
- Attributes include active rooms with tier, valve percentage, and duration

Per-room load-sharing status visible in `sensor.pyheat_{room}_state` attributes

**Configuration:**
```yaml
# rooms.yaml - optional per-room tuning
rooms:
  - id: lounge
    load_sharing:
      schedule_lookahead_m: 60    # Check schedules within 60 min (default)
      fallback_priority: 1         # Lower = higher priority for fallback
  
  - id: bedroom
    load_sharing:
      schedule_lookahead_m: 30    # Conservative for bedrooms
      # Omit fallback_priority to exclude from Tier 3 (privacy)

# boiler.yaml - system thresholds
boiler:
  load_sharing:
    min_calling_capacity_w: 3500       # Activation threshold
    target_capacity_w: 4000            # Target to reach
    fallback_comfort_target_c: 20.0    # Tier 2 Phase B pre-warming target (default: 20°C)
```

**Bedroom Configuration Strategy:**
- **Include in Tier 1**: Set `schedule_lookahead_m` (typically 30 min for conservative pre-warming)
- **Exclude from Tier 2 Phase B**: Omit `fallback_priority` to prevent unexpected heating during off-schedule periods
- This ensures bedrooms only pre-warm when scheduled, respecting privacy and comfort preferences

See [docs/load_sharing_proposal.md](docs/load_sharing_proposal.md) for complete design details.

### Holiday Mode

Enable `input_boolean.pyheat_holiday_mode` to set all rooms to a low temperature (12°C default).

### Frost Protection

PyHeat includes automatic frost protection to prevent rooms from getting dangerously cold.

**How it works:**
- Global safety threshold (default 8°C) configured in `config/boiler.yaml`
- Activates automatically when room temperature drops below threshold
- Uses emergency heating (100% valve opening) for rapid recovery
- Returns to normal behavior once room is safe

**Important notes:**
- ⚠️ **Frost protection does NOT activate for rooms in "off" mode** - use "off" only when you are certain the room can safely remain unheated
- Frost protection is disabled when `master_enable` is off (system-wide kill switch)
- You will receive a log warning when frost protection activates

**Configuration:**
```yaml
# config/boiler.yaml
system:
  frost_protection_temp_c: 8.0  # Adjust if needed (5-15°C range)
```

**Recommended settings:**
- **6-7°C**: Pipes-only protection (minimal intervention)
- **8-10°C**: Balanced approach (standard UK/EU frost protection)
- **11-15°C**: Conservative (earlier activation for peace of mind)

**What happens during frost protection:**
1. Room drops below threshold (e.g., 7.7°C with default settings)
2. System overrides normal mode and calls for heat
3. Valve opens to 100% for maximum heating
4. Heating continues until temp recovers above threshold (e.g., 8.1°C)
5. System returns to normal mode behavior

**Status display:** When active, room status shows `"FROST PROTECTION: 7.5C -> 8.0C (emergency heating)"`

### Short-Cycling Protection (Cooldown)

**Automatic overheat detection and cooldown enforcement** to prevent boiler damage from short-cycling.

#### How It Works

**Detection (Dual-Temperature Approach):**
Cooldown triggers on any flame-off event if **EITHER** condition is met (OR logic):

1. **Flow Temperature Overheat** (Primary): `flow_temp >= setpoint + 2°C`
   - Detects actual overheat condition (flow exceeding target)
   - Example: With 55°C setpoint, triggers if flow ≥ 57°C
   - Catches boiler's internal overheat shutdowns

2. **High Return Temperature** (Fallback): `return_temp >= setpoint - 5°C`
   - Backup detection if flow sensor fails
   - Example: With 55°C setpoint, triggers if return ≥ 50°C
   - Maintains existing protection behavior

**CRITICAL:** Only ONE condition needs to be met to trigger cooldown (not both).

**Sensor Lag Compensation:**
- Flow temperature sensor reports flame-OFF with 4-6 second delay after physical flame extinction
- By the time sensor reports OFF, flow temp may have dropped below threshold
- **Solution:** Tracks flow temp history (last 12 seconds) with timestamps and corresponding setpoints
- Compares each historical flow temp to the setpoint active at that same moment
- Correctly handles setpoint ramping (dynamic setpoint changes during heating)
- Triggers cooldown if flow temp exceeded threshold at ANY point in recent history
- Example: Peak 54°C at T-5s with setpoint 50°C triggers cooldown even if current flow is 48°C

**DHW Filtering:**
- Ignores flame-offs during domestic hot water demand
- Uses quad-check: DHW binary sensor + flow rate sensor, at flame-off and 2s later
- Includes 12-second history buffer to catch fast DHW events
- Prevents false positives from DHW interruptions

**Cooldown Enforcement:**
- Drops boiler setpoint to 30°C (prevents re-ignition)
- Saves original setpoint for restoration
- Records event in history for excessive cycling detection
- Monitors recovery every 10 seconds

**Recovery Exit:**
Cooldown ends when **BOTH** temperatures are safe (AND logic):
- Check: `max(flow_temp, return_temp) <= threshold`
- Threshold: `max(setpoint - 15°C, 45°C)` (dynamic, based on saved setpoint)
- Example: With 55°C setpoint, exits when both flow and return ≤ 45°C
- **CRITICAL:** BOTH temperatures must be safe to exit (opposite of entry logic)
- Restores original setpoint automatically

**Safety Features:**
- 30-minute timeout with forced recovery (alerts user)
- Excessive cycling detection (alerts if >3 cooldowns in 60 min)
- Persists state across AppDaemon restarts

#### Configuration

**Constants (in `constants.py`):**
```python
# Detection thresholds
CYCLING_FLOW_OVERHEAT_MARGIN_C = 2   # Flow must exceed setpoint by 2°C
CYCLING_HIGH_RETURN_DELTA_C = 5      # Return threshold: setpoint - 5°C

# Sensor lag compensation
CYCLING_FLOW_TEMP_LOOKBACK_S = 12    # Check last 12s for peak flow temp
CYCLING_FLOW_TEMP_HISTORY_BUFFER_SIZE = 50  # Buffer size for history tracking

# Cooldown behavior
CYCLING_COOLDOWN_SETPOINT = 30       # Setpoint during cooldown
CYCLING_RECOVERY_DELTA_C = 15        # Recovery threshold: setpoint - 15°C
CYCLING_RECOVERY_MIN_C = 45          # Absolute minimum recovery temp
```

**Tuning Guidelines:**
- `CYCLING_FLOW_OVERHEAT_MARGIN_C`: Lower (1°C) = more sensitive, Higher (3°C) = fewer triggers
- If seeing false positives: Increase flow margin or tighten return threshold
- If missing events: Check logs for flow/return temps at flame-off

#### Monitoring

**Home Assistant Entities:**
- `sensor.pyheat_cooldowns` - Total cooldown events (total_increasing)
- `sensor.pyheat_status` attributes:
  - `cycling_protection.state` - Current state (NORMAL/COOLDOWN/TIMEOUT)
  - `cycling_protection.cooldown_start` - Timestamp of current cooldown
  - `cycling_protection.cooldowns_last_hour` - Recent event count

**Log Messages:**
- `INFO` - Flame-off analysis with temp readings and threshold checks
- `WARNING` - Cooldown entry with trigger reason
- `INFO` - Cooldown exit with duration
- `ERROR` - Timeout or excessive cycling alerts

**What to Watch For:**
- Frequent cooldowns (>3/hour) suggest low radiator capacity or setpoint too high
- Long cooldown durations (>10 min) suggest recovery threshold may need adjustment
- Timeout events indicate system cooling too slowly

### Master Enable/Disable

Toggle `input_boolean.pyheat_master_enable` to turn entire system on/off.

## Monitoring

### Status Entity

`sensor.pyheat_status` provides real-time system status:
- **State**: "idle" or "heating (N rooms)" with contextual breakdown
  - Shows calling rooms, passive rooms, and load-sharing rooms separately
  - Example: "heating (3 active, 2 passive, +1 pre-warming)"
- **Attributes**:
  - Per-room details (mode, temp, target, call_for_heat)
  - Boiler state and reason
  - Total valve percentage
  - Last recompute timestamp
  - Load sharing status and active rooms
  - Cycling protection state

### Binary Sensors

`binary_sensor.pyheat_calling_for_heat` - System-wide heating demand:
- **State**: `on` when any room is calling for heat, `off` otherwise
- **Attributes**:
  - `active_rooms`: List of room IDs currently calling
  - `room_count`: Number of rooms calling

`binary_sensor.pyheat_cooldown_active` - Cycling protection cooldown:
- **State**: `on` when boiler is in cooldown mode (flame off, protecting against short-cycling)
- **Attributes** (when active):
  - `cooldown_start`: ISO timestamp when cooldown started
  - `saved_setpoint`: Setpoint that will be restored after cooldown
  - `recovery_threshold`: Temperature threshold for recovery

### Counters

`sensor.pyheat_cooldowns` - Cumulative cooldown events counter:
- Increments each time cycling protection triggers a cooldown
- Uses `state_class: total_increasing` for HA statistics integration
- Useful for monitoring boiler cycling patterns over time

### Room State Entities

`sensor.pyheat_{room}_state` - Per-room state with structured format:
- **State**: Structured string showing mode, load-sharing, calling status, and valve percentage
  - Example: "auto (active), LS off, calling, 100%"
  - Example: "auto (passive), LS T1, not calling, 65%"
- **Attributes**:
  - `load_sharing`: Load-sharing tier ("off", "T1", "T2") - convenience attribute
  - `valve`: Valve percentage (0-100) - convenience attribute
  - `passive_low`: Minimum temperature in passive mode (comfort floor) - null in other modes
  - `calling`: Boolean calling status - convenience attribute
  - Plus all standard room attributes (temp, target, mode, etc.)

`sensor.pyheat_{room}_target` - Current target temperature:
- In passive mode, shows the comfort floor (min_temp) - the actual heating target
- See `sensor.pyheat_{room}_passive_max_temp` for the upper limit in passive mode

`sensor.pyheat_{room}_passive_max_temp` - Passive mode upper limit:
- Shows the temperature at which valves close in passive mode
- Only populated when room is in passive mode
- Both min and max stored as entity states for reliable history tracking

### Boiler State Entity

`sensor.pyheat_boiler_state` - Dedicated boiler state tracking:
- **State**: Current boiler FSM state: `on`, `off`, `pending_on`, `pending_off`, `pump_overrun`, `interlock`
- Creates clean history entries for timeline visualization
- Used by pyheat-web for boiler timeline and graph shading

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
- `cooldown_active` - Boolean indicating if cycling protection cooldown is active
- `calling_count` - Number of rooms naturally calling for heat
- `passive_count` - Number of passive rooms receiving heat (valve open, not calling)
- `load_sharing_schedule_count` - Number of rooms in load-sharing schedule tiers (pre-warming)
- `load_sharing_fallback_count` - Number of rooms in load-sharing fallback tier
- `total_heating_count` - Total rooms receiving heat (sum of all categories)
- `load_sharing` - Load sharing status object (null if feature disabled):
  - `state` - Current state: "disabled", "inactive", "tier1_active", "tier2_active"
  - `active_rooms` - Array of rooms currently in load sharing with tier, valve_pct, reason, and duration_s
  - `trigger_capacity` - Capacity that triggered load sharing (kW)
  - `trigger_rooms` - Array of room IDs that triggered load sharing
  - `master_enabled` - Whether load sharing is enabled in configuration
  - `mode` - Current mode: "Off", "Conservative", "Balanced", or "Aggressive"
  - `decision_explanation` - Human-readable explanation of current load sharing decision

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
  ],
  "passive_min": [
    {"time": "2025-12-04T10:00:00+00:00", "value": 16.0}
  ],
  "passive_max": [
    {"time": "2025-12-04T10:00:00+00:00", "value": 19.0}
  ],
  "valve": [
    {"time": "2025-12-04T10:00:00+00:00", "valve_percent": 30}
  ],
  "load_sharing": [
    {"time": "2025-12-04T10:00:00+00:00", "load_sharing_active": true, "tier": 1, "valve_pct": 25, "reason": "schedule_30m"}
  ],
  "system_heating": [
    {"time": "2025-12-04T10:00:00+00:00", "system_heating": true}
  ]
}
```

Note: `passive_min` and `passive_max` arrays are only populated when room is in passive mode. These enable graph visualization of passive temperature ranges.

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
