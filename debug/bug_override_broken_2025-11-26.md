# BUG #3: Override Functionality Broken - Valve Commands Not Sent

**Date Discovered:** 2025-11-26 (14:42:32 test override)  
**Severity:** Critical - completely breaks override functionality  
**Status:** INVESTIGATING  
**Affected Component:** TRV Controller / Valve Coordinator integration

---

## Executive Summary

When setting a temperature override via the API, the override entities are correctly updated and the room controller calculates the correct valve percentage, but **no TRV command is sent**. The valve remains at its previous position indefinitely.

---

## Observed Behavior

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

---

## Evidence

### AppDaemon Logs (2025-11-26 14:42:32)

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

### CSV Logs (heating_logs/2025-11-26.csv)

```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
14:42:38,True,True,0,0
14:42:46,True,True,0,0
14:42:47,True,True,0,0
14:42:49,True,True,0,0
14:42:50,True,True,0,0
... (continued for 13+ minutes, valve_cmd never changed from 0)
```

### Comparison with Working Override (2025-11-25 06:52)

**Yesterday's override worked correctly:**
```csv
time,bathroom_override,bathroom_calling,bathroom_valve_cmd,bathroom_valve_fb
06:52:09,True,True,100,0
06:52:14,True,True,100,100      ← Valve command sent within 5 seconds
06:52:15,True,True,100,100      ← Feedback confirmed
... (continued working correctly)
```

**However, further investigation revealed this was a FALSE POSITIVE:**
- Bathroom valve was already commanded to 100% from hours earlier
- CSV logs show `bathroom_valve_cmd=100` since at least 06:45:00
- Room was NOT calling for heat (calling=False, target=10.0)
- When override triggered at 06:52:09, valve was already open
- No new command was needed, so bug wasn't exposed

---

## Root Cause Analysis

### Investigation Findings

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

### Possible Causes Investigated

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

---

## Attempted Fix #1: Valve Persistence Integration

**Date Attempted:** 2025-11-26 15:17  
**Commit:** f0985b6 (reverted in d286732)  
**Status:** DID NOT FIX THE BUG

**Theory:**
ValveCoordinator was introduced on 2025-11-19 with incomplete integration. The boiler returns `persisted_valves` from `update_state()`, but `app.py` never called `valve_coordinator.set_persistence_overrides()` with these values.

**Fix Applied:**
Added code in app.py after `boiler.update_state()` to pass persistence overrides:
```python
# Apply persistence overrides to valve coordinator (safety-critical)
if persisted_valves:
    if valves_must_stay_open:
        reason = "pump_overrun"
    else:
        reason = "interlock"
    self.valve_coordinator.set_persistence_overrides(persisted_valves, reason)
else:
    self.valve_coordinator.clear_persistence_overrides()
```

**Test Results:**
- Override triggered at 15:20:23
- TRV command WAS sent: "Setting TRV for room 'bathroom': 100% open (was 0%)"
- Hardware feedback confirmed: valve changed 0% → 100%
- **HOWEVER:** User reports this did NOT fix the actual bug
- The valve command sent during test was likely due to different initial conditions

**Conclusion:**
This fix addresses a real architectural issue (missing persistence integration) but does not solve the override bug. The fix may still be relevant for proper valve persistence handling during pump overrun and interlock states.

**Relevance:** UNCERTAIN - May be necessary but not sufficient to fix the override bug.

---

## Related Code Locations

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

---

## Investigation Status

**What we know:**
- Override worked yesterday morning (2025-11-25) - BUT this was a false positive (valve already open)
- Override broken today (2025-11-26 14:42+)
- Valve coordinator receives correct desired_percent (100%)
- TRV controller never logs "Setting TRV", indicating early return
- Change detection shouldn't trigger (0 != 100)

**What we don't know:**
- Why `set_valve()` returns early (change detection should not trigger)
- What the actual value of `trv_last_commanded['bathroom']` is at the moment of override
- Whether there's a race condition or state corruption issue
- Whether the attempted fix revealed or masked the real issue

**Commits since yesterday (2025-11-25 23:00 to 2025-11-26 14:13):**
- 10 commits related to Load Sharing implementation (Phase 0-4)
- 2 commits for boiler controller bug fixes
- 1 commit for unicode character removal
- 1 commit for config reload strategy

**Most likely culprit:** One of the load sharing integration commits modified the valve command flow in a subtle way that causes `set_valve()` to silently skip commands under certain conditions.

---

## Debug Steps Needed

1. Add debug logging before line 163 in trv_controller.py to log `last_commanded` and `percent` values
2. Check if `trv_last_commanded` has unexpected state
3. Add debug logging at start of `set_valve()` to confirm method is being called
4. Trace execution path through valve_coordinator to confirm `apply_valve_command()` calls `set_valve()`
5. Check if there's a code path that updates `trv_last_commanded` without sending commands

---

## Impact Assessment

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
- "I used overrides successfully this morning" - appears to be false positive
- "Everything was working fine yesterday" - confirms issue is recent

---

## Testing Notes

**To reproduce:**
1. Ensure bathroom valve is at 0% to start
2. Set override via API: `curl -X POST http://localhost:5050/api/appdaemon/pyheat_override -d '{"room":"bathroom","target":22.0,"minutes":60}'`
3. Check AppDaemon logs for "Setting TRV for room 'bathroom'" - will be missing
4. Check heating CSV logs - `bathroom_valve_cmd` will remain at 0 despite override active
5. Check HA entity: `sensor.trv_bathroom_valve_opening_degree_z2m` - will remain at previous value

**Important:** Start with valve at 0% to avoid false positives like yesterday's test.

---

## Context

This bug was discovered during fact-finding investigation into override functionality. The user reported that overrides worked yesterday and this morning but stopped working after today's load sharing implementation. 

Initial analysis suggested yesterday's override worked, but deeper investigation revealed it was a false positive - the valve was already at 100% from hours earlier, so no new command was needed when the override was triggered.

The actual bug has likely been present since one of the recent load sharing commits, but was masked by coincidental valve states until today's test exposed it.
