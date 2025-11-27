# PyHeat Bug Tracker

This document tracks known bugs and their resolutions.

---

## BUG #6: Load Sharing Valves Persist After Deactivation

**Status:** OPEN 🔴  
**Date Discovered:** 2025-11-27  
**Severity:** High - causes unnecessary heating of unscheduled rooms, wasted energy, and comfort issues

### Observed Behavior

When load sharing activates and opens additional valves to increase system capacity, those valves remain physically open even after load sharing deactivates. The valves persist indefinitely until:
1. Those rooms naturally call for heat (which may never happen if they're satisfied), or
2. A system restart occurs, or
3. Manual intervention

This causes unscheduled rooms to receive heat when they shouldn't, wasting energy and potentially overheating rooms.

**Specific incident on 2025-11-27:**

**Timeline:**
1. **12:20:32** - Games room calls for heat, boiler turns on, games_valve = 100%
2. **12:23:02** - Games stops calling, pump overrun starts (games_valve held at 100%)
3. **12:23:02** - Pump overrun ends, games_valve closes to 0% ✅
4. **13:00:01** - Bathroom calls for heat, boiler turns on, bathroom_valve = 100%
5. **13:01:16** - **Load sharing activates**: System opens lounge_valve=100%, games_valve=60% (neither room calling for heat)
   - Trigger: Low delta_t (heating_temp=73°C, return_temp=65°C → delta_t=8°C < 10°C threshold)
   - Total valve percentage jumps from 100% to 260%
6. **13:02:32** - Bathroom stops calling, boiler goes OFF
7. **13:02:32 to 14:08:32** - **BUG**: lounge_valve=100% and games_valve=60% remain open for **66 minutes** even though:
   - No rooms are calling for heat
   - Load sharing is inactive (pump_overrun_active=False)
   - Boiler is OFF
8. **14:02:01** - AppDaemon restarts, restores pump overrun state from HA entity: `{'games': 60, 'lounge': 100, 'bathroom': 100}`
   - This old state (from the 13:01:16 load sharing activation) was never cleared from the persistence entity
9. **14:05:55** - Pete's room calls for heat, then stops → pump overrun starts, capturing current valve positions including the stale load-sharing valves
10. **14:08:32** - Pump overrun ends, valves finally close

**Duration of bug:** 66 minutes (13:02:32 to 14:08:32) where lounge and games received unwanted heating.

### Evidence

**From heating_logs/2025-11-27.csv - Load sharing activation at 13:01:16:**
```
timestamp,boiler_state,pump_overrun_active,lounge_calling,lounge_valve_cmd,lounge_valve_fb,games_calling,games_valve_cmd,games_valve_fb,bathroom_calling,bathroom_valve_cmd,total_valve_pct,ot_heating_temp,ot_return_temp
2025-11-27 13:01:13,on,False,False,0,0,False,0,0,True,100,100,72,65
2025-11-27 13:01:15,on,False,False,0,0,False,0,0,True,100,100,72,65
2025-11-27 13:01:16,on,False,False,100,100,False,60,60,True,100,260,73,65
2025-11-27 13:01:20,on,False,False,100,100,False,60,60,True,100,260,72,65
```
Note: At 13:01:16, delta_t = 73-65 = 8°C (below 10°C threshold), triggering load sharing to open lounge and games valves.

**From heating_logs/2025-11-27.csv - Valves persist after bathroom stops:**
```
timestamp,boiler_state,bathroom_calling,lounge_calling,lounge_valve_cmd,games_calling,games_valve_cmd
2025-11-27 13:02:32,off,True,False,100,False,60
2025-11-27 13:02:34,on,True,False,100,False,60
2025-11-27 13:10:00,off,False,False,100,False,60
2025-11-27 13:20:00,off,False,False,100,False,60
2025-11-27 13:30:00,off,False,False,100,False,60
2025-11-27 13:40:00,off,False,False,100,False,60
2025-11-27 13:50:00,off,False,False,100,False,60
2025-11-27 14:00:00,off,False,False,100,False,60
2025-11-27 14:08:32,off,False,False,0,False,0
```
Valves stayed at lounge=100%, games=60% for entire period while boiler was OFF and no rooms calling.

**From AppDaemon logs - 14:02 restart restoration:**
```
2025-11-27 14:02:02.073921 INFO pyheat: ValveCoordinator: Restored pump overrun state from entity: {'games': 60, 'lounge': 100, 'bathroom': 100}
```
The persistence entity contained stale values from the 13:01:16 load sharing activation.

**From AppDaemon logs - 14:05 pump overrun capturing stale values:**
```
2025-11-27 14:05:25.485866 INFO pyheat: ValveCoordinator: Pump overrun enabled, persisting: {'pete': 100, 'games': 60, 'lounge': 100, 'abby': 0, 'office': 0, 'bathroom': 100}
```
Pump overrun captured the stale load-sharing valve positions (games=60, lounge=100), perpetuating them further.

### Root Cause Analysis

When load sharing exits the ACTIVE state and returns to INACTIVE/DISABLED, it clears the load sharing overrides from the valve coordinator via `clear_load_sharing_overrides()`. However, this only removes the *override layer* - it doesn't command the TRVs to close their physically-open valves.

**Flow:**
1. Load sharing activates → `set_load_sharing_overrides({'lounge': 100, 'games': 60})`
2. Valve coordinator applies these overrides → TRVs physically open to 100% and 60%
3. Load sharing deactivates → `clear_load_sharing_overrides()`
4. Override layer cleared, but TRVs remain physically at 100% and 60%
5. With boiler OFF and no rooms calling, no valve commands are generated
6. Result: TRVs stay open indefinitely at their last commanded positions

**Why this persists across restarts:**
- The persistence entity (`input_text.pyheat_room_persistence`) stores valve positions for pump overrun resilience
- When load sharing commands valves open, those positions are tracked in `current_commands`
- If pump overrun activates while load-sharing valves are still open, it captures those positions
- On restart, these stale positions are restored as "pump overrun state"
- This creates a cycle where load-sharing valve positions can persist indefinitely

### Possible Fix Approaches

Several approaches could address this issue:

**Option 1: Explicit valve closure on load sharing exit**
- When load sharing transitions from ACTIVE → INACTIVE, explicitly command all previously-opened load-sharing rooms to valve=0%
- Requires tracking which rooms were opened by load sharing
- Ensures clean state after load sharing deactivates

**Option 2: Clear valve coordinator overrides with explicit closure**
- Modify `clear_load_sharing_overrides()` to accept a list of rooms that need explicit closure commands
- Valve coordinator sends valve=0% commands to those rooms when clearing overrides

**Option 3: Only persist valves from calling rooms**
- When capturing pump overrun snapshot, only persist valves where the room is actually calling for heat
- Prevents load-sharing valves (which aren't from calling rooms) from being persisted
- Requires filtering `current_commands` by room calling state

**Option 4: Periodic valve state reconciliation**
- Add a periodic check (e.g., every 5 minutes) that closes valves for rooms that aren't calling and aren't in an override state
- Catches stale valve positions regardless of source
- More robust but adds complexity

**Note:** The correct fix requires careful consideration of edge cases (e.g., room starts calling during load sharing, pump overrun during load sharing, etc.).

### Impact

- **Energy waste:** Heating unscheduled rooms for extended periods (66 minutes in this incident)
- **Comfort issues:** Rooms may overheat when they shouldn't be receiving heat
- **Confusion:** Valve positions don't match calling state, making debugging difficult
- **Persistence amplification:** Stale values can persist across restarts via pump overrun state restoration

### Testing Notes

To reproduce:
1. Have low system capacity (e.g., one room calling with large radiator)
2. Wait for boiler to run long enough to establish low delta_t (return temp close to setpoint)
3. Load sharing will activate and open additional valves
4. Stop all heating demand (room reaches target)
5. Observe that load-sharing valves remain open even though boiler is OFF

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

## BUG #3: Override Functionality Broken - Valve Commands Not Sent

**Status:** INVESTIGATING  
**Date Discovered:** 2025-11-26 (14:42:32 test override)  
**Severity:** CRITICAL - completely breaks override functionality  
**Branch:** feature/load-sharing-phase4

### Observed Behavior

When setting a temperature override via the API:
```bash
curl -X POST "http://localhost:5050/api/appdaemon/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "bathroom", "target": 22.0, "minutes": 60}'
```

**What works:**
- Override service returns success ✓
- Timer started: `timer.pyheat_bathroom_override` active for 60 minutes ✓
- Target updated: 10.0°C → 22.0°C ✓
- Room controller calculates valve should be 100% (error=11.27°C, band 0→max) ✓
- Boiler turned ON correctly ✓

**What fails:**
- No "Setting TRV for room 'bathroom': 100%" log message ✗
- Valve command never sent to TRV hardware ✗
- Feedback sensor remains at 0% ✗
- Heating CSV logs show: `bathroom_valve_cmd=0` throughout override period ✗

### Root Cause Analysis

**Valve command flow:**
```
app.py → valve_coordinator.apply_valve_command() → trvs.set_valve() → TRV hardware
```

**Execution trace at 14:42:32:**
1. Room controller computed bathroom calling=True, valve_percent=100%
2. Boiler controller calculated total_from_bands=100%, returned persisted_valves
3. Valve coordinator called with desired_percent=100%
4. TRV controller: **Silent early return** - no "Setting TRV" log generated

**Possible cause:** Change detection in `trv_controller.py` line 160-162:
```python
last_commanded = self.trv_last_commanded.get(room_id)
if last_commanded == percent:
    return  # Silent early return if value unchanged
```

**Mystery:** `trv_last_commanded['bathroom']` should be 0 (from 14:33:19 command), desired percent is 100. These are different, so change detection should NOT have blocked the command. Yet no "Setting TRV" log appears.

**False positive from yesterday (2025-11-25):** Override appeared to work at 06:52:09, but valve was already at 100% from hours earlier. No new command was needed, so bug wasn't exposed.

**Most likely culprit:** One of the recent load sharing commits (2025-11-25 23:00 to 2025-11-26 14:13) modified the valve command flow in a way that causes `set_valve()` to silently skip commands.

### Evidence

**AppDaemon Logs (2025-11-26 14:42:32):**
```
14:42:32.232444 INFO: Override set: room=bathroom, target=22.0C, duration=3600s
14:42:32.293179 INFO: Room 'bathroom': valve band 0 -> max (error=11.27°C, valve=100%)
14:42:32.300486 INFO: Boiler: off -> on (demand and conditions met)
14:42:32.305969 INFO: Boiler ON
[MISSING: "Setting TRV for room 'bathroom': 100% open" - this line never appears]
```

**Last TRV command:** 14:33:19 (setting valve to 0% after pump overrun). No subsequent command at 14:42:32.

**CSV Logs (heating_logs/2025-11-26.csv):**
```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
14:42:38,True,True,0,0
14:42:46,True,True,0,0
... (continued for 13+ minutes, valve_cmd never changed from 0)
```

**Comparison with working override (2025-11-25 06:52) - FALSE POSITIVE:**
```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
06:52:09,True,True,100,0
06:52:14,True,True,100,100      ← Valve already at 100% from hours earlier
```
Further investigation revealed bathroom valve was already commanded to 100% since at least 06:45:00. Room was NOT calling for heat (calling=False, target=10.0). When override triggered, valve was already open, so no new command was needed.

### Related Code Locations

**Valve command path:**
- `app.py` lines 690-745: Main recompute loop
- `controllers/room_controller.py` line 292: `_persist_calling_state()` when calling changes
- `controllers/room_controller.py` line 171-194: `_persist_calling_state()` implementation
- `controllers/boiler_controller.py` line 59-175: `update_state()` - builds valve persistence
- `controllers/valve_coordinator.py` line 120-175: `apply_valve_command()` - priority system
- `controllers/trv_controller.py` line 140-185: `set_valve()` - rate limiting and change detection

**trv_controller.py line 157-166 (change detection):**
```python
last_commanded = self.trv_last_commanded.get(room_id)
if last_commanded == percent:
    return
```

**trv_controller.py line 177 (the log that never appeared):**
```python
self.ad.log(f"Setting TRV for room '{room_id}': {percent}% open (was {last_commanded}%)")
```

### Fix Strategy

**Debug steps needed:**
1. Add debug logging before line 163 in trv_controller.py to log `last_commanded` and `percent` values
2. Check if `trv_last_commanded` has unexpected state
3. Add debug logging at start of `set_valve()` to confirm method is being called
4. Trace execution path through valve_coordinator to confirm `apply_valve_command()` calls `set_valve()`
5. Check if there's a code path that updates `trv_last_commanded` without sending commands

**Attempted Fix #1: Valve Persistence Integration (DID NOT FIX BUG)**

**Date Attempted:** 2025-11-26 15:17  
**Commit:** f0985b6 (reverted in d286732)  
**Status:** UNCERTAIN RELEVANCE

**Theory:** ValveCoordinator introduced 2025-11-19 with incomplete integration. The boiler returns `persisted_valves`, but `app.py` never called `valve_coordinator.set_persistence_overrides()`.

**Fix Applied:** Added code in app.py after `boiler.update_state()`:
```python
if persisted_valves:
    if valves_must_stay_open:
        reason = "pump_overrun"
    else:
        reason = "interlock"
    self.valve_coordinator.set_persistence_overrides(persisted_valves, reason)
else:
    self.valve_coordinator.clear_persistence_overrides()
```

**Test Results:** Override triggered at 15:20:23, TRV command WAS sent, but user reports this did NOT fix the actual bug. The valve command sent during test was likely due to different initial conditions.

**Conclusion:** This fix addresses a real architectural issue (missing persistence integration) but does not solve the override bug. The fix may still be relevant for proper valve persistence handling during pump overrun and interlock states. **Relevance: UNCERTAIN** - may be necessary but not sufficient.

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

**Commits since last known working state (2025-11-25 23:00 to 2025-11-26 14:13):**
- 10 commits related to Load Sharing implementation (Phase 0-4)
- 2 commits for boiler controller bug fixes
- 1 commit for unicode character removal
- 1 commit for config reload strategy

### Testing Notes

**To reproduce:**
1. Ensure bathroom valve is at 0% to start (critical to avoid false positives)
2. Set override via API: `curl -X POST http://localhost:5050/api/appdaemon/pyheat_override -d '{"room":"bathroom","target":22.0,"minutes":60}'`
3. Check AppDaemon logs for "Setting TRV for room 'bathroom'" - will be missing
4. Check heating CSV logs - `bathroom_valve_cmd` will remain at 0 despite override active
5. Check HA entity: `sensor.trv_bathroom_valve_opening_degree_z2m` - will remain at previous value

**Important:** Start with valve at 0% to avoid false positives like 2025-11-25's test.

### Context

Discovered during fact-finding investigation into override functionality. User reported overrides worked yesterday but stopped working after today's load sharing implementation.

Initial analysis suggested yesterday's override worked, but deeper investigation revealed it was a false positive - the valve was already at 100% from hours earlier, so no new command was needed when the override was triggered.

The actual bug has likely been present since one of the recent load sharing commits, but was masked by coincidental valve states until today's test exposed it.

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
