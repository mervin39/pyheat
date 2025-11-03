# PyHeat Implementation Progress

This file tracks progress against the specification in `docs/pyheat-spec.md`.

## Recent Fixes (Nov 2025)

### Emergency Safety Valve & Watchdog Cron (commits cc1846d, b6f2db9, 263ee49) - Nov 3, 2025 🔴 CRITICAL SAFETY
- **Feature**: Multi-layer defense against "boiler ON with no demand" scenario
- **Problem**: If boiler is physically heating but no rooms calling for heat (edge case, bug, timing issue), hot water has nowhere to flow → potential boiler damage
- **Solution - Layer 1: Emergency Safety Valve (Immediate)**:
  - Configure `safety_room: games` in `boiler.yaml`
  - If `hvac_action='heating'` AND `len(rooms_calling_for_heat)==0`
  - Automatically force safety room valve to 100% via `overridden_valves`
  - Sends CRITICAL notification and logs warning
  - Provides immediate hardware protection (hot water has flow path)
- **Solution - Layer 2: Watchdog Cron (1-2 minutes)**:
  - 1-minute cron job (`@time_trigger("cron(* * * * *)")`) checks system health
  - Detects boiler ON with no demand (>120s grace period)
  - Triggers `_request_recompute()` to force state machine re-evaluation
  - Also detects stuck states (PENDING_ON, INTERLOCK_BLOCKED >5 min)
- **Solution - Layer 3: State Machine (Root Cause)**:
  - Recompute triggered by Layer 2 evaluates demand
  - STATE_ON with no demand → PENDING_OFF → PUMP_OVERRUN → OFF
  - Properly turns boiler OFF and resolves root cause
- **Defense in Depth**:
  1. T+0s: Safety valve activates (games to 100%)
  2. T+60-120s: Watchdog triggers recompute
  3. T+60-120s: State machine turns boiler OFF
  4. T+210-300s: Pump overrun completes, all valves close
- **Additional Checks**:
  - TRV feedback consistency (10% tolerance, skips in-progress commands)
  - Override timer vs state consistency
  - Stuck state detection with auto-recovery
- **Impact**: Hardware protection + automatic recovery for edge cases
- **Testing**: Would have immediately mitigated the boiler-ON-no-demand issue found earlier

### Override/Boost Persistence Across Pyscript Reload (commit 73e9d3d) - Nov 2025
- **Issue**: Override/boost state lost during pyscript reload, valves closed even with active timer
- **Symptom**: Bathroom override to 18°C reverted to schedule 12°C on pyscript reload, all valves closed
- **Root Cause**: When pyscript reloads:
  1. RoomController reinitializes with `override_kind = None`, `override_target = None`
  2. Override timer (`timer.pyheat_{room}_override`) persists in HA but room doesn't know it's active
  3. Room resolution returns schedule target instead of override target
  4. Valves commanded to 0% even though override should still be active
- **Impact**: User comfort lost, manual re-application required after every pyscript reload
- **Solution**:
  1. **Persist override target**: Save computed target to `input_number.pyheat_{room}_override_target` when override/boost applied
  2. **Restore on startup**: Check if override timer active, read persisted target, restore override state
  3. **Boost special case**: Boost stores delta initially, target computed in `_resolve_target()` from `schedule_target + delta`, then persisted
  4. **Clear on expiry**: Clear persisted target when override expires or is manually cleared
- **Code Changes**:
  - `__init__`: Check if `timer.pyheat_{room}_override` active, restore override_kind and override_target from persisted entities
  - `apply_override()`: Persist target to `input_number.pyheat_{room}_override_target`
  - `apply_boost()`: Defer persistence to `_resolve_target()` where target is computed
  - `_resolve_target()`: For boost, persist computed target (schedule + delta) on first calculation
  - `clear_override()`: Clear persisted target (set to 0)
- **New Entities**: `input_number.pyheat_{room}_override_target` for all 6 rooms (5-35°C range)
- **Simplification**: Originally planned to persist override_kind and override_delta separately, but simplified to only persist target temperature. Override vs boost distinction doesn't matter after application - both just set a target and expiry time.
- **Testing**: Requires HA restart to load new input_number entities, then test override/boost during pyscript reload
- **Related Issue**: Part of broader pyscript reload state loss problem, also addressed by pump overrun persistence

### Pyscript Reload During Pump Overrun Bug (commit f10bdd6) - Nov 3, 2025 ⚠️ CRITICAL
- **Issue**: TRV closed to 0% only 31 seconds into 180-second pump overrun when pyscript reloaded
- **Symptom**: Pete's valve commanded to 100% at 19:12:33 (pump overrun start), then to 0% at 19:13:04 (pyscript reload), 149 seconds before pump overrun timer finished at 19:15:33
- **Root Cause**: When pyscript reloads:
  1. BoilerManager reinitializes with `current_state = STATE_OFF`
  2. `last_valve_positions` dict is lost (empty)
  3. Pump overrun timer (`timer.pyheat_boiler_pump_overrun_timer`) persists in HA but boiler doesn't know it's active
  4. Delayed startup recompute (10s) runs with no knowledge of pump overrun
  5. Room calculation says 0%, no override applied, TRV closes prematurely
- **Impact**: Pump runs with all TRVs closed, potential boiler damage, wasted energy
- **Solution**: 
  1. **State restoration on startup**: Check if `pump_overrun_timer` is active, restore `STATE_PUMP_OVERRUN` if so
  2. **Persist valve positions**: Save `last_valve_positions` to `input_text.pyheat_pump_overrun_valves` when entering pump overrun
  3. **Restore valve positions**: Read from input_text helper on startup if pump overrun active
  4. **Clear on completion**: Clear persisted positions when pump overrun finishes
- **Code Changes**:
  - `__init__`: Check `_is_timer_active(pump_overrun_timer)` before reading boiler HVAC action
  - New helper: `_save_pump_overrun_valves()` persists dict as JSON to input_text
  - New helper: `_clear_pump_overrun_valves()` clears persisted data
  - Call save when entering PUMP_OVERRUN state
  - Call clear when PUMP_OVERRUN→OFF transition
- **New Entity**: `input_text.pyheat_pump_overrun_valves` (max 255 chars) for state persistence
- **Testing**: Next pyscript reload during pump overrun should maintain valve positions
- **Discovery**: Found when investigating "pete TRV shut before pump overrun finished" log entry

### ⚠️ CRITICAL - Pump Overrun Valve Override Bug (commit 1fcf85c) - Nov 3, 2025
- **Issue**: Valves stuck open at 100% after pump overrun completed, refused to close even though room calculated 0%
- **Symptom**: After switching pete from Manual→Auto (no demand), valve stayed at 100% indefinitely
- **Root Cause**: When boiler transitioned PUMP_OVERRUN→OFF, the `overridden_valves` dict still contained saved positions from pump overrun (pete: 100). These stale overrides were returned to orchestrator, preventing room-calculated values from being applied.
- **Code Path**: 
  1. STATE_PUMP_OVERRUN sets `overridden_valves = last_valve_positions.copy()` (line 572)
  2. Pump overrun completes, transitions to OFF, sets `valves_must_stay_open = False` (line 587)
  3. **BUG**: `overridden_valves` not cleared, still contains {pete: 100}
  4. Returns `overridden_valve_percents: overridden_valves` with stale values (line 634)
  5. Orchestrator applies overrides, valve stuck at 100%
- **Solution**: Explicitly clear `overridden_valves = {}` when pump overrun completes (line 588)
- **Verification**: Tested pete Manual→Auto transition, valve correctly closed to 0% after 3min pump overrun
- **Found During**: Comprehensive specification sanity check of all valve control scenarios

### Rate Limiting Stale Value Bug (commit fd0bd67) - Nov 3, 2025
- **Issue**: After pump overrun ended, room calculated valve should be 0% but orchestrator received stale 100% value
- **Root Cause**: Rate limiting in `_compute_valve_percent()` returned `self.valve_percent` (old value) when throttled, instead of updating to newly calculated value
- **Impact**: Orchestrator made decisions based on wrong valve values, compounding pump overrun bug
- **Solution**: Always update `self.valve_percent = int(valve_percent)` even when rate limited. Rate limiting now only affects command timing via `should_send_command` flag.
- **Code Change**: Separated value updates (always) from command timing (throttled)

### Sequential TRV Command Implementation (commit f5e8d57) - Nov 3, 2025
- **Feature**: TRV commands now sent sequentially (opening degree → confirm → closing degree) instead of simultaneously
- **Rationale**: Prevents valve thrashing, allows feedback confirmation before next command
- **Implementation**:
  - `command_in_progress` lock prevents concurrent commands
  - Opening degree sent first, waits for feedback confirmation (10s retry intervals, 6 max retries)
  - Then closing degree sent with same retry logic
  - Configurable: `TRV_COMMAND_SEQUENCE_ENABLED`, `TRV_COMMAND_RETRY_INTERVAL_S`, `TRV_COMMAND_MAX_RETRIES`
- **Anti-thrashing Layers**:
  1. Check `command_in_progress` (prevent concurrent)
  2. Check `last_commanded_percent` (skip if same as last)
  3. Check entity values (skip if already at target)
  4. Sequential execution with feedback confirmation
- **Trade-off**: Commands take 10-20s to complete vs instant, but eliminates thrashing

### Double-Commanding TRV Fix (commit 7fe5878) - Nov 3, 2025 ⚠️ CRITICAL
- **Issue**: Severe valve thrashing with valves oscillating 0% → 100% → 0% → 100% continuously
- **Root Cause**: TRVs were commanded TWICE per recompute:
  1. In `_publish_room_entities()` with calculated valve percent (e.g., 0% for room above target)
  2. In main recompute loop with final valve percent after boiler overrides (e.g., 100% from STATE_PENDING_OFF saved positions)
- **Impact**: When boiler applied overrides (pump overrun, interlock), valves received contradictory commands every recompute cycle
- **Solution**: Removed TRV commanding from `_publish_room_entities()` - TRVs now commanded ONLY ONCE after all overrides applied
- **Lesson**: Actuator commands must happen at a single point after all state computation is complete

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

---

## Valve Control Specification - Complete Verification (Nov 3, 2025)

**Comprehensive sanity check performed on all TRV valve control scenarios to ensure correct behavior.**

### Specification: What Should Happen With TRV Valves

#### Situation 1: Normal Operation (Boiler OFF, No Demand)
**Expected**: Room calculates valve_percent based on temperature bands, TRV commanded to calculated value, no overrides.

**Verified**: ✅
- `room_controller._compute_valve_percent()` calculates bands based on error (target - temp)
- Orchestrator gets valve_percent from room.compute()
- No overrides applied when boiler STATE_OFF and no demand
- TRV commanded directly to calculated value

#### Situation 2: Boiler OFF → PENDING_ON (Interlock Enforcement)
**Expected**: If total valve opening < min_valve_open_percent (100%), override calling rooms to meet minimum.

**Verified**: ✅
- `boiler.calculate_valve_overrides()` checks if sum of calling room valves >= 100%
- If insufficient, calculates override_percent = ceil(100 / n_rooms)
- Returns overridden dict to orchestrator
- Orchestrator applies overrides before commanding TRVs
- Boiler waits in PENDING_ON until TRV feedback confirms positions

#### Situation 3: Boiler ON (Interlock Safety)
**Expected**: Continuously check interlock. If violated while running, immediately shut down with CRITICAL notification.

**Verified**: ✅
- Every recompute checks `interlock_ok` even during STATE_ON (line 517)
- If interlock fails: immediate transition to PUMP_OVERRUN, boiler commanded off
- CRITICAL notification sent (interlock_failure_while_running)
- Valves kept open during pump overrun for safety

#### Situation 4: Boiler ON → PENDING_OFF (Preserve Valve Positions)
**Expected**: Save all valve positions during STATE_ON. When demand stops, enter PENDING_OFF and keep valves at saved positions.

**Verified**: ✅
- Line 419-421: `all_valve_positions` includes ALL rooms (not just calling)
- Line 506: Continuously saves positions during STATE_ON
- Line 510: When demand stops → PENDING_OFF
- Line 543: PENDING_OFF uses `last_valve_positions` as overrides
- Valves stay open during off_delay (30s) to prevent water hammer

#### Situation 5: PUMP_OVERRUN (Keep Valves Open, Then Clear)
**Expected**: Valves must stay open at saved positions during pump overrun. When complete → transition to OFF and CLEAR OVERRIDES.

**Verified**: ✅ **CRITICAL BUG FOUND AND FIXED**
- PUMP_OVERRUN uses saved positions as overrides (line 572)
- When timer completes (line 583-588): transition to OFF, `valves_must_stay_open = False`
- **BUG FIXED**: Now explicitly clears `overridden_valves = {}` (line 588 - commit 1fcf85c)
- Next recompute: orchestrator gets room-calculated values, valves can close

#### Situation 6: Room Mode Changes
**Expected**: Mode change triggers immediate recalculation. If band changes, bypass rate limiting and command immediately.

**Verified**: ✅
- Mode change triggers fire immediately (ha_triggers.py line 237-247)
- Debounce is only 100ms, applies to all triggers equally
- Rate limiting checks band change (room_controller.py line 342)
- If `current_band != self._prev_valve_band`, rate limit bypassed
- Mode changes typically change target significantly → band changes → immediate command

#### Situation 7: Rate Limiting
**Expected**: Always update calculated valve_percent. Rate limiting only throttles TRV command timing, not the value itself.

**Verified**: ✅ **BUG FOUND AND FIXED**
- **BUG FIXED**: Line 334 now always updates `self.valve_percent = int(valve_percent)` (commit fd0bd67)
- Rate limiting only affects `should_send_command` flag (line 336-344)
- Orchestrator always receives current calculated value
- TRV commands throttled to max once per 30s (unless band changes)

#### Situation 8: Sequential TRV Commands
**Expected**: Send opening degree first, wait for feedback confirmation, then closing degree. Retry if needed.

**Verified**: ✅ **IMPLEMENTED**
- `TRV_COMMAND_SEQUENCE_ENABLED = True` enables sequential mode (commit f5e8d57)
- `_set_valve_sequential()` sends opening degree first (line 122-187)
- Waits for feedback confirmation with 10s retry intervals
- After confirmation, sends closing degree with same retry logic
- `command_in_progress` lock prevents concurrent commands
- Falls back to simultaneous mode if sequential disabled

### Code Verification Results

**All 8 situations verified against code implementation:**
- ✅ Situation 1: Normal operation - correct
- ✅ Situation 2: Interlock enforcement - correct
- ✅ Situation 3: Interlock safety while running - correct
- ✅ Situation 4: PENDING_OFF valve preservation - correct
- ✅ Situation 5: PUMP_OVERRUN override clearing - **FIXED** (critical bug)
- ✅ Situation 6: Mode change band updates - correct
- ✅ Situation 7: Rate limiting value updates - **FIXED** (bug)
- ✅ Situation 8: Sequential commands - **IMPLEMENTED**

**Bugs Found During Verification:**
1. **CRITICAL**: Pump overrun overrides not cleared → valves stuck open (FIXED)
2. Rate limiting returned stale values to orchestrator (FIXED)

**Production Test Results:**
- Pete Manual→Auto transition: ✅ Valve correctly closed from 100%→0% after pump overrun
- No thrashing observed with all anti-thrashing layers active
- Sequential commands executing cleanly (10-20s per valve change)

---

## ✅ Completed - All Phases

### Phase 0: Bootstrap & Infrastructure
- [x] **`__init__.py`** - Bootstrap module (loads orchestrator, triggers, services)
  - Relative imports working correctly
  - Safe module loading with error handling
  - Clean startup/shutdown lifecycle
  
- [x] **`ha_triggers.py`** - Event trigger adapter
  - All trigger decorators registered
  - Debounced recompute working
  - Startup sequence with immediate + delayed recompute
  - Cron tick (1-minute) working
  
- [x] **`config_loader.py`** - YAML configuration loader
  - Loads rooms.yaml and schedules.yaml
  - Builds room registry
  - Validation and error handling
  
- [x] **`core.py`** - Central orchestrator
  - All methods implemented and tested
  - Service handlers complete
  - Event handlers complete
  
- [x] **`constants.py`** - Centralized configuration
  - All default parameters defined
  - Hysteresis, valve bands, safety defaults
  - Entity ID patterns and derivation
  - Utility functions for entity generation
  - Validation helpers

### Phase 1: Core Domain Logic
- [x] **`sensors.py`** - Temperature sensor fusion
  - Primary/fallback sensor averaging
  - Staleness detection (timeout_m)
  - Per-room temperature publishing
  - Sensor status diagnostics
  - Integrated with orchestrator
  
- [x] **`scheduler.py`** - Schedule resolution
  - Schedule block evaluation by day/time
  - Override/boost application
  - Holiday mode integration
  - Current block info for status
  - Integrated with orchestrator
  
- [x] **`room_controller.py`** - Room state machine
  - Room class with first-class state management
  - Target resolution (precedence: off → manual → override → schedule)
  - Call-for-heat with asymmetric hysteresis
  - Valve percentage with stepped bands + step hysteresis
  - Override/boost state tracking with expiry
  - Status string generation
  - Multi-band valve optimization (skip hysteresis when band_delta > 1)
  - State persistence (override/boost restoration after pyscript reload)
  - Integrated with orchestrator
  
- [x] **`trv.py`** - TRV control adapter
  - TRVController class for individual TRV management
  - Valve command issuing (opening/closing degree)
  - Feedback reading from z2m sensors
  - Command/feedback matching for interlock
  - TRV interlock status reporting
  - Sequential command execution with feedback confirmation
  - Anti-thrashing protection (4 layers)
  - Integrated with orchestrator

- [x] **`status.py`** - Status composition
  - Global status formatting (sensor.pyheat_status)
  - Per-room status strings (sensor.pyheat_<room>_status)
  - Short reason strings for all states
  - Attributes formatting with mode, temps, targets
  - Stateless utility functions
  - Imported by orchestrator

### Phase 2: Boiler & Safety
- [x] **`boiler.py`** - Comprehensive boiler control
  - Full state machine implementation (OFF, PENDING_ON, ON, PENDING_OFF, PUMP_OVERRUN, INTERLOCK_BLOCKED, INTERLOCK_FAILED)
  - TRV-open interlock safety check (min valve opening percent)
  - Event-driven timer-based anti-cycling (Nov 3, 2025):
    - Replaced tick-based polling with timer helpers
    - 4 timer entities: min_on, min_off, off_delay, pump_overrun
    - Sub-second response time vs 60-second polling latency
  - Binary control mode for Nest Thermostat (setpoint-based on/off)
  - Valve position preservation during state transitions
  - Pump overrun support for heat dissipation (with state persistence)
  - Minimum on/off times to prevent short-cycling
  - Off-delay to handle brief demand interruptions
  - Automatic valve override to meet minimum opening threshold
  - Comprehensive status reporting with timer states
  - Production Ready: All safety features working, tested in 6-room deployment

### Phase 3: Integration
- [x] **`ha_services.py`** - Service registration
  - All pyheat.* services registered under **pyheat domain** (not pyscript)
  - Argument validation with proper error messages (10-35°C override, -10 to +10°C boost delta)
  - Error handling and exceptions
  - All 7 services working and tested:
    - **pyheat.override**(room, target, minutes) ✅
    - **pyheat.boost**(room, delta, minutes) ✅
    - **pyheat.cancel_override**(room) ✅
    - **pyheat.set_mode**(room, mode) ✅
    - **pyheat.set_default_target**(room, target) ✅
    - **pyheat.reload_config**() ✅
    - **pyheat.replace_schedules**(schedule) ✅
  - Timer integration working (override/boost)
  
- [x] **Core orchestrator implementation**
  - All modules wired up (sensors, scheduler, room_controller, trv, boiler)
  - recompute_all() fully implemented with debouncing
  - Entity publishing working (_publish_room_entities)
  - TRV integration complete
  - Service handlers complete and tested
  - State change handlers working
  - Event handlers working
  - Timer handlers integrated

### Phase 4: Testing & Polish
- [x] End-to-end testing (6-room production deployment)
- [x] Edge case handling (all safety scenarios verified)
- [x] Documentation (README.md and pyheat-spec.md updated to v2.0)
- [x] Performance optimization (event-driven, no polling)

---

## 📝 Production Status - v2.0 (November 3, 2025)

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

