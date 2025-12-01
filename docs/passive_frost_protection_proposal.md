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

1. `config/boiler.yaml` - add global `frost_protection_temp_c` setting
2. `config/ha_yaml/pyheat_package.yaml` - add 6 `passive_min_temp` entities
3. `config/schedules.yaml` - support optional `min_target` in passive blocks

### Code Changes

1. `core/constants.py` - add helper template and default constant
2. `core/scheduler.py` - return min_target with passive mode dict
3. `controllers/room_controller.py` - implement dual-mode passive logic
4. `services/status_publisher.py` - display min_target in attributes
5. `services/heating_logger.py` - log min_target column
6. `app.py` - add state listeners for min_temp entity changes
7. `docs/` - document new behavior (README, ARCHITECTURE, changelog)

**Estimated:** ~7 files modified, ~150 lines of new code

### Testing Requirements

1. Normal passive operation (temp between min and max)
2. Safety mode trigger (temp drops below min_temp)
3. Safety mode exit (temp recovers above min_temp + off_delta)
4. Hysteresis at boundaries (no oscillation)
5. Scheduled min_target override
6. Manual min_temp entity override
7. Global frost protection fallback
8. Status display correctness
9. CSV logging correctness
10. Load sharing exclusion (verify no regression)

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

**Question:** What should the default passive comfort floor be?

**Options:**
- None (no default) - users must explicitly configure if desired
- 10°C - minimal comfort floor (2°C above frost protection)
- 12°C - balanced approach (4°C above frost protection)
- Equal to frost_protection_temp_c (8°C) - effectively disables comfort mode

**Recommendation:** **None (no default)** - make comfort floor optional
- Global frost protection (8°C) already provides safety
- Users who want comfort floor can configure per-room or per-schedule
- Simpler default behavior (passive mode stays truly passive)
- More explicit configuration (users know what they've set)

---

### 2. Should Comfort Mode Use Different Hysteresis?

**Question:** Should comfort mode use wider hysteresis than normal passive?

**Current proposal:** Use same on_delta/off_delta as normal passive and frost protection
- Example: on_delta=0.3°C, off_delta=0.1°C

**Alternative:** Use wider hysteresis for comfort mode
- Example: Enter at min_temp - 0.5°C, exit at min_temp + 0.5°C
- Prevents rapid re-triggering if room barely recovers

**Considerations:**
- Wider hysteresis = more overshoot (might reach 13-14°C from 12°C target)
- Overshoot is arguably desirable in comfort mode (thermal buffer)
- But too much overshoot might waste energy
- Current hysteresis values are well-tuned for active mode and frost protection

**Recommendation:** **Use existing hysteresis** for consistency across all modes

---

### 3. Validation: Min Temp Must Be >= Frost Protection Temp

**Question:** How strictly should we enforce min_temp >= frost_protection_temp_c?

**Options:**
- **Strict validation** - reject config if min_temp < frost_protection_temp_c
- **Warning only** - log warning but allow (system will use frost protection as floor anyway)
- **Silent override** - silently use max(min_temp, frost_protection_temp_c)

**Considerations:**
- Frost protection is the safety floor (8°C)
- Setting min_temp < frost_protection_temp_c makes no sense (frost protection would activate first)
- Clear error messages help users understand the system

**Recommendation:** **Strict validation** with clear error message
- Prevents user confusion
- Makes system behavior explicit
- Forces users to understand the two-tier protection model

---

### 4. Entity Initial Value Strategy

**Question:** How to handle `input_number` entities without overwriting user values?

**Problem:** HA `input_number` with `initial:` value overwrites user settings on every HA restart

**Options:**

**Option A: Omit initial value**
```yaml
input_number:
  pyheat_games_passive_min_temp:
    name: "Dining Room Passive Min Temp"
    min: 5
    max: 20
    # NO initial - entity created with value 5 (min) on first creation
```
- First creation: Entity starts at 5°C (min value)
- User must set to desired value (e.g., 10°C)
- Future restarts: Preserves user value

**Option B: Use restore state**
- HA has `restore_state: true` for some entity types
- Not available for `input_number` entities
- Not an option

**Option C: Initialize via automation**
- Create entities without initial value
- Add automation to set to 10°C only if entity is at min value (5°C)
- Preserves user changes (if set to 10.5°C or 12°C, automation won't touch it)
- Complexity: Requires additional automation configuration

**Option D: Document that users must set values**
- Entities created at 5°C (min)
- Documentation explicitly states: "Set each room's passive_min_temp to 10.0°C or desired value"
- Relies on user action

**Recommendation needed:** Which approach balances usability vs. simplicity?

---

### 5. Alert/Notification When Comfort Mode Activates?

**Question:** Should system send alerts when passive comfort mode activates?

**Comparison with frost protection:**
- Frost protection: Sends HA persistent notification (already implemented ✅)
- Comfort mode: Should it also send notification?

**Rationale for alerts:**
- Indicates room dropped below comfort threshold
- Might indicate heating system underperformance
- Might indicate need to adjust passive valve percentage or max_temp
- Provides visibility into comfort mode activation

**Rationale against alerts:**
- Less urgent than frost protection (comfort vs. safety)
- Might be expected behavior for passive rooms
- Could cause alert fatigue if triggers frequently
- User already has visibility via status entities and logs

**Recommendation:** **No alerts for comfort mode** (or make it optional)
- Comfort mode is less critical than frost protection
- Log INFO message (not WARNING like frost protection)
- Users can monitor via status entities and CSV logs
- If needed, add optional per-room alert enable/disable later

---

### 6. CSV Logging Column Name

**Question:** What should the CSV column be called?

**Options:**
- `passive_min_temp` - explicit, technical
- `min_target` - shorter, matches yaml field
- `frost_protection_temp` - describes purpose
- `safety_floor` - describes purpose

**Considerations:**
- CSV logs already have `target` column (max_temp for passive)
- Consistency: `target` / `min_target` pairs nicely
- Clarity: Column name should be self-explanatory

**Recommendation needed:** Pick column name for consistency and clarity.

---

### 7. Load Sharing During Comfort Mode

**Question:** When a passive room is in comfort mode (calling for heat), how should load sharing treat it?

**Answer:** ✅ No special handling needed - existing logic already correct

**Current behavior:**
- Load sharing excludes passive rooms (checks operating_mode='passive')
- When comfort mode activates, room is calling for heat but still operating_mode='passive'
- Room is already excluded from load sharing
- If comfort mode uses operating_mode='active', it would still be excluded (legitimately calling)

**Scenarios:**
- Games room in comfort mode: temp=11.7°C, calling=TRUE, valve=100%, operating_mode=passive
- Pete room calling: temp=18.5°C, target=19.0°C
- System has capacity for load sharing

Load sharing correctly:
- Excludes games (because operating_mode=passive, existing logic)
- OR if we use operating_mode='active' in comfort mode, excludes because calling=TRUE

**Recommendation:** Document that existing load sharing logic handles this correctly

---

### 8. Naming/Terminology

**Question:** What should we call this feature in user-facing documentation?

**Options:**
- "Passive mode with comfort floor"
- "Passive mode with minimum temperature"
- "Protected passive mode"
- "Comfort-protected passive mode"

**Distinction from frost protection:**
- **Frost protection** = safety floor (8°C, prevents pipe freezing)
- **Comfort floor** / **minimum temperature** = comfort threshold (12-15°C, prevents excessive cold)

**Recommendation:** **"Passive mode minimum temperature"** or **"comfort floor"**
- Clear distinction from "frost protection" (which is safety-focused)
- "Minimum temperature" is most generic/flexible
- "Comfort floor" emphasizes the purpose (comfort, not safety)
- Avoid "frost" terminology to prevent confusion with frost protection

**Usage:**
- Documentation: "passive mode minimum temperature" or "comfort floor"
- Entity names: `pyheat_{room}_passive_min_temp`
- Status text: `"Comfort heating (below 12.0°C)"`
- CSV columns: `min_target` (matches yaml field)
- Log messages: "Passive comfort mode activated"

---

## Next Steps

1. **Review updated proposal** - confirm approach with global frost protection as foundation
2. **Answer remaining open questions** - make decisions on:
   - Default comfort floor temperature (recommendation: none/optional)
   - Hysteresis strategy (recommendation: use existing)
   - Validation strictness (recommendation: strict validation)
   - Alert strategy (recommendation: no alerts or optional)
   - Terminology (recommendation: "comfort floor" or "minimum temperature")
3. **Create implementation plan** - break into phases/commits
4. **Implement** - write code with tests (smaller scope now that frost protection exists)
5. **Document** - update README, ARCHITECTURE, changelog
6. **Test in production** - validate comfort floor behavior
7. **Iterate** - adjust based on real-world usage

---

## Related Documents

- Global frost protection: `docs/changelog.md` (2025-12-01 entry) ✅ IMPLEMENTED
- Global frost protection proposal: `docs/frost_protection_proposal.md`
- Current passive mode implementation: `docs/changelog.md` (2025-11-30 entries)
- Passive mode architecture: `docs/ARCHITECTURE.md` (Room Control Logic section)
- Configuration examples: `README.md` (Passive Mode section)
