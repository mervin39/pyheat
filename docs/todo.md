# PyHeat Implementation Progress

This file tracks progress against the specification in `docs/pyheat-spec.md`.

## Recent Fixes (Nov 2024)

### Valve Thrashing Fix (commit aa88240)
- **Issue**: Valves rapidly oscillating after mode changes due to TRV feedback triggers
- **Root Cause**: New TRV feedback triggers fired on every sensor update, including during valve transitions with inconsistent feedback
- **Solution**: 
  - Added consistency check in feedback triggers (skip recompute if open% + close% != 100%)
  - Added command deduplication in TRV controller (skip if already at position)
  - Two-level protection: prevent unnecessary recomputes AND prevent duplicate commands

### Missing TRV Feedback Triggers (commit e2d04a0)
- **Issue**: System deadlocked in PENDING_ON state
- **Root Cause**: No triggers registered for TRV feedback sensors, couldn't detect when valves reached position
- **Solution**: Added 12 state triggers for all TRV feedback sensors (open/close degree for 6 rooms)

### TRV Feedback Safety Fix (commit 2403e83)
- **Issue**: Boiler turned on while TRVs mid-transition (feedback showing open=100%, close=35%)
- **Root Cause**: `get_feedback_percent()` accepted inconsistent feedback as valid
- **Solution**: Return None when open% + close% deviates from 100% by >5%

### Pump Overrun Bug (commit 446a427)
- **Issue**: Non-calling rooms closed valves during pump overrun
- **Root Cause**: Only tracked calling rooms' positions, pump overrun used incomplete data
- **Solution**: Track ALL room valve positions in STATE_ON, use during PENDING_OFF/PUMP_OVERRUN

### Notification System (commit 0d9097a)
- **Feature**: Persistent notifications for serious errors (boiler timeouts, interlock failures)
- **Implementation**: New `notifications.py` module with severity levels, spam prevention, auto-dismiss

## ✅ Completed

### Phase 0: Bootstrap & Infrastructure
- [x] **`__init__.py`** - Bootstrap module (loads orchestrator, triggers, services)
  - Relative imports working correctly
  - Safe module loading with error handling
  - Clean startup/shutdown lifecycle
  
- [x] **`ha_triggers.py`** - Event trigger adapter (stub)
  - All trigger decorators registered
  - Debounced recompute working
  - Startup sequence with immediate + delayed recompute
  - Cron tick (1-minute) working
  
- [x] **`config_loader.py`** - YAML configuration loader (working)
  - Loads rooms.yaml and schedules.yaml
  - Builds room registry
  - Validation and error handling
  
- [x] **`core.py`** - Central orchestrator (stub interface)
  - Basic structure with all required methods
  - Service handlers defined (stub)
  - Event handlers defined (stub)
  
- [x] **`constants.py`** - Centralized configuration ✨ NEW
  - All default parameters defined
  - Hysteresis, valve bands, safety defaults
  - Entity ID patterns and derivation
  - Utility functions for entity generation
  - Validation helpers

## 🚧 In Progress

### Phase 1: Core Domain Logic
- [x] **`sensors.py`** - Temperature sensor fusion ✨ NEW
  - Primary/fallback sensor averaging
  - Staleness detection (timeout_m)
  - Per-room temperature publishing
  - Sensor status diagnostics
  - Integrated with orchestrator
  
- [x] **`scheduler.py`** - Schedule resolution ✨ NEW
  - Schedule block evaluation by day/time
  - Override/boost application
  - Holiday mode integration
  - Current block info for status
  - Integrated with orchestrator
  
- [x] **`room_controller.py`** - Room state machine ✨ NEW
  - Room class with first-class state management
  - Target resolution (precedence: off → manual → override → schedule)
  - Call-for-heat with asymmetric hysteresis
  - Valve percentage with stepped bands + step hysteresis
  - Override/boost state tracking with expiry
  - Status string generation
  - Integrated with orchestrator
  
- [x] **`trv.py`** - TRV control adapter ✨ NEW
  - TRVController class for individual TRV management
  - Valve command issuing (opening/closing degree)
  - Feedback reading from z2m sensors
  - Command/feedback matching for interlock
  - TRV interlock status reporting
  - Integrated with orchestrator

- [x] **`status.py`** - Status composition ✨ NEW
  - Global status formatting (sensor.pyheat_status)
  - Per-room status strings (sensor.pyheat_<room>_status)
  - Short reason strings for all states
  - Attributes formatting with mode, temps, targets
  - Stateless utility functions
  - Imported by orchestrator

## 📋 Planned

### Phase 2: Boiler & Safety
- [x] **`boiler.py`** - Comprehensive boiler control ✨ COMPLETE
  - Full state machine implementation (OFF, PENDING_ON, ON, PENDING_OFF, PUMP_OVERRUN, INTERLOCK_BLOCKED)
  - TRV-open interlock safety check (min valve opening percent)
  - **Event-driven timer-based anti-cycling** (Nov 3, 2025):
    - Replaced tick-based polling with timer helpers
    - 4 timer entities: min_on, min_off, off_delay, pump_overrun
    - Removed 1-minute cron tick (now fully event-driven)
    - Sub-second response time vs 60-second polling latency
  - Binary control mode for Nest Thermostat (setpoint-based on/off)
  - Valve position preservation during state transitions
  - Pump overrun support for heat dissipation
  - Minimum on/off times to prevent short-cycling
  - Off-delay to handle brief demand interruptions
  - Automatic valve override to meet minimum opening threshold
  - Comprehensive status reporting with timer states
  - **Integration**: Wired into orchestrator, tested end-to-end
  - **Production Ready**: All safety features working, no errors in logs

### Phase 3: Integration
- [x] **`ha_services.py`** - Service registration ✨ COMPLETE
  - All pyheat.* services registered under **pyheat domain** (not pyscript)
  - Argument validation with proper error messages
  - Error handling and exceptions
  - All 7 services working:
    - **pyheat.override**(room, target, minutes) ✅ TESTED
    - **pyheat.boost**(room, delta, minutes) ✅ TESTED
    - **pyheat.cancel_override**(room) ✅ TESTED
    - **pyheat.set_mode**(room, mode) ✅ TESTED
    - **pyheat.set_default_target**(room, target) ✅ TESTED
    - **pyheat.reload_config**() ✅ TESTED
    - **pyheat.replace_schedules**(schedule)
  - Orchestrator service handlers fully implemented
  - Timer integration working (override/boost)
  
- [x] **Core orchestrator implementation** ✨ COMPLETE
  - All modules wired up (sensors, scheduler, room_controller, trv, boiler)
  - recompute_all() fully implemented with debouncing
  - Entity publishing working (_publish_room_entities)
  - **TRV integration**: orchestrator calls trv.set_valve_percent() ✅
  - Service handlers complete and tested
  - State change handlers working
  - Event handlers (cron tick) working
  - Timer handlers integrated

### Phase 4: Testing & Polish
- [ ] End-to-end testing
- [ ] Edge case handling
- [ ] Documentation
- [ ] Performance optimization

## 📝 Notes

**Current Status:** All core modules and services complete! System is operational end-to-end.

**Test Results:**
- ✅ All modules loading: sensors, scheduler, room_controller, trv, status, ha_services, **boiler**
- ✅ Sensor reading fixed: temperature 17.9°C from sensor.roomtemp_pete
- ✅ Room controller: auto mode, target 21.0°C (schedule), heating active
- ✅ Status publishing: global and per-room entities working
- ✅ **TRV control working END-TO-END**:
  - Commands sent via service.call("number", "set_value", ...)
  - Physical valve responds (audible clicks)
  - Feedback sensors update correctly (sensor.*_z2m entities)
  - Valve bands working (100% max, 65% mid, 35% low, 0% off)
  - Mode changes trigger valve updates (auto/manual/off)
  - Rate limiting enforced (30s minimum between updates)
- ✅ **Boiler control working**: 
  - Auto mode + heat demand → boiler ON (input_boolean.pyheat_boiler_actor = on)
  - Manual mode (low target) → boiler OFF
  - Logs confirm: "turning boiler ON (demand from 1 room(s))" / "turning boiler OFF (no demand)"
- ✅ Services in **pyheat domain** (pyheat.*, not pyscript.*):
  - **pyheat.boost**: Tested with delta +1.5°C for 45min → status "boost(+1.5) 44m", target=21.0°C ✅
  - **pyheat.override**: Tested with absolute 22.0°C for 30min → status "override(22.0) 29m" ✅
  - **pyheat.cancel_override**: Cleared override → status "heating", target=21.0°C (schedule) ✅
  - **pyheat.set_mode**: Switched auto→manual→auto → mode changes reflected, targets updated ✅
  - **pyheat.set_default_target**: Changed 19.5→20.0 → schedules.yaml updated, config reloaded ✅
  - **pyheat.reload_config**: Reloaded all modules (sensors, scheduler, rooms, TRV) successfully ✅
  - **pyheat.replace_schedules**: Replaced entire schedule → schedules.yaml atomically updated, scheduler reloaded, new targets applied ✅
  - Timer integration confirmed (start/cancel working)

**Next Steps:** 
- End-to-end integration testing with multiple rooms
- Edge case testing (sensor failures, network issues)
- Performance monitoring
- **All 7 services fully tested and working!** ✅
- **Boiler state machine fully tested and production ready!** ✅

## 🎯 Recent Updates (November 3, 2025)

### Boiler Safety Refactoring: Event-Driven Timers
Completed major refactoring to replace tick-based timestamp polling with event-driven timer helpers:

**What Changed:**
- **Removed**: 1-minute cron tick for anti-cycling checks
- **Removed**: `input_datetime` entities for timestamp tracking
- **Added**: 4 timer helper entities with `@state_trigger` events:
  - `timer.pyheat_boiler_min_on_timer` - Enforces minimum on time
  - `timer.pyheat_boiler_min_off_timer` - Prevents premature restart
  - `timer.pyheat_boiler_off_delay_timer` - Delays turn-off to prevent rapid cycling
  - `timer.pyheat_boiler_pump_overrun_timer` - Keeps valves open during pump overrun

**Benefits:**
- ✅ True event-driven architecture (no polling)
- ✅ Sub-second response time (vs 60-second cron latency)
- ✅ Simpler state machine logic (no timestamp arithmetic)
- ✅ Consistent with existing `@state_trigger` patterns
- ✅ Visual feedback in HA UI (timer states visible)
- ✅ Programmatic control (can pause/cancel timers)

**Commits:**
- `0946113` - Refactor boiler safety to use event-driven timer helpers
- `03b503d` - FIX: Correctly save and preserve valve positions during state transitions
- `9f815bc` - CRITICAL FIX: Keep valves open during pending_off and pump_overrun
- `5ac08dd` - Fix boiler control: set HVAC mode and update status sensor
- `e997d4b` - Fix timestamp parsing timezone awareness
- `5313d2e` - Fix datetime timezone awareness issue
- `1f1a75a` - Add stub mode for boiler initialization
- `02adad3` - Implement comprehensive boiler state machine with Nest Thermostat control
- `dd9e5cc` - Update valve percent entities after interlock override
- `bfe4c2d` - Fix TRV method name: set_valve → set_valve_percent
- `fbc4e8c` - Fix generator expressions and add valve percent safety clamp
- `1685581` - Implement TRV-open interlock safety check

**Status:** ✅ All changes tested and verified in production. No errors in logs. System fully operational.

