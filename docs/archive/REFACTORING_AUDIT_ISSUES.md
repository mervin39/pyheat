# PyHeat Refactoring Audit - Critical Issues Found

**Date:** 2025-11-05  
**Status:** IN PROGRESS - CRITICAL BUGS FOUND

## Executive Summary

A comprehensive audit of the refactored modular PyHeat code against the original monolithic version has revealed **MULTIPLE CRITICAL SAFETY ISSUES** that could cause:
- Boiler running with no flow path (potential damage)
- Valve commands being sent when they should be suppressed  
- Race conditions in recompute triggering
- Missing initialization logic

## Critical Issues Found

### 🔴 CRITICAL #1: Valve Persistence Logic Broken in app.py

**Location:** `app.py`, lines 324-338 (recompute_all method)

**Problem:** The refactored code is NOT correctly handling persisted valves for rooms that are NOT calling for heat during pump overrun and pending_off states.

**Original Code:**
```python
# 8. Apply valve commands with persistence priority
# If there are persisted valves (pump overrun or interlock), use them for affected rooms
# and use normal calculations for non-persisted rooms
persisted = boiler_status.get('persisted_valve_percents', {})

if persisted:
    # Send persistence commands first (critical for pump overrun safety)
    for room_id, valve_percent in persisted.items():
        self.set_trv_valve(room_id, valve_percent, now)
    
    # Send normal commands for rooms NOT in persistence dict
    for room_id, valve_percent in room_valve_percents.items():
        if room_id not in persisted:
            self.set_trv_valve(room_id, valve_percent, now)
else:
    # No overrides - send all normal valve commands
    for room_id, valve_percent in room_valve_percents.items():
        self.set_trv_valve(room_id, valve_percent, now)
```

**Refactored Code:**
```python
# Apply valve commands (using persisted values if boiler requires it)
for room_id in self.config.rooms.keys():
    data = room_data[room_id]
    
    # Use persisted valve if boiler state machine requires it, otherwise use computed
    if valves_must_stay_open and room_id in persisted_valves:
        valve_percent = persisted_valves[room_id]
        self.log(f"Room '{room_id}': using persisted valve {valve_percent}%", level="DEBUG")
    else:
        valve_percent = data['valve_percent']
    
    # Set TRV valve
    self.rooms.set_room_valve(room_id, valve_percent, now)
```

**Why This Is Critical:**
1. In the monolithic code, during pump overrun, `boiler_last_valve_positions` contains ALL room valve positions (from when boiler was ON)
2. Rooms that are NOT calling for heat still need their valves kept at saved positions during pump overrun
3. The refactored code only sets persisted valves for rooms in `persisted_valves` dict, but `persisted_valves` only contains calling rooms during interlock persistence!
4. During PUMP_OVERRUN state, `boiler_last_valve_positions` should contain ALL rooms that had open valves when boiler turned off
5. **Result:** Non-calling rooms will have valves closed immediately during pump overrun instead of staying open!

**Monolithic Logic:**
```python
# Save ALL valve positions (not just calling rooms) for pump overrun safety
if has_demand and all_valve_positions:
    self.boiler_last_valve_positions = all_valve_positions.copy()
```

**Refactored Logic (boiler_controller.py):**
```python
# Merge persisted valves with all room valve percents for pump overrun tracking
all_valve_positions = room_valve_percents.copy()
all_valve_positions.update(persisted_valves)
# ...
if has_demand and all_valve_positions:
    self.boiler_last_valve_positions = all_valve_positions.copy()
```

The issue is that `all_valve_positions` is saved correctly in boiler_controller.py, BUT when returning the persisted_valves to app.py, it only returns the interlock-persisted valves OR the pump_overrun valves, not both types correctly.

### 🔴 CRITICAL #2: Pump Overrun Valve Persistence Not Applied to Non-Calling Rooms

**Location:** `boiler_controller.py`, lines 215-234 (STATE_PUMP_OVERRUN case)

**Problem:** The pump overrun state correctly keeps `persisted_valves = self.boiler_last_valve_positions.copy()`, BUT the calling code in app.py only applies these to rooms that are in the dictionary. If a room that was previously calling for heat is no longer calling, it won't get its saved valve position during pump overrun.

**Impact:** Valves can close during pump overrun, reducing flow and potentially causing boiler to run with insufficient flow.

### 🔴 CRITICAL #3: Missing Safety Room Emergency Override

**Location:** `app.py`, recompute_all method

**Problem:** The original monolithic code has an emergency safety check:

```python
# CRITICAL SAFETY: Emergency valve override
# If boiler is physically ON (heating) but no rooms calling for heat,
# force safety room valve open to ensure there's a path for hot water
# Exclude normal transition states (PENDING_OFF, PUMP_OVERRUN) where we expect physical mismatch
if (self.boiler_safety_room and hvac_action in ("heating", "idle") and
    self.boiler_state not in (C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN)):
    if len(rooms_calling_for_heat) == 0:
        # Boiler is ON but no demand - EMERGENCY!
        persisted_valves[self.boiler_safety_room] = 100
        self.log(
            f"🔴 EMERGENCY: Boiler ON with no demand! Forcing {self.boiler_safety_room} valve to 100% for safety",
            level="ERROR"
        )
```

**The refactored code has this in boiler_controller.py, but it's NOT being applied in app.py!**

The boiler_controller returns this in persisted_valves, but app.py doesn't read the hvac_action and doesn't apply the safety override properly.

### 🔴 CRITICAL #4: Race Condition in Recompute Triggering

**Location:** `app.py`, trigger_recompute method

**Original:**
```python
def trigger_recompute(self, reason: str):
    self.recompute_count += 1
    now = datetime.now()
    self.last_recompute = now
    
    self.log(f"Recompute #{self.recompute_count} triggered: {reason}", level="DEBUG")
    
    # Perform the recompute
    self.recompute_all(now)
```

**Refactored:**
```python
def trigger_recompute(self, reason: str):
    self.log(f"Recompute triggered: {reason}")
    self.run_in(lambda kwargs: self.recompute_all(datetime.now()), 0.1)
```

**Problem:** 
1. The refactored version schedules recompute with a 0.1s delay, which could cause multiple rapid triggers to queue up
2. The original calls recompute_all() synchronously, which prevents queue buildup
3. Multiple sensor updates could trigger 10+ recomputes in quick succession

### 🔴 CRITICAL #5: room_controller.py Sets Valves to 0% for Off Rooms During Pump Overrun

**Location:** `room_controller.py`, compute_room method

**Problem:** When a room is off/stale/no target, `compute_room` returns `valve_percent: 0`. During pump overrun, the app.py logic should use persisted values, but if a room is not in the persisted_valves dict, it will use the computed 0%!

**Original Monolithic Code Comment:**
```python
else:
    # Room is off, stale, or has no target
    self.room_call_for_heat[room_id] = False
    room_valve_percents[room_id] = 0
    # Don't send valve command here - let persistence logic in step 8 handle it
    # (fixes pump overrun oscillation bug where OFF rooms had valves forced to 0%
    # even though pump overrun needed them at saved positions)
```

The refactored code doesn't have this comment or understanding! It just returns 0% valve for off rooms, and if they're not in persisted_valves, they get closed during pump overrun.

## Medium Priority Issues

### ⚠️ MEDIUM #1: Boiler Config Access Inconsistency

**Location:** Multiple files

**Problem:** Some boiler configuration values are accessed differently in refactored vs original:
- Original: `self.boiler_min_valve_open_percent`
- Refactored: `self.config.boiler_config.get('interlock', {}).get('min_valve_open_percent', DEFAULT)`

This is inconsistent and error-prone. The config_loader should expose these as properties.

### ⚠️ MEDIUM #2: Missing "First Boot" Suppression in Services

**Location:** service_handler.py

**Original:** Service calls would use `self.run_in(lambda kwargs: self.recompute_all("service"), 1)` with a 1-second delay
**Refactored:** Same pattern used

**Issue:** No functional difference found, but original had comments about avoiding immediate recompute on first boot that might not be captured.

## Low Priority Issues  

### ℹ️ LOW #1: Different Logging Verbosity

Multiple places have different log levels between original and refactored. This is cosmetic but could make debugging harder.

## Recommendations

### IMMEDIATE ACTIONS REQUIRED:

1. **FIX CRITICAL #1-5:** These are safety-critical bugs that could damage equipment
2. **Test pump overrun logic extensively** with multi-room scenarios
3. **Test safety room failsafe** by simulating boiler-on-with-no-demand
4. **Add integration tests** for state machine edge cases

### SHORT TERM:

1. Add comprehensive unit tests for boiler_controller state machine
2. Add integration tests for app.py orchestration  
3. Document pump overrun valve persistence requirements clearly
4. Add defensive checks for safety_room emergency override

### LONG TERM:

1. Consider making valve persistence more explicit in the API
2. Add telemetry/logging for safety interventions
3. Create state machine visualization tool

## Next Steps

1. Fix all CRITICAL issues immediately
2. Create test scenarios that validate fixes
3. Update docs/changelog.md with all fixes
4. Commit fixes to git (but don't push)

---

**Audit Status:** Issues identified, fixes pending
**Auditor:** GitHub Copilot  
**Reviewed:** Waiting for fixes to be implemented
