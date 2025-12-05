# PyHeat Bug Tracker

This document tracks known bugs and their resolutions.

---

## BUG #16: Passive Mode Valve Percent UI Shows Stale Value - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-12-05  
**Date Fixed:** 2025-12-05  
**Severity:** Medium - UI shows different value than actual valve position  
**Category:** Passive Mode / UI Sync

### Description

When a room is in user-selected passive mode and the user adjusts the valve slider in pyheat-web, the valve command is sent correctly but the UI reverts to showing an old value (from schedule's `default_valve_percent`) instead of the newly set value.

### Observed Behavior

**Incident on 2025-12-05 at ~13:19 (games/dining room):**

| Source | Value | Explanation |
|--------|-------|-------------|
| Room card (actual valve) | 80% | Valve physically at 80% after user moved slider |
| Modal slider (UI) | 10% | UI showing schedule's `default_valve_percent` instead of HA entity |
| Status line | 10% | "Passive: 8-18°, 10%" from formatted_status |
| HA entity | 80% | Correctly set to 80% by `set_passive_settings` service |

User workflow:
1. User opens games room modal, sees slider at some value
2. User moves slider to 80%
3. Service called: `pyheat.set_passive_settings: room=games, valve_percent=80%`
4. HA entity updated to 80%, valve commanded to 80%
5. User closes modal, opens it again
6. Slider shows 10% (schedule's default), status shows "Passive: 8-18°, 10%"
7. But actual valve is at 80%

### Root Cause Analysis

**Two code paths were using different sources:**

1. **`api_handler.py`** and **`status_publisher.py`** - Used for UI display:
   - Were checking `schedule.get('default_valve_percent')` FIRST
   - If schedule had a value (games: 10), they returned that
   - Never reached the HA entity value (80%)

2. **`scheduler.py`** - Used for actual valve control:
   - Correctly read from HA entity only
   - Valve operated correctly

**Key misunderstanding in original bug report:**
The schedule's `default_valve_percent` is ONLY for **auto mode with scheduled passive blocks** (when `default_mode: passive` in schedules.yaml). It should NOT be used for user-selected passive mode, which should ALWAYS read from the HA input_number entity.

### Fix Applied

Modified `api_handler.py` and `status_publisher.py` to always read from the HA entity:

**Before (broken):**
```python
# Check schedule's default_valve_percent first
schedule = self.config.schedules.get(room_id, {})
schedule_valve = schedule.get('default_valve_percent')
if schedule_valve is not None:
    return int(schedule_valve)  # Always returned 10 for games!
# Fall back to HA entity (never reached)
```

**After (fixed):**
```python
# Read from HA entity (runtime value)
passive_valve_entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room_id)
if self.ad.entity_exists(passive_valve_entity):
    return int(float(self.ad.get_state(passive_valve_entity)))
return C.PASSIVE_VALVE_PERCENT_DEFAULT
```

### Files Modified

- `services/api_handler.py` - Now reads from HA entity only for `passive_valve_percent`
- `services/status_publisher.py` - `_get_passive_valve_percent()` reads from HA entity only

### What About schedule's `default_valve_percent`?

The schedule's `default_valve_percent` is STILL used - but in the correct place:

**`core/scheduler.py`** `get_scheduled_target()` method:
- When a room is in **auto mode** with `default_mode: passive` in schedule
- During times when no scheduled block is active
- Returns the schedule's `default_valve_percent` as part of the scheduled target
- This is then used to SET the HA entity, which is then read by the UI

**Flow for scheduled passive:**
1. Schedule says `default_mode: passive`, `default_valve_percent: 10`
2. `scheduler.py` returns `valve_percent: 10` in scheduled target
3. `room_controller.py` sets HA entity to 10%
4. UI reads HA entity → shows 10%
5. User can override by moving slider → HA entity updates → UI shows new value

### Testing

After AppDaemon restart at 13:30:
- Set games valve to 80% via UI slider
- HA entity confirmed at 80%
- API returns `passive_valve_percent: 80`
- Status shows "Passive: 8-18°, 80%"
- All values consistent ✅

### Related

- Commit `33b7538` removed `initial: 30.0` from HA entity definitions (separate issue where entities reset on HA restart)

---

## BUG #15: Load Sharing Status Text Shows Incorrect Tier Information - FIXED

**Status:** FIXED ✅
**Date Discovered:** 2025-12-03
**Date Fixed:** 2025-12-03
**Severity:** Medium - misleading UI status text, no functional impact
**Category:** Status Display / Load Sharing

### Description

The status text displayed in pyheat-web for rooms activated via load sharing incorrectly shows "Pre-warming for schedule" for **all** load sharing activations (both tier 1 and tier 2), when it should distinguish between schedule-based pre-warming (tier 1) and fallback heating (tier 2).

### Observed Behavior

**Incident on 2025-12-03 at 17:07:**

**Room: Office**
- Current status text: `"Pre-warming for schedule"`
- Load sharing state from API:
  ```json
  {
    "state": "fallback_escalated",
    "active_rooms": [{
      "room_id": "office",
      "tier": 2,
      "reason": "fallback_p3",
      "valve_pct": 80.0
    }],
    "trigger_rooms": ["lounge"]
  }
  ```

**Expected status text:** `"Fallback heating P3"` (or similar) to indicate this is tier 2 fallback, not schedule-based pre-warming.

**What's wrong:**
- Office was selected via **Tier 2 (fallback)** with `reason="fallback_p3"`
- Status text shows `"Pre-warming for schedule"` which implies **Tier 1 (schedule-based)**
- This misleads users into thinking the room is heating for an upcoming schedule block
- Actual selection was emergency fallback due to insufficient scheduled capacity

### Root Cause Analysis

**Status formatting logic** (`services/status_publisher.py` lines 180-211):
```python
# Line 194-211: Load sharing status formatting
if activation.tier in [1, 2]:  # ❌ BUG: Treats BOTH tiers as schedule-aware
    prewarming_minutes = activation.prewarming_minutes
    valve_pct = activation.valve_pct
    
    if prewarming_minutes is not None:
        status = f"Pre-warming for schedule (in {prewarming_minutes}m, {valve_pct}%)"
    else:
        status = f"Pre-warming for schedule ({valve_pct}%)"
else:
    # Tier 3 fallback (priority-based)
    valve_pct = activation.valve_pct
    status = f"Fallback heating P{activation.priority} ({valve_pct}%)"
```

**The problem:**
- Line 194 checks `if activation.tier in [1, 2]`
- This treats **both** TIER_SCHEDULE (1) and TIER_FALLBACK (2) as schedule-aware
- Tier 2 rooms get "Pre-warming for schedule" text even though they have NO schedule
- Only tier 3 rooms show "Fallback heating P{priority}" text

**Tier definitions** (`managers/load_sharing_manager.py` lines 18-19):
```python
TIER_SCHEDULE = 1   # Schedule-aware: rooms with upcoming schedules
TIER_FALLBACK = 2   # Fallback: passive rooms + priority list (Phase A + B)
```

**Why this is misleading:**
- Tier 1: Rooms are genuinely pre-warming for an upcoming schedule block → "Pre-warming for schedule" is correct
- Tier 2: Rooms are in **passive mode** or **fallback priority list** with **no schedule**, selected purely for capacity → "Pre-warming for schedule" is wrong

### Evidence

**API Response from http://localhost:8000/api/status at 17:07:**
```json
{
  "rooms": [
    {
      "id": "office",
      "name": "Office",
      "formatted_status": "Pre-warming for schedule",
      "calling_for_heat": false,
      "valve_percent": 80.0,
      "current_temp": 13.73,
      "target_temp": 12.0,
      "mode": "auto",
      "operating_mode": "passive"
    }
  ],
  "system": {
    "load_sharing": {
      "state": "fallback_escalated",
      "active_rooms": [
        {
          "room_id": "office",
          "tier": 2,
          "reason": "fallback_p3",
          "valve_pct": 80.0,
          "prewarming_minutes": null,
          "priority": 3
        }
      ],
      "trigger_rooms": ["lounge"],
      "min_capacity_w": 2000.0,
      "target_capacity_w": 2500.0
    }
  }
}
```

**Key inconsistencies:**
- `formatted_status`: "Pre-warming for schedule"
- `tier`: 2 (TIER_FALLBACK)
- `reason`: "fallback_p3" (Phase B fallback priority 3)
- `prewarming_minutes`: null (no schedule)
- `operating_mode`: "passive" (no scheduled heating)

**Games room (earlier selection at 17:07:26):**
- Status: `"Auto (passive): 8-14°, 15% forever"` (correct - no longer in load sharing)
- Previously selected via tier 2, reason "fallback_p2"
- This was also incorrectly shown as "Pre-warming for schedule" during activation

### Related Code Locations

**services/status_publisher.py:**
- Lines 127-162: `_format_status_text()` method
- Line 134: Fixed tier check from `if activation.tier in [1, 2]` to `if activation.tier == 1`
- Lines 135-151: Schedule-aware status formatting (tier 1 only)
- Lines 152-158: Fallback status formatting (tier 2)

**managers/load_sharing_manager.py:**
- Lines 19-20: Tier constant definitions (TIER_SCHEDULE=1, TIER_FALLBACK=2)
- Lines 700-858: Tier 1 selection (schedule-based, uses `prewarming_minutes`)
- Lines 860-920: Tier 2 selection (fallback: Phase A passive rooms, Phase B priority list, NO schedule awareness)

**Tier 2 activation context:**
- Tier 2 rooms are **passive mode** or **fallback priority** rooms
- Selected when Tier 1 (schedule-based) provides insufficient capacity
- Phase A: Passive rooms (intentionally allows above max_temp)
- Phase B: Priority list (no schedule requirement)
- Sets `prewarming_minutes=None` (no schedule)
- Sets `reason="fallback_p{priority}"` to indicate fallback selection

### Resolution (2025-12-03)

**Fix Applied:**
Modified `services/status_publisher.py` to distinguish between tier 1 and tier 2 activations.

**Changes:**
- Line 134: Changed from `if activation.tier in [1, 2]` to `if activation.tier == 1`
- Lines 152-158: Added `elif activation.tier == 2` block for fallback-specific status
- Fallback status now shows: `"Fallback heating P{priority} ({valve_pct}%)"`

**Why This Works:**
- Tier 1 (TIER_SCHEDULE): Schedule-aware rooms show "Pre-warming for {time}"
- Tier 2 (TIER_FALLBACK): Fallback rooms show "Fallback heating P{priority} (valve%)"
- Status text now correctly reflects the selection logic that activated each room

**Files Modified:**
- `services/status_publisher.py`: Updated load sharing status formatting logic

### Impact

**Medium severity because:**
- Confuses users about why rooms are heating
- Users may think schedule is incorrect when it's actually fallback behavior
- Makes debugging harder - status text doesn't match actual system state
- No functional impact - heating works correctly, only display issue
- Users may waste time adjusting schedules when the issue is capacity, not scheduling

**User experience:**
- User sees "Pre-warming for schedule" for office
- User checks schedule, sees office scheduled for 12°C (parking temp)
- User confused why system is pre-warming for a 12°C target
- User doesn't realize it's emergency fallback to prevent boiler short-cycling

**Frequency:** Occurs whenever:
- Load sharing activates via Tier 2 (fallback passive rooms)
- Phase A (schedule-based) provides insufficient capacity
- System escalates to Phase B (passive room fallback)
- Common scenario when few rooms have upcoming schedules

### Configuration Context

**From docs/LOAD_SHARING.md - Phase B specification:**
```
Phase B: Fallback to Passive Rooms (Tier 2)
- Select passive rooms by fallback_priority (lowest first)
- NO temperature check (intentionally allows above max_temp)
- NO schedule requirement
- Uses prewarming_minutes=None (not schedule-aware)
```

**From config/rooms.yaml:**
```yaml
office:
  fallback_priority: 3
  mode: auto
  # Office often in passive mode, frequently selected for Tier 2
```

### Investigation Notes

Discovered while analyzing why "games" room was selected for load sharing. Initial analysis found games correctly selected via Phase B fallback (tier 2, priority 2), but when checking current status of office (also tier 2), noticed the status text claimed "Pre-warming for schedule" despite:
- No upcoming schedule (prewarming_minutes=null)
- Passive mode with parking temperature (12°C)
- Selection reason explicitly "fallback_p3"

Cross-referenced status_publisher.py code and found the tier check on line 194 treats tier 1 and tier 2 identically, applying schedule-aware formatting to both.

### Comparison: Correct vs Incorrect Behavior

**Tier 1 (Schedule) - Status text CORRECT:**
```
tier=1, reason="schedule", prewarming_minutes=15
→ "Pre-warming for schedule (in 15m, 100%)" ✓
```

**Tier 2 (Fallback) - Status text INCORRECT:**
```
tier=2, reason="fallback_p3", prewarming_minutes=null
→ "Pre-warming for schedule (80%)" ✗
Should be: "Fallback heating P3 (80%)" or similar
```

---

## BUG #14: Load Sharing Entry Condition Ignores Passive Mode Rooms - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-11-30  
**Date Fixed:** 2025-12-03  
**Severity:** Medium - causes unnecessary load sharing activation  
**Category:** Load Sharing / Capacity Calculation

### Description

When evaluating whether to activate load sharing, the system calculates total capacity by summing only the capacity of **calling rooms**, completely ignoring **passive mode rooms** that have their valves open and are actively contributing heat capacity to the system.

This causes load sharing to activate prematurely because it underestimates the actual system capacity.

### Observed Behavior

**Incident on 2025-11-30 at 22:36:37:**

**System State:**
- Pete's room: calling=True, mode=auto, valve=100%, capacity=1739W
- Games room: calling=False, mode=**passive**, valve=20%, capacity=2504W (at 100%)
- Office room: calling=False, mode=**passive**, valve=0%, capacity=777W (at 100%)

**Load sharing entry calculation:**
```python
# Only counts calling rooms (Pete)
total_capacity = 1739W

# Log message
"Load sharing entry: Low capacity (1739W < 2000W) + cycling protection active"
```

**Actual system capacity:**
- Pete (calling): 1739W
- Games (passive, 20% valve): ~501W (2504W × 0.20)
- **Actual total: ~2240W** (not 1739W!)

**Result:**
- Load sharing activated to add Bathroom at 100% (valve change heard in bathroom)
- This was unnecessary - system already had ~2240W of capacity
- Bathroom valve opened from 0% → 100%, then closed 3 minutes later when load sharing cleared

### Root Cause Analysis

**Entry condition check** (`managers/load_sharing_manager.py` lines 617-644):
```python
def _evaluate_entry_conditions(self, room_states: Dict, cycling_protection_state: str) -> bool:
    # Get calling rooms
    calling_rooms = [rid for rid, state in room_states.items() if state.get('calling', False)]
    
    # Calculate total calling capacity
    all_capacities = self.load_calculator.get_all_estimated_capacities()
    total_capacity = 0.0
    for room_id in calling_rooms:
        capacity = all_capacities.get(room_id)
        if capacity is not None:
            total_capacity += capacity  # ❌ Only counts calling rooms!
    
    # Check capacity threshold
    if total_capacity >= self.min_calling_capacity_w:
        return False
```

**What's missing:** Passive mode rooms with open valves are not included in the capacity calculation.

**Why this matters:**
- Passive mode rooms with open valves are actively dissipating heat
- They contribute to preventing boiler cycling just like calling rooms
- Ignoring them leads to underestimating system capacity by 20-30% in typical scenarios
- Load sharing activates when it shouldn't, causing unnecessary valve operations

### Evidence

**From heating_logs/2025-11-30.csv at 22:36:34:**
```csv
Room Capacities:
- abby_estimated_dump_capacity: 2598.0W
- bathroom_estimated_dump_capacity: 427.0W
- games_estimated_dump_capacity: 2504.0W (mode=passive, valve_cmd=20)
- lounge_estimated_dump_capacity: 2151.0W
- office_estimated_dump_capacity: 777.0W (mode=passive, valve_cmd=0)
- pete_estimated_dump_capacity: 1739.0W (calling=True, valve_cmd=100)

Total valve opening: 120%
System state: cycling protection active (COOLDOWN)
```

**From AppDaemon logs at 22:36:37:**
```
INFO pyheat: Load sharing entry: Low capacity (1739W < 2000W) + cycling protection active
INFO pyheat: Load sharing Tier 3 selection: bathroom - priority=4, valve=50%, target=20.0C, adds 213W (total: 1952W)
```

Note: 1739W + 213W = 1952W, confirming that only Pete's capacity (1739W) was counted before adding bathroom.

### Code Comparison

**Entry condition** (counts calling rooms only):
```python
# managers/load_sharing_manager.py line 632-638
calling_rooms = [rid for rid, state in room_states.items() if state.get('calling', False)]
total_capacity = 0.0
for room_id in calling_rooms:
    capacity = all_capacities.get(room_id)
    if capacity is not None:
        total_capacity += capacity
```

**Active capacity calculation** (counts calling + load sharing rooms):
```python
# managers/load_sharing_manager.py line 1277-1292
# Add calling rooms
for room_id, state in room_states.items():
    if state.get('calling', False):
        capacity = all_capacities.get(room_id)
        if capacity is not None:
            total += capacity

# Add load sharing rooms (with valve adjustment)
for room_id, activation in self.context.active_rooms.items():
    capacity = all_capacities.get(room_id)
    if capacity is not None:
        effective_capacity = capacity * (activation.valve_pct / 100.0)
        total += effective_capacity
```

**Neither function includes passive mode rooms with open valves.**

### Related Code Locations

**Load sharing entry evaluation:**
- `managers/load_sharing_manager.py` lines 617-667: `_evaluate_entry_conditions()`
- Line 632: Gets calling rooms only
- Lines 635-640: Calculates capacity from calling rooms only

**Passive mode exclusion in tier selection:**
- Lines 714-716 (Tier 1): Skip if `operating_mode == 'passive'`
- Lines 802-804 (Tier 2): Skip if `operating_mode == 'passive'`
- Lines 914-916 (Tier 3): Skip if `operating_mode == 'passive'`

**Room state structure:**
- `controllers/room_controller.py` lines 136-300: `compute_room()` returns dict with:
  - `'calling'`: bool - whether actively calling for heat
  - `'operating_mode'`: str - 'active', 'passive', or 'off'
  - `'valve_percent'`: int - current valve position (0-100)

**Passive mode valve control:**
- `controllers/room_controller.py` lines 225-269: Passive mode logic
  - Valves open/close based on temperature vs target
  - Room does NOT call for heat (`calling=False`)
  - Valve percentage stored and used

### Impact

**Medium severity because:**
- Causes unnecessary load sharing activation (false positives)
- Results in unwanted valve operations (user heard bathroom valve change)
- Wastes energy by heating rooms that don't need it
- Creates confusion - rooms appear to randomly receive heat
- No safety issues - system continues to function
- Load sharing eventually deactivates (usually within 3-5 minutes)

**Frequency:** Occurs whenever:
- System has passive mode rooms with open valves (common)
- One or more calling rooms have capacity below threshold (< 2000W)
- Cycling protection is active or high return temp detected
- Combined actual capacity (calling + passive) would be sufficient

**Typical scenario:**
- Office or Games in passive mode with 10-20% valve open (opportunistic heating)
- Pete's room calls for heat with 1700-1900W capacity
- Load sharing activates thinking capacity is only 1700W
- Actual capacity is 2200-2500W (including passive rooms)
- Bathroom or other low-priority room unnecessarily opened

### Configuration Context

**From config/rooms.yaml:**
- Games: delta_t50=2504W, frequently in passive mode
- Office: delta_t50=777W, frequently in passive mode  
- Pete: delta_t50=1900W, often the only calling room

**From config/boiler.yaml:**
- `min_calling_capacity_w: 2000` (load sharing entry threshold)
- `target_capacity_w: 2500` (load sharing target capacity)

### Investigation Notes

Discovered while investigating why bathroom valve changed at 22:36:37. User heard the valve actuator sound and asked for explanation. Analysis revealed:

1. Pete was the only calling room (1739W)
2. Games was in passive mode with 20% valve open (~500W effective capacity)
3. Load sharing entry condition saw only 1739W, triggered activation
4. Actual system capacity was ~2240W (Pete 1739W + Games ~500W)
5. This was above the 2000W threshold - activation was unnecessary

The `_calculate_total_system_capacity()` function (used during active load sharing) also doesn't count passive rooms, suggesting this is a systemic issue in capacity calculation throughout the load sharing system.

### Resolution (2025-12-03)

**Fix Applied:**
Modified both capacity calculation functions to include passive mode rooms with open valves.

**Changes Made:**

1. **`_evaluate_entry_conditions()`** - Entry condition check now includes passive room capacity:
   ```python
   # Add passive room capacity (valve-adjusted)
   # Passive rooms with open valves contribute to heat dissipation
   passive_capacity = 0.0
   for room_id, state in room_states.items():
       if state.get('operating_mode') == 'passive' and not state.get('calling', False):
           valve_pct = state.get('valve_percent', 0)
           if valve_pct > 0:
               capacity = all_capacities.get(room_id)
               if capacity is not None:
                   effective_capacity = capacity * (valve_pct / 100.0)
                   passive_capacity += effective_capacity
   
   total_capacity += passive_capacity
   ```

2. **`_calculate_total_system_capacity()`** - Active capacity calculation now includes passive rooms:
   - Added same passive room capacity calculation after calling rooms
   - Prevents double-counting by checking `not state.get('calling', False)`

**Why This Works:**
- Passive rooms with open valves ARE contributing to heat dissipation
- Using `valve_pct / 100.0` adjustment matches how load sharing rooms are counted
- Only counts rooms that are actually in passive mode AND have valves open
- Rooms in comfort mode (calling=True) are already counted at full capacity

**Example Fix Effect:**
With the fix, the incident from 2025-11-30 at 22:36:37 would calculate:
- Pete (calling): 1739W
- Games (passive, 20% valve): 2504W * 0.20 = 501W
- **Total: 2240W** (above 2000W threshold)
- Load sharing would NOT activate (correct behavior)

**Files Modified:**
- `managers/load_sharing_manager.py`: Both `_evaluate_entry_conditions()` and `_calculate_total_system_capacity()`

**Testing:**
- No errors in AppDaemon logs after changes
- App reloaded successfully
- Will verify effectiveness during next low-capacity + passive room scenario

---

## BUG #13: Load Sharing Selection Excluded Scheduled Passive Blocks - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-06-02  
**Date Fixed:** 2025-06-02  
**Severity:** Medium - passive rooms with scheduled blocks never selected for load sharing  
**Category:** Load Sharing / Room Selection

### Description

During load sharing room selection, passive mode rooms that had scheduled blocks (defined in `schedules.yaml`) were incorrectly excluded from selection, even during times when those scheduled blocks weren't active.

The tier selection logic checked `operating_mode == 'passive'` to skip rooms, but this incorrectly excluded ALL passive rooms regardless of whether they had current scheduled blocks.

### Root Cause

The selection code for both Tier 1 (Schedule) and Tier 2 (Fallback) skipped all rooms in passive operating mode:

```python
# Old code - skipped ALL passive rooms
if state.get('operating_mode') == 'passive':
    continue  # Skip passive rooms entirely
```

However, passive rooms should still be eligible for load sharing if:
1. They have no current active scheduled block, OR
2. They have a scheduled block but it's not the current time period

### Fix Applied

The room selection logic was updated to check for actual scheduled passive blocks, not just operating mode:

```python
# New code - only skip rooms with ACTIVE scheduled blocks
if schedule_info.get('schedule_block_active', False):
    continue  # Skip only if currently in a scheduled block
```

This allows passive mode rooms to participate in load sharing when they don't have an active schedule block, while still respecting scheduled passive periods.

### Impact

- Passive rooms with schedules can now be selected for load sharing during unscheduled times
- Scheduled passive blocks are still respected (rooms excluded during active schedule periods)
- Load sharing has more rooms available for selection, improving system efficiency

---

## BUG #12: Spurious "Not in persistence data" Warnings on Startup - FIXED

**Status:** FIXED ✅
**Date Discovered:** 2025-11-30
**Date Fixed:** 2025-12-05 (resolved naturally, no code changes)
**Severity:** Low - Cosmetic only, no functional impact
**Category:** Logging / Initialization

### Description

On AppDaemon restart, some rooms (lounge, abby, bathroom) consistently log WARNING messages claiming they're "Not in persistence data, defaulting to not calling" even though they ARE present in the persistence.json file with correct data structure.

### Observed Behavior

```
2025-11-30 16:01:31.352943 DEBUG pyheat: Room pete: Loaded persisted state - calling=False, passive_valve=10%
2025-11-30 16:01:31.353797 DEBUG pyheat: Room games: Loaded persisted state - calling=False, passive_valve=10%
2025-11-30 16:01:31.354632 WARNING pyheat: Room lounge: Not in persistence data, defaulting to not calling
2025-11-30 16:01:31.355539 WARNING pyheat: Room abby: Not in persistence data, defaulting to not calling
2025-11-30 16:01:31.356635 DEBUG pyheat: Room office: Loaded persisted state - calling=False, passive_valve=70%
2025-11-30 16:01:31.357492 WARNING pyheat: Room bathroom: Not in persistence data, defaulting to not calling
```

**Actual persistence.json content:**
```json
{
  "room_state": {
    "games": {"valve_percent": 0, "last_calling": false, "passive_valve": 0},
    "pete": {"valve_percent": 0, "last_calling": false, "passive_valve": 0},
    "lounge": {"valve_percent": 0, "last_calling": false, "passive_valve": 0},
    "abby": {"valve_percent": 0, "last_calling": false, "passive_valve": 0},
    "office": {"valve_percent": 0, "last_calling": false, "passive_valve": 0},
    "bathroom": {"valve_percent": 0, "last_calling": false, "passive_valve": 0}
  }
}
```

All 6 rooms are present in the file with identical structure.

### Additional Mystery

Some logs show rooms loading non-zero passive_valve values (10%, 70%) that don't exist in the actual persistence file on disk. These values don't match the file content at time of startup, suggesting possible:
- Race condition during initialization
- Bytecode caching issue
- Multiple PersistenceManager instances reading at different times
- Temp file from previous atomic write still cached somewhere

### Impact

- ✅ **Functional:** None - rooms default safely and system operates correctly
- ❌ **Cosmetic:** Confusing/misleading logs
- ❌ **Debugging:** Makes it harder to trust persistence-related logs

### Root Cause Hypothesis

Possible causes:
1. **Race condition:** Multiple controllers (RoomController, ValveCoordinator, CyclingProtection) each create PersistenceManager instances and may read file at different moments during initialization
2. **Dictionary iteration order:** The for-loop in `_load_persisted_state()` may have timing-dependent behavior
3. **File I/O timing:** File system cache or delayed writes/flushes creating stale reads
4. **AppDaemon initialization quirk:** Some aspect of how AppDaemon initializes apps

### Investigation Notes

File modification time shows persistence.json hasn't been written since first migration (15:48), yet logs show it being read with different values across restarts. This is unexplained.

**Location:** `controllers/room_controller.py:_load_persisted_state()` lines 80-115

### Workaround

None needed - system functions correctly. Could potentially downgrade WARNING to DEBUG for these cases, but want to keep WARNING for genuinely missing rooms (new additions to config).

### Resolution (2025-12-05)

**Status:** Issue resolved naturally without code changes.

**Verification:**
- Analyzed all AppDaemon logs from December 2025
- Found 13 successful restarts with persistence loading
- All 6 rooms (pete, games, lounge, abby, office, bathroom) successfully loaded on every restart
- Zero "Not in persistence data" warnings found in December logs
- All three previously-problematic rooms (lounge, abby, bathroom) now loading consistently

**Conclusion:**
The issue was likely transient, possibly related to:
- Temporary file system timing issues during the November 30 restart
- Race condition that has since been resolved by other changes
- One-time initialization quirk that cleared itself

No code changes were necessary. The persistence system is now working reliably across all rooms.

---

## BUG #11: Inconsistent Config Initialization Pattern Across Codebase - FIXED

**Status:** FIXED ✅
**Date Discovered:** 2025-11-29
**Date Fixed:** 2025-12-05
**Severity:** Low - Code quality/maintainability issue, no functional impact
**Category:** Code Architecture

### Description

Classes that load configuration in `initialize_from_ha()` use inconsistent patterns for initializing config attributes in `__init__()`. This creates confusion about what values are actually used and which are placeholder defaults vs. real fallbacks.

### Affected Classes

**LoadCalculator** (`managers/load_calculator.py`):
- **Pattern:** Sets misleading default values in `__init__()` that look like placeholders:
  ```python
  self.enabled = False  # Gets overwritten to True by default in initialize_from_ha()
  self.system_delta_t = C.LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT  # 10
  self.global_radiator_exponent = C.LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT  # 1.3
  ```
- **Issue:** The `False` for `enabled` is particularly misleading - it looks like "disabled by default" but actually defaults to `True` if not in config
- **Config loading:** Uses `.get()` with these constants as actual fallback defaults
- **Impact:** Confusing to read - unclear if these are real defaults or just placeholders

**LoadSharingManager** (`managers/load_sharing_manager.py`):
- **Pattern (FIXED 2025-11-29):** ✅ Now initializes all config to `None` with explicit validation
  ```python
  self.min_calling_capacity_w = None  # Loaded from config
  self.target_capacity_w = None
  # ... etc
  ```
- **Previous issue (NOW RESOLVED):** Had hardcoded values like `3500`, `15` that were always overwritten
- **Current state:** Best practice implementation - explicit that values MUST come from config

### Classes Without Issue

The following classes correctly avoid this pattern:
- **BoilerController**: Loads config on-demand in methods, not in `__init__`
- **CyclingProtection**: Only initializes state, no config loading in `initialize_from_ha()`
- **SensorManager**: Only initializes state dictionaries
- **TRVController**: Only initializes state dictionaries
- **RoomController**: Only initializes state dictionaries
- **ValveCoordinator**: Only initializes state dictionaries
- **OverrideManager**: No config attributes, only passes through to config object

### Recommendation

**LoadCalculator should be refactored to match LoadSharingManager's pattern:**
1. Initialize all config attributes to `None` in `__init__()`
2. Load from config in `initialize_from_ha()` with proper fallback defaults
3. Add validation to ensure config is loaded before use
4. Make it explicit when values are required vs. optional with defaults

**Example fix for LoadCalculator:**
```python
# In __init__:
self.enabled = None
self.system_delta_t = None
self.global_radiator_exponent = None

# In initialize_from_ha:
load_config = self.config.boiler_config.get('load_monitoring', {})
self.enabled = load_config.get('enabled', True)  # True is actual default
self.system_delta_t = load_config.get('system_delta_t', C.LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT)
self.global_radiator_exponent = load_config.get('radiator_exponent', C.LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT)

# Validate
if None in [self.enabled, self.system_delta_t, self.global_radiator_exponent]:
    raise ValueError("LoadCalculator: Configuration not properly initialized")
```

### Priority

**Low** - This is a code quality issue, not a functional bug. Current behavior is correct, just confusing to maintain.

### Resolution (2025-12-05)

**Fix Applied:**
Refactored LoadCalculator to match LoadSharingManager's initialization pattern.

**Changes Made:**

1. **`__init__()` method** - Changed config attributes to `None` with explicit comments:
   ```python
   # Configuration (loaded from boiler.yaml in initialize_from_ha)
   self.enabled = None  # Load monitoring enable/disable flag
   self.system_delta_t = None  # Assumed system delta-T for capacity calculations
   self.global_radiator_exponent = None  # Default radiator exponent (EN 442 standard)
   ```

2. **`initialize_from_ha()` method** - Added validation after loading config:
   ```python
   # Validate all required config loaded
   if None in [self.enabled, self.system_delta_t, self.global_radiator_exponent]:
       raise ValueError(
           "LoadCalculator: Configuration not properly initialized. "
           "Ensure initialize_from_ha() is called before use."
       )
   ```

3. **Added explicit comment** on the actual default value:
   ```python
   self.enabled = load_config.get('enabled', True)  # True is the actual default
   ```

**Why This Works:**
- `None` values make it explicit that these are placeholders, not defaults
- Validation catches initialization bugs immediately
- Matches the pattern used by LoadSharingManager (fixed 2025-11-29)
- Improves code maintainability and consistency

**Files Modified:**
- `managers/load_calculator.py`: Updated `__init__()` and `initialize_from_ha()` methods

**Testing:**
- AppDaemon reloaded successfully at 14:41:31
- LoadCalculator initialized without errors
- No errors in AppDaemon logs after changes
- System continues to operate normally

---

## BUG #10: HA Restarts Leave PyHeat with Stale State - OPEN

**Status:** OPEN 🔴
**Date Discovered:** 2025-11-28
**Date Verified:** 2025-12-05
**Severity:** Medium - Can cause incorrect heating decisions after HA restarts
**Category:** State Management

### Description

When Home Assistant restarts but AppDaemon remains running (separate Docker containers), PyHeat continues operating but:
1. Entity states in AppDaemon's cache become stale
2. Service calls to HA fail silently during reconnection
3. State diverges between PyHeat and actual HA/hardware state
4. No automatic re-initialization occurs when HA reconnects

### Investigation

AppDaemon detects HA restarts and provides `plugin_started()` and `plugin_stopped()` lifecycle callbacks. However, these are **NOT** triggered by simple HA restarts.

From testing (2025-11-28 15:35):
- HA restarted at 15:35:11 (container stopped)
- AppDaemon showed "Attempting reconnection" messages 15:35:11-15:35:21
- HA reconnected at 15:35:26 ("Connected to Home Assistant")
- AppDaemon showed "Processing restart for plugin namespace 'default'" at 15:35:52 (26 seconds later!)
- **plugin_started() and plugin_stopped() were NEVER called**

The lifecycle callbacks appear to only fire when the AppDaemon **plugin configuration** changes, not when HA itself restarts.

### Impact

**Medium severity because:**
- Occurs on every HA restart (common during updates, config changes)
- TRV feedback resilience (Bug #7 fix) mitigates some issues with grace period
- State usually resynchronizes within a few minutes through callbacks
- No catastrophic failures, but can cause:
  - Incorrect boiler decisions based on stale valve positions
  - Missed heating opportunities if room temperatures stale
  - Load sharing decisions based on outdated capacity data

**Mitigation currently in place:**
- TRV feedback grace period (120s) tolerates sensor unavailability during reconnection
- Sensor callbacks will update temperatures as they change
- Valve coordinator re-reads pump overrun state on first access

### Attempted Solution (Reverted)

Attempted to use `plugin_started()` and `plugin_stopped()` callbacks to:
- Pause operations during disconnect
- Re-initialize all state from HA on reconnect
- Cancel in-flight TRV commands
- Reset grace periods

**Why it was reverted:**
- Testing showed these callbacks are NOT called during HA restarts
- Callbacks may only trigger when AppDaemon plugin config changes
- Need to research alternative approaches (websocket events, entity monitoring)

**Commits reverted:**
- `18c99dd` - HA connection state management
- `d12061e` - Bug fix for missing newline (introduced during implementation)

### Potential Solutions to Research

1. **Monitor special HA entity**: Listen for state changes on an entity that indicates HA startup (e.g., `sensor.uptime` resetting)
2. **WebSocket events**: Check if AppDaemon exposes websocket connection/disconnection events
3. **Polling approach**: Periodically check if entity state reads return errors/None
4. **Accept current behavior**: Document that AppDaemon restart is needed after HA restart for clean state

### Verification (2025-12-05)

**Status: CONFIRMED - This is a REAL issue**

Verification performed by analyzing logs from HA restart on 2025-12-05 at 08:00 UTC.

**Evidence from logs:**
```
2025-12-05 08:00:14 INFO HASS: Attempting reconnection in 5.0s
2025-12-05 08:00:19 INFO HASS: Attempting reconnection in 5.0s
...
2025-12-05 08:00:54 INFO HASS: Connected to Home Assistant 2025.12.0
2025-12-05 08:01:23 INFO AppDaemon: Processing restart for plugin namespace 'default'
2025-12-05 08:01:59 INFO pyheat: Recompute #4: Heating 1 room(s) - lounge
```

**Key Findings:**
1. HA went down at ~08:00, AppDaemon detected disconnect and attempted reconnection
2. Reconnected at 08:00:54 after ~40 seconds of downtime
3. AppDaemon processed "restart for plugin namespace" at 08:01:23
4. **NO `plugin_started()` or `plugin_stopped()` callbacks were triggered** (confirmed by absence in logs and code)
5. PyHeat continued operating immediately with no re-initialization

**Research Findings:**

According to [AppDaemon official documentation](https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html):
- `plugin_started` and `plugin_stopped` events exist and fire "when a plugin notifies AppDaemon that it has started/stopped"
- These events are namespace-specific (not global) as of AppDaemon 3.0
- However, exact behavior during HA reconnection is not clearly documented
- Community reports confirm these callbacks don't fire reliably during simple HA restarts

**Known AppDaemon Limitations:**

From [GitHub Issue #1256](https://github.com/AppDaemon/appdaemon/issues/1256) and community discussions:
- "AppDaemon sometimes loses Home Assistant services after HA restart"
- State cache can differ from HA frontend/API for 3-4 minutes after reconnection
- Recommended workaround: Restart AppDaemon after HA restarts

**Impact Assessment:**

The bug is real but mitigated:
- State cache becomes stale during disconnection window
- Service calls may fail silently during reconnection
- No automatic re-initialization when HA reconnects
- **However:** TRV feedback grace period (Bug #7 fix) provides tolerance during reconnection
- State usually resyncs within minutes via sensor callbacks
- System continues to function, just potentially operates on stale data briefly

**Current Workaround:**

Manual AppDaemon restart after HA restarts (already being practiced - AppDaemon uptime: 32 minutes vs HA uptime: 8 hours as of verification).

**Recommendation:**

- **Keep bug OPEN** - Issue is real and should be tracked
- **Priority: Medium** - System continues to function, current mitigations are adequate
- **Best near-term solution:** Monitor `sensor.uptime` or similar entity that resets on HA restart
- **Alternative:** Accept limitation and document that AppDaemon restart recommended after HA restart

### Related Issues

- Bug #7: TRV feedback sensors showing "unknown" during HA restarts (FIXED - grace period mitigates this)
- The TRV resilience features help mask this issue but don't solve the root cause

**References:**
- [AppDaemon HASS API Reference](https://appdaemon.readthedocs.io/en/latest/HASS_API_REFERENCE.html)
- [AppDaemon App Guide - Events](https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html)
- [GitHub Issue #1256 - Services lost after HA restart](https://github.com/AppDaemon/appdaemon/issues/1256)
- [Community: Stale states after restart](https://community.home-assistant.io/t/restore-entity-state-after-ha-restart/85981)

---

## BUG #9: Load Sharing Exit Trigger F Was Previously Missing - FIXED

**Status:** FIXED ✅ (as part of 2025-11-28 overshoot prevention work)  
**Date Discovered:** 2025-11-28 (during analysis of load sharing exit conditions)  
**Date Fixed:** 2025-11-28  
**Severity:** Minor - edge case that rarely occurs but should be handled  
**Category:** Load Sharing

### Description

Load sharing did not have an exit condition to handle when a user changes a room's mode from `auto` to `manual` or `off` while the room is being pre-warmed by load sharing. This could cause the valve to remain open at the load sharing percentage even though the user explicitly changed the room mode.

### Root Cause

The `_evaluate_exit_conditions()` method checked several exit triggers:
- Exit Trigger A: Original calling rooms stopped
- Exit Trigger B: Additional rooms started calling
- Exit Trigger C: Load sharing room naturally calling
- Exit Trigger D: Tier 3 timeout

But it never checked if the room's mode changed from `auto` (which is required for load sharing selection).

### Impact

**Minor because:**
- Load sharing only selects rooms in `auto` mode initially
- Users rarely change room modes during active heating
- Even without Exit Trigger F, other exit conditions would eventually remove the room:
  - Exit Trigger A fires when original trigger rooms stop calling
  - Exit Trigger D fires after 15 minutes for Tier 3 rooms

**However, it violates user intent:**
- User switches room to `manual` or `off` → expects immediate valve control change
- Without Exit Trigger F, valve stays at load sharing percentage until other exit condition

### Fix

Added Exit Trigger F as part of 2025-11-28 overshoot prevention work (see changelog):

```python
# Exit Trigger F: Room mode changed from auto (NEW)
mode_changed_rooms = []
for room_id, activation in list(self.context.active_rooms.items()):
    state = room_states.get(room_id, {})
    if state.get('mode') != 'auto':
        self.ad.log(
            f"Load sharing: Room '{room_id}' mode changed from auto - removing",
            level="INFO"
        )
        mode_changed_rooms.append(room_id)

# Remove rooms with mode changes
for room_id in mode_changed_rooms:
    del self.context.active_rooms[room_id]
```

Now properly handles mode changes and immediately removes rooms from load sharing control.

---

## BUG #8: Tier 3 Target Calculation Uses Simple Fixed Margin - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-11-28 (during implementation of overshoot prevention)  
**Date Fixed:** 2025-11-28  
**Severity:** Minor - acceptable for emergency fallback behavior  
**Category:** Load Sharing

### Description

Tier 3 (fallback priority) rooms previously used a simple `current_target + 1°C` calculation for their exit temperature threshold. This fixed margin failed when rooms were "parked" at low temperatures (10-12°C default_target) but actually at ambient temperature (15-17°C), causing immediate exit from load sharing.

### Context

**Why Tier 3 is different:**
- Tier 1/2 rooms are selected based on upcoming schedules → have explicit target temperature
- Tier 3 rooms are selected by priority alone → have NO schedule-based target
- Tier 3 is emergency fallback when schedules don't provide enough capacity

**Current implementation:**
```python
# In _select_tier3_rooms()
current_target = state.get('target')  # e.g., 16°C (default schedule)
if current_target is None:
    current_target = 16.0  # Safe default

tier3_target = current_target + 1.0  # Emergency heating tolerance
```

**Exit condition:** Room exits load sharing when `temp >= tier3_target + off_delta` (e.g., 17.3°C with 0.3°C hysteresis)

### Why This is Acceptable

**Tier 3 rooms are emergency fallback:**
- Only activated when Tier 1/2 don't provide enough capacity
- Indicates schedule gaps that should be addressed
- Accepting 1°C overheat is reasonable trade-off vs short-cycling

**Alternative approaches would be complex:**
1. **Adaptive margin based on room size:** Requires capacity estimates per room
2. **No temperature exit:** Rely only on 15-minute timeout (can overheat more)
3. **Current_target only:** Would exit too early for emergency heating purpose

**Current behavior is reasonable:**
- 1°C above current target is noticeable but not uncomfortable
- Prevents runaway heating (without exit, could reach 20°C+ from 16°C)
- Timeout (15 minutes) provides secondary safety limit
- Logged as WARNING to encourage schedule improvements

### Potential Improvements (Future Work)

If Tier 3 usage becomes frequent:
1. **Dynamic margin based on room capacity:** Larger rooms get larger margin
2. **Historical learning:** Adjust margin based on past overshoot patterns
3. **Boiler modulation feedback:** Exit when modulation drops below threshold

**For now:** Document as known limitation, acceptable for emergency fallback behavior.

### Resolution (2025-11-28)

**Fix Applied:**
Replaced `current_target + 1.0°C` calculation with configurable global comfort target.

**Root Cause:**
Rooms used for Tier 3 load sharing are typically "parked" at low default temperatures (Games: 12°C, Office: 12°C, Bathroom: 10°C, Lounge: 16°C) when not scheduled for heating. However, these rooms often sit at ambient temperature (15-17°C) due to heat transfer from adjacent rooms or external factors. The old logic:
```python
tier3_target = current_target + 1.0  # 11-13°C target when current is 10-12°C
```
Produced targets of 11-17°C. With ambient temps of 15-17°C, rooms with targets below ambient would exit load sharing immediately (already above target), making Tier 3 effectively useless.

**New Implementation:**
```python
# config/boiler.yaml
load_sharing:
  tier3_comfort_target_c: 20.0  # Global comfort target for Tier 3 pre-warming

# managers/load_sharing_manager.py (lines 920-923)
ls_config = self.config.boiler_config.get('load_sharing', {})
tier3_target = ls_config.get('tier3_comfort_target_c', 20.0)
```

**Why This Works:**
- **Parking temps don't matter:** Pre-warming target is always 20°C regardless of room's scheduled default
- **Above ambient:** 20°C comfort target is higher than typical ambient temperatures (15-17°C)
- **Reasonable heating:** Provides genuine pre-warming (e.g., 16°C → 20°C) without overheating
- **Simple and predictable:** No complex calculations, one global configuration value
- **Edge case proof:** Works even if room is already at 18°C (still provides 2°C of pre-warming)

**Configuration:**
- **Parameter:** `tier3_comfort_target_c` under `load_sharing` section in `boiler.yaml`
- **Default:** 20.0°C (comfortable room temperature)
- **Rationale:** High enough to provide genuine pre-warming, low enough to prevent discomfort
- **Customization:** User can adjust based on personal comfort preferences

**Edge Cases Handled:**
1. **Config missing:** Falls back to 20.0°C default
2. **Invalid value:** Non-numeric values caught by YAML parsing
3. **Room already at target:** Exit Trigger E removes room (temp >= 20.0 + off_delta)
4. **Room calling naturally:** Exit Trigger C removes room (normal demand takes priority)
5. **Mode change:** Exit Trigger F removes room (respects user control)

**Impact:**
- ✅ Tier 3 rooms now stay in load sharing long enough to provide capacity
- ✅ Pre-warming actually occurs (16°C → 20°C instead of immediate exit)
- ✅ Works with low parking temperatures (10-12°C scheduled defaults)
- ✅ Simple configuration with sensible default
- ✅ No complex logic or edge cases to maintain

**Files Modified:**
- `config/boiler.yaml`: Added `tier3_comfort_target_c: 20.0` configuration
- `managers/load_sharing_manager.py` (lines 920-923): Replaced 8 lines with 3 lines of simple config lookup

**Testing:**
- Syntax validated: No Python errors
- AppDaemon logs: No errors after implementation
- Next Tier 3 activation will verify effectiveness in production

---

## BUG #7: Cycling Protection Triggers on Intentional Boiler Shutdown - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-11-27  
**Date Fixed:** 2025-11-28  
**Severity:** Medium - causes unnecessary cooldown cycles and setpoint drops  

### Observed Behavior

When pyheat intentionally turns the boiler off because no rooms are calling for heat, the cycling protection system incorrectly evaluates this as a potential overheat situation and triggers a cooldown if the return temperature is above the threshold.

**Specific incident on 2025-11-27 at 22:12:50:**
- Pete's room reached target temperature (19.13°C) and stopped calling for heat
- No rooms calling for heat (num_rooms_calling = 0)
- Boiler state machine correctly transitioned: `ON` → `PENDING_OFF` (22:12:05)
- After 30-second off-delay, boiler commanded off at 22:12:50
- Flame turned OFF as expected (intentional shutdown)
- **2 seconds later (22:12:52):** Cycling protection evaluated the flame-off event
- Return temp: 61°C, Threshold: 60°C (setpoint 70°C - 10°C delta)
- **Cooldown incorrectly triggered** even though this was an intentional shutdown, not a boiler overheat

### Root Cause Analysis

The cycling protection system monitors the `binary_sensor.opentherm_flame` entity and evaluates every flame-off event to detect boiler overheating (when the boiler automatically shuts itself down due to high return temperature).

**Current checks in `on_flame_off()` and `_evaluate_cooldown_need()`:**
1. ✅ **Already in cooldown?** → Skip (prevents double-triggering)
2. ✅ **DHW (hot water) active?** → Skip (4 separate checks for DHW detection)
3. ✅ **Return temp high?** → Trigger cooldown if `return_temp >= (setpoint - 10°C)`

**Missing check:**
❌ **Was this an intentional shutdown by pyheat?** → Not checked at all

The cycling protection cannot distinguish between:
- **Automatic boiler shutdown**: Boiler safety system turned off the flame due to overheat (cooldown needed)
- **Intentional pyheat shutdown**: State machine commanded boiler off because no rooms calling (cooldown NOT needed)

**Why this matters:**

The cooldown system is designed to detect short-cycling caused by insufficient radiator capacity - when the boiler keeps automatically shutting itself off due to high return temperature because there's not enough heat dissipation. This is a genuine problem that needs intervention (dropping setpoint to 30°C to allow system to cool).

However, when pyheat intentionally turns the boiler off because heating is no longer needed, the return temperature being "high" is **expected and normal** - the system was just actively heating! This does not indicate a cycling problem.

### Evidence

**From heating_logs/2025-11-27.csv:**
```csv
time,trigger,num_rooms_calling,boiler_state,ot_flame,cycling_state,ot_setpoint_temp,ot_heating_temp,ot_return_temp
22:12:04,opentherm_modulation,1,on,on,NORMAL,70.0,70,63
22:12:05,opentherm_heating_return_temp,0,on,on,NORMAL,70.0,70,63
22:12:27,sensor_office_changed,0,pending_off,on,NORMAL,70.0,69,62
22:12:50,opentherm_heating_temp,0,pending_off,off,NORMAL,70.0,65,61
22:12:53,opentherm_heating_temp,0,pending_off,off,COOLDOWN,70.0,64,61
22:13:01,opentherm_heating_setpoint_temp,0,pump_overrun,off,COOLDOWN,30.0,62,60
```

**Timeline:**
1. 22:12:05 - Pete's room stopped calling (temp reached 19.13°C vs target 19.0°C)
2. 22:12:05-22:12:27 - Boiler remained ON (30-second off-delay timer)
3. 22:12:27 - Boiler entered `pending_off` state (off-delay expired)
4. 22:12:50 - Flame turned OFF (intentional - commanded by pyheat)
5. 22:12:52 (approx) - Cycling protection evaluated: return_temp=61°C >= threshold=60°C
6. 22:12:53 - **COOLDOWN triggered** (setpoint will drop to 30°C)

### Related Code Locations

**controllers/cycling_protection.py:**
- Line 115-156: `on_flame_off()` - captures DHW state, schedules evaluation after 2s delay
- Line 127-134: Guard for already-in-cooldown (prevents double-trigger)
- Line 290-377: `_evaluate_cooldown_need()` - evaluates if cooldown needed
- Line 321-336: DHW checks (4 separate checks to filter out hot water events)
- Line 354-365: Return temperature check and cooldown trigger decision

**What's missing:** No check of boiler state machine state to determine if shutdown was intentional

### Fix Strategy

**Option 1: Check boiler state machine state (Recommended)**

Pass boiler controller reference to CyclingProtection and check state before evaluation:

```python
# In cycling_protection.py __init__:
def __init__(self, ad, config, alert_manager=None, boiler_controller=None):
    self.boiler_controller = boiler_controller

# In on_flame_off():
if new == 'off' and old == 'on':
    # Check if this is an intentional shutdown
    if self.boiler_controller:
        boiler_state = self.boiler_controller.boiler_state
        if boiler_state in [C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN]:
            self.ad.log(
                f"Flame OFF: Intentional shutdown by state machine "
                f"(state={boiler_state}) - skipping cooldown evaluation",
                level="DEBUG"
            )
            return
    
    # Continue with existing DHW and return temp checks...
```

**Option 2: Track last commanded state**

Add tracking of when pyheat last commanded the boiler off, compare flame-off timing:
- If flame turns off within ~5 seconds of pyheat commanding off → intentional
- If flame turns off with no recent command → automatic overheat

**Option 3: Use climate entity state transitions**

Monitor `climate.opentherm_thermostat` state changes instead of flame sensor:
- Only evaluate cooldown when state changes `heat→off` without pyheat commanding it
- Requires climate entity state to accurately reflect boiler state

**Recommendation:** Option 1 - direct state machine check is simplest and most reliable

### Resolution (2025-11-28)

**Fix Applied:**
Modified cycling protection to check boiler state machine state before evaluating cooldown need.

**Implementation:**
1. Added `boiler_controller` parameter to `CyclingProtection.__init__()` (cycling_protection.py line 34)
2. Updated `app.py` line 73 to pass `self.boiler` reference when initializing CyclingProtection
3. Added state check in `on_flame_off()` before scheduling cooldown evaluation (cycling_protection.py line 141-151)

**New Logic:**
```python
# GUARD: Skip evaluation if this is an intentional shutdown by state machine
if self.boiler_controller:
    boiler_state = self.boiler_controller.boiler_state
    if boiler_state in [C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN]:
        self.ad.log(
            f"Flame OFF: Intentional shutdown by state machine "
            f"(state={boiler_state}) - skipping cooldown evaluation",
            level="DEBUG"
        )
        return
```

**Why This Works:**
- During `PENDING_OFF` and `PUMP_OVERRUN`, the flame turning off is expected (pyheat commanded it)
- High return temp after active heating is normal and doesn't indicate cycling problems
- Cooldown only triggers for unexpected shutdowns (flame off while state is `ON`)
- Preserves original functionality for detecting genuine overheat scenarios

**Files Modified:**
- `controllers/cycling_protection.py` (lines 34, 141-151): Added boiler state check
- `app.py` (line 73): Pass boiler controller reference to CyclingProtection

**Testing:**
- No errors in AppDaemon logs after changes
- System continues to operate normally
- Next occurrence of intentional shutdown will verify fix effectiveness

### Impact Assessment

**Severity:** Medium
- Causes unnecessary cooldown cycles during normal operation
- Drops setpoint to 30°C when not needed
- System recovers automatically (cooldown exits when return temp falls below threshold)
- Does not prevent heating or cause safety issues
- Wastes energy by extending time until next heating cycle

**Frequency:** Occurs whenever:
- Boiler is heating with return temp within 10°C of setpoint (common)
- Last room stops calling for heat (normal end of heating cycle)
- System intentionally shuts down

**Typical scenario:**
- Single room calling with high target → high setpoint (e.g., 70°C)
- Room reaches target and stops calling
- Return temp still high (e.g., 61°C) from active heating
- Intentional shutdown incorrectly triggers cooldown

### Testing Notes

To verify the fix:
1. Start heating with one room calling
2. Wait for room to reach target temperature and stop calling
3. Observe boiler shutdown sequence: `ON → PENDING_OFF → (flame off) → PUMP_OVERRUN`
4. Check AppDaemon logs for cycling protection evaluation
5. Verify cooldown does NOT trigger if return temp is high but shutdown was intentional
6. Verify cooldown DOES trigger if flame turns off while boiler state is still `ON` (actual overheat)

To simulate actual overheat scenario for testing:
- Would require boiler to shut itself off due to high return temp while state machine thinks it's still ON
- Difficult to reproduce in testing without risking equipment
- May need to rely on log analysis if this rare event occurs in production

### Context

Discovered during analysis of heating logs from 2025-11-27 evening. Initially appeared that cooldown was correctly protecting against high return temp, but further investigation revealed:
- Shutdown was intentional (no rooms calling for heat)
- Return temp being high is normal after active heating
- Cycling protection is designed for automatic boiler shutdowns, not intentional ones

The cycling protection system was implemented to detect and prevent short-cycling caused by insufficient radiator capacity. It was not designed to handle the case where pyheat intentionally turns the boiler off with a naturally-high return temperature after normal heating operation.

---

## BUG #6: Load Sharing Valves Persist After Deactivation - FIXED

**Status:** FIXED ✅  
**Date Discovered:** 2025-11-27  
**Date Fixed:** 2025-11-27  
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

### Fix Applied

**Implementation Date:** 2025-11-27  
**Approach:** Explicit valve closure on load sharing deactivation (Fix Option 1)

**Changes Made:**

1. **LoadSharingManager (`managers/load_sharing_manager.py`)**
   - Added `last_deactivated_rooms` instance variable to track rooms opened by load sharing
   - Modified `_deactivate()` to populate this list before clearing context
   - Rooms are tracked for explicit closure by app.py

2. **App.py (`app.py`)**
   - Added explicit closure logic when load sharing returns empty commands
   - Checks `last_deactivated_rooms` and closes valves for non-calling rooms
   - Updates `current_commands` immediately to prevent pump overrun capture
   - Logs closure actions for visibility

**How It Works:**
```
1. Load sharing deactivates → tracks which rooms it had opened
2. App.py receives empty load_sharing_commands (deactivated)
3. For each deactivated room:
   - Check if room is naturally calling for heat
   - If NOT calling: force valve=0% immediately
   - If calling: leave valve open (preserve natural demand)
4. Update current_commands to 0 (prevents stale snapshot)
5. Clear load sharing overrides
```

**Benefits:**
- ✅ Valves close immediately on deactivation (no delay)
- ✅ Prevents pump overrun from capturing stale positions
- ✅ Preserves natural demand (rooms that start calling remain open)
- ✅ Simple, direct solution with minimal code changes
- ✅ No cross-component dependencies

**Testing:**
- Comprehensive simulation testing: 24/24 tests passed
- Edge case analysis: All scenarios handled correctly
- No syntax errors or import issues
- Ready for live testing

### Testing Notes

To reproduce:
1. Have low system capacity (e.g., one room calling with large radiator)
2. Wait for boiler to run long enough to establish low delta_t (return temp close to setpoint)
3. Load sharing will activate and open additional valves
4. Stop all heating demand (room reaches target)
5. Observe that load-sharing valves remain open even though boiler is OFF

---

## BUG #3: Override Functionality Broken - Valve Commands Not Sent - INVESTIGATING

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


## BUG #2: Safety Valve False Positive During PENDING_OFF Transition - FIXED

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


## BUG #1: Override Targets Not Being Applied (CRITICAL) - FIXED

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
## BUG #N: Title - [FIXED | OPEN | INVESTIGATING]

**Status:** [OPEN 🔴 | INVESTIGATING ⚠️ | FIXED ✅]
**Date Discovered:** YYYY-MM-DD
**Date Fixed:** YYYY-MM-DD (if applicable)
**Date Verified:** YYYY-MM-DD (if applicable)
**Severity:** [Critical | High | Medium | Low]
**Category:** [State Management | Load Sharing | TRV Control | etc.]
**Branch:** branch-name (if on feature branch)

### Description
Brief description of what the bug is

### Observed Behavior
What happens (with examples/commands, actual vs expected)

### Root Cause Analysis
What's actually wrong and why

### Evidence
Logs, error messages, debug output, CSV data, screenshots

### Related Code Locations
Files and line numbers involved

### Fix Strategy
How to fix it (with code snippets if helpful)
- Option 1: Description
- Option 2: Description
- Recommendation: Which option and why

### Resolution (if fixed)
**Date Fixed:** YYYY-MM-DD

**Changes Made:**
- File 1: What changed
- File 2: What changed

**Why This Works:**
Explanation of why the fix addresses the root cause

**Files Modified:**
- `path/to/file.py`

**Testing:**
How the fix was verified

### Impact Assessment
**Severity:** Explanation
- Bullet points on impact
- Frequency of occurrence
- Workarounds available

### Verification (if needed)
**Status:** CONFIRMED | UNABLE TO REPRODUCE | etc.

Evidence from testing or production logs

### Related Issues
Links to related bugs or issues

### Testing Notes
How to reproduce and verify the fix

### Lessons Learned (optional)
What to do differently next time

### References (optional)
External documentation, GitHub issues, etc.
```

**Template Notes:**
- Add status indicator to title: `- FIXED`, `- OPEN`, or `- INVESTIGATING`
- Include both emoji and text status in Status field for visibility
- Date fields: Add as relevant (Discovered is required, Fixed/Verified optional)
- Category helps group related bugs
- Resolution section only needed for fixed bugs
- Verification section useful for confirming reported issues are real
- References section for external links (GitHub, docs, community posts)
