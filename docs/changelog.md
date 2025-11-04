# PyHeat AppDaemon Implementation Changelog

## [In Progress] Complete AppDaemon Migration

### Completed Components

#### Core Foundation (2024)
- ✅ **constants.py**: Complete constants module with all defaults
  - Hysteresis configuration (on_delta_c: 0.30, off_delta_c: 0.10)
  - Valve band defaults (stepped percentages: 0%, 33%, 66%, 100%)
  - Safety interlocks (min_on_s: 180, min_off_s: 180)
  - TRV entity patterns for deriving number./sensor. from climate. entities
  - Boiler state machine constants (7 states)
  - Helper entity ID templates

- ✅ **pyheat.py - Initialization & Infrastructure**
  - PyHeat class inheriting from hass.Hass
  - Configuration loading from YAML (rooms.yaml, schedules.yaml)
  - TRV entity ID derivation and validation
  - Callback registration for all state changes:
    - Master enable/disable
    - Holiday mode
    - Per-room mode changes (off/auto/manual)
    - Per-room setpoint changes
    - Per-room timer changes (override/boost)
    - Temperature sensor updates
    - TRV feedback (valve position sensors)
  - 60-second periodic recompute timer
  - State tracking dictionaries initialized

- ✅ **pyheat.py - Core Heating Logic**
  - `recompute_all()`: Main orchestration with master enable check
  - `get_room_temperature()`: Sensor fusion with staleness detection
    - Primary/fallback sensor roles
    - Per-sensor timeout configuration
    - Averaging of multiple sensors
  - `resolve_room_target()`: Target temperature resolution with precedence
    - Precedence: off → manual → override/boost → schedule → default
    - Manual setpoint support
    - Override timer checking
  - `get_scheduled_target()`: Schedule parsing
    - Day-of-week block matching
    - Holiday mode support
    - Default target fallback
  - `compute_call_for_heat()`: Asymmetric hysteresis
    - Per-room on_delta_c and off_delta_c
    - State memory (maintains previous state in deadband)
  - `compute_valve_percent()`: Stepped valve bands
    - Four-band control (0%, low%, mid%, max%)
    - Error-based band selection
  - `set_trv_valve()`: TRV command dispatch
    - Rate limiting (min_interval_s)
    - Change detection (avoid redundant commands)
    - Dual command (opening degree + closing degree)
  - `control_boiler()`: Simplified boiler control
    - On when any room calling
    - Off when no rooms calling
    - Timestamp tracking (last_on, last_off)
  - `publish_status()`: Status entity with attributes
    - State: "heating (N rooms)" or "idle"
    - Attributes: any_call_for_heat, active_rooms, counts, timestamps

- ✅ **Configuration Migration**
  - Copied rooms.yaml (4 rooms: pete, games, lounge, abby)
  - Copied schedules.yaml (weekly schedules)
  - Copied boiler.yaml (boiler configuration)
  - Created config/ directory structure

- ✅ **App Registration**
  - Added pyheat to apps.yaml
  - Configuration: module: pyheat.pyheat, class: PyHeat, log: main_log

### Pending Implementation

#### High Priority
- [ ] **Full Boiler State Machine** (Task 5)
  - 7-state FSM: OFF, PENDING_ON, ON_STABILIZING, ON_STEADY, PENDING_OFF, OFF_STABILIZING, FAULT
  - Anti-cycling: min_on_s, min_off_s enforcement
  - TRV-open interlock
  - Safety timeouts (max_on_m, max_off_m)
  
- [ ] **Valve Band Step Hysteresis** (Task 11)
  - step_hysteresis_c to prevent band oscillation
  - Multi-band jump optimization

- [ ] **TRV Feedback & Retry** (Task 7)
  - Position sensor monitoring
  - Retry on failure (max_retries)
  - Position tolerance checking

#### Medium Priority
- [ ] **Override/Boost Services** (Task 6)
  - Service handlers for room_boost
  - Timer management
  - Target temperature setting

- [ ] **Service Handlers** (Task 9)
  - pyheat.reload: Configuration reload
  - pyheat.force_valve: Manual valve control
  - pyheat.room_boost: Boost temperature

- [ ] **Enhanced Status** (Task 8)
  - Per-room status details
  - Error tracking
  - Valve position reporting

#### Low Priority
- [ ] **Error Handling** (Task 10)
  - Comprehensive try/except blocks
  - Entity validation
  - Graceful degradation

- [ ] **Integration Testing** (Task 12)
  - All mode combinations
  - Sensor failure scenarios
  - Schedule transitions

### Architecture Notes

**Migration from PyScript to AppDaemon:**
- PyScript used async/await with decorator-based triggers (@state_trigger)
- AppDaemon uses synchronous callback model with listen_state()
- PyScript had direct state access, AppDaemon uses call_service()
- Reason for migration: "insurmountable pyscript issues creating a single, consistent state"

**Current Status:** Basic heating operation is implemented. System can:
- Read temperature sensors with staleness detection
- Resolve target temperatures from schedules
- Apply hysteresis to determine call-for-heat
- Control TRV valves with rate limiting
- Turn boiler on/off based on room demand
- Publish status entity

**Next Steps:** Task 4 (Testing) followed by Task 5 (Full boiler state machine)
