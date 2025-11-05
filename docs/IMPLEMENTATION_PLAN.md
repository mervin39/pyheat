# PyHeat AppDaemon Implementation Plan

## Status: Foundation Complete, Core Logic Required

### What's Been Completed ✅

1. **Project Structure**
   - `/opt/appdata/appdaemon/conf/apps/pyheat/` directory created
   - Configuration files copied from PyScript version
   - `constants.py` - All configuration defaults and constants
   - `pyheat.py` - Main app skeleton with initialization

2. **Foundation Features Implemented**
   - AppDaemon app class structure (inherits from `hass.Hass`)
   - YAML configuration loading (rooms.yaml, schedules.yaml)
   - Entity validation and TRV entity derivation from climate entities
   - Complete callback registration system for:
     - Master enable toggle
     - Holiday mode
     - Per-room mode selectors
     - Manual setpoints
     - Override/boost timers
     - Temperature sensors
     - TRV feedback sensors
   - Periodic recompute scheduling (1-minute intervals)
   - Startup behavior with delayed recomputes
   - Logging infrastructure

### What Needs Implementation 🚧

#### **Critical Path Items** (Required for basic operation)

1. **Sensor Fusion & Staleness Detection** (~2-3 hours)
   - Average multiple primary sensors per room
   - Fall back to fallback sensors if no primaries available
   - Detect stale sensors based on timeout_m
   - Mark rooms as stale if all sensors unavailable
   - **Code location**: New method `get_room_temperature(room_id, now)`

2. **Schedule Resolution** (~2-3 hours)
   - Parse weekly schedule for current day/time
   - Find active schedule block or use default_target
   - Apply holiday mode substitution
   - **Code location**: New method `get_scheduled_target(room_id, now)`

3. **Target Resolution with Precedence** (~2 hours)
   - Check room mode (off/manual/auto)
   - Apply precedence: off → manual → override → schedule
   - Handle override/boost from timers
   - **Code location**: New method `resolve_room_target(room_id, now)`

4. **Call-for-Heat Logic with Hysteresis** (~2-3 hours)
   - Calculate error: `e = target - temp`
   - Apply asymmetric hysteresis (on_delta_c, off_delta_c)
   - Maintain previous state in deadband zone
   - **Code location**: New method `compute_call_for_heat(room_id, target, temp)`

5. **Valve Band Calculation** (~2-3 hours)
   - Map error to valve bands (0%, low%, mid%, max%)
   - Apply step hysteresis to prevent band flapping
   - Implement multi-band jump optimization
   - **Code location**: New method `compute_valve_percent(room_id, target, temp)`

6. **TRV Command Execution** (~3-4 hours)
   - Send opening_degree and closing_degree commands
   - Sequential execution with feedback confirmation
   - Retry logic (10s interval, 6 max retries)
   - Rate limiting (min_interval_s)
   - Anti-thrashing checks
   - **Code location**: New method `set_trv_valve(room_id, percent)`

7. **Boiler State Machine** (~4-5 hours)
   - Implement 7-state machine:
     - OFF, PENDING_ON, ON, PENDING_OFF
     - PUMP_OVERRUN, INTERLOCK_BLOCKED, INTERLOCK_FAILED
   - Anti-cycling timers (min_on_s, min_off_s)
   - TRV-open interlock validation
   - Pump overrun timer
   - Off-delay for brief demand interruptions
   - **Code location**: New class `BoilerController` or methods in main app

8. **Status Publishing** (~1-2 hours)
   - Create/update `sensor.pyheat_status`
   - Set state string ("heating (2 rooms)", "idle", etc.)
   - Build attributes dict with per-room details
   - **Code location**: New method `publish_status()`

#### **Service Handlers** (~2-3 hours)

9. **Service Registration & Implementation**
   - `pyheat.override(room, target, minutes)`
   - `pyheat.boost(room, delta, minutes)`
   - `pyheat.cancel_override(room)`
   - `pyheat.set_mode(room, mode)`
   - `pyheat.set_default_target(room, target)`
   - `pyheat.reload_config()`
   - `pyheat.replace_schedules(schedule_dict)`
   - **Code location**: New methods `handle_service_*()` + registration in `initialize()`

#### **Polish & Robustness** (~2-3 hours)

10. **Error Handling**
    - Graceful handling of missing entities
    - Invalid state values
    - Configuration errors
    - Service call failures

11. **State Persistence**
    - Restore override/boost from timer states on startup
    - Pump overrun valve position persistence
    - Last commanded valve positions

### Implementation Complexity Analysis

**Total Estimated Development Time: 25-35 hours**

This is a **substantial rewrite** because:

1. **Different Execution Model**
   - PyScript: Async/await, decorators, direct state access
   - AppDaemon: Callback-based, service calls, thread-safe

2. **State Management Differences**
   - PyScript: Can create entities directly via `state.set()`
   - AppDaemon: Must use `set_state()` or helper entities

3. **Complexity of Original System**
   - 7-state boiler state machine
   - Complex TRV interlock with feedback confirmation
   - Multi-sensor fusion with staleness detection
   - Asymmetric hysteresis at multiple levels
   - Override/boost with persistence
   - Schedule parsing with DST handling

4. **Safety-Critical Code**
   - Boiler short-cycle protection
   - TRV-open interlock (prevents boiler running with closed valves)
   - Pump overrun for heat dissipation
   - Multiple timeout mechanisms

### Recommended Approach

Given the scope, I recommend one of these paths:

**Option 1: Incremental Development** (Recommended)
- Complete items 1-6 for basic heating control
- Test thoroughly with real hardware
- Add items 7-11 in subsequent iterations
- Time: Can be broken into 2-3 focused sessions

**Option 2: Simplified MVP**
- Implement basic schedule following
- Simple on/off boiler control
- No override/boost initially
- Time: 10-15 hours

**Option 3: Full Feature Parity**
- Complete all items as specified
- Match PyScript functionality exactly
- Time: 25-35 hours as estimated above

### Current File Status

```
/opt/appdata/appdaemon/conf/apps/pyheat/
├── constants.py          ✅ COMPLETE - All constants defined
├── pyheat.py            🚧 PARTIAL - Framework ready, core logic needed
├── config/
│   ├── rooms.yaml       ✅ COPIED - Configuration ready
│   ├── schedules.yaml   ✅ COPIED - Configuration ready
│   └── boiler.yaml      ✅ COPIED - Configuration ready
└── docs/                ❌ NOT CREATED - Optional
```

### Next Steps

To continue, you would need to:

1. **Decide on approach** (Option 1, 2, or 3 above)
2. **Allocate development time** (10-35 hours depending on approach)
3. **Set up test environment** (safe to test with real boiler/TRVs)
4. **Implement in phases** with testing between each phase

Would you like me to:
- **A**: Begin implementing Option 1 (incremental, basic heating first)
- **B**: Create Option 2 (simplified MVP)
- **C**: Provide more detailed pseudocode for the core functions
- **D**: Something else

Let me know how you'd like to proceed!
