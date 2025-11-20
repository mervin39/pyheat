# PyHeat Bug Tracker

This document tracks known bugs and their resolutions.

---

## BUG #1: Override Targets Not Being Applied (CRITICAL)

**Status:** Identified, not yet fixed  
**Date Discovered:** 2025-11-20  
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
