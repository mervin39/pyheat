# Proposal: Passive Mode with Frost Protection

**Status:** PLANNING  
**Date:** 2025-11-30  
**Author:** System proposal based on user requirements

---

## Problem Statement

Passive mode currently allows rooms to heat opportunistically (valve opens when other rooms are heating) but provides no protection against rooms getting dangerously cold. Users want:

1. Rooms in passive mode most of the time (opportunistic heating)
2. Automatic active heating when temperature drops below a safety threshold
3. Protection against frozen pipes, excessive cold, and discomfort

**Current limitation:** Passive mode NEVER calls for heat. If a passive room drops to 8°C, the system does nothing.

---

## Proposed Solution

Add a **lower temperature bound** to passive mode that triggers active heating when crossed, providing frost protection while maintaining normal passive behavior most of the time.

### Key Concept

**Dual-mode passive behavior:**

1. **Normal passive mode (temp ≥ min_temp):**
   - Room does NOT call for heat
   - Valve opens to user-configured percentage when temp < max_temp
   - Standard opportunistic heating (unchanged from current behavior)

2. **Safety mode (temp < min_temp):**
   - Room calls for heat (acts like active mode)
   - Valve forced to 100% for rapid recovery
   - Aggressive heating until temp recovers above threshold
   - Returns to normal passive behavior once safe

---

## Configuration Architecture

### Global Frost Protection

Add system-wide frost protection setting in `config/boiler.yaml`:

```yaml
system:
  frost_protection_temp_c: 10.0  # Global safety floor for all rooms
```

**Rationale:**
- Single configuration point for system-wide safety
- All passive rooms automatically protected
- Reasonable default that works for most users
- One place to adjust if user prefers different threshold

### Per-Room Override (Manual Passive)

Add optional per-room entities (6 total - one per room):

```yaml
# config/ha_yaml/pyheat_package.yaml
input_number:
  pyheat_games_passive_min_temp:
    name: "Dining Room Passive Min Temp"
    min: 5
    max: 20
    step: 0.5
    unit_of_measurement: "°C"
    mode: box
    icon: mdi:snowflake-thermometer
    # NO initial value - preserves user settings across HA restarts
```

**Usage:**
- Leave at default (10.0°C) to use global frost protection
- Set to 12.0°C to override with higher floor for that room
- Set to 8.0°C to override with lower floor (e.g., rarely-used room)

### Per-Schedule Override (Scheduled Passive)

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
          min_target: 12.0    # Lower bound (NEW, optional)
          valve_percent: 50   # Passive valve opening (existing)
```

**Usage:**
- Omit `min_target` to use global frost protection (10°C)
- Specify `min_target: 12.0` to override for this schedule period
- Different schedule blocks can have different min_target values

### Precedence Order

When determining the minimum temperature threshold:

1. **Scheduled min_target** (if specified) - highest priority
2. **Manual passive_min_temp entity** (if set to non-default value)
3. **Global frost_protection_temp_c** - fallback for all cases

---

## Behavioral Specification

### Temperature Zones

For a room in passive mode with `max_temp=18°C`, `min_temp=12°C`, `passive_valve=10%`:

**Zone 1: Normal Passive (temp ≥ 12°C)**
- Room does NOT call for heat
- Valve opens to 10% when temp < 18°C (with hysteresis)
- Valve closes when temp ≥ 18°C
- Opportunistic heating only

**Zone 2: Safety Mode (temp < 12°C)**
- Room calls for heat (active heating)
- Valve forced to 100% (ignores passive_valve setting)
- Aggressive heating for rapid recovery
- Continues until temp > min_temp + off_delta (e.g., 12.1°C)
- Returns to Zone 1 behavior once recovered

### Hysteresis

Uses existing per-room `on_delta_c` and `off_delta_c` values:

- **Enter safety mode:** temp < (min_temp - on_delta)
  - Example: < 11.7°C with on_delta=0.3°C
- **Exit safety mode:** temp > (min_temp + off_delta)
  - Example: > 12.1°C with off_delta=0.1°C
- **Dead band:** 11.7°C to 12.1°C maintains current state

**Prevents oscillation** at the threshold boundary.

### Valve Control in Safety Mode

**Decision: Force 100% valve (not passive_valve)**

**Rationale:**
1. Emergency/safety situation requires aggressive heating
2. Passive valve might be very low (10-20%) - too slow for recovery
3. Rapid heating prevents prolonged exposure to cold
4. Intentional overshoot provides thermal buffer
5. Clear behavioral distinction: normal passive vs. safety mode

**Alternative considered:** Use PID control with active mode bands
- Rejected: Too slow, defeats the purpose of safety mode
- Room might stay cold for extended period with low valve percentages

---

## Example Scenario

**Room:** Games (passive mode)
**Configuration:**
- `max_temp = 18°C`
- `min_temp = 12°C` (using global frost protection)
- `passive_valve = 10%`
- `on_delta = 0.3°C`, `off_delta = 0.1°C`

**Temperature Journey:**

| Temp | State | Calling | Valve | Behavior |
|------|-------|---------|-------|----------|
| 15°C | Normal passive | FALSE | 10% | Opportunistic heating |
| 14°C | Normal passive | FALSE | 10% | Opportunistic heating |
| 13°C | Normal passive | FALSE | 10% | Opportunistic heating |
| 12°C | Normal passive | FALSE | 10% | At threshold, still passive |
| 11.7°C | **Safety mode triggered** | **TRUE** | **100%** | Below min_temp - on_delta |
| 11.8°C | Safety mode active | TRUE | 100% | Heating rapidly |
| 12.0°C | Safety mode active | TRUE | 100% | Within hysteresis |
| 12.2°C | **Safety mode exit** | **FALSE** | 10% | Above min_temp + off_delta |
| 13°C | Normal passive | FALSE | 10% or 0% | Likely overshot (desirable) |
| 14°C | Normal passive | FALSE | 10% or 0% | Returns to normal behavior |

**Note:** Overshoot to 13-14°C is intentional and beneficial - provides thermal buffer and reduces risk of re-triggering.

---

## Integration with Existing Systems

### Load Sharing

**No changes needed.**

Current code already excludes passive rooms from load sharing selection:
- When in normal passive mode: excluded (user controls valve)
- When in safety mode (calling for heat): excluded (legitimately calling)

Load sharing sees the room as either passive or calling - both are excluded from selection.

### Boiler Interlock

**No changes needed.**

Valve coordinator already counts all open valves toward interlock, regardless of whether room is calling for heat. Safety mode valve (100%) will be counted correctly.

### Status Publishing

**Minor enhancement needed.**

Status entities should display:
- Current min_target value (for user visibility)
- Indication when in safety mode vs. normal passive
- Example: `"Passive (opportunistic)"` vs. `"Safety heating (below 12.0°C)"`

### Heating Logger

**Minor enhancement needed.**

CSV logs should include min_target column for analysis:
- Helps identify when/why safety mode triggered
- Allows post-analysis of frost protection effectiveness

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

1. **First frost protection in PyHeat** - genuine safety feature
2. **Prevents frozen pipes** - automatic emergency heating
3. **Prevents excessive cold** - rooms won't drop below safe threshold
4. **Flexible configuration** - global default with per-room/per-schedule overrides
5. **Set and forget** - automatic protection without user intervention

### System Benefits

1. **Simple configuration** - one global setting covers all rooms
2. **Low entity overhead** - only 6 new entities (one per room)
3. **Backward compatible** - existing passive schedules continue working
4. **Intuitive behavior** - clear separation between passive and safety modes
5. **Rare activation** - minimal impact on normal operation

### Energy Benefits

1. **Prevents emergency heating cycles** - proactive rather than reactive
2. **Intentional overshoot** - reduces re-triggering frequency
3. **Only activates when needed** - not running continuously

---

## Open Questions

### 1. Default Global Frost Protection Temperature

**Question:** What should `frost_protection_temp_c` default to?

**Options:**
- 8°C - True frost protection (pipes won't freeze)
- 10°C - Balanced approach (protection + minimal comfort)
- 12°C - Comfort-oriented (prevents rooms feeling very cold)

**Considerations:**
- UK frost protection typically 7-10°C
- Pipes freeze around 0-5°C (depends on insulation, external temp)
- User comfort threshold around 15-16°C
- Lower value = less frequent activation = less energy use

**Recommendation needed:** Pick one based on primary use case.

---

### 2. Should Safety Mode Exit Use Different Hysteresis?

**Question:** Should safety mode use wider hysteresis than normal passive?

**Current proposal:** Use same on_delta/off_delta as normal passive mode
- Example: on_delta=0.3°C, off_delta=0.1°C

**Alternative:** Use wider hysteresis for safety mode
- Example: Enter at min_temp - 0.5°C, exit at min_temp + 0.5°C
- Prevents rapid re-triggering if room barely recovers

**Considerations:**
- Wider hysteresis = more overshoot (might reach 13-14°C from 12°C target)
- Overshoot is arguably desirable in safety mode (thermal buffer)
- But too much overshoot might be uncomfortable
- Current hysteresis values are well-tuned for active mode

**Recommendation needed:** Use existing hysteresis or implement separate safety mode hysteresis?

---

### 3. Should Min Temp Apply to Non-Passive Modes?

**Question:** Should frost protection extend beyond passive mode?

**Current proposal:** Passive mode only

**Alternative scenarios:**

**Scenario A: Room in "off" mode**
- Currently: No heating at all
- With global frost protection: Heat if temp < 10°C even when "off"
- Pro: True safety override, prevents frozen pipes even if user forgets
- Con: Users might want truly off (e.g., closed vents, winterizing)

**Scenario B: Room in "auto" mode with no schedule**
- Currently: No target, no heating
- With global frost protection: Heat if temp < 10°C
- Pro: Safety net for misconfigured schedules
- Con: Might mask configuration errors

**Scenario C: All modes get frost protection**
- Pro: Comprehensive safety system
- Con: Very invasive, might surprise users

**Considerations:**
- Passive mode: Users expect some heating, frost protection is natural extension
- Off mode: Users expect NO heating, frost protection overrides user intent
- Auto mode: Users expect schedule-driven heating, frost protection is fallback

**Recommendation needed:** Phase 1 = passive only, or implement for all modes?

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

### 5. Alert/Notification When Safety Mode Activates?

**Question:** Should system send alerts when frost protection activates?

**Rationale for alerts:**
- Indicates unusual condition (room got very cold)
- Might indicate heating system problem
- Might indicate configuration issue (schedule gap)
- Might indicate external issue (open window, extreme weather)

**Rationale against alerts:**
- Might be expected behavior for rarely-used rooms
- Could cause alert fatigue if triggers frequently
- User already has visibility via status entities

**Possible implementation:**
- Alert on first activation per day
- Alert if activation lasts > X minutes
- Alert if activation frequency exceeds threshold
- Optional per-room alert enable/disable

**Recommendation needed:** Implement alerts? If yes, what triggers and conditions?

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

### 7. Load Sharing During Safety Mode

**Question:** When a passive room is in safety mode (calling for heat), how should load sharing treat it?

**Current proposal:** No special handling - room is excluded because it's calling for heat

**Alternative:** Room is excluded because it's in passive mode (operating_mode check)

**Scenario to consider:**
- Games room in safety mode: temp=11.7°C, calling=TRUE, valve=100%
- Pete room calling: temp=18.5°C, target=19.0°C
- System has capacity for load sharing

Should load sharing:
- **Option A:** Exclude games (because calling=TRUE, like any other calling room)
- **Option B:** Exclude games (because operating_mode=passive, existing logic)
- **Option C:** Include games as Tier 1 (it's calling and has upcoming target... no wait, it doesn't have a schedule)

**Likely answer:** Existing logic already handles this correctly (passive rooms excluded regardless of calling state), but worth confirming logic flow.

**Recommendation needed:** Verify load sharing exclusion logic, document expected behavior.

---

### 8. Naming/Terminology

**Question:** What should we call this feature in user-facing documentation?

**Options:**
- "Passive mode with frost protection"
- "Passive mode with safety floor"
- "Passive mode with minimum temperature"
- "Protected passive mode"
- "Frost-protected passive mode"

**Usage locations:**
- README.md user documentation
- ARCHITECTURE.md technical documentation  
- Status entity attributes (`passive_frost_protection_temp`)
- Entity names (`pyheat_games_passive_min_temp`)
- Log messages

**Considerations:**
- "Frost protection" clearly communicates purpose
- "Safety floor" emphasizes emergency nature
- "Minimum temperature" is most generic/flexible
- Consistency across all documentation and code

**Recommendation needed:** Pick terminology and stick with it everywhere.

---

## Next Steps

1. **Review proposal** - confirm overall approach is sound
2. **Answer open questions** - make decisions on unresolved items
3. **Create implementation plan** - break into phases/commits
4. **Implement** - write code with tests
5. **Document** - update README, ARCHITECTURE, changelog
6. **Test in production** - validate behavior with real heating
7. **Iterate** - adjust based on real-world usage

---

## Related Documents

- Current passive mode implementation: `docs/changelog.md` (2025-11-30 entries)
- Passive mode architecture: `docs/ARCHITECTURE.md` (Room Control Logic section)
- Configuration examples: `README.md` (Passive Mode section)
