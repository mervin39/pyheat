# Proposal: Passive Mode with Minimum Temperature (Uses Global Frost Protection)

**Status:** UPDATED - 2025-12-01  
**Date:** Originally 2025-11-30, Updated 2025-12-01  
**Author:** System proposal based on user requirements

**IMPORTANT UPDATE (2025-12-01):** Global frost protection has been implemented system-wide (applies to all modes except "off"). This proposal now focuses on allowing passive mode to optionally use a HIGHER minimum temperature than the global frost protection threshold for comfort purposes, while the global frost protection (8°C default) provides the safety floor.

---

## Problem Statement

**RESOLVED:** System-wide frost protection now prevents all rooms (except "off" mode) from dropping below 8°C (configurable).

**NEW FOCUS:** Users want passive rooms to maintain a higher comfort floor (e.g., 12-15°C) without calling for heat under normal circumstances, but with automatic active heating if temperature drops below this comfort threshold.

**Use case examples:**
1. Games room in passive mode: wants to stay above 12°C for comfort, but doesn't need active scheduled heating
2. Office in passive mode: wants to stay above 15°C when working from home occasionally
3. Bedroom in passive mode: wants to stay above 10°C for sleeping comfort

**How this differs from global frost protection:**
- Global frost protection (8°C): Safety floor for ALL rooms - prevents pipe freezing
- Passive minimum temperature (10-15°C): Comfort floor for PASSIVE rooms - prevents excessive cold

---

## Proposed Solution

Add an **optional comfort floor** to passive mode that triggers active heating when crossed. This is SEPARATE from and HIGHER than the global frost protection threshold.

### Key Concept

**Three temperature zones for passive rooms:**

1. **Normal passive mode (temp ≥ min_temp, e.g., ≥ 12°C):**
   - Room does NOT call for heat
   - Valve opens to user-configured percentage when temp < max_temp
   - Standard opportunistic heating (unchanged from current behavior)

2. **Comfort mode (frost_temp < temp < min_temp, e.g., 8-12°C):**
   - Room calls for heat (acts like active mode)
   - Valve forced to 100% for rapid recovery
   - Heating until temp recovers above min_temp threshold
   - Returns to normal passive behavior once recovered

3. **Frost protection mode (temp < frost_temp, e.g., < 8°C):**
   - **Handled by global frost protection system** (already implemented)
   - Room calls for heat with 100% valve (emergency heating)
   - Safety floor applies to ALL modes (except "off")
   - Should rarely reach this zone if comfort mode is configured

**Priority:** Frost protection > Comfort mode > Normal passive mode

---

## Configuration Architecture

### Global Frost Protection (ALREADY IMPLEMENTED ✅)

System-wide frost protection is configured in `config/boiler.yaml`:

```yaml
system:
  frost_protection_temp_c: 8.0  # Global safety floor for all rooms (default 8°C)
```

**Status:** ✅ Implemented 2025-12-01
**Applies to:** All modes except "off"
**Behavior:** Emergency heating at 100% valve when temp < (frost_temp - on_delta)
**This is the SAFETY FLOOR** - prevents frozen pipes and property damage

### Per-Room Comfort Floor (Manual Passive) - NEW

Add optional per-room entities (6 total - one per room):

```yaml
# config/ha_yaml/pyheat_package.yaml
input_number:
  pyheat_games_passive_min_temp:
    name: "Dining Room Passive Min Temp"
    min: 8     # Must be >= global frost_protection_temp_c (8°C)
    max: 20
    step: 0.5
    unit_of_measurement: "°C"
    mode: box
    icon: mdi:thermometer-low
    # NO initial value - preserves user settings across HA restarts
```

**Usage:**
- Set to 12.0°C to add comfort floor (triggers active heating below 12°C)
- Set to 15.0°C for higher comfort floor
- Leave unset or set equal to frost_protection_temp_c to disable (rely on global frost protection only)

**Validation:** Must be >= global frost_protection_temp_c (8°C by default)
**Purpose:** Comfort floor, NOT safety floor (safety handled by global frost protection)

### Per-Schedule Comfort Floor (Scheduled Passive) - NEW

Add optional `min_target` field to passive schedule blocks:

```yaml
# config/schedules.yaml
rooms:
  - id: games
    week:
      mon:
        - start: "00:00"
          end: "23:59"
          mode: passive
          target: 18.0        # Upper bound (existing)
          min_target: 12.0    # Lower bound (NEW, optional, must be >= 8°C)
          valve_percent: 50   # Passive valve opening (existing)
```

**Usage:**
- Omit `min_target` to rely on global frost protection only (8°C)
- Specify `min_target: 12.0` to add comfort floor for this schedule period
- Different schedule blocks can have different min_target values

**Validation:** Must be >= global frost_protection_temp_c (8°C by default)

### Precedence Order

When determining the minimum temperature threshold for passive mode comfort floor:

1. **Scheduled min_target** (if specified and > frost_protection_temp_c) - highest priority
2. **Manual passive_min_temp entity** (if set and > frost_protection_temp_c)
3. **Global frost_protection_temp_c** - ALWAYS ACTIVE as safety floor (8°C default)

**Note:** Global frost protection ALWAYS provides the safety floor. The passive min_temp settings only ADD a higher comfort floor on top of this.

---

## Behavioral Specification

### Temperature Zones

For a room in passive mode with:
- `max_temp=18°C` (upper bound)
- `min_temp=12°C` (comfort floor, optional)
- `frost_protection_temp_c=8°C` (global safety floor, already implemented)
- `passive_valve=10%`

**Zone 1: Normal Passive (temp ≥ 12°C)**
- Room does NOT call for heat
- Valve opens to 10% when temp < 18°C (with hysteresis)
- Valve closes when temp ≥ 18°C
- Opportunistic heating only

**Zone 2: Comfort Mode (8°C < temp < 12°C)** ← NEW
- Room calls for heat (active heating)
- Valve forced to 100% (ignores passive_valve setting)
- Aggressive heating for rapid recovery
- Continues until temp > min_temp + off_delta (e.g., 12.1°C)
- Returns to Zone 1 behavior once recovered

**Zone 3: Frost Protection Mode (temp < 8°C)** ← ALREADY IMPLEMENTED ✅
- **Handled by global frost protection system**
- Room calls for heat with 100% valve (emergency heating)
- Uses operating_mode='frost_protection'
- Should rarely reach this zone if Zone 2 is configured

### Hysteresis

Uses existing per-room `on_delta_c` and `off_delta_c` values:

**For comfort mode (min_temp threshold):**
- **Enter comfort mode:** temp < (min_temp - on_delta)
  - Example: < 11.7°C with min_temp=12°C, on_delta=0.3°C
- **Exit comfort mode:** temp > (min_temp + off_delta)
  - Example: > 12.1°C with min_temp=12°C, off_delta=0.1°C
- **Dead band:** 11.7°C to 12.1°C maintains current state

**For frost protection mode (frost_temp threshold):**
- **Already implemented** ✅ - uses same hysteresis logic
- Enter at frost_temp - on_delta (7.7°C with default 8°C)
- Exit at frost_temp + off_delta (8.1°C with default 8°C)

**Prevents oscillation** at both threshold boundaries.

### Valve Control in Comfort Mode

**Decision: Force 100% valve (not passive_valve)**

**Rationale:**
1. Comfort threshold crossed - user wants room warmer
2. Passive valve might be very low (10-20%) - too slow for recovery
3. Faster heating prevents prolonged discomfort
4. Intentional overshoot provides thermal buffer
5. Clear behavioral distinction: normal passive vs. comfort mode vs. frost protection

**Comparison:**
- **Normal passive** (temp ≥ min_temp): Uses configured passive_valve percent (10-50%)
- **Comfort mode** (frost_temp < temp < min_temp): Forces 100% valve
- **Frost protection** (temp < frost_temp): Forces 100% valve (already implemented ✅)

**Alternative considered:** Use PID control with active mode bands
- Rejected: Too slow, defeats the purpose of comfort floor
- Room might stay uncomfortably cold for extended period

---

## Example Scenario

**Room:** Games (passive mode)
**Configuration:**
- `max_temp = 18°C` (upper bound)
- `min_temp = 12°C` (comfort floor - using per-room override)
- `frost_protection_temp_c = 8°C` (global safety floor - system-wide)
- `passive_valve = 10%`
- `on_delta = 0.3°C`, `off_delta = 0.1°C`

**Temperature Journey:**

| Temp | State | Calling | Valve | Operating Mode | Behavior |
|------|-------|---------|-------|----------------|----------|
| 15°C | Normal passive | FALSE | 10% | passive | Opportunistic heating |
| 14°C | Normal passive | FALSE | 10% | passive | Opportunistic heating |
| 13°C | Normal passive | FALSE | 10% | passive | Opportunistic heating |
| 12°C | Normal passive | FALSE | 10% | passive | At comfort threshold |
| 11.7°C | **Comfort mode** | **TRUE** | **100%** | active | Below min_temp - on_delta |
| 11.8°C | Comfort mode | TRUE | 100% | active | Heating rapidly |
| 12.0°C | Comfort mode | TRUE | 100% | active | Within hysteresis |
| 12.2°C | **Returns to passive** | **FALSE** | 10% | passive | Above min_temp + off_delta |
| 13°C | Normal passive | FALSE | 10% or 0% | passive | Likely overshot (desirable) |
| 14°C | Normal passive | FALSE | 10% or 0% | passive | Returns to normal behavior |

**Note:** If temperature somehow continued dropping below 8°C (unlikely with comfort mode active):
- Frost protection would activate (operating_mode='frost_protection')
- Already implemented system-wide feature would take over
- Emergency heating with 100% valve until temp > 8.1°C

**Overshoot to 13-14°C is intentional and beneficial** - provides thermal buffer and reduces risk of re-triggering.

---

## Integration with Existing Systems

### Global Frost Protection (ALREADY IMPLEMENTED ✅)

**Status:** Implemented 2025-12-01

The system-wide frost protection feature already:
- Monitors ALL rooms (except "off" mode)
- Activates emergency heating when temp < (frost_temp - on_delta)
- Forces 100% valve opening
- Uses operating_mode='frost_protection'
- Logs WARNING on activation
- Sends HA persistent notification alerts
- Auto-clears when temp > (frost_temp + off_delta)

**Integration with passive comfort mode:**
- Frost protection is checked FIRST (highest priority)
- If frost protection activates, comfort mode is bypassed
- In practice, comfort mode should prevent reaching frost protection threshold
- If both are configured (comfort at 12°C, frost at 8°C), comfort mode acts as first line of defense

### Load Sharing

**No changes needed.**

Current code already excludes passive rooms from load sharing selection:
- When in normal passive mode: excluded (user controls valve)
- When in comfort mode (calling for heat): excluded (legitimately calling)
- When in frost protection mode: excluded (emergency heating)

Load sharing sees the room as either passive or calling - both are excluded from selection.

### Boiler Interlock

**No changes needed.**

Valve coordinator already counts all open valves toward interlock, regardless of whether room is calling for heat. Safety mode valve (100%) will be counted correctly.

### Status Publishing

**Minor enhancement needed.**

Status entities should display:
- Current min_target value (for user visibility)
- Indication when in comfort mode vs. normal passive vs. frost protection
- Example status texts:
  - `"Passive (opportunistic)"` - normal passive (temp ≥ min_temp)
  - `"Comfort heating (below 12.0°C)"` - comfort mode active
  - `"FROST PROTECTION: 7.5C -> 8.0C (emergency heating)"` - frost protection ✅ already implemented

### Heating Logger

**Minor enhancement needed.**

CSV logs should include min_target column for analysis:
- Helps identify when/why comfort mode triggered
- Allows post-analysis of passive comfort floor effectiveness
- Complements existing `frost_protection` column (already implemented ✅)

---

## Implementation Scope

### Configuration Files

1. ✅ `config/boiler.yaml` - global `frost_protection_temp_c` setting **ALREADY IMPLEMENTED**
2. `config/ha_yaml/pyheat_package.yaml` - add 6 `passive_min_temp` entities (NEW)
3. `config/schedules.yaml` - support optional `min_target` in passive blocks (NEW)

### Code Changes

1. `core/constants.py` - add passive min temp helper template (if needed)
2. `core/scheduler.py` - return min_target with passive mode dict
3. `controllers/room_controller.py` - implement comfort mode logic for passive rooms
4. `services/status_publisher.py` - display comfort mode status and min_target in attributes
5. `services/heating_logger.py` - log `passive_min_temp` column
6. `app.py` - add state listeners for min_temp entity changes
7. `docs/` - document new behavior (README, ARCHITECTURE, changelog)

**Estimated:** ~6-7 files modified, ~150 lines of new code (smaller scope - frost protection already exists)

### Testing Requirements

1. Normal passive operation (temp between min and max) - valve at configured %
2. Comfort mode trigger (temp drops below min_temp - on_delta)
3. Comfort mode exit (temp recovers above min_temp + off_delta)
4. Hysteresis at boundaries (no oscillation)
5. Scheduled min_target override (schedule value takes precedence)
6. Manual min_temp entity override
7. ✅ Global frost protection fallback - **ALREADY TESTED** (implemented 2025-12-01)
8. Status display correctness (shows "Comfort heating" when active)
9. CSV logging correctness (`passive_min_temp` column)
10. Load sharing exclusion (verify comfort mode doesn't break existing logic)

---

## Benefits

### User Benefits

1. **Comfort floor for passive rooms** - prevents rooms getting uncomfortably cold (12-15°C)
2. **Layered protection** - comfort floor (12°C) + safety floor (8°C already implemented ✅)
3. **Prevents frozen pipes** - global frost protection already handles this ✅
4. **Flexible configuration** - global safety floor with optional per-room comfort overrides
5. **Set and forget** - automatic comfort heating without scheduled active mode

### System Benefits

1. **Simple configuration** - optional feature, not required
2. **Low entity overhead** - only 6 new entities (one per room, optional)
3. **Backward compatible** - existing passive schedules continue working with just frost protection
4. **Intuitive behavior** - clear separation between passive, comfort, and frost protection modes
5. **Rare activation** - minimal impact on normal operation (only when temp drops below comfort floor)

### Energy Benefits

1. **Prevents excessive heating** - comfort floor lower than normal targets
2. **Intentional overshoot** - reduces re-triggering frequency
3. **Only activates when needed** - not running continuously
4. **More efficient than scheduled active mode** - heats only when necessary

---

## Open Questions

### 1. Default Comfort Floor Temperature

**Answer:** ✅ **Equal to frost_protection_temp_c (8°C)** - no separate comfort floor by default

**Rationale:**
- Global frost protection (8°C) already provides safety
- Users who want comfort floor can configure per-room or per-schedule
- Simpler default behavior (passive mode stays truly passive)
- More explicit configuration (users know what they've set)
- When min_temp equals frost_protection_temp_c, comfort mode effectively disabled (frost protection handles everything)

---

### 2. Should Comfort Mode Use Different Hysteresis?

**Answer:** ✅ **Use same on_delta/off_delta as normal passive and frost protection**

**Rationale:**
- Consistency across all heating modes (active, passive, frost protection, comfort)
- Existing values are well-tuned (on_delta=0.3°C, off_delta=0.1°C)
- Simpler implementation - no special cases
- Overshoot is still desirable for thermal buffer
- If needed, users can adjust per-room hysteresis values (applies to all modes for that room)

---

### 3. Validation: Min Temp Must Be >= Frost Protection Temp

**Answer:** ✅ **Strict validation** - reject config if min_temp < frost_protection_temp_c

**Rationale:**
- Prevents user confusion about which threshold takes precedence
- Makes system behavior explicit and predictable
- Forces users to understand the two-tier protection model (safety floor + optional comfort floor)
- Clear error messages help users learn the system

**Implementation:**
- Validate `input_number.min` value >= frost_protection_temp_c in HA entity config
- Log ERROR and skip room control if scheduled `min_target` < frost_protection_temp_c
- Error message: "Room {room}: min_target ({value}°C) must be >= frost_protection_temp_c ({frost_temp}°C)"

---

### 4. Entity Initial Value Strategy

**Answer:** ✅ **Option A: Omit initial value** - preserves user settings across HA restarts

**Implementation:**
```yaml
input_number:
  pyheat_games_passive_min_temp:
    name: "Dining Room Passive Min Temp"
    min: 8     # Must match frost_protection_temp_c
    max: 20
    step: 0.5
    unit_of_measurement: "°C"
    mode: box
    icon: mdi:thermometer-low
    # NO initial value - preserves user settings across HA restarts
```

**Behavior:**
- First creation: Entity starts at 8°C (min value = frost_protection_temp_c)
- User sets to desired value (e.g., 12°C for comfort floor)
- Future restarts: Preserves user value (HA stores state)
- If user wants no comfort floor: leave at 8°C (equivalent to frost protection only)

**Rationale:**
- Simple implementation - no automations needed
- Preserves user intent across restarts
- Clear default (min value = frost protection temperature)
- Self-documenting: min value enforces >= frost_protection_temp_c constraint

---

### 5. Alert/Notification When Comfort Mode Activates?

**Answer:** ✅ **No alerts** - frost protection already handles critical alerts, not needed for comfort mode

**Rationale:**
- Comfort mode is less urgent than frost protection (comfort vs. safety)
- User explicitly set the comfort floor - activation is expected behavior
- Frost protection alerts already cover safety-critical events
- Prevents alert fatigue from non-critical events
- User already has visibility via:
  - Status entities showing "Comfort heating (below 12.0°C)"
  - CSV logs with `passive_min_temp` column
  - INFO level log messages

**Implementation:**
- Log INFO message on comfort mode activation (not WARNING)
- Log INFO message on comfort mode deactivation
- No AlertManager calls for comfort mode
- If frost protection activates (below 8°C), that already generates alerts

---

### 6. CSV Logging Column Name

**Answer:** ✅ **`passive_min_temp`** - explicit and technical

**Rationale:**
- Explicit: Clearly indicates this is the passive mode minimum temperature
- Technical: Matches entity naming convention (`pyheat_{room}_passive_min_temp`)
- Distinguishes from `frost_protection` column (already exists)
- Avoids confusion with `target` column (which is the upper bound for passive mode)
- Self-documenting in CSV analysis

**CSV columns after implementation:**
- `target` - upper bound (max_temp) for passive/active modes
- `passive_min_temp` - lower comfort bound (NEW)
- `{room}_frost_protection` - emergency safety heating (already exists)
- `{room}_operating_mode` - passive/active/frost_protection (already exists)

---

### 7. Load Sharing During Comfort Mode

**Answer:** ✅ **No special handling needed** - existing logic already correct

**Decision:** Keep `operating_mode='passive'` even when in comfort mode

**Rationale:**
- Load sharing already excludes all passive rooms (checks `operating_mode='passive'`)
- Room in comfort mode: `calling=TRUE`, `valve=100%`, `operating_mode='passive'`
- Load sharing will not select this room for de-prioritization
- Behavior is correct: room legitimately needs heat (below comfort floor)
- Consistent with frost protection (which also keeps original operating_mode)

**Implementation:**
- No changes to load sharing logic required
- Comfort mode returns `operating_mode='passive'` (not 'active')
- Status display shows "Comfort heating" to distinguish from normal passive behavior

---

### 8. Naming/Terminology

**Answer:** ✅ **"Passive mode"** - no special terminology needed

**Rationale:**
- This is just an enhancement to existing passive mode
- Adding optional minimum temperature doesn't change the core concept
- Users already understand "passive mode"
- Avoid creating new terms that might confuse users

**Usage in documentation/UI:**
- Feature name: "Passive mode" (with optional minimum temperature)
- Entity names: `pyheat_{room}_passive_min_temp` (technical, explicit)
- Status text when active: `"Comfort heating (below 12.0°C)"` (descriptive)
- Status text when passive: `"Passive (opportunistic)"` (existing)
- CSV column: `passive_min_temp` (technical)
- YAML field: `min_target` (matches existing `target` field)
- Log messages: "Room {room} comfort heating activated (temp below min_temp)"

**Distinction preserved:**
- **Frost protection** = system-wide safety floor (8°C, emergency heating)
- **Passive min temp** = optional per-room comfort floor (12-15°C, normal heating)

---

## Next Steps

1. ✅ **Review updated proposal** - confirmed approach with global frost protection as foundation
2. ✅ **Answer open questions** - all decisions made (see sections 1-8 above)
3. **Ready for implementation** - all design decisions finalized:
   - Default: min_temp = frost_protection_temp_c (8°C) - no separate comfort floor unless configured
   - Hysteresis: use existing on_delta/off_delta values
   - Validation: strict (reject if min_temp < frost_protection_temp_c)
   - Entity strategy: omit initial value (preserves user settings)
   - Alerts: no alerts for comfort mode (INFO logs only)
   - CSV column: `passive_min_temp`
   - Load sharing: no changes needed
   - Terminology: "passive mode" (no special name)
4. **Create implementation plan** - break into phases/commits
5. **Implement** - write code with tests
6. **Document** - update README, ARCHITECTURE, changelog
7. **Test in production** - validate comfort floor behavior
8. **Iterate** - adjust based on real-world usage

---

## Related Documents

- Global frost protection: `docs/changelog.md` (2025-12-01 entry) ✅ IMPLEMENTED
- Global frost protection proposal: `docs/frost_protection_proposal.md`
- Current passive mode implementation: `docs/changelog.md` (2025-11-30 entries)
- Passive mode architecture: `docs/ARCHITECTURE.md` (Room Control Logic section)
- Configuration examples: `README.md` (Passive Mode section)
