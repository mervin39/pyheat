# PyHeat Architecture

## Introduction

PyHeat is a multi-room heating control system built on AppDaemon for Home Assistant. The system manages per-room temperature control through TRV (Thermostatic Radiator Valve) management, boiler state control with safety interlocks, and flexible scheduling capabilities.

The architecture is modular and event-driven, with clear separation of concerns across specialized controllers. Each module has a specific responsibility within the heating control pipeline, from sensor fusion through to physical valve commands.

**Key Design Principles:**
- **Rooms as first-class objects** - Each room maintains its own state, sensors, schedule, and heating decisions
- **Event-driven coordination** - Controllers respond to Home Assistant state changes and time triggers
- **Safety-first boiler control** - Multi-state FSM with anti-cycling protection and failure interlocks
- **Deterministic target resolution** - Clear precedence hierarchy for manual/override/scheduled control
- **Hysteresis throughout** - Prevents oscillation in heating decisions, valve commands, and boiler cycling

---

## Project Structure

PyHeat is organized into logical subdirectories for improved maintainability:

```
pyheat/
├── app.py                          # Main orchestrator and entry point
├── config/                         # Configuration files
│   ├── rooms.yaml
│   ├── schedules.yaml
│   └── boiler.yaml
├── controllers/                    # Hardware control modules
│   ├── boiler_controller.py        # Boiler FSM and safety logic
│   ├── cycling_protection.py       # Short-cycling prevention
│   ├── room_controller.py          # Per-room heating decisions
│   ├── setpoint_ramp.py            # Dynamic setpoint ramping
│   ├── trv_controller.py           # TRV valve management
│   └── valve_coordinator.py        # Multi-room valve orchestration
├── managers/                       # State and monitoring managers
│   ├── alert_manager.py            # Alert tracking and notifications
│   ├── load_calculator.py          # Radiator capacity estimation (EN 442)
│   ├── load_sharing_manager.py     # Intelligent load balancing
│   ├── load_sharing_state.py       # Load sharing state machine
│   ├── override_manager.py         # Override state management
│   └── sensor_manager.py           # Sensor fusion and staleness
├── core/                           # Core utilities
│   ├── config_loader.py            # Configuration loading and validation
│   ├── constants.py                # System-wide constants
│   └── scheduler.py                # Target temperature resolution
├── services/                       # External interfaces
│   ├── api_handler.py              # HTTP API endpoints
│   ├── heating_logger.py           # CSV logging
│   ├── service_handler.py          # AppDaemon service registration
│   └── status_publisher.py         # Home Assistant entity publishing
└── docs/                           # Documentation
```

AppDaemon automatically discovers all Python files in subdirectories, allowing for clean organization without complex import paths.

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
│    ├─ Apply EMA smoothing (optional, per-room configurable)                  │
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
│    3. Passive mode      → passive_min to passive_max range                   │
│    4. Override active   → override_target (absolute temp, from target/delta) │
│    5. Schedule block    → block target for current time/day                  │
│    6. Default           → default_target (outside schedule blocks)           │
│    7. Holiday mode      → 15.0°C (energy saving)                             │
│  Return: {target, mode: 'active'|'passive', valve_percent, min_target}       │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   ROOM HEATING LOGIC (room_controller.py)                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  For each room:                                                               │
│    ├─ Calculate error = target - current_temp                                │
│    ├─ PASSIVE MODE: Threshold control with comfort floor                     │
│    │    • Comfort mode (temp < min_temp): Calls for heat, 100% valve         │
│    │    • Normal passive (temp >= min_temp): No heat call, opportunistic     │
│    │    • Valve opens if error > on_delta (temp < max_temp - on_delta)       │
│    │    • Valve closes if error < -off_delta (temp > max_temp + off_delta)   │
│    │    • Dead band maintains previous valve state (prevents cycling)        │
│    ├─ ACTIVE MODE: Call-for-heat decision (asymmetric hysteresis):           │
│    │    • error ≥ on_delta (0.30°C)  → start calling                         │
│    │    • error ≤ off_delta (0.10°C) → stop calling                          │
│    │    • Between deltas             → maintain previous state               │
│    ├─ ACTIVE MODE: Valve percentage (stepped bands with hysteresis):         │
│    │    • Band 0 (0%):  Not calling (room satisfied)                         │
│    │    • Band 1 (40%): error < 0.30°C  (gentle heating)                     │
│    │    • Band 2 (70%): 0.30°C ≤ error < 0.80°C  (moderate)                  │
│    │    • Band Max (100%): error ≥ 0.80°C  (maximum heating)                 │
│    │    • Band transitions require step_hysteresis (0.05°C) crossing         │
│    └─ Return: {temp, target, calling, valve_percent, error, mode, operating_mode, │
│                passive_min_temp, comfort_mode, frost_protection}             │
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
│    │    OFF → PENDING_ON → ON                                                 │
│    │    ON → PENDING_OFF → PUMP_OVERRUN → OFF                                │
│    │    (Any) → INTERLOCK_BLOCKED if insufficient valve opening              │
│    ├─ Pump overrun handling:                                                 │
│    │    • Save valve positions when demand ceases                            │
│    │    • Keep valves open for 180s after boiler off                         │
│    │    • Ensures residual heat circulation                                  │
│    ├─ Update ValveCoordinator with persistence overrides                     │
│    └─ Return: (state, reason, persisted_valves{}, must_stay_open)            │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  LOAD SHARING (load_sharing_manager.py)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│  Evaluate load sharing needs and select additional rooms:                    │
│    ├─ Entry conditions:                                                      │
│    │    • Low capacity (calling capacity < 3500W)                            │
│    │    • Cycling risk evidence (recent cooldown OR high return temp)        │
│    ├─ Two-tier cascading selection with one-room-at-a-time escalation:       │
│    │    • Schedule tier: Rooms with upcoming schedule (2x lookahead window)  │
│    │    • Fallback tier: Passive rooms + priority list (when schedules fail) │
│    ├─ Exit conditions:                                                       │
│    │    • Original calling rooms stopped (Trigger A)                         │
│    │    • New room joined with sufficient capacity (Trigger B)               │
│    │    • Load sharing room naturally calling (Trigger C)                    │
│    │    • Fallback room timeout (Trigger D)                                  │
│    │    • Room reached target temperature (Trigger E)                        │
│    │    • Room mode changed from auto (Trigger F)                            │
│    │    • Minimum activation: 5 minutes (prevents oscillation)               │
│    └─ Return: {room_id: valve_percent} for load sharing rooms                │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                VALVE COORDINATION (valve_coordinator.py)                     │
├─────────────────────────────────────────────────────────────────────────────┤
│  Single authority for final valve command decisions:                         │
│    ├─ Receive persistence overrides from boiler controller                   │
│    ├─ Receive load sharing overrides from load sharing manager               │
│    ├─ Apply priority logic for each room:                                    │
│    │    1. Persistence overrides (safety: pump overrun, interlock)           │
│    │    2. Load sharing overrides (intelligent load balancing)               │
│    │    3. Correction overrides (unexpected TRV positions)                   │
│    │    4. Normal desired values (from room heating logic)                   │
│    ├─ Pass persistence_active flag to TRV controller                         │
│    └─ Send final valve commands with all overrides applied                   │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   STATUS PUBLICATION (status_publisher.py)                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Publish state to Home Assistant entities:                                   │
│    ├─ Per-room state entities (sensor.pyheat_{room}_state):                  │
│    │    • State: structured string for reliable HA history tracking          │
│    │      Format: "$mode, $load_sharing, $calling, $valve"                   │
│    │      Examples: "auto (active), LS off, not calling, 0%"                 │
│    │                "auto (passive), LS T1, calling, 100%"                   │
│    │    • Every field change creates new history entry (graph shading)       │
│    │    • Attributes: target, mode, operating_mode, calling, valve_percent   │
│    │    • passive_min_temp, passive_max_temp (when in passive mode)         │
│    │    • formatted_status with schedule/override information                │
│    │    • next_change, override_end_time (for UI countdowns)                 │
│    ├─ Boiler state entity (sensor.pyheat_boiler_state):                      │
│    │    • State: on, off, pending_on, pending_off, pump_overrun, interlock   │
│    │    • Updates only when boiler state changes (clean history)             │
│    │    • Used by pyheat-web for graph shading (system_heating detection)    │
│    │    • Also used by api_get_boiler_history() for boiler timeline          │
│    ├─ System status (sensor.pyheat_status):                                  │
│    │    • State: "heating", "idle", or "master_off"                          │
│    │    • Attributes: boiler_state, calling_rooms[], all room_data           │
│    │    • Room data includes operating_mode for passive detection            │
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

                       ┌─────────────────────────────────────────┐
                       │  CYCLING PROTECTION (cycling_protection.py) │
                       ├─────────────────────────────────────────┤
                       │  Flame OFF event monitoring:            │
                       │    • 2s delay for sensor stabilization  │
                       │    • Triple-check DHW detection:        │
                       │      - Binary + flow rate sensors       │
                       │      - Captured + current states        │
                       │    • High return temp check             │
                       │  Cooldown logic:                        │
                       │    • Drop setpoint to 30°C              │
                       │    • Monitor recovery every 10s         │
                       │    • Restore when threshold reached     │
                       │  3-state FSM: NORMAL/COOLDOWN/TIMEOUT   │
                       └─────────────────────────────────────────┘
```

**Key Flow Characteristics:**

1. **Event-Driven**: Any state change (sensor, mode, timer, manual) triggers immediate recompute
2. **Periodic Baseline**: 60-second timer ensures regular evaluation even without events
3. **Synchronous Processing**: Recompute runs synchronously to prevent race conditions
4. **Safety Priority**: Boiler safety logic can override room heating decisions via valve persistence
5. **Stateful**: Previous states (calling, band) maintained for hysteresis calculations
6. **Non-Blocking**: TRV commands use async feedback confirmation to avoid blocking control loop

---

## Timer Handling

### Overview

PyHeat uses Home Assistant timer entities for various time-based operations, including room temperature overrides and boiler FSM state management. The system employs a **hybrid event + polling approach** that combines immediate event-driven responses with a safety-net polling mechanism for maximum reliability.

**Timer Types:**
- **Room Override Timers** (6 timers, one per room): `timer.pyheat_{room}_override`
- **Boiler Min On Timer**: `timer.pyheat_boiler_min_on_timer` (FSM state enforcement)
- **Boiler Min Off Timer**: `timer.pyheat_boiler_min_off_timer` (FSM state enforcement)
- **Boiler Off Delay Timer**: `timer.pyheat_boiler_off_delay_timer` (graceful shutdown delay)
- **Pump Overrun Timer**: `timer.pyheat_boiler_pump_overrun_timer` (post-heating circulation)

### Event-Driven Approach (Primary Mechanism)

PyHeat registers event listeners for all timer entities during initialization (`app.py` lines 302-338):

```python
# Room override timers
for room_id in room_ids:
    timer_entity = f"timer.pyheat_{room_id}_override"
    self.listen_event(self.timer_finished, "timer.finished", entity_id=timer_entity)
    self.listen_event(self.timer_cancelled, "timer.cancelled", entity_id=timer_entity)

# Boiler FSM timers
for timer_name in ["min_on", "min_off", "off_delay", "pump_overrun"]:
    timer_entity = f"timer.pyheat_boiler_{timer_name}_timer"
    self.listen_event(self.timer_finished, "timer.finished", entity_id=timer_entity)
    self.listen_event(self.timer_cancelled, "timer.cancelled", entity_id=timer_entity)
```

**Event Handlers:**

When Home Assistant fires a `timer.finished` or `timer.cancelled` event, PyHeat immediately responds:

```python
def timer_finished(self, event_name, data, kwargs):
    """Handle timer.finished events from Home Assistant"""
    entity_id = data.get("entity_id", "unknown")
    self.ad.log(f"Timer finished event received: {entity_id}", level="DEBUG")
    self.trigger_recompute(reason=f"timer_finished:{entity_id}")

def timer_cancelled(self, event_name, data, kwargs):
    """Handle timer.cancelled events from Home Assistant"""
    entity_id = data.get("entity_id", "unknown")
    self.ad.log(f"Timer cancelled event received: {entity_id}", level="DEBUG")
    self.trigger_recompute(reason=f"timer_cancelled:{entity_id}")
```

Both events trigger an immediate recompute cycle with a reason string that identifies the specific timer for debugging.

**Benefits:**
- **Immediate response**: No polling delay (0s latency vs up to 60s with polling alone)
- **Efficient**: Only runs recompute when timers actually change state
- **Traceable**: Reason strings show exact timer that triggered recompute in logs

### State Polling (Safety Net)

PyHeat maintains existing state listeners for backward compatibility and as a safety net:

```python
# room_timer_changed state listener (backup mechanism)
self.listen_state(self.room_timer_changed, override_timer)
```

The `room_timer_changed()` handler also triggers a recompute when timer entity state changes.

**Why Keep Polling?**
- **AppDaemon restart safety**: When AppDaemon restarts with expired timers, events are not re-fired but state reflects the expired state
- **Missed events**: Network issues or HA restart could theoretically cause missed events
- **No downside**: `recompute_all()` is idempotent - redundant triggers are harmless

**Restart Behavior:**
When AppDaemon restarts while a timer is expired:
1. Event listeners register at startup (no retroactive events fired)
2. Periodic recompute (60s timer) polls all entity states
3. Expired timer detected via state check within 3-13 seconds (first periodic + stagger)
4. Recompute processes expired timer (override cleared or FSM state transition)

**Maximum delay on restart:** 3-13 seconds (acceptable for temperature control)

### Implementation Details

**Initialization Sequence (app.py initialize() method):**
1. Load room and boiler configurations
2. Register event listeners for `timer.finished` and `timer.cancelled` on all 10 timer entities
3. Register state listeners for room override timers (backward compatibility)
4. Log summary: "Registered timer events for N room override timers" + "Registered timer events for N boiler FSM timers"

**Event Listener Registration:**
- Uses AppDaemon's `listen_event()` with entity_id filter
- Separate listeners for finished and cancelled events (different semantics)
- Filters ensure only relevant timer events trigger handlers

**Reason Strings:**
All timer events include the entity_id in the recompute reason:
- `timer_finished:timer.pyheat_pete_override`
- `timer_cancelled:timer.pyheat_boiler_min_off_timer`

This enables precise debugging via `sensor.pyheat_status` last_recompute_reason attribute.

**Idempotency:**
Multiple mechanisms (events + state polling) can trigger recomputes for the same timer state change. This is safe because:
- `recompute_all()` reads current state and makes decisions based on that state
- No cumulative effects - each recompute is a fresh evaluation
- Duplicate recomputes waste CPU cycles but produce identical results

### Trade-offs Analysis

**Option 1: Polling Only** (Previous Implementation)
- ✅ Simple, no event registration needed
- ✅ Catches expired timers after restart
- ❌ 0-60s delay (poor UX for override cancellation)
- ❌ Wastes CPU on regular polling

**Option 2: Events Only**
- ✅ Immediate response (0s latency)
- ✅ Efficient (only runs on actual changes)
- ❌ Restart gap: expired timers not detected until first periodic recompute
- ❌ Vulnerable to missed events

**Option 3: Hybrid (Current Implementation)** ✅
- ✅ Immediate response via events (0s latency)
- ✅ Safety net via periodic polling (3-13s restart delay)
- ✅ No downside due to idempotency
- ⚠️ Slightly more complex (two mechanisms)

**Conclusion:** Option 3 provides best user experience (immediate response) with maximum reliability (safety net). The complexity cost is minimal (two event registrations per timer + preserved state listeners).

---

### Core Components

- **app.py** - Main AppDaemon application and orchestration
- **sensor_manager.py** - Temperature sensor fusion, EMA smoothing, and staleness detection
- **scheduler.py** - Schedule parsing and time-based target calculation
- **override_manager.py** - Temperature override management (timers and targets)
- **load_calculator.py** - Radiator capacity estimation using EN 442 thermal model
- **load_sharing_manager.py** - Intelligent load balancing to prevent short-cycling
- **load_sharing_state.py** - State machine infrastructure for load sharing
- **room_controller.py** - Per-room heating logic and target resolution
- **valve_coordinator.py** - Single authority for valve command decisions with priority handling
- **trv_controller.py** - TRV valve commands and setpoint locking
- **boiler_controller.py** - 6-state FSM boiler control with safety interlocks
- **cycling_protection.py** - Automatic short-cycling prevention via dual-temperature monitoring (flow + return) with sensor lag compensation using 12-second flow temp history tracking
- **setpoint_ramp.py** - Dynamic setpoint ramping to prevent short-cycling during heating
- **alert_manager.py** - Error tracking and Home Assistant persistent notifications
- **service_handler.py** - Home Assistant service registration and handling
- **status_publisher.py** - Entity creation and status publication to HA
- **config_loader.py** - YAML configuration validation and loading
- **api_handler.py** - REST API endpoints for external control
- **constants.py** - System-wide configuration defaults

---

## Temperature Sensing and Fusion

### Overview

The `SensorManager` class (`sensor_manager.py`) implements a robust sensor fusion system that combines multiple temperature sensors per room with staleness detection, role-based prioritization, and exponential moving average (EMA) smoothing. This provides reliable and stable temperature readings even when individual sensors fail or when multiple sensors report slightly different values.

**Key Features:**
- Multiple sensors per room with primary/fallback roles
- Automatic averaging of available sensors
- EMA (Exponential Moving Average) smoothing applied to fused temperatures
- Smoothing used for BOTH display AND control decisions (consistent behavior)
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
      - entity_id: climate.trv_pete
        role: fallback
        timeout_m: 180
        temperature_attribute: current_temperature  # Read from attribute instead of state
```

**Configuration Parameters:**
- `entity_id`: Home Assistant sensor entity (must report numeric temperature in °C)
- `role`: Either `"primary"` or `"fallback"` (determines priority)
- `timeout_m`: Staleness timeout in minutes (default: 180 minutes / 3 hours)
- `temperature_attribute`: (optional) Attribute name to read temperature from instead of entity state
  - Use this for entities that expose temperature as an attribute rather than as state
  - Common use case: Climate entities with `current_temperature` attribute (TRV internal sensors)
  - If not specified, reads from entity state (default behavior)

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

### Temperature Smoothing (EMA)

For rooms with multiple sensors (especially sensors in different locations), the raw averaged temperature can fluctuate as individual sensors report slightly different readings. This is particularly noticeable when sensors are intentionally placed in different spots to capture room-wide temperature gradients.

**EMA Smoothing** (Exponential Moving Average) reduces these fluctuations by blending the new fused temperature reading with the historical smoothed value:

```python
smoothed = alpha * raw_fused + (1 - alpha) * previous_smoothed
```

**Configuration** (per-room in `rooms.yaml`):
```yaml
rooms:
  - id: lounge
    name: Lounge
    smoothing:
      enabled: true
      alpha: 0.3    # 30% new reading, 70% historical (default: 0.3)
    sensors:
      - entity_id: sensor.lounge_xiaomi_temp    # Cool side of room (~16.0°C)
        role: primary
      - entity_id: sensor.lounge_awair_temp     # Warm side of room (~16.9°C)
        role: primary
```

**Smoothing Parameters:**
- `alpha`: Weighting factor (0.0 to 1.0)
  - Lower alpha (e.g., 0.1-0.3): More smoothing, slower response to changes
  - Higher alpha (e.g., 0.5-0.9): Less smoothing, faster response to changes
  - Default: 0.3 (recommended for most multi-sensor rooms)
  - Alpha = 0.3 → 95% response time ≈ 10 sensor updates

**Behavior:**
- Smoothing is applied AFTER sensor fusion (averaging)
- Smoothing state is initialized on first reading (no historical value)
- Smoothing is applied consistently to both:
  - Real-time display updates (`sensor.pyheat_<room>_temperature`)
  - Control logic (deadband checks, heating decisions)
- When smoothing is disabled, raw fused temperature is used directly
- Smoothing state persists across sensor updates but resets on AppDaemon restart

**When to Use:**
- **Enable for multi-sensor rooms** where sensors are in different locations
- Reduces visible "bouncing" in temperature displays (e.g., 16.4 ↔ 16.5 ↔ 16.6)
- Prevents unnecessary heating state changes near temperature boundaries
- Particularly useful with sensors that have different calibration or response times

**When NOT to Use:**
- Single-sensor rooms (no benefit, adds unnecessary lag)
- Rooms requiring rapid response to temperature changes
- Sensors that are co-located (already naturally stable)

### Sensor Change Deadband Optimization

To prevent unnecessary recomputes when sensor readings hover around display rounding boundaries, PyHeat implements a **sensor change deadband**. This optimization significantly reduces CPU usage and log noise without affecting heating control accuracy.

**Problem Without Deadband:**
```
Sensor reports: 16.94°C → rounds to 16.9°C
Sensor reports: 16.96°C → rounds to 17.0°C
Sensor reports: 16.94°C → rounds to 16.9°C
Sensor reports: 16.96°C → rounds to 17.0°C

Result: Triggers recompute on every update even though
        temperature hasn't meaningfully changed
```

**Solution - Deadband Filter:**
```python
# In app.py:sensor_changed()
# Deadband: Only recompute if change exceeds half a display unit
if old_rounded is not None:
    deadband = 0.5 * (10 ** -precision)  # 0.05°C for precision=1
    temp_delta = abs(new_rounded - old_rounded)
    
    if temp_delta < deadband:
        return  # Skip recompute
```

**Configuration:**
- Deadband threshold is **automatic** based on room precision setting
- `precision=1` (0.1°C display) → 0.05°C deadband
- `precision=2` (0.01°C display) → 0.005°C deadband
- No manual configuration needed

**How It Works:**
1. Sensor update received with new raw temperature
2. Temperature rounded to room's display precision
3. Compare rounded value to last published rounded value
4. If difference < 0.05°C (for precision=1), skip recompute
5. If difference ≥ 0.05°C, proceed with recompute

**Example Timeline:**
```
Time    Raw Temp    Rounded    Published    Action
─────────────────────────────────────────────────────
10:00   16.94°C     16.9°C     16.9°C       Recompute (initial)
10:01   16.96°C     17.0°C     16.9°C       Skip (0.1°C < 0.05°C? No → Recompute)
10:02   16.95°C     17.0°C     17.0°C       Skip (0.0°C < 0.05°C)
10:03   16.93°C     16.9°C     17.0°C       Recompute (0.1°C change)
10:04   16.92°C     16.9°C     16.9°C       Skip (0.0°C < 0.05°C)
```

**Interaction with EMA Smoothing:**

When EMA smoothing is enabled, the deadband check uses the **smoothed** temperature:
```python
# Flow with smoothing enabled:
1. Raw sensor value received
2. Sensor fusion (averaging)
3. EMA smoothing applied
4. Smoothed temp rounded to precision
5. Deadband check against last published smoothed temp
6. If deadband exceeded → recompute
```

This provides **double filtering**:
- EMA smoothing reduces high-frequency fluctuations in raw readings
- Deadband prevents recomputes from display rounding artifacts

**Impact on Heating Control:**

The deadband is **safe for heating control** because:
- Boiler hysteresis (on_delta=0.30°C) >> deadband (0.05°C)
- A 0.05°C temperature change cannot flip heating decisions
- Periodic recompute (60s) ensures system responsiveness
- Manual overrides and schedule changes bypass deadband

**Performance Benefits:**
- Reduces unnecessary recomputes by 80-90%
- Significantly decreases log volume
- Lower CPU usage in high-sensor environments
- No impact on heating quality or responsiveness

**Why 0.5 × Precision?**
- Half of display unit ensures visible changes always trigger recompute
- Sensor hovering at 16.95°C (rounds to 17.0°C or 16.9°C) won't cause flapping
- Strikes balance between responsiveness and efficiency
- Conservative enough to never mask meaningful temperature changes

**Edge Cases:**
- **First sensor update:** No old value, always triggers recompute
- **Sensor becomes available:** State changes from unknown/unavailable, triggers recompute
- **Precision change:** Deadband automatically adjusts to new precision
- **EMA disabled:** Deadband still applies to raw fused temperature

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

The `Scheduler` class (`scheduler.py`) is responsible for determining target temperatures based on time-based schedules, user overrides, and system modes. It implements a precedence system that allows temporary overrides while maintaining underlying schedule logic for automatic resumption.

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
2. MANUAL mode       → manual setpoint (ignores schedule/override) - active heating
3. PASSIVE mode      → passive_min to passive_max range (opportunistic heating)
                        - passive_min_temp: comfort floor (triggers active if breached)
                        - passive_max_temp: upper limit (valve closes when reached)
4. OVERRIDE active   → override target (absolute, calculated from target or delta) - active heating
5. SCHEDULE block    → block target for current time (may specify active or passive)
6. DEFAULT           → default_target (gap between blocks) - active heating
7. HOLIDAY mode      → 15.0°C (if no schedule/override/manual) - active heating
```

**Precedence Examples:**

| Mode | Override | Schedule | Result |
|------|----------|----------|--------|
| Off | - | 18.0°C | **None** (off wins) |
| Manual | 21.0°C | 18.0°C | **20.0°C** (manual setpoint, active, ignores override) |
| Passive | 21.0°C | 18.0°C | **16-19°C range** (passive_min to passive_max, ignores override/schedule) |
| Auto | 21.0°C | 18.0°C | **21.0°C** (override wins, active) |
| Auto | 20.0°C (from delta +2) | 18.0°C | **20.0°C** (override calculated from delta, active) |
| Auto | - | 18.0°C | **18.0°C** (scheduled block, mode from schedule) |
| Auto | - | (gap) | **14.0°C** (default_target, active) |
| Auto | - | (gap, holiday) | **15.0°C** (holiday mode, active) |

**Key Behaviors:**
- **Off mode** always wins - even if override active
- **Manual mode** ignores ALL overrides and schedules, uses active heating to setpoint
- **Passive mode** ignores ALL overrides and schedules, uses passive range (min_temp to max_temp)
  - passive_min_temp = comfort floor (triggers active heating if breached)
  - passive_max_temp = upper limit (valve closes when reached)
- **Override** is stored as absolute temperature (delta only used at creation time), always uses active heating
- **Schedule blocks** can specify `mode: 'passive'` for scheduled passive periods
- **Holiday mode** only applies when no override active
- **Stale sensors** prevent heating EXCEPT in manual mode

**Passive Mode Details:**
- Passive mode has two sub-modes: normal passive and comfort mode
- **Normal passive mode** (temp ≥ min_temp):
  - Never calls for heat (calling = False)
  - Uses same hysteresis deltas (on_delta/off_delta) as active mode for consistency
  - Valve opens to configured percentage when temp < max_temp - on_delta
  - Valve closes when temp > max_temp + off_delta
  - Dead band between thresholds maintains previous valve state (prevents cycling)
  - No PID control (fixed valve percentage, not proportional to error)
  - Useful for opportunistic heating when other rooms call for heat
  - Excluded from load sharing (user has manual valve control)
- **Comfort mode** (frost_temp < temp < min_temp):
  - Activated when temp drops below passive_min_temp (comfort floor)
  - Calls for heat (calling = True) with 100% valve for rapid recovery
  - Uses same hysteresis as normal modes for consistency
  - Returns to normal passive mode when temp > min_temp + off_delta
  - Prevents passive rooms from getting uncomfortably cold
  - Default min_temp is 8°C (equals frost_protection_temp_c)
  - Users can configure higher comfort floor (e.g., 12-15°C) via entities or schedules
- **Frost protection** (temp < frost_temp):
  - Emergency heating applies to all modes (see Frost Protection section)
  - Provides safety floor below comfort mode

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

---

## Override Management

### Overview

The `OverrideManager` class (`override_manager.py`) provides centralized management of temperature overrides. It encapsulates all knowledge of timer entities and override target entities, providing a clean interface for other components.

**Responsibilities:**
- Check if override is active for a room
- Get override target temperature
- Set override (target and timer)
- Cancel active override
- Handle timer expiration cleanup

**Key Design:** Single source of truth for override operations - no other component directly manipulates timer or target entities.

### Override Architecture

**Component Interactions:**
```
┌──────────────┐
│  Scheduler   │  Calls: get_override_target()
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Override   │  ◄── Single authority for overrides
│   Manager    │      Encapsulates entity knowledge
└──────┬───────┘
       │
       ├─→ Set/cancel via ServiceHandler
       └─→ Timer expiry handled by app.py
```

**Benefits:**
- Encapsulation: Entity structure hidden from other components
- Testability: Override logic can be unit tested independently
- Maintainability: Changes to entity structure only affect OverrideManager
- Consistency: All override operations follow same code path

### Public Interface

```python
class OverrideManager:
    def is_override_active(room_id: str) -> bool:
        """Check if override timer is active or paused."""
    
    def get_override_target(room_id: str) -> Optional[float]:
        """Get override target temperature if active, None otherwise."""
    
    def set_override(room_id: str, target: float, duration_seconds: int) -> bool:
        """Set override target and start timer. Returns success status."""
    
    def cancel_override(room_id: str) -> bool:
        """Cancel active override. Returns success status."""
    
    def handle_timer_expired(room_id: str) -> None:
        """Clean up when timer expires (clear target to sentinel value)."""
```

**Usage Examples:**

```python
# Scheduler checking for override
override_target = override_manager.get_override_target(room_id)
if override_target is not None:
    return override_target  # Use override instead of schedule

# Service handler setting override
success = override_manager.set_override(
    room='pete', 
    target=21.5, 
    duration_seconds=7200  # 2 hours
)

# App handling timer expiry
if timer_state == "idle":
    override_manager.handle_timer_expired(room_id)
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

## Valve Coordination

### Overview

The `ValveCoordinator` class (`valve_coordinator.py`) acts as the **single authority** for all valve command decisions. It was introduced in November 2025 to eliminate architectural coupling between components and provide clear separation of concerns.

**Key Responsibilities:**
- Accept persistence overrides from boiler controller (safety)
- Apply correction overrides from TRV controller (unexpected positions)
- Enforce explicit priority: persistence > corrections > normal
- Pass `persistence_active` flag to TRV controller to prevent fighting
- Coordinate final valve commands with all overrides applied

**Why ValveCoordinator Exists:**

Before this component, valve persistence logic was fragmented:
- `boiler_controller.py` decided when persistence was needed
- `app.py` orchestrated which valve commands to apply
- `room_controller.py` checked for corrections
- `trv_controller.py` checked boiler state to avoid conflicts

This created tight coupling and made debugging difficult. ValveCoordinator provides a single coordination point.

### Architecture

```
┌──────────────────────┐
│  BoilerController    │  Decides: "These valves must persist for safety"
└──────────┬───────────┘
           │ set_persistence_overrides({room: valve%}, reason)
           ▼
┌──────────────────────┐
│  ValveCoordinator    │  Single Authority: Applies all overrides
├──────────────────────┤  Priority: persistence > corrections > normal
│ • persistence_active │
│ • persistence_overrides
│ • applies corrections
└──────────┬───────────┘
           │ apply_valve_command(room, desired_valve, now)
           ▼
┌──────────────────────┐
│   TRVController      │  Executes: Sends hardware commands
└──────────────────────┘
```

**Benefits:**
- **Clear responsibilities** - Each component has one job
- **No cross-component coupling** - Boiler doesn't know about TRVs, TRVs don't know about boiler
- **Explicit priority** - Code clearly shows decision logic
- **Easy to extend** - Add new override types in one place
- **Easier debugging** - Single point to trace valve decisions

### Priority System

The coordinator enforces a strict four-level priority:

**Priority 1: Persistence Overrides (Safety)**
- Source: `boiler_controller.py` during pump overrun or interlock
- Purpose: Keep valves open for pump flow, ensure minimum valve opening
- Examples:
  - Pump overrun: Keep valves at last positions after boiler turns off
  - Interlock: Force valves to meet minimum total opening requirement
- Takes precedence over ALL other commands

**Priority 2: Load Sharing Overrides (Intelligent Load Balancing)**
- Source: `load_sharing_manager.py` when cycling risk detected
- Purpose: Prevent boiler short-cycling by distributing load across additional radiators
- Examples:
  - Schedule-aware pre-warming: Open rooms that will need heat soon
  - Extended lookahead: Open rooms with later schedules
  - Fallback priority: Open configured fallback rooms
- Only applied when NO persistence is active

**Priority 3: Correction Overrides (Unexpected Positions)**
- Source: `trv_controller.py` when TRV feedback doesn't match commanded position
- Purpose: Correct manual TRV changes or communication errors
- Examples:
  - User manually adjusts TRV using physical buttons
  - TRV firmware glitch changes position unexpectedly
- Only applied when NO persistence or load sharing is active

**Priority 4: Normal Values (Heating Logic)**
- Source: `room_controller.py` based on temperature error and valve bands
- Purpose: Normal proportional heating control
- Used when no overrides are active

### Key Methods

#### set_persistence_overrides()

Called by boiler controller when valve persistence is needed:

```python
# In boiler_controller.update_state():
if self.boiler_state == C.STATE_PUMP_OVERRUN:
    persisted = self.boiler_last_valve_positions.copy()
    self.valve_coordinator.set_persistence_overrides(
        persisted, 
        f"{self.boiler_state}: pump overrun active"
    )
```

**Parameters:**
- `overrides`: Dict[room_id → valve_percent] - Positions to persist
- `reason`: Human-readable explanation for logging

**Effect:**
- Stores overrides internally
- Sets `persistence_active = True`
- Logs INFO message with all persisted rooms and values

#### clear_persistence_overrides()

Called when persistence is no longer needed:

```python
# In boiler_controller.update_state():
if self.boiler_state == C.STATE_OFF:
    self.valve_coordinator.clear_persistence_overrides()
```

**Effect:**
- Clears overrides
- Sets `persistence_active = False`
- Logs DEBUG message

#### set_load_sharing_overrides()

Called by load sharing manager when load balancing is needed:

```python
# In app.py recompute_all():
load_sharing_commands = self.load_sharing.evaluate(room_data, boiler_state, cycling_state)
if load_sharing_commands:
    self.valve_coordinator.set_load_sharing_overrides(
        load_sharing_commands, 
        "load_sharing"
    )
```

**Parameters:**
- `overrides`: Dict[room_id → valve_percent] - Load sharing valve positions
- `reason`: Human-readable explanation for logging

**Effect:**
- Stores overrides internally
- Sets `load_sharing_active = True`
- Logs INFO message with all load sharing rooms and values

#### clear_load_sharing_overrides()

Called when load sharing is no longer needed:

```python
# In app.py recompute_all():
if not load_sharing_commands:
    self.valve_coordinator.clear_load_sharing_overrides()
```

**Effect:**
- Clears overrides
- Sets `load_sharing_active = False`
- Logs DEBUG message

#### apply_valve_command()

Main interface used by app.py for all valve commands:

```python
# In app.py recompute_all():
for room_id in self.config.rooms:
    data = room_data[room_id]
    desired_valve = data['valve_percent']
    
    final_valve = self.valve_coordinator.apply_valve_command(
        room_id, 
        desired_valve, 
        now
    )
    
    # final_valve is what was actually commanded (after overrides)
```

**Logic Flow:**
```python
1. Check persistence_overrides dict:
   If room in dict → use persisted value (Priority 1)

2. Else, check load_sharing_overrides dict:
   If room in dict → use load sharing value (Priority 2)

3. Else, check trvs.unexpected_valve_positions:
   If room in dict → use expected value (Priority 3)
   
4. Else → use desired value (Priority 4)

5. Call trvs.set_valve(room, final_value, now, 
                      is_correction=(Priority 3),
                      persistence_active=bool(Priority 1))

6. Log decision if override applied

7. Return final_value
```

**Parameters:**
- `room_id`: Room identifier
- `desired_percent`: Valve percentage from room heating logic
- `now`: Current datetime

**Returns:**
- `final_percent`: Actual valve percentage commanded (after overrides)

#### is_persistence_active()

Query method to check if persistence is active:

```python
# In app.py trv_feedback_changed():
persistence_active = self.valve_coordinator.is_persistence_active()
self.trvs.check_feedback_for_unexpected_position(
    room_id, 
    feedback_percent, 
    now, 
    persistence_active
)
```

Used to prevent TRV feedback checks from triggering corrections during persistence.

### Integration Points

**Boiler Controller → Valve Coordinator:**
```python
# boiler_controller.py __init__:
def __init__(self, ad, config, alert_manager, valve_coordinator):
    self.valve_coordinator = valve_coordinator

# In update_state():
if persisted_valves and valves_must_stay_open:
    self.valve_coordinator.set_persistence_overrides(
        persisted_valves, 
        f"{self.boiler_state}: {reason}"
    )
elif not valves_must_stay_open:
    self.valve_coordinator.clear_persistence_overrides()
```

**App.py → Valve Coordinator:**
```python
# app.py __init__:
self.valve_coordinator = ValveCoordinator(self, self.trvs)

# Pass to boiler controller:
self.boiler = BoilerController(self, self.config, self.alerts, self.valve_coordinator)

# In recompute_all():
# Apply load sharing overrides
load_sharing_commands = self.load_sharing.evaluate(room_data, boiler_state, cycling_state)
if load_sharing_commands:
    self.valve_coordinator.set_load_sharing_overrides(load_sharing_commands, "load_sharing")
else:
    self.valve_coordinator.clear_load_sharing_overrides()

# Apply final valve commands with all overrides
for room_id in self.config.rooms:
    data = room_data[room_id]
    final_valve = self.valve_coordinator.apply_valve_command(
        room_id, data['valve_percent'], now
    )
    data['valve_percent'] = final_valve  # Update for status publishing
```

**Valve Coordinator → TRV Controller:**
```python
# In apply_valve_command():
self.trvs.set_valve(
    room_id, 
    final_percent, 
    now, 
    is_correction=is_correction,
    persistence_active=self.persistence_active
)
```

### Logging

ValveCoordinator provides detailed logging for debugging:

**Persistence Set:**
```
[INFO] Valve persistence ACTIVE: STATE_PUMP_OVERRUN: pump overrun active 
       [pete=75%, lounge=50%, office=25%]
```

**Load Sharing Set:**
```
[INFO] Valve load sharing ACTIVE: load_sharing 
       [lounge=70%, games=70%]
```

**Persistence Cleared:**
```
[DEBUG] Valve persistence CLEARED
```

**Load Sharing Cleared:**
```
[DEBUG] Valve load sharing CLEARED
```

**Override Applied:**
```
[DEBUG] Room 'pete': valve=75% (persistence: STATE_PUMP_OVERRUN: pump overrun active)
[DEBUG] Room 'lounge': valve=70% (load_sharing)
[DEBUG] Room 'office': valve=60% (correction)
```

**Normal Command:**
```
(No log - only overrides are logged to reduce noise)
```

### State Management

The coordinator maintains minimal internal state:

```python
self.persistence_overrides = {}  # {room_id: valve_percent}
self.persistence_reason = None   # Human-readable explanation
self.persistence_active = False  # Boolean flag

self.load_sharing_overrides = {}  # {room_id: valve_percent}
self.load_sharing_reason = None   # Human-readable explanation
self.load_sharing_active = False  # Boolean flag
```

**State Lifecycle:**
```
INACTIVE → set_persistence_overrides() → ACTIVE
         ← clear_persistence_overrides() ← 

INACTIVE → set_load_sharing_overrides() → ACTIVE
         ← clear_load_sharing_overrides() ←
```

**State is NOT persisted** across AppDaemon restarts:
- Resets to INACTIVE on restart
- Boiler controller re-establishes persistence as needed
- Safe behavior: defaults to normal operation

### Performance

**Computational Cost:**
- O(1) per valve command (dict lookup)
- Negligible CPU usage (<0.01ms per room)

**Memory:**
- ~100 bytes per persisted room
- Typical: 3-6 rooms during pump overrun = 300-600 bytes
- Minimal footprint

**Call Frequency:**
- Called every recompute cycle (60s) for all rooms
- Additional calls on sensor changes
- Typical: 10-20 calls per minute across all rooms

---

## Load Sharing

### Overview

The `LoadSharingManager` class (`managers/load_sharing_manager.py`) implements **intelligent load balancing** to prevent boiler short-cycling when primary calling rooms have insufficient radiator capacity to dissipate the boiler's minimum output. Instead of relying on a single fixed dump radiator (which wastes energy), load sharing distributes excess load across available radiators while prioritizing efficiency through schedule-aware pre-warming.

**Key Features:**
- Three-tier cascading selection strategy (schedule-aware → extended lookahead → fallback priority)
- Explicit state machine with deterministic transitions
- Calling pattern-based exit conditions (not arbitrary timers)
- Per-room configurable lookahead windows
- Graceful degradation through all tiers

**Design Philosophy:** Minimize unwanted heating by prioritizing rooms that will need heat soon anyway, with deterministic fallback when schedules don't help.

### Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         LOAD SHARING STATE MACHINE                            │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  DISABLED ──(master enable)──▶ INACTIVE                                      │
│      ▲                             │                                           │
│      │                             │ Entry: Low capacity + Cycling risk        │
│      │                             ▼                                           │
│      │                        TIER1_ACTIVE                                     │
│      │                             │                                           │
│      │                             ├─(insufficient)─▶ TIER1_ESCALATED          │
│      │                             │                        │                  │
│      │                             │                        ├─▶ TIER2_ACTIVE   │
│      │                             │                        │        │         │
│      │                             │                        │   TIER2_ESCALATED│
│      │                             │                        │        │         │
│      │                             │                        └───▶ TIER3_ACTIVE │
│      │                             │                                 │         │
│      │                             │                           TIER3_ESCALATED │
│      │                             │                                           │
│      └──(exit conditions met)──────┴─────────────────────────────────────────┘
│                                                                                │
│  Exit Triggers:                                                                │
│    A: Original calling rooms stopped                                           │
│    B: New room joined with sufficient capacity                                 │
│    C: Load sharing room now naturally calling                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### State Machine

**LoadSharingState Enum:**
```python
class LoadSharingState(Enum):
    DISABLED = "disabled"                    # Feature disabled
    INACTIVE = "inactive"                    # Monitoring, not active
    TIER1_ACTIVE = "tier1_active"           # Schedule-aware pre-warming
    TIER1_ESCALATED = "tier1_escalated"     # Tier 1 valves escalated
    TIER2_ACTIVE = "tier2_active"           # Extended lookahead
    TIER2_ESCALATED = "tier2_escalated"     # Tier 2 valves escalated
    TIER3_ACTIVE = "tier3_active"           # Fallback priority
    TIER3_ESCALATED = "tier3_escalated"     # Tier 3 valves escalated
```

**State Transitions:**
- **DISABLED**: Feature off (master switch or config)
- **INACTIVE → TIER1_ACTIVE**: Entry conditions met (low capacity + cycling risk)
- **TIER1_ACTIVE → TIER1_ESCALATED**: Insufficient capacity, escalate valves
- **TIER1_ESCALATED → TIER2_ACTIVE**: Still insufficient, add Tier 2 rooms
- **TIER2_ACTIVE → TIER2_ESCALATED → TIER3_ACTIVE**: Continue cascading
- **Any active state → INACTIVE**: Exit conditions met (calling pattern changed)

### LoadSharingContext (Single Source of Truth)

The state machine uses a single dataclass to track all state:

```python
@dataclass
class LoadSharingContext:
    state: LoadSharingState = LoadSharingState.DISABLED
    
    # Trigger snapshot (immutable once set)
    trigger_calling_rooms: Set[str] = field(default_factory=set)
    trigger_capacity_w: float = 0.0
    trigger_reason: str = ""
    activated_at: Optional[datetime] = None
    
    # Active load sharing rooms
    active_rooms: Dict[str, RoomActivation] = field(default_factory=dict)
    
    # Computed properties (tier-based queries)
    @property
    def tier1_rooms(self) -> List[RoomActivation]:
        return [r for r in self.active_rooms.values() if r.tier == 1]
    
    @property
    def is_active(self) -> bool:
        return self.state not in (LoadSharingState.DISABLED, LoadSharingState.INACTIVE)
```

**Benefits:**
- Single source of truth (no duplicate state)
- Computed properties ensure data consistency
- Immutable trigger snapshot prevents confusion
- Explicit state transitions are debuggable

### Three-Tier Cascading Strategy

#### Tier 1: Schedule-Aware Pre-Warming (Primary)

**Selection Criteria:**
- Room in "auto" mode (respects user intent)
- Room NOT in passive operating mode (passive rooms excluded from load sharing)
- Next scheduled block within `schedule_lookahead_m` minutes (per-room configurable, default: 60)
- Scheduled target > current temp + 0.5°C (will definitely need heating)
- Not currently calling for heat

**Sorting:** By temperature deficit (neediest first)

**Valve Opening:** 70% initial → 80% escalated

**Rationale:** Pre-warm rooms that will need heat soon anyway - minimizes wasted energy while providing the load sharing benefit.

#### Tier 2: Extended Lookahead (Secondary)

**Selection Criteria:**
- Same as Tier 1, but with 2× `schedule_lookahead_m` window
- Room with 60 min lookahead → check 120 min window
- Room NOT in passive operating mode (passive rooms excluded from load sharing)
- Catches rooms with later schedules that might be acceptable to pre-warm

**Valve Opening:** 40% initial → 50% escalated (gentler than Tier 1)

**Rationale:** Extended window provides more coverage while lower valve % prevents over-heating from early pre-warming.

#### Tier 3: Passive Rooms + Fallback Priority (Tertiary)

Tier 3 is split into two phases:
- **Phase A:** Passive room opportunistic heating
- **Phase B:** Fallback priority list (if Phase A insufficient)

##### Tier 3 Phase A: Passive Room Opportunistic Heating

**Selection Criteria:**
- Current `operating_mode == 'passive'` (room is passive RIGHT NOW)
- Not currently calling for heat (excludes comfort/frost protection modes)
- Current temperature < passive_max_temp (room can still accept heat)
- Temperature sensors not stale

**Target Temperature:** Room's configured passive_max_temp (upper limit for valve control)
- Note: passive_min_temp is the comfort floor that triggers active heating if breached
- In passive mode, the room heats opportunistically up to passive_max_temp

**Valve Opening:** 50% initial → 60% → 70% → 80% → 90% → 100% (standard Tier 3 escalation)
- **Overrides user's `passive_valve_percent`** during load sharing
- Normal passive operation: 10-30% (gentle opportunistic)
- Load sharing active: 50-100% (effective heat dumping)

**Selection Order:** Sorted by temperature deficit (neediest first)

**Rooms Included:**
- Manual passive mode rooms (user explicitly wants opportunistic heating)
- Scheduled passive blocks with no upcoming schedule in lookahead window
- Scheduled passive blocks after their active schedule target is reached

**Rationale:** Passive mode means "I want opportunistic heating" - load sharing provides it more effectively. Different contexts warrant different valve percentages: normal passive uses low % to avoid diverting heat, load sharing uses high % to dump excess capacity.

##### Tier 3 Phase B: Fallback Priority List

**Selection Criteria:**
- Only runs if Phase A provides insufficient capacity
- Explicit `fallback_priority` ranking from room configs (1, 2, 3, ...)
- Only "auto" mode rooms eligible
- Room NOT in passive operating mode (Phase A handles these)
- **No temperature check** - ultimate fallback accepts any room to prevent cycling
- Excludes "off" and "manual" mode rooms

**Target Temperature:** Global comfort target (default 20°C) from `tier3_comfort_target_c` config
- Bypasses low parking temperatures (rooms parked at 10-12°C but often at ambient 15-17°C)
- Provides genuine pre-warming above ambient temperature
- Configurable in boiler.yaml under load_sharing section

**Valve Opening:** 50% initial → 60% → 70% → 80% → 90% → 100% (progressive escalation)

**Maximize-Existing Strategy:** Escalate current rooms to maximum before adding next priority room.

**Timeout:** 15 minutes maximum for Tier 3 rooms (prevents long-term unwanted heating)

**Rationale:** Deterministic behavior when schedules don't help. Uses global comfort target instead of parking + margin to ensure rooms stay in load sharing long enough to provide capacity. Accepts trade-off of heating above parking temp to prevent boiler cycling wear.

### Entry Conditions

Load sharing activates when **ALL** of these are true:

1. **Low Capacity:** `total_calling_capacity < min_calling_capacity_w` (default: 3500W)
2. **Cycling Risk Evidence** (either):
   - Recent cooldown: `cycling_protection.last_cooldown_within(15 minutes)`
   - High return temp risk: `boiler_state == ON AND return_temp > (setpoint - 15°C)`

**Rationale:** Low capacity alone isn't proof of a problem. Only activate when there's evidence of inefficiency.

### Exit Conditions

Load sharing persists for the duration of the calling pattern that triggered it. Exit is triggered by **changes in the calling situation**, not arbitrary timers.

#### Exit Trigger A: Original calling room(s) stopped
- If none of the `trigger_calling_rooms` are still calling → **Deactivate**
- Rationale: Original need is gone

#### Exit Trigger B: Additional room(s) started calling
- If new room(s) join the calling set (not in `trigger_calling_rooms`)
- Recalculate total capacity with new configuration
- If `new_total_capacity >= 4000W` → **Deactivate** (sufficient now)
- If still insufficient → **Update trigger set and continue**
- Rationale: Additional callers may provide sufficient capacity

#### Exit Trigger C: Load sharing room now naturally calling
- If a load sharing room transitions to naturally calling (reaches its own on_delta threshold)
- **Remove from load sharing control**, let room controller manage it
- If no load sharing rooms remain → **Deactivate**
- Rationale: Room now needs heat anyway, not just helping

#### Exit Trigger D: Tier 3 timeout expired
- Tier 3 rooms have 15-minute maximum duration
- Prevents long-term heating of fallback priority rooms
- **Cooldown enforcement:** Room enters 30-minute cooldown after timeout
  - Recorded in `tier3_timeout_history` with timeout timestamp
  - Excluded from Tier 3 selection during cooldown period
  - Prevents oscillation (timeout → re-select → timeout loop)
  - Forces system to try next priority room or accept cycling
- Rationale: Tier 3 accepts heating above parking temp, but needs time limit and anti-oscillation

#### Exit Trigger E: Room reached target temperature
- Check: `temp >= target_temp + off_delta` (same hysteresis as normal control)
- Prevents overshoot by closing valve when pre-warming succeeds
- Uses target_temp tracked in RoomActivation dataclass
- Rationale: Exit when pre-warming goal achieved

#### Exit Trigger F: Room mode changed from auto
- Check: `mode != 'auto'` (user switched to manual/off)
- Respects user control - immediately remove room from load sharing
- Rationale: User intent overrides automation

**Minimum Activation Duration:** 5 minutes (prevents rapid oscillation)

### Valve Command Priority

Load sharing commands are inserted into the ValveCoordinator priority system:

**Priority Order:**
1. **Persistence overrides** (safety: pump overrun, interlock)
2. **Load sharing overrides** (intelligent load balancing) ← **NEW**
3. **Correction overrides** (unexpected TRV positions)
4. **Normal commands** (room heating logic)

```python
# In valve_coordinator.apply_valve_command():
if room_id in self.persistence_overrides:
    final_percent = self.persistence_overrides[room_id]
elif room_id in self.load_sharing_overrides:
    final_percent = self.load_sharing_overrides[room_id]
elif room_id in self.trvs.unexpected_valve_positions:
    final_percent = self.trvs.unexpected_valve_positions[room_id]['expected']
else:
    final_percent = desired_percent
```

**Integration Point:**
```python
# In app.py recompute_all():
load_sharing_commands = self.load_sharing.evaluate(room_data, boiler_state, cycling_state)
if load_sharing_commands:
    self.valve_coordinator.set_load_sharing_overrides(load_sharing_commands, "load_sharing")
else:
    self.valve_coordinator.clear_load_sharing_overrides()
```

### Configuration

**Global Configuration (boiler.yaml):**
```yaml
boiler:
  load_sharing:
    # Entry conditions
    min_calling_capacity_w: 3500  # Capacity trigger
    cooldown_lookback_m: 15       # Recent cooldown window
    return_temp_risk_delta_c: 15  # High return temp threshold
    
    # Exit conditions
    sufficient_capacity_w: 4000   # Exit threshold
    min_activation_duration_m: 5  # Prevent oscillation
    
    # Tier 1: Schedule-aware pre-warming
    schedule_lookahead_m: 60      # Default lookahead
    tier_1_initial_pct: 70
    tier_1_escalated_pct: 80
    
    # Tier 2: Extended lookahead
    extended_window_multiplier: 2.0
    tier_2_initial_pct: 40
    tier_2_escalated_pct: 50
    
    # Tier 3: Fallback priority
    tier_3_initial_pct: 50
    tier_3_escalated_pct: 60
    tier_3_max_duration_m: 15        # 15 minutes timeout
    tier_3_cooldown_m: 30            # 30 minutes before re-eligible after timeout
```

**Per-Room Configuration (rooms.yaml):**
```yaml
rooms:
  - id: lounge
    load_sharing:
      schedule_lookahead_m: 90   # Override global default
      fallback_priority: 1        # Lower = higher priority
```

**Master Control:**
- `input_select.pyheat_load_sharing_mode` - Mode selector (Off/Conservative/Balanced/Aggressive)
- Checked first in `evaluate()` - returns empty if mode is Off

### Capacity Calculation with Valve Adjustment

Valve opening affects effective radiator capacity:

```python
effective_capacity = delta_t50_rating × (valve_opening_pct / 100) × flow_efficiency
```

Where `flow_efficiency` = 1.0 (linear scaling, conservative estimate)

**Examples:**
- 70% valve ≈ 70% effective capacity
- 50% valve ≈ 50% effective capacity
- 100% valve = 100% effective capacity

**Usage:** Selection algorithm calculates effective capacity per-candidate during tier evaluation to ensure accurate total capacity estimates.

### Status Publishing

**System-Level Entity:**
- `sensor.pyheat_load_sharing_status`
- State: current state machine state
- Attributes:
  - `active_rooms`: List of room IDs and valve %
  - `trigger_rooms`: Original calling rooms
  - `trigger_capacity`: Capacity that triggered activation
  - `state`: Current state enum
  - `tier_1_count`, `tier_2_count`, `tier_3_count`: Rooms per tier

**Per-Room Attributes:**
- Added to existing `sensor.pyheat_room_{room}` entities
- `load_sharing_active`: Boolean
- `load_sharing_tier`: 1, 2, or 3
- `load_sharing_reason`: "schedule_60m", "schedule_120m", "fallback_p1", etc.
- `load_sharing_since`: Timestamp

### Logging

**INFO Level (state changes only):**
```
Load sharing ACTIVATED: Low capacity (433W < 3500W) + recent cooldown
Load sharing: Tier 1 active - added 2 rooms [lounge=70%, office=70%]
State: TIER1_ACTIVE → TIER1_ESCALATED (escalate to 80%)
State: TIER1_ESCALATED → TIER2_ACTIVE (add Tier 2 rooms)
Load sharing DEACTIVATED: Original calling rooms stopped (duration: 12.3 min)
```

**DEBUG Level (evaluation details):**
```
Load sharing: Evaluating entry conditions (capacity=3200W, cycling_state=NORMAL)
Load sharing: Tier 1 evaluation - found 3 candidates
Load sharing: Tier 2 evaluation - found 1 candidate
```

**WARN Level (fallback indicators):**
```
Load sharing: Tier 3 ACTIVATED (fallback) - consider improving schedules
```

### Performance

**Computational Cost:**
- O(n) per recompute where n = number of rooms (~6)
- Schedule lookahead queries: O(n) per room (n = schedule blocks)
- Capacity calculations: Already performed by LoadCalculator
- Negligible CPU usage (<0.1ms per evaluation)

**Memory:**
- Per-room config: ~50 bytes × rooms
- Context state: ~500 bytes
- Active rooms: ~100 bytes × active rooms
- Total: <5KB typical

**Call Frequency:**
- Once per recompute cycle (60s baseline + state change events)
- Only evaluates when feature enabled

### Edge Cases Handled

1. **No schedules defined:** Falls through to Tier 3 (priority list)
2. **All rooms at temperature:** No load sharing needed (capacity met)
3. **Schedule changes:** Rooms re-evaluated every recompute cycle
4. **Room mode changes:** Immediately excluded if changed to "off" or "manual"
5. **Cycling protection active:** Load sharing still operates (provides cooling assistance)
6. **Room reaches target early:** Transitions to normal control (Exit Trigger C)
7. **Tier 1 empty:** Initializes trigger context and tries Tier 2/3 (BUG #5 fix)
8. **All fallback rooms excluded:** System accepts insufficient capacity (safety over aggression)

### Integration with Existing Systems

**Verified Compatible With:**
- ✅ **ValveCoordinator Priority System:** Load sharing at Priority 2 (between persistence and corrections)
- ✅ **Persistence Overrides (Pump Overrun/Interlock):** Safety takes absolute priority
- ✅ **Cycling Protection:** Works synergistically (increases capacity, helps cooling)
- ✅ **TRV Feedback/Corrections:** Load sharing rooms respect correction overrides
- ✅ **Boiler State Machine:** Independent evaluation (doesn't modify FSM)
- ✅ **Room Controller Hysteresis:** Load sharing rooms transition to normal when naturally calling
- ✅ **Master Enable:** System properly handles enable/disable
- ✅ **Manual Mode:** Excluded from selection (only "auto" mode eligible)
- ✅ **Passive Mode:** Excluded from selection (user controls valve opening manually)
- ✅ **Safety Room:** Different priority tiers (no conflict)
- ✅ **Recompute Triggers:** Existing triggers adequate (sensor changes, timers, mode changes)

**Synergy with Cycling Protection:**
- Load sharing increases total capacity → reduces return temp rise
- Helps cycling protection by providing more thermal mass
- Both systems work independently but complement each other

---

## Room Control Logic

### Overview

The `RoomController` class (`room_controller.py`) is the core decision-making engine that determines whether each room should heat and at what intensity. It implements control algorithms with **asymmetric hysteresis** to prevent oscillation and **stepped valve bands** for proportional control without complex PID tuning.

**Key Responsibilities:**
- Coordinate sensor fusion, scheduling, and TRV control
- Determine call-for-heat status with hysteresis
- Calculate valve opening percentages using stepped bands
- Maintain per-room state across recompute cycles
- Handle startup initialization from persisted state

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
- `room_call_for_heat` and `passive_valve` persisted to local file: `state/persistence.json`
- Restored on AppDaemon restart from persistence file (single source of truth)
- Updated every time calling state or passive valve position changes

**Initialization on Restart:**

State is initialized from local persistence file:

```python
# Load persisted state on init
persistence = PersistenceManager('/opt/appdata/appdaemon/conf/apps/pyheat/state/persistence.json')
data = persistence.load()
room_state = data.get('room_state', {})

for room_id in rooms:
    room_call_for_heat[room_id] = room_state[room_id].get('last_calling', False)
    room_last_valve[room_id] = room_state[room_id].get('passive_valve', 0)

# Initialize target tracking to prevent false "changed" on first recompute
room_last_target[room_id] = current_target
```

**Persistence File Format:**
```json
{
  "room_state": {
    "pete": {"valve_percent": 0, "last_calling": false, "passive_valve": 0}
  },
  "cycling_protection": {"mode": "NORMAL", "saved_setpoint": null, "cooldown_start": null}
}
```

**Single Source of Truth:**
- `state/persistence.json` is the authoritative source for all internal state
- Missing/invalid data defaults to safe values (`False` for calling, `0` for valves) with ERROR logs
- First recompute (within seconds) establishes correct state from temperature vs target
- Atomic writes (temp file + rename) prevent corruption on crashes

**Why This Matters:**
- Hysteresis state survives restarts correctly (prevents phantom bugs)
- No 255-character size limits (was constrained by HA input_text entities)
- Faster I/O (direct file access vs HA API calls)
- Easier debugging (can inspect file directly)
- No HA entity clutter
- Conservative defaults prevent spurious heating on data loss

### The compute_room() Pipeline

The main entry point that orchestrates all room-level logic:

```python
def compute_room(room_id: str, now: datetime) -> Dict:
    """Returns room state with heating decisions."""
```

**Processing Steps:**

```
1. Read room mode (auto/manual/passive/off) from helper entity
2. Read holiday mode (system-wide) from helper entity
3. Get fused temperature from SensorManager
4. FROST PROTECTION CHECK (highest priority):
   - Only for modes other than "off" and only when master_enable is on
   - If temp < (frost_temp - on_delta) → activate frost protection
   - If already active and temp > (frost_temp + off_delta) → deactivate
   - If active → return frost protection heating (100% valve, calling=True)
5. Resolve target temperature and operating mode from Scheduler
6. Validate inputs:
   - If target is None → calling=False, valve=0%
   - If temp is None (sensors stale) → calling=False, valve=0%
   - Exception: Manual mode still requires valid temp
7. Branch on operating mode:
   - PASSIVE: Range control (valve open if temp < passive_max_temp, heating triggered if temp < passive_min_temp)
   - ACTIVE: Calculate error, hysteresis, and stepped valve bands
8. Return room state dict
```

**Return Value:**
```python
{
    'temp': 21.3,              # Current temperature (°C) or None
    'target': 22.0,            # Target temperature (°C) or None
    'is_stale': False,         # True if sensors stale
    'mode': 'auto',            # Room mode (auto/manual/passive/off)
    'operating_mode': 'active',# Operating mode ('active', 'passive', 'frost_protection', or 'off')
    'calling': True,           # Whether room calls for heat
    'valve_percent': 65,       # Commanded valve opening (0-100)
    'error': 0.7,              # target - temp (°C)
    'frost_protection': False, # True if frost protection active
    'manual_setpoint': None    # For manual mode status display
}
```

**Safety Checks:**
- No target → no heating (off mode or invalid schedule)
- Stale sensors → no heating (safety, prevents runaway)
- Manual mode with stale sensors → still no heating (safety override)

**Passive Mode Behavior:**
- Never calls for heat (`calling` always False)
- Uses same hysteresis deltas as active mode to prevent valve cycling
- Valve opens to configured percentage when `temp < max_temp - on_delta`
- Valve closes when `temp > max_temp + off_delta`
- Dead band maintains previous valve state to prevent oscillation
- No PID control (fixed valve percentage, not proportional)
- Excluded from load sharing calculations
- Valve state not persisted (defaults to closed on reload, recomputes within 10-60s)

### Frost Protection

**Priority: Highest** - Checked before all other mode logic (including target resolution).

PyHeat includes automatic frost protection to prevent rooms from dropping to dangerously cold temperatures that could cause frozen pipes or property damage.

**Configuration (system-wide in boiler.yaml):**
```yaml
system:
  frost_protection_temp_c: 8.0  # Default: 8°C (standard UK/EU frost protection)
```

**Activation Conditions (ALL must be true):**
1. Room mode is NOT "off" (respects explicit user disable)
2. `master_enable` is ON (respects system-wide kill switch)
3. Temperature sensor is valid (not stale)
4. `temp < (frost_protection_temp_c - on_delta)`

**Deactivation (Recovery):**
- `temp > (frost_protection_temp_c + off_delta)`
- Uses existing per-room hysteresis values to prevent oscillation

**Behavior During Frost Protection:**
- Room calls for heat (`calling = True`) - boiler will turn on
- Valve forced to 100% (ignores normal valve bands and passive settings)
- Target temperature = frost_protection_temp_c (8°C default)
- Operating mode = 'frost_protection' (special state)
- System logs WARNING on activation, INFO on deactivation
- Returns to normal mode behavior after recovery

**Mode Interactions:**
- **Off mode**: Frost protection does NOT activate (user explicitly disabled room)
- **Auto/Manual/Passive modes**: Frost protection activates if temperature drops below threshold
- **Holiday mode**: Frost protection activates if holiday target (15°C) fails to prevent drop below 8°C

**Example Scenario:**
```
Room in auto mode, target 12°C
External temperature: -10°C (extreme cold)
Room temp drops: 10°C → 9°C → 8°C → 7.7°C
  ↓
Frost protection ACTIVATES at 7.7°C (8.0 - 0.3)
  ↓
Emergency heating: valve 100%, calling for heat
  ↓
Room warms: 7.8°C → 8.0°C → 8.2°C → 9.0°C
  ↓
Frost protection DEACTIVATES at 8.1°C (8.0 + 0.1)
  ↓
Returns to normal auto mode behavior (target 12°C)
```

**Safety Notes:**
- Frost protection is a safety override, not a comfort feature
- Intentional overshoot (9-10°C) provides thermal buffer
- Alert notification sent on activation (one per activation, rate-limited)
- CSV logs include frost_protection column for post-analysis

### Asymmetric Hysteresis (Call-for-Heat Decision)

Prevents rapid on/off cycling by using different thresholds for turning on vs. turning off.

**Configuration (per room):**
```yaml
hysteresis:
  on_delta_c: 0.30    # Start heating when 0.30°C below target (default)
  off_delta_c: 0.10   # Stop heating when 0.10°C above target (default)
```

**Key Concept:**
- `on_delta_c`: Temperature must fall **below** `target - on_delta` to start heating
- `off_delta_c`: Temperature must rise **above** `target + off_delta` to stop heating
- **Deadband**: Between `target - on_delta` and `target + off_delta`, maintain previous state

#### Temperature Zones

Using notation where:
- `t` = current room temperature
- `S` = setpoint (target temperature)
- `on_delta` = on_delta_c
- `off_delta` = off_delta_c

**Normal Operation (target unchanged):**

```
Zone 1: t < S - on_delta           → START/Continue heating (too cold)
Zone 2: S - on_delta ≤ t ≤ S + off_delta  → MAINTAIN previous state (deadband)
Zone 3: t > S + off_delta          → STOP heating (overshot target)
```

**Example with S=18.0°C, on_delta=0.40, off_delta=0.10:**
```
Temperature Zones:
─────────────────────────────────────────────────
18.1°C  ├─────────── Stop Threshold (S + off_delta)
        │ Zone 3: STOP heating (too warm)
18.0°C  ├─────────── Target (S)
        │
        │ Zone 2: DEADBAND
        │ (Maintain previous state)
        │
17.6°C  ├─────────── Start Threshold (S - on_delta)
        │ Zone 1: START heating (too cold)
─────────────────────────────────────────────────
```

#### Algorithm (in terms of error)

Since `error = S - t` (positive when below target):

**Normal Operation:**
```python
error = target - temp

if error > on_delta:           # t < S - on_delta (Zone 1)
    return True                # Too cold → heat
elif error < -off_delta:       # t > S + off_delta (Zone 3)
    return False               # Too warm → don't heat
else:                          # -off_delta ≤ error ≤ on_delta (Zone 2)
    return prev_calling        # Deadband → maintain state
```

**Target Change (bypass deadband):**

When target changes (schedule transition, override, mode change), the "previous state" is meaningless because it was relative to a different target. The deadband logic is bypassed:

```python
# Constants for target change detection
TARGET_CHANGE_EPSILON = 0.01°C  # Floating-point tolerance

if abs(target - prev_target) > TARGET_CHANGE_EPSILON:
    # Target changed → make fresh decision based on upper threshold only
    if error >= -off_delta:     # t ≤ S + off_delta
        return True             # Not yet overshot → heat
    else:                       # t > S + off_delta
        return False            # Already overshot → don't heat
```

**Why use only off_delta on target change?**
- When target changes, we want to heat toward the new target
- Continue heating until we reach the "overshoot" threshold (S + off_delta)
- This ensures responsive behavior without immediately stopping in the deadband

#### State Transitions

**Scenario 1: Normal heating cycle**
1. Temp drops to 17.5°C (error = 0.5 > 0.4) → **START heating**
2. Temp rises to 17.8°C (error = 0.2, in deadband) → **Continue heating**
3. Temp rises to 18.0°C (error = 0.0, in deadband) → **Continue heating**
4. Temp rises to 18.15°C (error = -0.15 < -0.1) → **STOP heating**
5. Temp drifts down to 17.9°C (error = 0.1, in deadband) → **Stay off**
6. Temp drifts down to 17.5°C (error = 0.5 > 0.4) → **START heating** (cycle repeats)

**Scenario 2: Target change from 14.0°C to 18.0°C**
1. Before change: Target=14.0°C, Temp=17.9°C → Not heating (above target)
2. Target changes to 18.0°C → Temp=17.9°C, error=0.1
3. Check: error (0.1) >= -off_delta (-0.1) → **TRUE** → **START heating**
4. Next recompute: Target unchanged, temp=17.9°C, error=0.1 (in deadband)
5. Use normal hysteresis: prev_calling=True → **Continue heating**
6. Heat until temp > 18.1°C, then stop

**Scenario 3: Target change from 20.0°C to 18.0°C**
1. Before change: Target=20.0°C, Temp=19.0°C → Heating
2. Target changes to 18.0°C → Temp=19.0°C, error=-1.0
3. Check: error (-1.0) >= -off_delta (-0.1) → **FALSE** → **STOP heating**
4. Already well above new target, no heating needed

#### Graphical Representation

```
Temperature relative to target (S = 18.0°C example)
─────────────────────────────────────────────────
19.0°C  │
        │  Zone 3: OFF
18.1°C  ├─────────── S + off_delta (stop threshold)
        │
18.0°C  ├─────────── S (target)
        │  Zone 2: DEADBAND
        │  (maintain previous state)
        │
17.6°C  ├─────────── S - on_delta (start threshold)
        │
        │  Zone 1: ON
17.0°C  │
─────────────────────────────────────────────────

State Transitions:
  • OFF + cross below 17.6°C → START heating
  • ON + cross above 18.1°C → STOP heating
  • In deadband (17.6-18.1°C) → NO CHANGE (maintain state)
  • Target changes + in deadband → Heat if t ≤ 18.1°C, else don't heat

Deadband (0.1-0.3°C): No state change
```

#### Why Asymmetric Hysteresis?

- **Prevents flapping**: Room temperature oscillates naturally; hysteresis prevents rapid on/off cycling
- **Accounts for overshoot**: Allow heating to continue past target so residual heat brings room to target
- **Deadband stability**: Wide band (`on_delta + off_delta = 0.5°C` typical) provides stable operation
- **Responsive to changes**: Target changes bypass deadband for immediate response

#### Tuning Guidance

- **Tight control**: `on_delta=0.3`, `off_delta=0.05` (more cycles, tighter temp range)
- **Default**: `on_delta=0.4`, `off_delta=0.10` (balanced stability and precision)
- **Very stable**: `on_delta=0.5`, `off_delta=0.15` (fewer cycles, wider temp range)
- **Rule of thumb**: Total deadband width (`on_delta + off_delta`) should be 0.4-0.7°C
- **off_delta**: Should be slightly larger than typical temperature sensor noise/variation

### Stepped Valve Bands (Proportional Control)

Instead of simple on/off, valve opening is calculated using **3 discrete heating bands** (plus Band 0 for not calling) based on temperature error. This provides proportional control without PID complexity.

**Configuration (per room):**
```yaml
valve_bands:
  # Thresholds (temperature error in °C below setpoint)
  band_1_error: 0.30   # Band 1 applies when error < 0.30°C
  band_2_error: 0.80   # Band 2 applies when 0.30 ≤ error < 0.80°C
                       # Band Max applies when error ≥ 0.80°C
  
  # Valve openings (percentage 0-100)
  band_0_percent: 0.0      # Not calling (default: 0.0)
  band_1_percent: 40.0     # Close to target (gentle heating)
  band_2_percent: 70.0     # Moderate distance (moderate heating)
  band_max_percent: 100.0  # Far from target (maximum heating)
  
  step_hysteresis_c: 0.05  # Band transition hysteresis
```

**Band Mapping:**
```
Error (target - temp)     Band       Valve Opening
────────────────────────────────────────────────────
Not calling               Band 0     0%     (room satisfied)
< 0.30°C                  Band 1     40%    (gentle, close to target)
0.30 - 0.80°C             Band 2     70%    (moderate distance)
≥ 0.80°C                  Band Max   100%   (far from target)
```

**Visual Representation:**
```
Error (°C below target)
    2.0 ████████████████████████████ 100% (Band Max)
        │
    0.8 ├────────────────────────────── band_2_error threshold
        │
    0.5 ████████████████ 70% (Band 2)
        │
    0.3 ├────────────────────────────── band_1_error threshold
        │
    0.15 ███████ 40% (Band 1)
         │
    0.0  ─────────────────────────────── setpoint
```

**Key Features:**
- **Numbered naming**: `band_N_error` and `band_N_percent` for clarity and extensibility
- **Flexible structure**: Supports 0, 1, or 2 thresholds (0/1/2 heating bands)
- **Cascading defaults**: Missing percentages cascade to next higher band
- **Invariant enforcement**: If calling for heat, valve MUST be > 0% (prevents stuck states)

**Why Stepped Bands?**
- **Proportional response** without PID complexity
- **Fast response** to large errors (100% immediately)
- **Gentle approach** near target (40% prevents overshoot)
- **Simple tuning** (2 thresholds + 3 percentages)
- **Predictable behavior** (discrete states easier to debug)

### Band Transition Hysteresis

Band changes use hysteresis to prevent rapid switching between bands:

**Algorithm:**
```python
# Determine target_band based on error (no hysteresis)
if error < band_1_error:           target_band = 1
elif error < band_2_error:         target_band = 2
else:                              target_band = 'max'

# Apply hysteresis when changing bands
if target_band > current_band:
    # Increasing (need to cross threshold to move up)
    if error >= threshold:
        new_band = target_band
        
elif target_band < current_band:
    # Decreasing (need to drop below threshold - hysteresis)
    if error < threshold - step_hysteresis_c:
        new_band = target_band  # Can drop multiple bands at once
```

**Key Rules:**
1. **Increasing demand**: Must reach threshold to jump bands
2. **Decreasing demand**: Must drop below threshold - 0.05°C to reduce
3. **Prevents oscillation**: 0.05°C hysteresis on each threshold

**Example Transition:**

```
Current band: Band 1 (40%), error = 0.28°C

Temperature drops, error increases to 0.31°C:
  • band_1_error (0.30) reached
  • Transition to Band 2 (70%)

Temperature rises, error decreases to 0.24°C:
  • band_1_error (0.30) - step_hyst (0.05) = 0.25°C
  • 0.24 < 0.25 → transition to Band 1 (40%)
```

**Hysteresis Gap:** Each threshold has a 0.05°C gap where band won't change when decreasing.

**Implementation:**
- System calculates target_band based on current error
- Multi-band jumps allowed when increasing demand (fast response to large errors)
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

Room controller computes desired valve positions, but valve commands are coordinated through the `ValveCoordinator`:

**Normal Operation:**
```python
# In app.py recompute_all():
for room_id in config.rooms:
    data = rooms.compute_room(room_id, now)  # Calculate valve %
    
    # ValveCoordinator applies all overrides and sends final command
    valve_coordinator.apply_valve_command(room_id, data['valve_percent'], now)
```

**Pump Overrun / Boiler Safety:**
```python
# Boiler controller sets persistence in valve coordinator
boiler.update_state(...)  # Internally calls valve_coordinator.set_persistence_overrides()

# App just calls coordinator for all rooms - it handles persistence automatically
for room_id in config.rooms:
    data = rooms.compute_room(room_id, now)
    valve_coordinator.apply_valve_command(room_id, data['valve_percent'], now)
    # Coordinator applies: persistence > corrections > normal (priority handled internally)
```

**Key Point:** Room controller computes desired valve position. `ValveCoordinator` acts as single authority, applying persistence overrides from boiler controller, corrections from TRV feedback, and normal values with explicit priority handling.

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
    "band_1_error": 0.30,
    "band_2_error": 0.80,
    "band_0_percent": 0.0,
    "band_1_percent": 40.0,
    "band_2_percent": 70.0,
    "band_max_percent": 100.0,
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
      band_max_percent: 80     # Limit max heat
      # Other params inherit defaults (with cascading)
```

**Merging Logic:** Room-specific values override defaults, missing percentages cascade to next higher band.

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

The `TRVController` class (`trv_controller.py`) manages all interactions with Thermostatic Radiator Valves (TRVs), implementing command/feedback logic with automatic retry, position verification, and setpoint locking. The system is designed specifically for **TRVZB valves** via Zigbee2MQTT.

**Key Features:**
- Non-blocking command execution with feedback confirmation
- Automatic retry on command failures (up to 3 attempts)
- Rate limiting to prevent excessive TRV commands
- Unexpected position detection and correction
- TRV setpoint locking at 35°C (bypasses internal control)
- Tolerance-based feedback matching
- **Cached feedback reads** - 5-second TTL reduces redundant HA API calls
- **Encapsulated sensor access** - Single source of truth for TRV feedback

### TRV Responsibility Encapsulation (2025-11-20)

The TRVController provides three public methods that encapsulate all TRV sensor access, eliminating cross-component coupling:

**1. `get_valve_feedback(room_id) -> Optional[int]`**
Returns current TRV valve position from feedback sensor (0-100%). Results are cached for 5 seconds to avoid excessive HA queries.

```python
feedback = self.trvs.get_valve_feedback('pete')
# Returns: 70 (or None if unavailable)
```

**Why caching matters:**
- Multiple components need TRV feedback during same recompute cycle
- Without cache: N components = N HA API calls per TRV per cycle
- With cache: N components = 1 HA API call per TRV per 5 seconds
- Reduces load on HA and improves performance

**2. `get_valve_command(room_id) -> Optional[int]`**
Returns the last commanded valve position (what we sent to the TRV, not what it reports back).

```python
commanded = self.trvs.get_valve_command('pete')
# Returns: 65 (what we commanded)
```

**Critical distinction:**
- `get_valve_feedback()` returns what the TRV **reports** (current actual position)
- `get_valve_command()` returns what we **commanded** (last sent position)
- Used for proper feedback validation (see below)

**3. `is_valve_feedback_consistent(room_id, tolerance=5.0) -> bool`**
Checks if TRV feedback matches last commanded value within tolerance.

```python
consistent = self.trvs.is_valve_feedback_consistent('pete', tolerance=5.0)
# Returns: True if abs(feedback - commanded) <= 5.0
```

**Used by boiler controller:**
The boiler state machine uses this to validate that TRVs have responded to commands before transitioning states. **CRITICAL**: This checks against `trv_last_commanded` (what was previously sent), NOT against future desired positions.

**Benefits of encapsulation:**
- **Single source of truth** - All TRV feedback reads go through one path
- **Decoupling** - Components don't need to know TRV entity naming patterns
- **Performance** - 5-second cache reduces redundant HA API calls
- **Consistency** - All components see same cached value during recompute
- **Testability** - Mock TRVController methods instead of HA entities
- **Maintainability** - TRV entity changes only affect `trv_controller.py`

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
Critical check during pump overrun - do NOT trigger corrections when valve persistence is active. This is coordinated through the `ValveCoordinator`:

```python
# In trv_controller.check_feedback_for_unexpected_position():
if persistence_active:
    return  # Valve persistence is expected - don't fight it

# Called from app.py with persistence status from coordinator:
persistence_active = self.valve_coordinator.is_persistence_active()
self.trvs.check_feedback_for_unexpected_position(
    room_id, feedback_percent, now, persistence_active
)
```

**Key Change (Nov 2025):** Previously checked `boiler_state` directly, creating coupling. Now uses `persistence_active` flag from `ValveCoordinator`, eliminating cross-component dependency.

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

The `BoilerController` class (`boiler_controller.py`) implements a **6-state finite state machine (FSM)** that manages central boiler operation with safety features. The system prevents dangerous conditions like no-flow heating, excessive cycling, and residual heat buildup.

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
- `set_boiler_on()` - Call climate.turn_on service
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
- `set_boiler_off()` - Call climate.turn_off service
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
  • pete: 40% (Band 1)
  • lounge: 40% (Band 1)
  • abby: 0% (not calling)
  • Total: 80%

Min required: 100%
Result: INTERLOCK_BLOCKED (80 < 100)
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
   - Boiler commanded off (via climate.turn_off)
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
call_service('climate/turn_on', entity_id='climate.opentherm_heating')
```

**Turn Off:**
```python
call_service('climate/turn_off', entity_id='climate.opentherm_heating')
```

**Control Method:**
- Direct on/off control via climate entity services
- Entity `climate.opentherm_heating` manages its own setpoint
- No need to set temperature during on/off commands
- Simpler and cleaner than previous setpoint-based control

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
  entity_id: climate.opentherm_heating
  
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

### State Desynchronization Detection

**Purpose:** Automatically detect and recover from state machine desynchronization where the FSM state doesn't match the actual boiler entity state.

**Problem Scenario:**
State desynchronization can occur when:
- Master enable is toggled OFF then ON
- AppDaemon restarts while boiler is heating
- Manual intervention via Home Assistant UI
- Network issues cause command delivery failure

**Symptom:**
```
State Machine: STATE_ON
Boiler Entity: off (not heating)

Result: System thinks it already commanded boiler on,
        so it won't send turn_on command again.
        Heating is stuck disabled.
```

**Detection Logic:**
```python
# In boiler_controller.py:update_state()
# Runs at start of every state update cycle

boiler_entity_state = get_state('climate.opentherm_heating')

if self.boiler_state == STATE_ON and boiler_entity_state == "off":
    # DESYNC DETECTED!
    log WARNING: "Boiler state desync detected"
    
    # Automatic correction
    transition_to(STATE_OFF, now, "state desync correction - entity is off")
    
    # Cancel stale timers that may prevent proper restart
    cancel_timer(HELPER_BOILER_MIN_ON_TIMER)
    cancel_timer(HELPER_BOILER_OFF_DELAY_TIMER)
    
    # Next cycle will re-evaluate from STATE_OFF and command ON if needed
```

**Why This Works:**
1. Detection happens before any state transition logic
2. Forces state machine back to known-good state (OFF)
3. Cancels timers that assumed continuous operation
4. Next recompute cycle will properly evaluate demand
5. If demand exists, normal OFF → PENDING_ON → ON transition occurs

**Logging:**
```
WARNING: ⚠️ Boiler state desync detected: state machine=ON but climate entity=off.
WARNING: This can occur after master enable toggle or system restart.
WARNING: Resetting state machine to STATE_OFF to allow proper re-ignition.
```

**Edge Cases Handled:**
- **Multiple consecutive desyncs:** Each cycle will correct until synchronized
- **Entity state unknown/unavailable:** No correction (wait for valid state)
- **Other states (PENDING_ON, PUMP_OVERRUN):** Only checks STATE_ON (most critical)

**Why Only STATE_ON:**
- STATE_ON expects entity to be actively heating
- Other states have more ambiguous entity expectations
- False positives in other states would cause unnecessary disruption
- STATE_ON desync is the most dangerous (blocked heating)

**Common Causes:**

| Cause | How It Happens | Recovery |
|-------|----------------|----------|
| Master enable toggle | Master OFF forces entity off but FSM may not update instantly | Immediate on next cycle |
| AppDaemon restart | FSM state lost, initialized from entity state, but can be wrong | Immediate on next cycle |
| Manual boiler control | User turns off via HA UI while FSM thinks ON | Immediate on next cycle |
| Command delivery failure | turn_on service call fails but FSM transitions anyway | Detected and corrected |

**Safety Impact:**
- **Without detection:** Heating can be stuck disabled indefinitely
- **With detection:** Automatic recovery within 60 seconds (one recompute cycle)
- **No false shutdowns:** Only corrects when clear desync detected

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

## Setpoint Ramping

### Overview

The `SetpointRamp` class (`setpoint_ramp.py`) implements physics-aware dynamic boiler setpoint ramping to prevent short-cycling **within** heating cycles. When enabled, it monitors the "headroom" between current flow temperature and the boiler's shutoff point (setpoint + hysteresis), and jumps the setpoint to an optimal position when headroom becomes critically low.

**Key Features:**
- Optional feature controlled by `input_boolean.pyheat_setpoint_ramp_enable`
- Physics-aware: Uses actual boiler hysteresis from `number.opentherm_heating_hysteresis`
- Single-step convergence: Jumps directly to target position (not incremental)
- DHW detection: Skips ramping during hot water events (prevents incorrect readings)
- Maximum setpoint limit for safety (`input_number.pyheat_opentherm_setpoint_ramp_max`)
- Automatic reset to baseline on flame-OFF events
- Coordination with cycling protection for robust short-cycling prevention
- Integer-only setpoints for boiler compatibility

### Configuration

Setpoint ramping is configured in `boiler.yaml`:

```yaml
boiler:
  setpoint_ramp:
    buffer_c: 2.0            # Trigger ramping when headroom <= 2°C
    setpoint_offset_c: 2     # Set new setpoint 2°C below flow temp
```

**Critical constraint:** `buffer_c + setpoint_offset_c + 1 <= boiler_hysteresis`
- Validated on startup, fails loudly if violated
- Ensures stability (prevents immediate re-trigger after ramping)
- The `+1` accounts for floor() precision loss

Home Assistant entities:
- `input_boolean.pyheat_setpoint_ramp_enable` - Enable/disable feature
- `number.opentherm_heating_hysteresis` - Boiler's internal hysteresis (required, typically 5°C)
- `input_number.pyheat_opentherm_setpoint` - Baseline setpoint (user's desired value)
- `input_number.pyheat_opentherm_setpoint_ramp_max` - Maximum ramped setpoint (safety limit)

### Operation

#### Headroom-Based Algorithm

The algorithm directly calculates proximity to boiler shutoff:

```python
current_headroom = setpoint + boiler_hysteresis - flow_temp

if current_headroom <= buffer_c:
    new_setpoint = floor(flow_temp) - setpoint_offset_c
```

**Example (hysteresis=5°C, buffer=2°C, offset=2°C):**
```
Baseline setpoint: 55°C
Boiler shutoff point: 55 + 5 = 60°C
Flow temp: 58.5°C

Headroom: 55 + 5 - 58.5 = 1.5°C
1.5 <= 2.0 → TRIGGER RAMP

New setpoint: floor(58.5) - 2 = 56°C
New shutoff point: 56 + 5 = 61°C
New headroom: 56 + 5 - 59 = 2°C (stable, won't re-trigger)
```

**Why this works:**
- Monitors actual shutoff risk, not arbitrary thresholds
- Single jump to optimal position (not gradual stepping)
- Adapts to different boiler hysteresis values
- Integer setpoints match boiler's actual behavior

#### DHW Detection

Before evaluating ramping, the algorithm checks for DHW (Domestic Hot Water) activity:

```python
# Check both DHW binary sensor and flow rate
dhw_active = (dhw_binary == 'on') or (dhw_flow_rate > 0.0)

if dhw_active:
    return None  # Skip ramping - flow temp is hot water, not CH
```

**Why DHW detection is critical:**
- During hot water use, flow temp sensor reads hot water temperature (e.g., 67°C)
- This is NOT central heating flow - it's DHW circuit temperature
- Without detection, algorithm would incorrectly ramp based on artificially high readings
- DHW check prevents spurious ramping during morning/evening hot water usage

#### Reset Strategy: Flame-OFF Events

The setpoint is automatically reset to baseline whenever the flame goes OFF:

```python
# In setpoint_ramp.py:on_flame_off()
def on_flame_off(self, entity, attribute, old, new, kwargs):
    if old == "on" and new == "off":
        # Skip reset if in cooldown (cooldown owns setpoint control)
        if cycling_state != COOLDOWN:
            self.reset_to_baseline()
```

**Why flame-OFF?**
- Flame OFF indicates end of heating cycle (normal completion or DHW interruption)
- Natural reset point: boiler must restart heating with user's desired baseline
- Prevents setpoint "creep" across multiple heating cycles
- Simple and robust: no complex state tracking needed

**Flame-OFF triggers include:**
- **DHW events**: Hot water tap usage interrupts heating, flame goes OFF
- **Demand loss**: All rooms satisfied, no further heating needed
- **Cooldown**: Cycling protection forces cooldown period (reset deferred until cooldown exit)

#### Coordination with Cycling Protection

Setpoint ramping and cycling protection work together:

**Within a heating cycle:**
- Setpoint ramp prevents flame cycling by maintaining adequate headroom to shutoff
- If flame cycles anyway, cycling protection takes over with cooldown

**Between heating cycles:**
- Flame-OFF resets ramped setpoint to baseline
- Next heating cycle starts fresh with user's desired setpoint
- Cycling protection monitors for excessive flame cycling across burns

**Critical interaction:**
- Cycling protection stores `(timestamp, flow_temp, setpoint)` tuples in history
- Each flow measurement paired with its corresponding setpoint
- Correctly handles ramped setpoints when detecting overheat conditions
- During cooldown, setpoint ramp defers to cycling protection (cooldown owns setpoint at 30°C)

**Key insight:** Setpoint ramping prevents **intra-cycle** short-cycling (flame ON→OFF→ON during single demand period). Cycling protection prevents **inter-cycle** short-cycling (rapid successive heating cycles).

### State Management

#### Physical State Detection

Setpoint ramp state is **inferred from physical boiler setpoint** at startup (not persisted):

```python
def initialize_from_ha(self):
    """Detect state from actual boiler setpoint."""
    boiler_setpoint = self.ad.get_state(C.OPENTHERM_CLIMATE, attribute='temperature')
    helper_setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
    
    if boiler_setpoint > helper_setpoint + 0.1 and flame_is_on:
        # Actively ramping - continue from current position
        self.state = RAMPING
        self.current_ramped_setpoint = boiler_setpoint
    else:
        # Normal operation - start at baseline
        self.state = INACTIVE
        self.baseline_setpoint = helper_setpoint
```

**Rationale:**
- Physical boiler setpoint is source of truth
- Eliminates desync issues from stale persistence
- Self-correcting on every restart
- Simpler than maintaining separate state file

#### Runtime State

During operation, tracks:
- `baseline_setpoint`: User's desired baseline from helper
- `current_ramped_setpoint`: Current ramped value (if actively ramping)
- `state`: INACTIVE or RAMPING
- `ramp_steps_applied`: Count of ramp steps (for logging)
- `buffer_c`, `setpoint_offset_c`, `boiler_hysteresis`: Configuration values

### Integration Points

**App.py Initialization:**
```python
# Create setpoint ramp controller
self.setpoint_ramp = SetpointRamp(
    ad=self,
    config=self.config,
    cycling_protection_ref=self.cycling,
    app_ref=self
)

# Initialize from physical boiler state
self.setpoint_ramp.initialize_from_ha()

# Register flame sensor callback for automatic reset
self.listen_state(
    self.setpoint_ramp.on_flame_off,
    C.OPENTHERM_FLAME
)
```

**Recompute Cycle:**
```python
# In app.py:recompute_all()
# After boiler controller decides to heat
if boiler_state == C.STATE_ON and self.setpoint_ramp:
    new_setpoint = self.setpoint_ramp.evaluate_and_apply(
        flow_temp=flow_temp,
        current_setpoint=current_setpoint,
        baseline_setpoint=baseline_setpoint,
        boiler_state=boiler_state,
        cycling_state=cycling_state
    )
    
    if new_setpoint is not None:
        # Apply new ramped setpoint to climate entity
        self.call_service(
            'climate/set_temperature',
            entity_id=C.OPENTHERM_CLIMATE,
            temperature=new_setpoint
        )
```

**Validation:**
```python
# Startup validation in _load_and_validate_config()
if buffer_c + setpoint_offset_c + 1 > boiler_hysteresis:
    raise ValueError(
        f"Invalid setpoint_ramp config: buffer ({buffer_c}) + "
        f"offset ({setpoint_offset_c}) + 1 must be <= "
        f"hysteresis ({boiler_hysteresis}) to prevent oscillation"
    )
```

**Boiler Controller:**
```python
# Setpoint ramp operates independently of boiler FSM
# Boiler FSM sees ramped setpoint as current target
# Changes applied directly to input_number.pyheat_opentherm_setpoint
```

### Logging

Setpoint ramp provides detailed logging for debugging:

**Ramp Triggered:**
```
INFO: Setpoint ramp: 65.0C -> 66.0C (flow=62.8C, within 3.0C threshold)
```

**Reset on Flame-OFF:**
```
INFO: Setpoint ramp reset: flame OFF (67.0C -> 65.0C)
```

**Feature Disabled:**
```
DEBUG: Setpoint ramp: disabled via input_boolean
```

**Max Limit Reached:**
```
INFO: Setpoint ramp: at maximum 70.0C (not ramping further)
```

### Use Cases

**Scenario 1: Single Room Heating**
```
Pete's room calls for heat
Boiler starts at 65°C baseline
Flow temp rises: 60° → 62° → 63°
At 62°C: ramp to 66°C
At 63°C: ramp to 67°C
Pete's room satisfied, demand drops
Flame goes OFF → reset to 65°C
```

**Scenario 2: DHW Interruption**
```
Multiple rooms heating
Setpoint ramped to 68°C
Someone turns on hot water tap
DHW takes priority, flame goes OFF
Setpoint resets to 65°C
DHW finishes, heating resumes at 65°C baseline
```

**Scenario 3: Cooldown Recovery**
```
High return temp triggers cycling protection cooldown
Flame goes OFF → setpoint reset to 65°C
Cooldown period: boiler at 30°C, monitoring return temp
Cooldown exits when return temp drops
Heating resumes at 65°C baseline (not ramped value)
```

### Benefits

1. **Prevents intra-cycle flame cycling**: Maintains healthy delta-T during heating
2. **Simple and predictable**: Always resets on flame-OFF, no complex state preservation
3. **User-centric**: Each heating cycle starts with user's preferred baseline
4. **Safe**: Maximum limit prevents excessive setpoint
5. **Non-invasive**: Optional feature, easily disabled via input_boolean
6. **Complementary**: Works alongside cycling protection for comprehensive short-cycle prevention

### Implementation Details

**Module:** `controllers/setpoint_ramp.py` (497 lines)

**Key Methods:**
- `initialize_from_ha()`: Start at baseline on AppDaemon restart
- `evaluate_and_apply()`: Check conditions and ramp if needed
- `on_flame_off()`: Reset to baseline on flame OFF events
- `on_cooldown_exited()`: Verify baseline after cooldown (safety check)
- `reset_to_baseline()`: Core reset logic
- `should_trigger_ramp()`: Evaluate ramp conditions
- `compute_next_setpoint()`: Calculate next ramped value

**Dependencies:**
- `constants.py`: Entity names and default values
- `config_loader.py`: Setpoint ramp configuration from boiler.yaml
- `cycling_protection.py`: Cooldown state coordination

**Home Assistant Entities Used:**
- `input_boolean.pyheat_setpoint_ramp_enable`
- `input_number.pyheat_opentherm_setpoint` (read and write)
- `input_number.pyheat_opentherm_setpoint_ramp_max`
- `binary_sensor.opentherm_flame` (state listener)
- `sensor.opentherm_heating_temp` (flow temperature)

---

## Master Enable Control

### Overview

The master enable switch (`input_boolean.pyheat_master_enable`) provides a global on/off control for the entire PyHeat system. When disabled, the system enters a safe state that allows manual boiler operation while protecting radiators and maintaining safe water circulation.

**Entity:** `input_boolean.pyheat_master_enable`

**Purpose:**
- Emergency system shutdown
- Maintenance mode (manual boiler control)
- Seasonal disable (summer months)
- Testing and debugging

### Behavior When Master Enable = OFF

When the master enable is turned OFF, PyHeat executes a coordinated shutdown sequence:

**1. All Valves Forced to 100%**
```python
for room_id in rooms:
    set_valve(room_id, 100, now, is_correction=True)
```

**Why 100%?**
- Allows manual boiler operation without PyHeat control
- Ensures all radiators can receive heat if boiler runs
- Prevents pressure buildup in heating system
- Provides safe water circulation paths
- User can manually control boiler via Home Assistant or physical controls

**2. Boiler Turned Off**
```python
call_service('climate/turn_off', entity_id='climate.opentherm_heating')
```

**3. State Machine Reset**
```python
transition_to(STATE_OFF, now, "master enable disabled")
```

**Critical:** State machine MUST be reset to `STATE_OFF`. Without this:
- FSM remains in previous state (e.g., `STATE_ON`)
- When master enable turns back ON, FSM thinks it already commanded boiler
- No `turn_on` command sent → heating remains off
- State desync detection would eventually correct, but explicit reset is cleaner

**4. All Timers Cancelled**
```python
cancel_timer(HELPER_BOILER_MIN_ON_TIMER)
cancel_timer(HELPER_BOILER_OFF_DELAY_TIMER)
cancel_timer(HELPER_PUMP_OVERRUN_TIMER)
cancel_timer(HELPER_BOILER_MIN_OFF_TIMER)
```

**Why?** Stale timers from previous operation could interfere with restart.

**5. TRV Setpoints Remain at 35°C**

Note: When master enable is OFF, TRV setpoints are NOT unlocked. This is intentional:
- Setpoints already at 35°C (locked during normal operation)
- No need to change them
- Prevents TRVs from closing valves to their internal setpoint
- Maintains 100% valve opening for manual control

**6. Status Sensors Updated**
```python
for room_id in rooms:
    publish_room_entities(room_id, {
        'valve_percent': 100,
        'calling': False,
        'target': None,
        'mode': 'off',
        'temp': current_temp,
        'is_stale': is_stale
    })
```

**System status:** `master_off`

**7. No Recompute Triggered**

Critical: After disabling, NO recompute is triggered. Why?
- Recompute would overwrite the 100% valve positions
- Status already updated manually
- System is disabled, no control decisions needed

### Behavior When Master Enable = ON

When master enable is turned back ON, PyHeat resumes normal operation:

**1. Lock All TRV Setpoints to 35°C**
```python
run_in(lock_all_trv_setpoints, 1)  # 1 second delay

def lock_all_trv_setpoints():
    for room_id, room_config in rooms.items():
        trv_config = room_config['trv']
        climate_entity = trv_config['climate_entity']
        call_service('climate/set_temperature',
                    entity_id=climate_entity,
                    temperature=35.0)
```

**Why 1 second delay?**
- Allows Home Assistant to process the input_boolean state change
- Prevents overwhelming HA with simultaneous service calls
- TRV climate entities need time to become available

**2. Trigger Full System Recompute**
```python
trigger_recompute("master_enable_changed")
```

**What happens:**
- All room temperatures re-evaluated
- Targets recalculated (schedules, overrides, modes)
- Call-for-heat decisions made
- Valve percentages computed
- Boiler FSM evaluates demand
- Normal operation resumes

**Initial State After Enable:**
- Boiler FSM: `STATE_OFF` (from explicit reset during disable)
- Room states: Re-initialized from current conditions
- Valves: Will be commanded to calculated positions (likely not 100% anymore)
- Boiler: Will turn on if demand exists and interlocks satisfied

### Implementation Details

**Callback Registration:**
```python
# In app.py:initialize()
listen_state(master_enable_changed, 'input_boolean.pyheat_master_enable')
```

**Full Callback Code:**
```python
def master_enable_changed(entity, attribute, old, new, kwargs):
    log(f"Master enable changed: {old} -> {new}")
    
    if new == "off":
        log("Master enable OFF - opening all valves to 100% and shutting down system")
        now = datetime.now()
        
        # Force all valves to 100%
        for room_id in rooms.keys():
            # is_correction=True bypasses rate limiting
            trvs.set_valve(room_id, 100, now, is_correction=True)
            
            # Update status sensors
            temp, is_stale = sensors.get_room_temperature_smoothed(room_id, now)
            room_data = {
                'valve_percent': 100,
                'calling': False,
                'target': None,
                'mode': 'off',
                'temp': temp,
                'is_stale': is_stale
            }
            status.publish_room_entities(room_id, room_data, now)
        
        # Shut down boiler
        boiler._set_boiler_off()
        boiler._transition_to(STATE_OFF, now, "master enable disabled")
        
        # Cancel all timers
        boiler._cancel_timer(HELPER_BOILER_MIN_ON_TIMER)
        boiler._cancel_timer(HELPER_BOILER_OFF_DELAY_TIMER)
        boiler._cancel_timer(HELPER_PUMP_OVERRUN_TIMER)
        boiler._cancel_timer(HELPER_BOILER_MIN_OFF_TIMER)
        
        # DO NOT trigger recompute
    
    elif new == "on":
        log("Master enable ON - locking TRV setpoints and resuming operation")
        
        # Lock setpoints (1 second delay)
        run_in(lock_all_trv_setpoints, 1)
        
        # Resume normal operation
        trigger_recompute("master_enable_changed")
```

### Use Cases

**1. Emergency Shutdown**
```
Scenario: Temperature sensor fails, room overheating
Action: Turn off master enable
Result: All valves 100%, boiler off, manual control possible
```

**2. Manual Boiler Control**
```
Scenario: Testing new boiler configuration
Action: Disable master enable
Result: Can manually control boiler via HA, all radiators available
```

**3. Seasonal Disable**
```
Scenario: Summer months, no heating needed
Action: Turn off master enable
Result: System fully disabled, no unnecessary valve commands or processing
```

**4. Maintenance Mode**
```
Scenario: Bleeding radiators, plumbing work
Action: Disable master enable
Result: All valves open, boiler off, safe for maintenance
```

**5. Debugging**
```
Scenario: Investigating system behavior
Action: Toggle master enable to reset all state
Result: Clean restart with known initial conditions
```

### Safety Considerations

**Valve Forcing (100%):**
- ✅ Allows emergency heat distribution if boiler runs
- ✅ Prevents water hammer from closed valves
- ✅ Maintains system pressure balance
- ✅ Enables manual heating control
- ⚠️ All rooms equally open (no zone control)

**State Machine Reset:**
- ✅ Prevents desync issues on re-enable
- ✅ Clean slate for restart
- ✅ Cancels stale timers
- ℹ️ Loses FSM history (not usually needed)

**No Recompute on Disable:**
- ✅ Preserves 100% valve positions
- ✅ Status already updated
- ⚠️ System remains in disabled state until re-enabled

**TRV Setpoint Behavior:**
- ℹ️ Setpoints remain at 35°C when disabled
- ℹ️ Re-locked to 35°C when re-enabled
- ✅ Prevents TRV internal control from closing valves

### Interaction with Other Features

**Recompute Cycle:**
```python
# In app.py:recompute_all()
if master_enable != "on":
    # Skip all control logic
    return
```

**Periodic Trigger:**
```python
# 60-second timer still fires, but recompute_all() exits early
# Minimal CPU usage when disabled
```

**Service Calls:**
```python
# Services (override, set_mode, etc.) still accepted
# But have no effect until master enable turned ON
```

**Configuration Reload:**
```python
# Config can be reloaded while disabled
# Changes take effect when re-enabled
```

### Logging

**Disable:**
```
INFO: Master enable changed: on -> off
INFO: Master enable OFF - opening all valves to 100% and shutting down system
INFO: Boiler: on → off (master enable disabled)
```

**Enable:**
```
INFO: Master enable changed: off -> on
INFO: Master enable ON - locking TRV setpoints to 35C and resuming operation
INFO: Locking TRV setpoints to 35C for all rooms
INFO: Triggering recompute: master_enable_changed
```

---

## Service Interface

### AppDaemon Services

PyHeat registers services with **AppDaemon** (not Home Assistant). These services are **internal to AppDaemon** and are not automatically exposed as native Home Assistant services.

**Service Registration:**
```python
# In service_handler.py
ad.register_service("pyheat/override", svc_override)
ad.register_service("pyheat/cancel_override", svc_cancel_override)
# ... etc
```

**Available Services:**
- `pyheat/override` - Set temperature override (absolute target OR relative delta, duration OR end time)
- `pyheat/cancel_override` - Cancel active override
- `pyheat/set_mode` - Change room mode (auto/manual/off)
- `pyheat/set_default_target` - Update schedule default target
- `pyheat/reload_config` - Reload configuration files
- `pyheat/get_schedules` - Retrieve schedule configuration
- `pyheat/get_rooms` - Retrieve room configuration
- `pyheat/replace_schedules` - Replace entire schedule config
- `pyheat/get_status` - Get complete system and room status

**How These Services Are Accessible:**

1. **From other AppDaemon apps** - Using `self.call_service("pyheat/override", room="pete", ...)`
2. **Via AppDaemon's REST API** - HTTP endpoints at `/api/appdaemon/...` (see REST API section below)
3. **From pyheat-web** - Uses the REST API endpoints

**Calling from Home Assistant:**

AppDaemon services are NOT automatically available as native HA services. To call them from Home Assistant, use one of these approaches:

**Option 1: REST Commands (Direct API calls)**
```yaml
# In configuration.yaml
rest_command:
  pyheat_override:
    url: "http://localhost:5050/api/appdaemon/pyheat_override"
    method: POST
    content_type: "application/json"
    payload: >
      {
        "room": "{{ room }}",
        "target": {{ target }},
        "minutes": {{ minutes }}
      }

# Then call in automations:
service: rest_command.pyheat_override
data:
  room: "pete"
  target: 21.0
  minutes: 120
```

**Option 2: Script Wrappers (Cleaner interface)**
```yaml
# In scripts.yaml
pyheat_override:
  alias: "PyHeat Temperature Override"
  fields:
    room:
      description: "Room ID (pete, lounge, office, etc.)"
      example: "pete"
    target:
      description: "Target temperature in °C"
      example: 21.0
    minutes:
      description: "Duration in minutes"
      example: 120
  sequence:
    - service: rest_command.pyheat_override
      data:
        room: "{{ room }}"
        target: "{{ target }}"
        minutes: "{{ minutes }}"

# Then call in automations:
service: script.pyheat_override
data:
  room: "pete"
  target: 21.0
  minutes: 120
```

### Service Handler Implementation

The `ServiceHandler` class manages service registration and execution with parameter validation and error handling. Services are synchronous (blocking) to ensure state consistency.

---

## Alert Manager

PyHeat includes an alert management system (`alert_manager.py`) that creates Home Assistant persistent notifications for critical issues requiring user attention.

### Design Principles

**Debouncing**: Requires 3 consecutive identical errors before creating an alert to prevent false positives from transient issues.

**Rate Limiting**: Maximum 1 notification per alert type per hour to prevent notification spam.

**Auto-clearing**: Automatically dismisses notifications when the underlying condition resolves (for most alert types).

**Room Context**: Includes affected room information in notifications when applicable.

**Severity Levels**: Critical (immediate attention) and Warning (informational) levels.

### Alert Types

**Critical Alerts:**

1. **Boiler Interlock Failure** (`ALERT_BOILER_INTERLOCK_FAILURE`)
   - Trigger: Boiler was running but valve interlock check failed (insufficient valve opening)
   - Action: Boiler immediately turned off for safety
   - Auto-clear: Yes (when valves reopen properly)

2. **TRV Feedback Timeout** (`ALERT_TRV_FEEDBACK_TIMEOUT_{room_id}`)
   - Trigger: TRV valve commanded but feedback doesn't match after 3 retries
   - Indicates: TRV battery low, connectivity issue, or mechanical failure
   - Auto-clear: Yes (when valve feedback confirms position)

3. **TRV Unavailable** (`ALERT_TRV_UNAVAILABLE_{room_id}`)
   - Trigger: TRV feedback sensor unavailable/unknown after multiple retries
   - Indicates: Lost communication with TRV
   - Auto-clear: Yes (when sensor becomes available)

4. **Boiler Control Failure** (`ALERT_BOILER_CONTROL_FAILURE`)
   - Trigger: Failed to turn boiler on/off via HA service call
   - Indicates: Network issue or boiler entity unavailable
   - Auto-clear: Yes (when next control command succeeds)

5. **Configuration Load Failure** (`ALERT_CONFIG_LOAD_FAILURE`)
   - Trigger: YAML syntax error or validation failure during config load
   - Indicates: Configuration file corruption or syntax error
   - Auto-clear: No (requires manual fix and reload)

### Integration with Controllers

The alert manager is initialized first in `app.py` and passed to controllers that need to report errors:

```python
self.alerts = AlertManager(self)  # Initialize first
self.trvs = TRVController(self, self.config, self.alerts)
self.boiler = BoilerController(self, self.config, self.alerts)
```

**TRV Controller Integration:**
- Reports `ALERT_TRV_FEEDBACK_TIMEOUT` after valve command retries exhausted
- Reports `ALERT_TRV_UNAVAILABLE` when feedback sensor is unavailable
- Clears alerts when valve feedback confirms position

**Boiler Controller Integration:**
- Reports `ALERT_BOILER_INTERLOCK_FAILURE` when running with insufficient valves open
- Reports `ALERT_BOILER_CONTROL_FAILURE` on HA service call exceptions
- Clears alerts when control commands succeed

**App Integration:**
- Reports `ALERT_CONFIG_LOAD_FAILURE` on YAML load exceptions
- Used during initialization and config reload operations

### Implementation Details

**Consecutive Error Tracking:**
```python
self.error_counts[alert_id] = self.error_counts.get(alert_id, 0) + 1
if self.error_counts[alert_id] >= self.debounce_threshold:
    # Create alert
```

**Rate Limiting:**
```python
last_notified = self.notification_history.get(alert_id)
if last_notified and (now - last_notified).total_seconds() < rate_limit_seconds:
    return  # Skip notification
```

**Auto-clearing:**
```python
def clear_error(self, alert_id: str) -> None:
    self.error_counts[alert_id] = 0
    if alert_id in self.active_alerts and alert['auto_clear']:
        self._dismiss_notification(alert_id)
        del self.active_alerts[alert_id]
```

**Home Assistant Integration:**
```python
self.ad.call_service(
    "persistent_notification/create",
    title="⚠️ PyHeat Critical Alert",
    message=full_message,
    notification_id=f"pyheat_{alert_id}"
)
```

Notifications appear in Home Assistant's notification center (bell icon) and can be dismissed manually or automatically.

### API Methods

- `report_error(alert_id, severity, message, room_id, auto_clear)` - Report error condition
- `clear_error(alert_id)` - Clear error condition and dismiss notification
- `get_active_alerts()` - Query currently active alerts
- `get_alert_count(severity)` - Count active alerts by severity

For detailed alert documentation, see [ALERT_MANAGER.md](ALERT_MANAGER.md).

---

## Load Calculator (Radiator Capacity Estimation)

PyHeat includes a load calculator system (`load_calculator.py`) that estimates radiator heat output in real-time using the EN 442 thermal model. This provides visibility into heating system capacity utilization for monitoring and future optimization.

### Design Principles

**Read-Only Monitoring**: Phase 1 implementation provides observability without affecting control decisions. Estimates are exposed via sensors and logged to CSV for analysis.

**Physics-Based Calculation**: Uses EN 442 standard radiator thermal model with measured temperatures and rated radiator specifications.

**Conservative Estimation**: Acknowledges ±20-30% uncertainty due to unknowns (actual flow rate, real radiator condition, installation factors). Not suitable for absolute capacity decisions.

**DHW-Compatible**: Uses helper setpoint (not actual flow temperature) to remain valid during DHW cycles when flow temp may be elevated but heating system is off.

**Cycling-Protection Compatible**: Uses helper setpoint (not climate entity) to remain valid during cycling protection cooldown when climate setpoint temporarily drops to 30°C.

### Thermal Model

The EN 442 standard defines radiator heat transfer as:

```
P = P₅₀ × (ΔT / 50)^n

Where:
  P    = Actual heat output (W)
  P₅₀  = Rated heat output at ΔT = 50°C (W)
  ΔT   = (T_flow + T_return) / 2 - T_room (°C)
  n    = Radiator exponent (1.2-1.3 typical)
```

**PyHeat Implementation:**

```python
# Calculate mean water temperature from estimated system delta_t
t_mean_estimated = setpoint - (system_delta_t / 2)

# Calculate actual delta_t for EN 442 formula
delta_t = t_mean_estimated - room_temp

# Apply EN 442 formula
estimated_capacity = delta_t50 × (delta_t / 50) ^ radiator_exponent
```

**Key Assumptions:**
- **System Delta-T**: Configurable (default 10°C) - difference between flow and return
- **Flow Temperature**: Uses `input_number.pyheat_opentherm_setpoint` (helper entity, not climate setpoint)
- **Radiator Exponent**: Per-room configurable (default 1.3 for panels, 1.2 for towel rails)
- **Delta-T50 Rating**: From manufacturer specifications (configured per-room in `rooms.yaml`)

### Configuration

**Per-Room (rooms.yaml):**

```yaml
pete:
  # ... existing room config ...
  delta_t50: 1900            # Rated output at ΔT=50°C (Watts)
  radiator_exponent: 1.3     # Optional, overrides global default

bathroom:
  # ... existing room config ...
  delta_t50: 415             # Smaller radiator
  radiator_exponent: 1.2     # Towel rail (different exponent)
```

**System-Wide (boiler.yaml):**

```yaml
load_monitoring:
  enabled: true              # Enable capacity estimation
  system_delta_t: 10         # Expected flow-return delta (°C)
  radiator_exponent: 1.3     # Global default (overrideable per-room)
```

**Validation:**
- All rooms must have `delta_t50` configured (raises error on missing)
- `system_delta_t` must be positive
- `radiator_exponent` typically 1.2-1.3 (not validated, user responsibility)

### Home Assistant Entities

**Status Sensor** (`sensor.pyheat_status`):
- **Per-Room Attributes** (in `rooms` dictionary):
  - Standard fields: `mode`, `temperature`, `target`, `calling_for_heat`, `valve_percent`, `is_stale`
  - **New field**: `estimated_dump_capacity` (Watts) - radiator capacity estimate for this room
- **System Attributes**:
  - **New attribute**: `total_estimated_dump_capacity` (Watts) - sum of all per-room capacities

**Example structure:**
```yaml
sensor.pyheat_status:
  state: heating
  attributes:
    rooms:
      pete:
        mode: auto
        temperature: 16.3
        target: 19.0
        calling_for_heat: true
        valve_percent: 80
        is_stale: false
        estimated_dump_capacity: 1502  # NEW
      games:
        mode: auto
        temperature: 18.5
        target: 20.0
        calling_for_heat: true
        valve_percent: 60
        is_stale: false
        estimated_dump_capacity: 1923  # NEW
      # ... other rooms
    total_estimated_dump_capacity: 8450  # NEW
    total_valve_percent: 240
    # ... other system attributes
```

### CSV Logging

**Added Columns** (heating_logs/YYYY-MM-DD.csv):
- `total_estimated_dump_capacity`: System-wide total (W)
- `{room}_estimated_capacity`: Per-room values (7 columns for 7 rooms)

**Update Frequency**: Logged every 60 seconds during periodic recompute cycle.

**Use Case**: Historical analysis of heating capacity utilization, correlation with outdoor temperature, boiler cycling patterns.

### Integration with PyHeat

**Initialization** (app.py):

```python
from managers.load_calculator import LoadCalculator

# After sensor_manager initialization
self.load_calculator = LoadCalculator(self, self.config, self.sensors)
try:
    self.load_calculator.initialize()
except ValueError as e:
    self.log(f"LoadCalculator initialization failed: {e}", level="ERROR")
```

**Periodic Updates** (app.py recompute):

```python
# Calculate estimated capacities
self.load_calculator.update_capacities(
    room_data,  # Dict with room_id -> (target, actual_temp, heating_active)
    self.get_entity("input_number.pyheat_opentherm_setpoint").state
)

# Collect for logging
load_data = {
    'total': self.load_calculator.total_estimated_capacity,
    'rooms': self.load_calculator.estimated_capacities.copy()
}
```

**Status Publishing** (status_publisher.py):

```python
# Create per-room capacity sensors
for room_id in config.rooms:
    self.set_state(
        f"sensor.pyheat_{room_id}_estimated_dump_capacity",
        state=capacity_watts,
        attributes={
            "delta_t50_rating": room_config['delta_t50'],
            # ... other attributes
        }
    )

# Add system total to status sensor
status_attributes['total_estimated_dump_capacity'] = total_capacity
```

### Limitations and Future Work

**Current Limitations (Phase 1):**
- ±20-30% uncertainty due to flow rate unknowns and real-world factors
- Uses estimated mean water temp (not measured flow/return)
- No validation against actual boiler output
- Read-only monitoring (no control integration)

**Future Enhancements (Phase 2+):**
- Room selection algorithm integration (prefer high-capacity rooms when multiple need heat)
- Load-based valve interlock threshold (replace fixed 2-valve minimum with capacity-based check)
- Boiler sizing validation (ensure boiler can meet calculated demand)
- Flow/return temperature sensors for improved accuracy
- Correlation analysis with outdoor temperature and boiler cycling frequency

**When NOT to Use:**
- Absolute capacity calculations (use ±20-30% as guideline only)
- Safety-critical decisions (thermal model is estimation not measurement)
- Real-time control logic (Phase 1 is monitoring-only by design)

### API Methods

**Public Interface:**

```python
def initialize() -> None:
    """Validate configuration and prepare for calculations. Raises ValueError on missing delta_t50."""

def update_capacities(room_data: Dict, helper_setpoint: float) -> None:
    """
    Calculate estimated capacities for all rooms.
    
    Args:
        room_data: Dict[room_id] -> (target_temp, actual_temp, heating_active)
        helper_setpoint: Value from input_number.pyheat_opentherm_setpoint
    
    Updates:
        self.estimated_capacities: Dict[room_id] -> capacity_watts
        self.total_estimated_capacity: Sum of all room capacities
    """

def get_estimated_capacity(room_id: str) -> float:
    """Get most recent estimated capacity for a room (Watts)."""

def calculate_estimated_dump_capacity(
    delta_t50: float,
    room_temp: float, 
    desired_setpoint: float,
    radiator_exponent: float
) -> float:
    """
    Calculate estimated radiator capacity using EN 442 thermal model.
    Returns capacity in Watts, or 0.0 if calculation invalid.
    """
```

**Internal State:**

```python
self.estimated_capacities: Dict[str, float]  # room_id -> estimated_watts
self.total_estimated_capacity: float          # Sum of all rooms
self.system_delta_t: float                    # Configured flow-return delta
self.global_exponent: float                   # Global default radiator exponent
```

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
- `climate.turn_on` - Turn boiler on
- `climate.turn_off` - Turn boiler off

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

- [README.md](../README.md) - Installation, setup, and REST API reference
- [STATUS_FORMAT_SPEC.md](STATUS_FORMAT_SPEC.md) - Status attribute format
- [ALERT_MANAGER.md](ALERT_MANAGER.md) - Alert system documentation
- [TODO.md](TODO.md) - Project tracking and completed features
- [changelog.md](changelog.md) - Detailed change history

### Configuration Examples

- [config/examples/](../config/examples/) - Sample YAML configuration files

---

**Document Version**: 2.0  
**Last Updated**: 2025-11-13  
**Author**: PyHeat Development Team
