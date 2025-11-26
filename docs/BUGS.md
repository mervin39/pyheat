# PyHeat Bug Tracker

This document tracks known bugs and their resolutions.

---

## BUG #3: Override Functionality Broken - Valve Commands Not Sent

**Status:** Identified  
**Date Discovered:** 2025-11-26 (14:42:32 test override)  
**Severity:** Critical - completely breaks override functionality  
**Introduced:** Unknown - occurred between yesterday (2025-11-25) and today

### Observed Behavior

When setting a temperature override via the API, the override entities are correctly updated and the room controller calculates the correct valve percentage, but **no TRV command is sent**. The valve remains at its previous position indefinitely.

**Test case on 2025-11-26 at 14:42:32:**
```bash
curl -X POST "http://localhost:5050/api/appdaemon/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "bathroom", "target": 22.0, "minutes": 60}'
```

**What worked:**
- Override service returns success ✓
- Timer started: `timer.pyheat_bathroom_override` active for 60 minutes ✓
- Target updated: 10.0°C → 22.0°C ✓
- Room controller calculated: valve should be 100% (error=11.27°C, band 0→max) ✓
- Boiler turned ON correctly ✓

**What failed:**
- No "Setting TRV for room 'bathroom': 100%" log message ✗
- Valve command never sent to TRV hardware ✗
- Feedback sensor remained at 0% (last_updated: 14:33:21, 9 minutes before override) ✗
- Heating CSV logs show: `bathroom_valve_cmd=0` throughout override period ✗

### Evidence

**From AppDaemon logs (2025-11-26 14:42:32):**
```
14:42:32.207947 INFO: pyheat.override: absolute mode: target=22.0C
14:42:32.232444 INFO: Override set: room=bathroom, target=22.0C, duration=3600s
14:42:32.236033 INFO: Room 'bathroom' override started
14:42:32.281029 DEBUG: Room bathroom: Target changed 10.0->22.0C, making fresh heating decision
14:42:32.282695 DEBUG: call_service: input_text/set_value, {'entity_id': 'input_text.pyheat_room_persistence', 'value': '{"office":[0,0],"pete":[0,0],"games":[0,0],"lounge":[0,0],"abby":[0,0],"bathroom":[0,1]}'}
14:42:32.293179 INFO: Room 'bathroom': valve band 0 -> max (error=11.27°C, valve=100%)
14:42:32.296104 DEBUG: Boiler: total valve opening 100% >= min 100%
14:42:32.300486 INFO: Boiler: off -> on (demand and conditions met)
14:42:32.305969 INFO: Boiler ON
[MISSING: "Setting TRV for room 'bathroom': 100% open" - this line never appears]
```

**Key observation:** Last TRV command for bathroom was at 14:33:19 (setting valve to 0% after pump overrun ended). No subsequent command was sent at 14:42:32.

**From heating_logs/2025-11-26.csv:**
```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
14:42:38,True,True,0,0
14:42:46,True,True,0,0
14:42:47,True,True,0,0
14:42:49,True,True,0,0
14:42:50,True,True,0,0
... (continued for 13+ minutes, valve_cmd never changed from 0)
```

**Comparison with working override from yesterday (2025-11-25 06:52):**
```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
06:52:09,True,True,0,0
06:52:14,True,True,100,0        ← Valve command sent within 5 seconds
06:52:15,True,True,100,100      ← Feedback confirmed
... (continued working correctly)
```

**Yesterday's override worked correctly** - valve command was sent 5 seconds after override activation and feedback confirmed the valve opened to 100%.

### Root Cause Analysis

**Investigation findings:**

The valve command flow is:
```
app.py → valve_coordinator.apply_valve_command() → trvs.set_valve() → TRV hardware
```

**Execution trace at 14:42:32:**

1. **Room controller (line 697 in app.py):**
   - Computed bathroom calling=True, valve_percent=100%
   - Saved persistence: `bathroom:[0,1]` (valve=0 from OLD state, calling=1 from NEW state)
   - This happens BEFORE valve calculation completes

2. **Boiler controller (line 707 in app.py):**
   - Read room_data with valve_percent=100%
   - Calculated `total_from_bands = 100%` (≥ min 100%)
   - Returned `persisted_valves = {bathroom: 100}` and `valves_must_stay_open = False`
   - Since boiler transitioned OFF→PENDING_ON (not pump overrun), persistence was NOT activated

3. **Valve coordinator (line 734 in app.py):**
   - Called with desired_percent=100%
   - No persistence overrides active (boiler cleared them)
   - No load sharing overrides active
   - Should have passed through to TRV controller with percent=100%

4. **TRV controller (trv_controller.py line 140+):**
   - **Silent early return** - no "Setting TRV" log generated
   - Command never sent to hardware
   - Valve feedback sensor never updated

**Possible causes investigated:**

- ❌ Load sharing interference: Ruled out (no "Load sharing" logs at 14:42:32)
- ❌ Persistence override: Ruled out (persistence not active during OFF→PENDING_ON transition)
- ❌ Rate limiting: Ruled out (last command at 14:33:19, 9+ minutes elapsed > 30s min_interval)
- ⚠️ **Change detection (line 163 in trv_controller.py):** Most likely culprit
  ```python
  last_commanded = self.trv_last_commanded.get(room_id)
  if last_commanded == percent:
      return  # Silent early return if value unchanged
  ```

**Mystery:** 
- `trv_last_commanded['bathroom']` should be 0 (from 14:33:19 command)
- Desired percent is 100
- These are different, so change detection should NOT have blocked the command
- Yet no "Setting TRV" log appears, indicating `set_valve()` returned early

**Timing anomaly:**
The persistence entity was written at 14:42:32.282695 with `bathroom:[0,1]` BEFORE the valve calculation completed at 14:42:32.293179. This creates stale valve data in the persistence entity, but:
- The boiler reads valve data from `room_data`, not from the persistence entity
- The valve coordinator doesn't use the persistence entity directly
- So this shouldn't prevent the command from being sent

### Related Code Locations

**Valve command path:**
- `app.py` lines 690-745: Main recompute loop
- `controllers/room_controller.py` line 292: `_persist_calling_state()` called when calling changes
- `controllers/room_controller.py` line 171-194: `_persist_calling_state()` implementation (updates calling, preserves OLD valve)
- `controllers/boiler_controller.py` line 59-175: `update_state()` - builds valve persistence
- `controllers/valve_coordinator.py` line 120-175: `apply_valve_command()` - priority system
- `controllers/trv_controller.py` line 140-185: `set_valve()` - rate limiting and change detection

**Key code snippets:**

**trv_controller.py line 157-166 (change detection):**
```python
# Check if value actually changed
last_commanded = self.trv_last_commanded.get(room_id)
if last_commanded == percent:
    return
```

**trv_controller.py line 177 (the log that never appeared):**
```python
self.ad.log(f"Setting TRV for room '{room_id}': {percent}% open (was {last_commanded}%)")
```

### Investigation Status

**What we know:**
- Override worked yesterday morning (2025-11-25)
- Override broken today (2025-11-26 14:42+)
- Something changed between yesterday and today
- The valve command reaches `apply_valve_command()` but never logs "Setting TRV"
- This indicates `set_valve()` returns early before line 177

**What we don't know:**
- Why `set_valve()` returns early (change detection should not trigger)
- What changed between yesterday and today to cause this
- Whether `trv_last_commanded['bathroom']` has incorrect state (should be 0, might be 100?)
- Whether there's a new code path that updates `trv_last_commanded` without sending commands

**Commits since yesterday (2025-11-25 23:00 to 2025-11-26 14:13):**
- 10 commits related to Load Sharing implementation (Phase 0-4)
- 2 commits for boiler controller bug fixes
- 1 commit for unicode character removal
- 1 commit for config reload strategy

**Most likely culprit:** One of the load sharing integration commits modified the valve command flow in a subtle way that causes `set_valve()` to silently skip commands under certain conditions.

### Impact Assessment

**Severity:** CRITICAL
- All temperature overrides completely non-functional
- Room calling for heat but valve stays closed
- Boiler runs but no heat delivered to overridden room
- User cannot manually control room temperatures
- Workaround: None

**Scope:**
- Affects all rooms when using override service
- May affect normal heating operation if similar issue exists in non-override path
- System continues to heat based on schedules (non-override heating may still work)

**User Impact:**
- "I used overrides successfully this morning" - confirms recent regression
- "Everything was working fine yesterday" - confirms issue introduced today

### Testing Notes

**To reproduce:**
1. Set override via API: `curl -X POST http://localhost:5050/api/appdaemon/pyheat_override -d '{"room":"bathroom","target":22.0,"minutes":60}'`
2. Check AppDaemon logs for "Setting TRV for room 'bathroom'" - will be missing
3. Check heating CSV logs - `bathroom_valve_cmd` will remain at 0 despite override active
4. Check HA entity: `sensor.trv_bathroom_valve_opening_degree_z2m` - will remain at previous value

**Debug steps needed:**
1. Add debug logging before line 163 in trv_controller.py to log `last_commanded` and `percent` values
2. Check if `trv_last_commanded` has unexpected state
3. Add debug logging at start of `set_valve()` to confirm method is being called
4. Trace execution path through valve_coordinator to confirm `apply_valve_command()` calls `set_valve()`

### Context

This bug was discovered during fact-finding investigation into override functionality. The user reported that overrides worked yesterday and this morning but stopped working after today's load sharing implementation. Analysis of heating logs confirmed that yesterday's override (06:52) sent valve commands within 5 seconds and worked correctly, while today's override (14:42) never sent valve commands despite all other logic executing correctly.

The bug appears to be a silent failure in the TRV command path where the valve coordinator and boiler logic work correctly, but the TRV controller skips sending the command without logging why.

---

## BUG #2: Safety Valve False Positive During PENDING_OFF Transition

**Status:** FIXED ✅  
**Date Discovered:** 2025-11-21  
**Date Fixed:** 2025-11-25  
**Severity:** Medium - causes unnecessary valve operations and temperature disturbances

### Observed Behavior

During normal heating operation, when the last room stops calling for heat and the boiler enters the `PENDING_OFF` state, the safety valve mechanism incorrectly triggers and forces the safety room's valve to 100%.

**Specific incident on 2025-11-21 at 13:12:18:**
- Lounge stopped calling for heat (was the only active room)
- Boiler correctly transitioned: `STATE_ON` → `STATE_PENDING_OFF`
- Valve positions preserved: `{'pete': 0, 'games': 0, 'lounge': 100, 'abby': 0, 'office': 0, 'bathroom': 0}`
- Safety mechanism incorrectly triggered: "🔥 SAFETY: Climate entity is heat with no demand! Forcing games valve to 100% for safety"
- Games valve forced from 0% → 100% at 13:12:24
- Result: Cold water from games radiator circulated through system
- 23 seconds later (13:12:47): Temperature drop of 11°C (59°C → 48°C) on return sensor

### Evidence

**From AppDaemon logs (2025-11-21 13:12:18):**
```
2025-11-21 13:12:18.174529 INFO pyheat: Boiler: STATE_ON -> PENDING_OFF, preserved valve positions: {'pete': 0, 'games': 0, 'lounge': 100, 'abby': 0, 'office': 0, 'bathroom': 0}
2025-11-21 13:12:18.180746 WARNING pyheat: 🔥 SAFETY: Climate entity is heat with no demand! Forcing games valve to 100% for safety
2025-11-21 13:12:18.226814 INFO pyheat: Valve persistence ACTIVE: pending_off
```

**From heating_logs/2025-11-21.csv at 13:12:24:**
```csv
timestamp,boiler_state,calling,games_valve_command,opentherm_heating_return_temp
2025-11-21 13:12:18,pending_off,False,0,59.0
2025-11-21 13:12:24,pending_off,False,100,59.0
2025-11-21 13:12:30,pending_off,False,100,56.0
2025-11-21 13:12:36,pending_off,False,100,54.0
2025-11-21 13:12:41,pending_off,False,100,52.0
2025-11-21 13:12:47,pending_off,False,100,48.0
```

Temperature dropped 11°C in 29 seconds after games valve opened, despite no heating demand.

### Root Cause Analysis

**Safety Check Logic (boiler_controller.py, line 365):**
```python
if safety_room and boiler_entity_state != "off" and len(active_rooms) == 0:
    # Force safety valve to 100%
```

**Trigger Conditions Met:**
1. `safety_room = "games"` ✓ (configured in boiler.yaml)
2. `boiler_entity_state != "off"` ✓ (climate entity was "heat")
3. `len(active_rooms) == 0` ✓ (no rooms calling)

**Why This Is a False Positive:**

During the `PENDING_OFF` state:
- **Climate entity "heat" state is expected** - The climate entity is not turned off until the boiler transitions to `PUMP_OVERRUN` state (30 seconds later)
- **Valve persistence is already active** - Line 242-248 of boiler_controller.py preserves valve positions and sets `valves_must_stay_open = True`
- **Adequate flow already exists** - Lounge radiator maintained at 100% provides sufficient circulation path
- **Boiler will turn off automatically** - The state machine handles the off-delay (30s) and then calls `_set_boiler_off()` when entering `PUMP_OVERRUN`

The safety check does not distinguish between:
- **Abnormal scenario** (genuine safety concern): Climate entity stuck "heat" when it should be off, with no demand
- **Normal scenario** (this case): Climate entity legitimately "heat" during the `PENDING_OFF` transition state where valve persistence is already handling safety

### Related Code Locations

**boiler_controller.py:**
- Line 365: Safety valve trigger condition (does not check boiler FSM state)
- Lines 240-248: `STATE_ON` → `STATE_PENDING_OFF` transition (climate entity NOT turned off)
- Lines 278-288: `PENDING_OFF` state handling (valve persistence active, uses persisted positions)
- Line 295: `PENDING_OFF` → `PUMP_OVERRUN` transition calls `_set_boiler_off()`
- Line 541-553: `_set_boiler_on()` implementation (called when entering STATE_ON)
- Line 570-579: `_set_boiler_off()` implementation (called when entering PUMP_OVERRUN)

**State Machine Timing:**
- Climate entity turned ON: When entering `STATE_ON`
- Climate entity turned OFF: When entering `PUMP_OVERRUN` (30 seconds after entering `PENDING_OFF`)
- Safety check runs: On every valve position update during `PENDING_OFF`

### Impact Assessment

**Severity:** Medium
- Causes unnecessary valve operations during every heating cycle shutdown
- Introduces cold water circulation when not needed
- Can cause temperature disturbances (11°C drop observed)
- Does not affect safety (valve persistence already provides protection)
- Does not prevent heating or cause equipment damage

**Frequency:** Occurs on every heating cycle when:
- Last active room stops calling for heat
- Boiler enters PENDING_OFF state
- Safety room valve was not already open

**System Behavior:** 
- Heating system continues to function correctly
- Short-cycling protection works as designed (this bug is unrelated)
- Temperature control eventually recovers
- No equipment safety issues

### Testing Notes

To reproduce:
1. Start heating with one or more rooms calling
2. Wait for last room to stop calling for heat
3. Observe boiler transition to `PENDING_OFF`
4. Check AppDaemon logs for "SAFETY: Climate entity is heat with no demand"
5. Check heating CSV logs for safety room valve forced to 100%
6. Observe temperature drop on return sensor ~20-30 seconds later

### Context

This bug was discovered during analysis of short-cycling protection field testing on 2025-11-21. The cycling protection implementation worked correctly, but investigation of a temperature anomaly revealed this pre-existing safety valve issue.

The safety valve mechanism exists to protect against scenarios where the boiler could heat with no flow path, but it incorrectly triggers during normal state machine transitions where valve persistence is already active.

### Resolution (2025-11-25)

**Fix Applied:**
Modified safety valve check to be state-aware by adding `self.boiler_state == C.STATE_OFF` condition.

**Old Logic (Buggy):**
```python
if safety_room and boiler_entity_state != "off" and len(active_rooms) == 0:
    # Force safety valve - triggers during PENDING_OFF! ❌
```

**New Logic (Fixed):**
```python
if safety_room and self.boiler_state == C.STATE_OFF and boiler_entity_state != "off" and len(active_rooms) == 0:
    # Only trigger when state machine is OFF - not during PENDING_OFF/PUMP_OVERRUN ✅
```

**Why This Works:**
- During `PENDING_OFF` and `PUMP_OVERRUN`, valve persistence is already active (provides flow path)
- Safety valve only needed when state machine is `STATE_OFF` but entity could heat (genuine desync)
- Legitimate scenarios (master toggle, entity unavailability recovery) properly detected
- Normal state machine transitions no longer trigger false positives

**Recurrence on 2025-11-25:**
- Bug recurred after 2025-11-23 "desync detection fix"
- That fix added startup detection but treated `PENDING_OFF` + `entity=heat` as "unexpected desync"
- Caused entity to be turned off, triggering recompute that hit safety check before entity state updated
- Created double-trigger pattern: once on transition, once on desync correction

**Additional Analysis:**
See `debug/safety_valve_analysis_2025-11-25.md` for comprehensive timeline analysis, code execution flow, and edge case testing.

**Files Modified:**
- `controllers/boiler_controller.py` (line 384): Added state machine check to safety valve condition

**Testing:**
- No more false positives during normal `PENDING_OFF` transitions
- Safety valve still triggers for legitimate desyncs (master toggle, manual control)
- Verified with all edge cases: startup, pump overrun, entity unavailability

---

## BUG #1: Override Targets Not Being Applied (CRITICAL)

**Status:** Fixed  
**Date Discovered:** 2025-11-20  
**Date Fixed:** 2025-11-20  
**Severity:** Critical - breaks override functionality completely  
**Branch:** `trv-responsibility-encapsulation`

### Observed Behavior

When setting a temperature override via the AppDaemon service:
```bash
curl -X POST "http://localhost:5050/api/appdaemon/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "target": 15.0, "minutes": 60}'
```

The service returns success and the override entities are correctly updated:
- `input_number.pyheat_pete_override_target` = 15.0 ✓
- `timer.pyheat_pete_override` = active ✓

However, the room's target temperature sensor does NOT update:
- `sensor.pyheat_pete_target` = 14.0 (unchanged) ✗

Expected: `sensor.pyheat_pete_target` should show 15.0

### Root Cause Analysis

**Primary Cause:** Missing TRV controller reference in BoilerController initialization

During Issue #5 Part A resolution (TRV Encapsulation, 2025-11-20), the following changes were made to `boiler_controller.py`:
- Added calls to `self.trvs.is_valve_feedback_consistent()` (line 498)
- Added calls to `self.trvs.get_valve_command()` (line 499)  
- Added calls to `self.trvs.get_valve_feedback()` (line 500)

However, the `trvs` reference was never added to:
1. `BoilerController.__init__()` method signature
2. The initialization call in `app.py` line 64

**Exception Thrown:**
```python
AttributeError: 'BoilerController' object has no attribute 'trvs'
```

**Execution Flow:**
1. Override service called → entities updated successfully
2. Recompute triggered → `recompute_all()` executes
3. Room computation completes → target correctly resolved to 15.0 by `scheduler.resolve_room_target()`
4. Boiler state update called → `boiler.update_state()` throws AttributeError
5. Exception prevents execution from reaching status publishing code
6. Target sensor never updated, remains at old value

### Evidence

Debug logging added during investigation showed:
```
DEBUG: Starting room computation loop
DEBUG resolve_room_target(pete): override_target=15.0
DEBUG resolve_room_target(pete): Returning override target 15.0 -> 15.0 (precision=1)
ERROR: Exception in boiler.update_state(): 'BoilerController' object has no attribute 'trvs'
```

The execution stops after "Starting room computation loop" and never reaches the valve publishing loop where `publish_room_entities()` is called.

### Related Code Locations

**Files Modified in Issue #5 Part A:**
- `trv_controller.py`: Added `get_valve_feedback()`, `get_valve_command()`, `is_valve_feedback_consistent()` methods
- `boiler_controller.py`: Lines 498-500 now call these TRV methods
- `room_controller.py`: Removed direct TRV sensor access

**Missing Updates:**
- `boiler_controller.py` line 32: `__init__()` method signature - needs `trvs` parameter
- `boiler_controller.py` line 42: Need to add `self.trvs = trvs` assignment
- `app.py` line 64: Need to pass `self.trvs` to BoilerController constructor

### Fix Strategy

**Option 1: Pass TRV Reference (Recommended)**
```python
# In boiler_controller.py __init__:
def __init__(self, ad, config, alert_manager=None, valve_coordinator=None, trvs=None):
    ...
    self.trvs = trvs

# In app.py:
self.boiler = BoilerController(self, self.config, self.alerts, self.valve_coordinator, self.trvs)
```

**Option 2: Remove TRV Feedback Check**
Remove lines 498-500 from boiler_controller.py and handle TRV validation elsewhere. However, this would lose the safety check that was intentionally added.

**Recommendation:** Use Option 1 - the TRV feedback validation is valuable for safety, we just need to complete the integration properly.

### Resolution

**Date Fixed:** 2025-11-20

**Changes Made:**
1. Updated `boiler_controller.py` line 32: Added `trvs=None` parameter to `__init__()` signature
2. Updated `boiler_controller.py` line 44: Added `self.trvs = trvs` assignment
3. Updated `app.py` line 64: Modified initialization to pass `self.trvs` to BoilerController

**Verification:**
- All component initializations audited to ensure no similar issues exist
- Other controllers verified to have complete dependency chains:
  - `ValveCoordinator`, `TRVController`, `RoomController`, `Scheduler`, `SensorManager`, `StatusPublisher` - all ✅

**Additional Pattern Analysis:**
Performed systematic audit of all controller `__init__` methods against their `self.*` attribute usage to identify any similar missing dependency patterns. No other issues found.

### Impact Assessment

**Severity:** CRITICAL
- All overrides are non-functional
- Status entities not updating during recomputes (though temperature sensors still update via sensor callbacks)
- System still heating based on schedules, but control loop is broken
- Every periodic recompute (every 60 seconds) throws an exception

**Workaround:** None - system must be fixed

**Introduced By:** Commit related to Issue #5 Part A (TRV Encapsulation) on 2025-11-20

### Testing Notes

After fix is applied, verify:
1. ✓ Override service successfully sets target
2. ✓ `sensor.pyheat_<room>_target` updates immediately
3. ✓ No exceptions in AppDaemon logs during recompute
4. ✓ Boiler state machine executes completely
5. ✓ All room status entities update correctly
6. ✓ TRV feedback validation works as intended

### Lessons Learned

**Integration Testing:** When refactoring cross-component dependencies:
1. Grep for all usages of new methods being added
2. Verify all components that need the new dependency receive it
3. Test the entire system end-to-end, not just individual components
4. Check for AttributeError exceptions after refactoring

**Component Coupling:** While TRV encapsulation was correct architecturally, the implementation missed updating all dependent components. This highlights the importance of:
- Following the dependency chain completely
- Using IDE refactoring tools that track all usages
- Having integration tests that exercise all code paths

---

## Bug Template

```markdown
## BUG #N: Title

**Status:** [Identified | In Progress | Fixed | Verified]
**Date Discovered:** YYYY-MM-DD
**Severity:** [Critical | High | Medium | Low]
**Branch:** branch-name

### Observed Behavior
What happens (with examples/commands)

### Root Cause Analysis
What's actually wrong and why

### Evidence
Logs, error messages, debug output

### Related Code Locations
Files and line numbers involved

### Fix Strategy
How to fix it (with code snippets if helpful)

### Impact Assessment
Who/what is affected

### Testing Notes
How to verify the fix

### Lessons Learned
What to do differently next time
```
