# Load Sharing Feature: Schedule-Aware Load Balancing

**Date:** 26 November 2025  
**Status:** Design Stage
**Strategy:** Three-tier cascading selection with phased rollout

---

## Executive Summary

Implement intelligent load sharing to reduce boiler short-cycling when primary calling rooms have insufficient radiator capacity. Uses schedule-aware pre-warming as primary strategy, with extended lookahead and explicit fallback priorities for comprehensive coverage.

**Key Features:**
- Schedule-aligned pre-warming (minimizes unwanted heating)
- Per-room configurable lookahead windows
- Graceful degradation through three selection tiers
- Phased implementation to minimize risk

---

## Problem Statement

PyHeat currently operates on binary call-for-heat logic: rooms either call (valve open) or don't call (valve closed). When a single room or small group of rooms calls for heat but cannot dissipate the boiler's minimum viable output (~6kW), we face two suboptimal scenarios:

1. **Moderate short cycling** - Boiler rapidly heats the limited radiator capacity then cycles off (now mitigated but not eliminated)
2. **Single dump radiator** - Opening one fixed "overflow" radiator wastes energy by heating unwanted spaces

**Goal:** Intelligently distribute excess load across available radiators while prioritizing efficiency over convenience (some short cycling is acceptable if it avoids heating unused rooms).

---

## Current System Architecture (Relevant Context)

### Existing Components
- **LoadCalculator** - Estimates radiator capacity using EN 442 thermal model (±20-30% accuracy)
- **CyclingProtection** - Monitors return temp, drops setpoint to 30°C during cooldown periods
- **ValveCoordinator** - Central authority for valve commands with priority system (safety > corrections > normal)
- **BoilerController** - 6-state FSM with anti-cycling timers and TRV interlock
- **RoomController** - Per-room hysteresis logic for call-for-heat decisions

### Key Configuration Data
- `delta_t50` - Manufacturer-rated capacity at ΔT50 (watts) per room
- `radiator_exponent` - Heat transfer exponent (1.3 panels, 1.2 towel rails)
- **Schedules** - Time-based targets per room with default_target for off-schedule periods
- **Room modes** - auto/manual/off per room

### Constraints
- LoadCalculator estimates are relative comparison only (not absolute thermal measurements)
- Short cycling protection already exists and is working
- Valve persistence system already handles safety overrides (pump overrun, interlock)
- Must not heat rooms that are explicitly set to "off" mode

---

## Solution Design: Schedule-Aware Load Balancing

**Concept:** Three-tier cascading selection strategy that prioritizes schedule-aligned pre-warming, then extends the window, then falls back to explicit priority list.

### Architecture Integration

**New Files:**
- `managers/load_sharing_state.py` - State machine infrastructure (enums, dataclasses)
- `managers/load_sharing_manager.py` - Main business logic and tier selection

**Modified Files:**
- `app.py` - Integration point (minimal changes, initialize and call in recompute)
- `controllers/valve_coordinator.py` - Add load_sharing priority tier (between persistence and corrections)
- `core/scheduler.py` - Add `get_next_schedule_block()` lookahead API
- `managers/load_calculator.py` - Add `calculate_effective_capacity()` with valve adjustment
- `core/config_loader.py` - Parse new `load_sharing` sections from YAML
- `ha_yaml/pyheat_package.yaml` - Add `input_boolean.pyheat_load_sharing_enable` and status sensor

**Priority System:** safety (persistence) > **load_sharing** > corrections > normal

**Master Control:** `input_boolean.pyheat_load_sharing_enable` provides user-facing on/off switch (checked first in `evaluate()`)

### Configuration (rooms.yaml)
```yaml
rooms:
  - id: lounge
    name: "Living Room"
    delta_t50: 2290
    load_sharing:
      schedule_lookahead_m: 60  # Check for schedules within 60 minutes (default)
      fallback_priority: 1       # Lower number = higher priority for fallback
```

### Selection Algorithm (Cascading Strategy)

**TIER 1: Schedule-Aware Pre-warming (Primary)**
- Trigger: Total calling capacity < 3500W (configurable threshold)
- Selection criteria:
  - Room in "auto" mode (respects user intent)
  - Next scheduled block within `schedule_lookahead_m` minutes (per-room configurable, default 60)
  - Scheduled target > current temp + 0.5°C (will definitely need heating)
  - Not currently calling for heat
- Sort by: (scheduled_target - current_temp) DESC (neediest first)
- Add rooms until total system capacity >= 4000W (target capacity)

**TIER 2: Extended Lookahead (Secondary)**
- Trigger: Tier 1 found insufficient rooms (still < 4000W capacity)
- Re-evaluate with 2× schedule_lookahead_m window per room
  - Room with 60 min lookahead → check 120 min window
  - Room with 90 min lookahead → check 180 min window
- Same selection criteria as Tier 1, just wider time window
- This catches rooms with later schedules that might be acceptable to pre-warm

**TIER 3: Fallback Priority List (Tertiary)**
- Trigger: Tiers 1+2 still insufficient capacity
- Use explicit `fallback_priority` ranking from room configs
- Process rooms in priority order (1, 2, 3, ...)
- For each candidate room:
  - Skip if already active or calling
  - Skip if in "off" or "manual" mode
  - **No temperature check** - ultimate fallback accepts any room in "auto" mode
  - Calculate new total capacity with this room added
  - If new_total >= 4000W (target), stop adding rooms
  - Otherwise, add room and continue to next priority
- This provides deterministic behavior when schedules don't help

### Valve Control Strategy

**Pre-warming rooms (Tier 1 & 2):**
- **Within schedule_lookahead_m minutes:** Open to 70% (band 2) - active pre-warming
- **Beyond schedule_lookahead_m (only in 2× window):** Open to 40% (band 1) - gentle pre-warming
- **If insufficient capacity:** Increase Tier 1/2 rooms to 80% before moving to Tier 3
- Transition to normal hysteresis control once schedule becomes active

**Fallback rooms (Tier 3):**
- Open to 50% initially (compromise between flow and energy)
- **No temperature check** - ultimate fallback to prevent cycling
  - Any room in "auto" mode is eligible ("off" and "manual" modes excluded)
  - Accepts heating rooms above target as trade-off to prevent boiler cycling
- **If still insufficient:** Increase to 60% before adding more rooms
- Maintain until:
  - Primary rooms stop calling (demand satisfied)
  - Total capacity drops below threshold again (need reevaluation)
  - 15 minutes elapsed (prevent long-term unwanted heating)

**Valve Opening Guidelines:**
- All percentages in 10% increments (avoids micro-adjustments)
- Higher openings prioritized for existing selections over adding new rooms
- Valve opening affects effective capacity (accounted for in calculations)
- **Escalation timing:** Immediate based on capacity calculation (no waiting period)
- **TRV interlock interaction:** Load sharing rooms count toward interlock threshold (>50% valve = interlock risk)
  - This is intentional - interlock is a safety feature and shouldn't be bypassed

**Capacity Adjustment:**
Valve opening reduces effective radiator capacity. LoadCalculator applies correction:
```
effective_capacity = delta_t50_rating × (valve_opening_pct / 100) × flow_efficiency
```
Where `flow_efficiency` = 1.2 (starting estimate - **will require real-world tuning** based on observed cycling correlation)

This means:
- 70% valve ≈ 84% effective capacity (70% × 1.2)
- 50% valve ≈ 60% effective capacity (50% × 1.2, capped at 100%)
- 40% valve ≈ 48% effective capacity (40% × 1.2)

**Escalation Strategy:**
1. Select Tier 1 rooms at 70%
2. If insufficient, increase Tier 1 rooms to 80%
3. If still insufficient, add Tier 2 rooms at 40%
4. If still insufficient, increase Tier 2 rooms to 50%
5. If still insufficient, add Tier 3 rooms at 50%
6. If still insufficient, increase Tier 3 rooms to 60%
7. Only add additional rooms after maximizing existing selections

### State Management

**Entry Conditions:**
- Load sharing activates when ALL of these are true:
  1. `total_calling_capacity < min_calling_capacity_w` (default: 3500W)
  2. **Cycling risk evidence** (either):
     - Recent cooldown: `cycling_protection.last_cooldown_within(15 minutes)`
     - High return temp risk: `boiler_state == ON AND return_temp > (setpoint - 15°C)`

**Rationale:** Low capacity alone isn't proof of a problem. Only activate when there's evidence of inefficiency (recent cycling or imminent risk).

**Snapshot at Activation:**
- Record `trigger_calling_rooms` - the set of rooms that caused activation
- Record `trigger_capacity` - their combined capacity
- Select and open load sharing rooms via Tier 1/2/3 cascade

**Exit Conditions (Re-evaluate on calling pattern changes):**

Load sharing persists for the duration of the calling pattern that triggered it. Exit is triggered by **changes in the calling situation**, not arbitrary timers.

**Exit Trigger A: Original calling room(s) stopped**
- If none of the `trigger_calling_rooms` are still calling → **Deactivate load sharing**
- Rationale: Original need is gone

**Exit Trigger B: Additional room(s) started calling**
- If new room(s) join the calling set (not in `trigger_calling_rooms`)
- Recalculate total capacity with new configuration
- If `new_total_capacity >= 4000W` → **Deactivate load sharing** (sufficient now)
- If still insufficient → **Update trigger set and continue** (adapt to new pattern)
- Rationale: Additional callers may provide sufficient capacity

**Exit Trigger C: Load sharing room now naturally calling**
- If a load sharing room transitions to naturally calling (reaches its own on_delta threshold)
- **Remove from load sharing control**, let room controller manage it (normal hysteresis)
- If no load sharing rooms remain → **Deactivate load sharing**
- Rationale: Room now needs heat anyway, not just helping

**Minimum Activation Duration:**
- Enforce 5-minute minimum before allowing exit (prevents rapid oscillation)
- Applied to all exit triggers

**Persistence:**
- Load sharing rooms maintain activation for entire duration of calling pattern
- No arbitrary timeouts (except Tier 3 fallback: 15 minutes max)
- Room flagged with `load_sharing_reason`: "schedule_60m" | "schedule_120m" | "fallback_p1"
- Published to HA attributes for visibility

### Configuration Examples

**Aggressive pre-warming (living spaces):**
```yaml
- id: lounge
  load_sharing:
    schedule_lookahead_m: 90   # Look ahead 90 minutes
    fallback_priority: 1        # First choice for fallback
```

**Conservative (bedrooms):**
```yaml
- id: pete
  load_sharing:
    schedule_lookahead_m: 30   # Only pre-warm 30 min before
    fallback_priority: 99       # Never use as fallback (or omit)
```

**Omit config for sensible defaults:**
```yaml
- id: games
  # Uses 60 min lookahead and no fallback priority (won't be selected in Tier 3)
```

### Advantages
- **Intelligent:** Prioritizes rooms that will need heat anyway (reduces wasted energy)
- **Flexible:** Per-room configuration allows household-specific tuning
- **Deterministic:** Fallback priority list ensures predictable behavior
- **Graceful degradation:** Three-tier cascade handles edge cases (no schedules, weekends, etc.)
- **Comfort improvement:** Pre-warming reduces cold-start delays when schedules activate
- **User-respecting:** Only heats "auto" mode rooms, never "off" or "manual"

### Disadvantages
- **Configuration complexity:** Requires per-room tuning for optimal results
- **Schedule dependency:** Effectiveness varies based on how well schedules are defined
- **Fallback list maintenance:** Priority list needs manual setup and updates
- **Extended lookahead risk:** 2× window might pre-warm too aggressively for some rooms

### Edge Cases Handled
1. **No schedules defined:** Falls through to Tier 3 (priority list)
2. **All rooms at temperature:** No load sharing needed (capacity met by primary calling)
3. **Schedule changes:** Rooms re-evaluated every recompute cycle (60s baseline)
4. **Room mode changes:** Immediately excluded if changed to "off" or "manual"
5. **Cycling protection active:** Load sharing still operates (provides cooling assistance)
6. **Room reaches target early:** Transitions to normal control (hysteresis takes over)

---

## Technical Assessment

### Technical Feasibility: ✅ HIGH

**Existing Infrastructure:**
- ✅ `LoadCalculator` already calculates per-room capacity estimates
- ✅ `Scheduler` has schedule parsing and lookahead capability
- ✅ `ValveCoordinator` supports priority-based override system (3-tier: persistence > corrections > normal)
- ✅ Room configuration framework exists in `rooms.yaml`
- ✅ Boiler configuration framework exists in `boiler.yaml`
- ✅ Recompute trigger system handles sensor changes, mode changes, setpoint changes (adequate for load sharing)

**New Requirements:**
- New `LoadSharingManager` class (straightforward, follows existing patterns)
- Extend `Scheduler` with lookahead query API (minor addition to existing code)
- Add per-room `load_sharing` config section (standard YAML parsing)
- **Insert** load_sharing priority tier into `ValveCoordinator` between persistence and corrections
- Add `input_boolean.pyheat_load_sharing_enable` for user control
- Add `sensor.pyheat_load_sharing_status` for visibility
- State persistence for minimum activation duration (use existing helpers)

**Complexity Score: MODERATE**
- Tier 1 (schedule lookahead): Moderate - requires schedule parsing enhancement
- Tier 2 (extended lookahead): Trivial - same logic, different window
- Tier 3 (fallback priority): Simple - ordered list iteration
- Cascade logic: Simple - state machine handles tier progression
- State machine: Moderate - explicit states, transitions, and context management
- Configuration: Simple - follows established patterns

### Algorithm Soundness: ✅ HIGH

**Selection Logic:**
- Schedule-aware: Mathematically sound (pre-warm rooms that will need heat)
- Extended window: Conservative expansion (catches edge cases)
- Fallback priority: Deterministic (guarantees a selection)
- Capacity checking: Proven approach (similar to existing interlock logic)

**Potential Issues & Mitigations:**

1. **Over-pre-warming (heating too early)**
   - Risk: LOW - Extended window (2×S) might start pre-warming too aggressively
   - Mitigation: Use lower valve % (40%) for extended window vs primary window (70%)
   - Tuning: Per-room `schedule_lookahead_m` allows household-specific optimization

2. **Fallback priority list maintenance**
   - Risk: MODERATE - Requires manual setup and updates when rooms change
   - Mitigation: Make `fallback_priority` optional (rooms without it excluded from Tier 3)
   - Documentation: Provide clear examples and guidance

3. **Oscillation (rooms activating/deactivating rapidly)**
   - Risk: MODERATE - Rooms near capacity threshold could oscillate
   - Mitigation: Enforce 5-minute minimum activation duration
   - Hysteresis: Use different thresholds for activation (3500W) vs deactivation (4000W)

4. **Schedule changes (user modifies schedule while pre-warming active)**
   - Risk: LOW - Room might be heated unnecessarily if schedule deleted
   - Mitigation: Recompute every 60s (catches changes quickly)
   - Exit condition: Load sharing rooms have 15-minute timeout

5. **Interaction with cycling protection**
   - Risk: LOW - Load sharing might interfere with cooldown logic
   - Mitigation: Load sharing operates independently (different priority tier)
   - Synergy: Load sharing actually helps cycling protection by increasing capacity

6. **Capacity calculation inaccuracy (±20-30%)**
   - Risk: MODERATE - False activations when actual capacity is sufficient
   - Mitigation: Exit triggers handle this (deactivates when calling pattern changes)
   - Monitoring: Log WARNING via alert manager if high false activation rate (>20%)
   - Tuning: If persistent, tighten entry threshold (3200W) or require sustained low capacity

### Configuration Complexity: ⚠️ MODERATE

**User-Facing Configuration:**

Minimal (sensible defaults):
```yaml
# rooms.yaml - optional per-room tuning
- id: lounge
  load_sharing:
    schedule_lookahead_m: 60    # Optional, default 60
    fallback_priority: 1         # Optional, omit to exclude from fallback
```

Global (boiler.yaml):
```yaml
boiler:
  load_sharing:
    enabled: true
    min_calling_capacity_w: 3500  # Trigger threshold
    target_capacity_w: 4000       # Stop adding rooms threshold
    min_activation_duration_m: 5  # Prevent oscillation
```

**Tuning Required:**
- **Low for basic operation:** Works with all defaults
- **Moderate for optimization:** Per-room lookahead and priority tuning
- **Documentation critical:** Need clear examples and tuning guidance

### Performance Impact: ✅ LOW

**Computational Overhead:**
- Schedule lookahead queries: O(n) per room (n = schedule blocks) - negligible
- Capacity calculations: Already performed by `LoadCalculator`
- Priority sorting: O(n log n) where n = number of rooms (~6) - trivial
- Executed once per recompute cycle (60s baseline + state change events)

**Memory Impact:**
- Minimal: Per-room config (2 values), manager state (~10 variables)

**I/O Impact:**
- HA state reads: +1 per load_sharing room per cycle (negligible)
- HA state writes: +1 for load_sharing_status entity per cycle

### Integration Risk: ✅ LOW

**Conflicts with Existing Systems:**
- ❌ No conflicts: Load sharing uses established valve priority system
- ✅ Synergy with cycling protection: Helps by increasing total capacity
- ✅ Respects room modes: Only "auto" rooms eligible
- ✅ Respects manual overrides: Excluded from selection
- ✅ Works with safety_room: Different priority tiers (no conflict)

**Failure Modes:**
- Schedule parsing fails → Falls through to Tier 2/3 (graceful degradation)
- No eligible rooms found → No load sharing active (safe default)
- Capacity calculation unavailable → Exclude room from selection (safe)
- LoadCalculator disabled → Load sharing disabled (documented dependency)

### Energy Efficiency Impact: ✅ POSITIVE

**Expected Outcomes:**
- ✅ Reduces cycling frequency (primary goal)
- ✅ Minimizes unwanted heating (schedule-aligned)
- ✅ Improves comfort (rooms pre-warmed before schedule)
- ⚠️ Slight energy increase (pre-warming cost vs cycling losses)

**Efficiency Analysis:**
- Pre-warming 30-60 min early: Small energy cost, large comfort gain
- Fallback heating (Tier 3): Acceptable trade-off vs boiler cycling wear
- Overall: Net positive (reduced cycling losses > pre-warming cost)

### Maintenance Burden: ⚠️ MODERATE

**Ongoing Maintenance:**
- Configuration updates when adding/removing rooms: LOW (optional field)
- Threshold tuning: MODERATE (requires monitoring and adjustment)
- Documentation updates: MODERATE (need tuning guidelines and examples)

**Code Maintenance:**
- New component (`LoadSharingManager`): Standard patterns, clear responsibility
- Integration points: Well-defined interfaces (Scheduler, ValveCoordinator)
- Testing: Requires CSV log analysis and cycling frequency metrics

### Integration Conflicts: ✅ NONE IDENTIFIED

**Verified Compatible With:**
1. **ValveCoordinator Priority System:** Load sharing inserts between persistence (safety) and corrections (no conflict)
2. **Persistence Overrides (Pump Overrun/Interlock):** Safety takes absolute priority over load sharing (correct)
3. **Cycling Protection:** Load sharing works synergistically (increases capacity, helps cooling)
4. **TRV Feedback/Corrections:** Load sharing rooms respect correction overrides if they trigger
5. **Boiler State Machine:** Independent evaluation (load sharing doesn't modify boiler FSM)
6. **Room Controller Hysteresis:** Load sharing rooms transition to normal control when naturally calling (Exit Trigger C)
7. **Master Enable:** System properly handles enable/disable (load sharing respects master OFF state)
8. **Manual Mode:** Excluded from selection (only "auto" mode rooms eligible)
9. **Safety Room:** Different priority tiers (no conflict - safety room uses persistence priority)
10. **Recompute Triggers:** Existing triggers adequate (no new triggers needed)

**Verified Behaviors:**
- Load sharing valves count toward TRV interlock threshold (intentional - safety not bypassed)
- Load sharing rooms can trigger unexpected position corrections (intentional - corrections take priority)
- Persistence overrides can override load sharing commands (intentional - safety first)
- Load sharing respects room modes, sensor staleness, and configuration disable flags

### Recommendation: ✅ VIABLE - PROCEED WITH IMPLEMENTATION

**Strengths:**
- Technically sound with existing infrastructure
- Graceful degradation via three-tier cascade
- Per-room configurability allows optimization
- **Zero conflicts** with existing systems (verified)
- Positive energy efficiency impact
- Follows established architectural patterns

---

## Open Questions (For Proposal 2 Implementation)

### Answered by Refined Proposal:
1. ✅ **Capacity threshold:** 3500W trigger, 4000W target (configurable in boiler.yaml)
2. ✅ **Valve percentages:** Variable - 70% initial, escalate by tier (10% increments)
3. ✅ **Duration guarantees:** Yes - 5 minute minimum activation (prevents oscillation)
4. ✅ **Mode restrictions:** Yes - only "auto" mode rooms eligible (respects user intent)
5. ✅ **Schedule interaction:** No restriction - load sharing helps during active schedules too
6. ✅ **Boiler state dependencies:** No - evaluates based on capacity and cycling risk
7. ✅ **Safety interaction:** Separate priority tiers - load_sharing < safety_room (no conflict)
8. ✅ **Entry condition:** Only activates with cycling risk evidence (cooldown OR high return temp)
9. ✅ **Exit condition:** Calling pattern changes trigger exit (not arbitrary timers)

### Still Requires Real-World Tuning:
1. **Default schedule_lookahead_m:** Is 60 minutes optimal or should it be 45/75/90?
2. **Extended window multiplier:** Is 2× the right factor or should it be 1.5× or 3×?
3. **Capacity thresholds:** Are 3500W/4000W correct for your boiler or need adjustment?
4. **Return temp risk delta:** Is 15°C appropriate or needs tuning?
5. **Cooldown lookback:** Is 15 minutes the right window?
6. **Flow efficiency factor:** Is 1.2 accurate based on observed behavior?
7. **Escalation strategy:** Does prioritizing higher % on fewer rooms work better?

### Implementation Details to Resolve:
1. ✅ **Schedule lookahead query API:** Return single next block `(start_time, end_time, target_temp)` or `None` (simpler, sufficient)
2. ✅ **Capacity calculation timing:** Calculate per-candidate during selection (more accurate, minimal overhead)
3. ✅ **State persistence:** Create new `input_text.pyheat_load_sharing_context` entity (stores context as JSON)
4. ✅ **Logging verbosity:** INFO for state changes only, DEBUG for evaluation details, WARN for Tier 3
5. ✅ **HA entity exposure:** System-level `sensor.pyheat_load_sharing_status` + per-room attributes on existing room sensors
6. ✅ **Error handling:** Skip failing rooms, log warning, continue with remaining candidates (graceful degradation)
7. ✅ **Configuration validation:** Exclude room from load sharing, log warning (graceful - don't fail entire feature)
8. ✅ **Input boolean control:** `input_boolean.pyheat_load_sharing_enable` checked first in `evaluate()` (return `{}` if off)
9. ✅ **Valve coordinator integration:** Insert load_sharing tier between persistence and corrections (explicit priority order)
10. ✅ **CyclingProtection API:** Add `last_cooldown_within(minutes)` method for entry condition checking

---

## Implementation Plan

### Phased Rollout Strategy (Risk-Minimized)

The implementation is structured in progressive phases, each fully functional and independently testable. Each phase can be deployed, validated, and rolled back if needed before proceeding to the next.

---

### **PHASE 0: Infrastructure Preparation** ⚙️
*Goal: Set up foundation without changing heating behavior*

**Duration:** 2-3 hours  
**Risk:** MINIMAL - No behavioral changes  
**Rollback:** N/A (pure additions)

#### Tasks:
1. **Create state machine infrastructure** (`managers/load_sharing_state.py`)
   - Define `LoadSharingState` enum with explicit states:
     - DISABLED, INACTIVE, TIER1_ACTIVE, TIER1_ESCALATED
     - TIER2_ACTIVE, TIER2_ESCALATED, TIER3_ACTIVE, TIER3_ESCALATED
   - Define `RoomActivation` dataclass (room_id, tier, valve_pct, activated_at, reason)
   - Define `LoadSharingContext` dataclass (single source of truth):
     - Current state, trigger snapshot, active rooms dictionary
     - Computed properties: tier1_rooms, tier2_rooms, tier3_rooms
     - Helper methods: activation_duration, can_exit, has_tier3_timeouts

2. **Create LoadSharingManager skeleton** (`managers/load_sharing_manager.py`)
   - Initialize with LoadSharingContext (state machine)
   - Method stubs returning empty results
   - Always returns "no load sharing needed"
   - Integration with app.py (but always disabled)

3. **Add configuration schema**
   - Add `load_sharing` section to `boiler.yaml` with `enabled: false`
   - Add `load_sharing` section to `rooms.yaml` schema (optional fields)
   - Update ConfigLoader to parse new sections (with defaults)
   - Add configuration validation (warn on missing delta_t50, duplicate priorities)

4. **Add HA helper entities** (via `ha_yaml/pyheat_package.yaml`)
   - `input_boolean.pyheat_load_sharing_enable` (master on/off control)
   - `sensor.pyheat_load_sharing_status` (state: "disabled")
   - Attributes: active_rooms[], reason, total_capacity, trigger_capacity, state
   - Example YAML:
     ```yaml
     input_boolean:
       pyheat_load_sharing_enable:
         name: "PyHeat Load Sharing"
         icon: mdi:share-variant
         initial: false  # Start disabled
     
     sensor:
       - platform: template
         sensors:
           pyheat_load_sharing_status:
             friendly_name: "PyHeat Load Sharing Status"
             value_template: "{{ state_attr('input_text.pyheat_load_sharing_context', 'state') | default('disabled') }}"
     ```

5. **Extend Scheduler API** (`core/scheduler.py`)
   - Add `get_next_schedule_block(room_id, from_time, within_minutes)` method
   - Returns: `(block_start, block_end, target_temp)` or `None`
   - No changes to existing schedule resolution logic

6. **Testing:**
   - AppDaemon starts without errors
   - Config loads successfully
   - Helper entity appears in HA (state: "disabled")
   - Heating operates normally (load sharing never activates)
   - State machine enums import correctly

**Commit checkpoint:** "feat: Load sharing infrastructure with state machine (disabled)"

---

### **PHASE 1: Tier 1 Implementation (MVP)** 🎯
*Goal: Schedule-aware pre-warming with global lookahead*

**Duration:** 3-4 hours  
**Risk:** LOW - Explicit enable flag, conservative defaults  
**Rollback:** Set `enabled: false` in boiler.yaml

#### Tasks:
1. **Implement state machine manager** (`LoadSharingManager`)
   ```python
   def evaluate(self, calling_rooms, room_data, now):
       # State machine dispatcher
       if self.context.state == LoadSharingState.DISABLED:
           return {}
       
       if self.context.state == LoadSharingState.INACTIVE:
           self._evaluate_entry(calling_rooms, room_data, now)
       else:
           self._evaluate_active(calling_rooms, room_data, now)
       
       # Return valve overrides from context
       return {rid: room.valve_pct for rid, room in self.context.active_rooms.items()}
   
   def _evaluate_entry(self, calling_rooms, room_data, now):
       # Entry Condition 1: Low capacity
       capacity = self._calculate_total_capacity(calling_rooms, room_data)
       if capacity >= self.config.min_calling_capacity_w:
           return
       
       # Entry Condition 2: Cycling risk evidence
       cycling_reason = self._check_cycling_risk()
       if cycling_reason is None:
           return
       
       # Transition to active
       self._transition_to_active(calling_rooms, capacity, cycling_reason, now)
   
   def _transition_to_active(self, calling_rooms, capacity, reason, now):
       # Snapshot trigger state (immutable)
       self.context.trigger_calling_rooms = set(calling_rooms)
       self.context.trigger_capacity_w = capacity
       self.context.trigger_reason = reason
       self.context.activated_at = now
       
       # Select Tier 1 rooms
       tier1_selected = self._select_tier1_rooms(room_data, now)
       self.context.active_rooms = tier1_selected
       
       if tier1_selected:
           self.context.state = LoadSharingState.TIER1_ACTIVE
       else:
           self._try_tier2(room_data, now)
   
   def _evaluate_active(self, calling_rooms, room_data, now):
       # FIRST: Check exit conditions (highest priority)
       exit_trigger = self._check_exit_conditions(calling_rooms, room_data, now)
       if exit_trigger:
           self._deactivate(exit_trigger, now)
           return
       
       # SECOND: Check Tier 3 timeouts
       tier3_timeouts = self.context.has_tier3_timeouts(now, self.config.tier_3_max_duration_m)
       if tier3_timeouts:
           self._remove_rooms(tier3_timeouts, "tier3_timeout")
       
       # THIRD: Check if need more capacity
       total_capacity = self._calculate_system_capacity(calling_rooms, room_data, now)
       if total_capacity >= self.config.target_capacity_w:
           return
       
       # FOURTH: Escalate or add next tier
       self._escalate_or_add_tier(room_data, now)
   ```
   - Use LoadSharingContext as single source of truth
   - Explicit state transitions with logging
   - Global `schedule_lookahead_m` from boiler.yaml (default: 60)
   - Initial valve percentage: 70% (Tier 1)
   - State-specific escalation logic

2. **Implement exit condition checking**
   ```python
   def _check_exit_conditions(self, calling_rooms, room_data, now) -> Optional[str]:
       # Must meet minimum duration first
       if not self.context.can_exit(now, self.config.min_activation_duration_m):
           return None
       
       # Exit Trigger A: Original calling rooms stopped
       if not self.context.trigger_calling_rooms & calling_rooms:
           return "trigger_a_original_rooms_stopped"
       
       # Exit Trigger B: New room started calling with sufficient capacity
       new_callers = calling_rooms - self.context.trigger_calling_rooms
       if new_callers:
           new_capacity = self._calculate_total_capacity(calling_rooms, room_data)
           if new_capacity >= self.config.sufficient_capacity_w:
               return "trigger_b_new_caller_sufficient_capacity"
           else:
               self.context.trigger_calling_rooms = set(calling_rooms)
       
       # Exit Trigger C: Load sharing room now naturally calling
       naturally_calling = self._check_naturally_calling_rooms(calling_rooms)
       if naturally_calling:
           self._remove_rooms(naturally_calling, "naturally_calling")
           if not self.context.active_rooms:
               return "trigger_c_all_rooms_naturally_calling"
       
       return None
   
   def _deactivate(self, trigger: str, now: datetime):
       duration = self.context.activation_duration_minutes(now)
       self.ad.log(
           f"Load sharing DEACTIVATED: {trigger} (duration: {duration:.1f}min)",
           level="INFO"
       )
       self.context = LoadSharingContext(state=LoadSharingState.INACTIVE)
   ```
   - Centralized exit logic using state machine context
   - All three exit triggers (A/B/C) implemented
   - Minimum duration enforced via context helper

3. **Implement capacity adjustment in LoadCalculator**
   - Apply correction: `effective = rated × (valve_pct/100) × 1.2`
   - Cap at 100% (flow_efficiency can't exceed full capacity)
   - Used for selection decisions, not for display

4. **Update ValveCoordinator** (`controllers/valve_coordinator.py`)
   - Add `load_sharing_overrides` dict (similar to persistence_overrides)
   - Add `load_sharing_reason` string (for logging)
   - Priority: safety (persistence) > **load_sharing** > corrections > normal
   - Method: `set_load_sharing_overrides(overrides, reason)`
   - Update `apply_valve_command()` to check load_sharing before corrections:
     ```python
     # Priority 1: Persistence overrides (safety)
     if room_id in self.persistence_overrides:
         final_percent = self.persistence_overrides[room_id]
         reason = f"persistence: {self.persistence_reason}"
     # Priority 2: Load sharing overrides (NEW)
     elif room_id in self.load_sharing_overrides:
         final_percent = self.load_sharing_overrides[room_id]
         reason = f"load_sharing: {self.load_sharing_reason}"
     # Priority 3: Correction overrides
     elif room_id in self.trvs.unexpected_valve_positions:
         final_percent = self.trvs.unexpected_valve_positions[room_id]['expected']
         reason = "correction"
     ```

5. **Integrate into main recompute cycle** (`app.py`)
   ```python
   # In initialize():
   from load_sharing_manager import LoadSharingManager
   self.load_sharing = LoadSharingManager(
       self, self.config, self.scheduler, 
       self.load_calculator, self.cycling
   )
   
   # In recompute():
   # After room controller calculates normal valves
   # Before valve coordinator applies commands
   ls_overrides = self.load_sharing.evaluate(calling_rooms, room_data, now)
   if ls_overrides:
       self.valve_coordinator.set_load_sharing_overrides(ls_overrides, "load_sharing")
   else:
       self.valve_coordinator.clear_load_sharing_overrides()
   ```
   - **Verified:** RoomController does NOT trigger recompute on call_for_heat state changes
     - Recompute triggered by: sensor changes, mode changes, setpoint changes, timers, periodic (60s)
     - **In practice:** Temperature sensors trigger recompute, which causes call_for_heat evaluation
     - **Result:** Exit conditions respond within 60s maximum (typically faster via sensor triggers)
     - **Acceptable:** No additional trigger needed - 60s is adequate for load sharing exit response
   - **Input boolean check:** `LoadSharingManager.evaluate()` checks `input_boolean.pyheat_load_sharing_enable` first

6. **Add logging**
   - INFO: State changes only (activated, deactivated, state transitions)
   - INFO: Load sharing activated (trigger rooms, capacity, risk reason)
   - INFO: Load sharing deactivated (exit trigger: A/B/C, duration)
   - INFO: State transitions (TIER1_ACTIVE → TIER1_ESCALATED, etc.)
   - DEBUG: Continuous evaluation details (capacity calculations, tier evaluation)
   - DEBUG: Exit trigger evaluation details (checked every recompute)
   - WARN: Tier 3 activation (indicates schedule gap - consider improving schedules)
   - WARN: High false activation rate detected (via alert manager integration)

7. **Update status publisher**
   - Populate `sensor.pyheat_load_sharing_status` attributes
   - Add `load_sharing_active` flag to `sensor.pyheat_status`
   - Add `load_sharing_reason` to per-room attributes
   - Publish current state machine state (for debugging)

**Configuration Example:**
```yaml
# boiler.yaml
load_sharing:
  enabled: false  # AppDaemon-level enable (must be true for feature to work)
  # Note: Also requires input_boolean.pyheat_load_sharing_enable=on for runtime control
  
  # Entry conditions
  min_calling_capacity_w: 3500  # Capacity trigger
  cooldown_lookback_m: 15       # Recent cooldown window
  return_temp_risk_delta_c: 15  # High return temp = setpoint - X
  
  # Exit conditions
  sufficient_capacity_w: 4000   # Exit threshold when new caller joins
  min_activation_duration_m: 5  # Prevent rapid oscillation
  
  # Tier 1 configuration
  schedule_lookahead_m: 60
  tier_1_initial_pct: 70
  tier_1_escalated_pct: 80
  
  # Capacity correction
  flow_efficiency_factor: 1.2
```

#### Testing Protocol:
1. Deploy with `enabled: false` - verify no changes
2. Monitor baseline cycling frequency for 24-48 hours
3. **Test entry conditions:**
   - Wait for single room calling with low capacity
   - Verify load sharing does NOT activate (no cycling evidence)
   - Wait for cooldown event or high return temp
   - Verify load sharing activates correctly
4. **Test exit conditions:**
   - Exit Trigger A: Wait for original room to stop calling
   - Exit Trigger B: Have additional room start calling
   - Exit Trigger C: Let load sharing room reach natural calling threshold
5. Check logs for activation/deactivation reasons
6. Verify 5-minute minimum duration enforced
7. Analyze heating_logs/ CSV for capacity correlation
8. If issues: set `enabled: false` and investigate

**Success Criteria:**
- Load sharing does NOT activate for low capacity without cycling evidence
- Load sharing activates when cooldown occurs or return temp risk detected
- Load sharing persists until calling pattern changes (not premature exit)
- Exit Trigger A works: Deactivates when original room stops calling
- Exit Trigger B works: Deactivates when new room provides sufficient capacity
- Exit Trigger C works: Load sharing room transitions to natural control
- State machine transitions are correct and logged
- Context properties (tier1_rooms, tier2_rooms, etc.) are accurate
- No AppDaemon errors or crashes
- 5-minute minimum duration prevents oscillation

**Commit checkpoint:** "feat: Load sharing Tier 1 with state machine (schedule-aware pre-warming)"

---

### **PHASE 2: Extended Lookahead (Tier 2)** 🔍
*Goal: Add 2× window fallback for better coverage*

**Duration:** 2-3 hours  
**Risk:** LOW - Extension of proven Tier 1 logic  
**Rollback:** Disable Tier 2 via config flag (or set multiplier to 1.0)

#### Tasks:
1. **Extend state machine with Tier 2 cascade**
   ```python
   def _escalate_or_add_tier(self, room_data, now):
       if self.context.state == LoadSharingState.TIER1_ACTIVE:
           self._escalate_tier1(room_data, now)
       elif self.context.state == LoadSharingState.TIER1_ESCALATED:
           self._try_tier2(room_data, now)
       # ... other states
   
   def _escalate_tier1(self, room_data, now):
       for room in self.context.tier1_rooms.values():
           room.valve_pct = self.config.tier_1_escalated_pct
       self.context.state = LoadSharingState.TIER1_ESCALATED
       self.ad.log("State: TIER1_ACTIVE → TIER1_ESCALATED (80%)", level="INFO")
   
   def _try_tier2(self, room_data, now):
       tier2_selected = self._select_tier2_rooms(room_data, now)
       if tier2_selected:
           self.context.active_rooms.update(tier2_selected)
           self.context.state = LoadSharingState.TIER2_ACTIVE
           self.ad.log(f"State: TIER1_ESCALATED → TIER2_ACTIVE (+{len(tier2_selected)} rooms)", level="INFO")
       else:
           self._try_tier3(room_data, now)
   ```
   - State transitions are explicit and logged
   - Use `2 × schedule_lookahead_m` window
   - Initial valve percentage: 40%

2. **Enhance escalation logic**
   - Track tier in RoomActivation dataclass (already part of state machine)
   - Apply tier-specific escalation via state machine:
     - TIER1_ACTIVE → TIER1_ESCALATED (70% → 80%)
     - TIER2_ACTIVE → TIER2_ESCALATED (40% → 50%)
   - Only move to next tier after maximizing current tier

3. **Add per-room configuration support**
   - Parse `load_sharing.schedule_lookahead_m` from rooms.yaml
   - Fall back to global value if not specified
   - Validate range (15-180 minutes recommended)

3. **Update logging**
   - Distinguish Tier 1 vs Tier 2 selections in logs
   - Log: "Room {id} selected via Tier 2 (schedule in 90 min)"

4. **Add configuration**
   ```yaml
   # boiler.yaml
   load_sharing:
     extended_window_multiplier: 2.0  # Tier 2 = 2× Tier 1 window
     extended_window_valve_pct: 40    # Lower % for extended window
   ```

**Configuration Example:**
```yaml
# rooms.yaml
- id: lounge
  load_sharing:
    schedule_lookahead_m: 90  # Override global default
```

#### Testing Protocol:
1. Deploy during off-schedule period with no Tier 1 matches
2. Verify Tier 2 activates and selects rooms with later schedules
3. Monitor valve percentages (should be 40% not 70%)
4. Check rooms don't overheat from early pre-warming
5. Verify per-room overrides work correctly

**Success Criteria:**
- Tier 2 only activates when Tier 1 insufficient (state: TIER1_ESCALATED)
- State transition TIER1_ESCALATED → TIER2_ACTIVE is logged
- Extended window correctly calculates 2× per room
- Lower valve percentage prevents overheating
- Per-room configuration respected
- context.tier2_rooms property returns correct rooms

**Commit checkpoint:** "feat: Load sharing Tier 2 with state transitions (extended lookahead)"

---

### **PHASE 3: Fallback Priority (Tier 3)** 📋
*Goal: Deterministic fallback for schedule-free periods*

**Duration:** 2-3 hours  
**Risk:** LOW - Only activates when Tiers 1+2 fail  
**Rollback:** Disable via config or remove fallback_priority from all rooms

#### Tasks:
1. **Implement Tier 3 in state machine**
   ```python
   def _try_tier3(self, room_data, now):
       tier3_selected = self._select_tier3_rooms(room_data, now)
       if tier3_selected:
           self.context.active_rooms.update(tier3_selected)
           self.context.state = LoadSharingState.TIER3_ACTIVE
           self.ad.log(
               f"State: TIER2_ESCALATED → TIER3_ACTIVE (+{len(tier3_selected)} fallback rooms)",
               level="WARN"  # WARN because indicates schedule gap
           )
       else:
           self.ad.log(
               "Load sharing: All tiers exhausted, insufficient capacity. "
               "Accepting cycling as lesser evil.",
               level="INFO"
           )
   ```
   - Only runs if state is TIER2_ESCALATED
   - Process rooms in `fallback_priority` order (1, 2, 3, ...)
   - **No temperature check** - ultimate fallback accepts any "auto" mode room
     - Only exclusions: "off" mode, "manual" mode, already calling, no fallback_priority
   - Initial valve: 50%, escalate to 60% before adding more rooms
   - Stop when target capacity reached

2. **Add configuration parsing**
   - Parse `load_sharing.fallback_priority` from rooms.yaml
   - Optional field (None = excluded from Tier 3)
   - Validate: must be positive integer

3. **Add 15-minute timeout for Tier 3**
   - Already tracked in RoomActivation.activated_at
   - Use context.has_tier3_timeouts(now, timeout_m) helper
   - Checked in _evaluate_active() before exit conditions
   - Removes timed-out rooms, doesn't deactivate entire system

4. **Update logging**
   - Log: "Room {id} selected via Tier 3 (fallback priority {p})"
   - WARN: Tier 3 activated (indicates schedules need review)

**Configuration Example:**
```yaml
# rooms.yaml
- id: lounge
  load_sharing:
    fallback_priority: 1  # First choice

- id: games
  load_sharing:
    fallback_priority: 2  # Second choice

- id: pete
  # No fallback_priority = never used in Tier 3
```

#### Testing Protocol:
1. Test during weekend/off-schedule when no schedules within 120 min
2. Verify Tier 3 only activates with cycling risk evidence
3. Confirm Tier 3 activates in priority order
4. **Test edge case:** All fallback rooms in "off" or "manual" mode (should remain inactive)
5. Verify calling pattern change exits work correctly
6. Check rooms without fallback_priority excluded
7. Monitor for unwanted heating patterns
8. Test 15-minute timeout for Tier 3 rooms (if original room still calling)

**Success Criteria:**
- Tier 3 only activates when state is TIER2_ESCALATED AND cycling risk present
- Priority order respected (1 before 2 before 3)
- No temperature check - any "auto" mode room accepted
- State transition TIER2_ESCALATED → TIER3_ACTIVE is logged with WARN
- Calling pattern changes trigger appropriate exits
- 15-minute timeout only for Tier 3 fallback rooms
- context.tier3_rooms property returns correct rooms
- System always finds rooms when needed (unless all fallback rooms excluded by mode)

**Commit checkpoint:** "feat: Load sharing Tier 3 with state machine (fallback priority)"

---

### **PHASE 4: Optimization & Polish** ✨
*Goal: Hysteresis, minimum durations, intelligent valve percentages*

**Duration:** 3-4 hours  
**Risk:** LOW - Refinements to working system  
**Rollback:** Adjust parameters, not code changes

#### Tasks:
1. **Implement minimum activation duration (5 minutes)**
   - Already implemented via context.can_exit(now, min_duration_m)
   - Used in _check_exit_conditions() before any exit trigger
   - Prevents rapid oscillation

2. **Add capacity hysteresis**
   - Different thresholds for activation vs deactivation
   - Activation: < 3500W
   - Deactivation: >= 4200W (not 4000W)
   - Prevents oscillation near threshold

3. **Refine valve escalation logic**
   - Smooth progression through percentages via state transitions
   - State machine enforces correct order:
     - TIER1_ACTIVE (70%) → TIER1_ESCALATED (80%) → TIER2_ACTIVE
     - TIER2_ACTIVE (40%) → TIER2_ESCALATED (50%) → TIER3_ACTIVE  
     - TIER3_ACTIVE (50%) → TIER3_ESCALATED (60%)
   - All increments in 10% steps
   - Phase 4 adds max escalation: 90%, 60%, 70% (new states if needed)

4. **Enhanced status publishing**
   - Per-room attributes: `load_sharing_tier`, `load_sharing_reason`, `load_sharing_since`
   - System-level metrics: `state`, `tier_1_count`, `tier_2_count`, `tier_3_count`
   - State machine state published for debugging
   - Effectiveness: `cycling_events_prevented` (estimated)

5. **Performance metrics**
   - Track cycling frequency before/after
   - Calculate energy impact (estimated)
   - Log activation patterns for analysis

**Configuration Example:**
```yaml
# boiler.yaml
load_sharing:
  min_activation_duration_m: 5
  deactivation_hysteresis_w: 200  # Deactivate at trigger + 200W
  
  tier_1_valve_pct: 70
  tier_1_imminent_valve_pct: 100  # Within 15 min of schedule
  tier_2_valve_pct: 40
  tier_3_valve_pct: 50
  tier_3_max_duration_m: 15
```

#### Testing Protocol:
1. Monitor activation/deactivation patterns for oscillation
2. Verify minimum duration prevents rapid cycling
3. Verify calling pattern change exits work smoothly
4. Validate valve percentages appropriate for each tier
5. Review effectiveness metrics weekly
6. Test all three exit triggers (A/B/C) in real scenarios
7. Verify load sharing persists appropriately (not premature exit)

**Success Criteria:**
- No oscillation (rooms activating/deactivating rapidly)
- Exit logic works correctly for all three triggers
- State machine enforces correct escalation order
- Appropriate valve percentages for comfort
- Cycling frequency reduced measurably
- System stable over 1-week test period
- Load sharing persists for duration of calling pattern
- State transitions are logical and debuggable

**Commit checkpoint:** "feat: Load sharing optimization with state machine polish (hysteresis, durations, metrics)"

---

### **PHASE 5: Documentation & Tuning** 📚
*Goal: Production-ready documentation and tuning guidelines*

**Duration:** 2-3 hours  
**Risk:** NONE - Documentation only  

#### Tasks:
1. **Update ARCHITECTURE.md**
   - Add LoadSharingManager component description
   - Document state machine design (enum, dataclasses, context)
   - Document three-tier cascade logic with state transitions
   - Update data flow diagram
   - Explain integration with ValveCoordinator
   - Include state machine transition diagram

2. **Create configuration guide** (`docs/LOAD_SHARING.md`)
   - Explain each configuration parameter
   - Provide tuning guidelines for different scenarios
   - Include troubleshooting section
   - Real-world examples from testing

3. **Update changelog.md**
   - Document new feature thoroughly
   - Breaking changes: None
   - Configuration additions
   - Migration notes (all optional, defaults safe)

4. **Add examples to config files**
   - Comment out load_sharing examples in boiler.yaml
   - Add example configurations to rooms.yaml comments

5. **Create tuning guide**
   - How to analyze heating_logs/ for effectiveness
   - When to increase/decrease thresholds
   - Household-specific recommendations
   - Common issues and solutions

**Deliverables:**
- `docs/LOAD_SHARING.md` - Complete configuration guide
- Updated `docs/ARCHITECTURE.md`
- Updated `docs/changelog.md`
- Commented examples in config files
- Tuning guidelines based on real testing

**Commit checkpoint:** "docs: Load sharing complete documentation"

---

## Configuration Reference

### Global Configuration (boiler.yaml)

```yaml
boiler:
  # ... existing boiler config ...
  
  load_sharing:
    # Master enable/disable flag
    enabled: false  # Start disabled, enable after baseline monitoring
    
    # Entry conditions
    min_calling_capacity_w: 3500  # Trigger when calling capacity below this
    cooldown_lookback_m: 15       # Recent cooldown detection window
    return_temp_risk_delta_c: 15  # High return temp threshold (setpoint - X)
    
    # Exit conditions
    sufficient_capacity_w: 4000   # Exit when new caller brings capacity above this
    min_activation_duration_m: 5  # Prevent oscillation (enforced on all exits)
    
    # Capacity thresholds (watts)
    target_capacity_w: 4000       # Stop adding rooms when capacity reaches this
    deactivation_hysteresis_w: 200  # Deactivate at target + hysteresis
    
    # Tier 1: Schedule-aware pre-warming (10% increments)
    schedule_lookahead_m: 60      # Default lookahead window (minutes)
    tier_1_initial_pct: 70        # Initial valve opening
    tier_1_escalated_pct: 80      # Before adding Tier 2 rooms
    tier_1_max_pct: 90            # Maximum escalation (Phase 4)
    
    # Tier 2: Extended lookahead (10% increments)
    extended_window_multiplier: 2.0  # Multiply Tier 1 window by this
    tier_2_initial_pct: 40        # Gentler pre-warming
    tier_2_escalated_pct: 50      # Before adding Tier 3 rooms
    tier_2_max_pct: 60            # Maximum escalation (Phase 4)
    
    # Tier 3: Fallback priority (10% increments)
    tier_3_initial_pct: 50        # Compromise valve opening
    tier_3_escalated_pct: 60      # Before adding more fallback rooms
    tier_3_max_pct: 70            # Maximum escalation (Phase 4)
    tier_3_max_duration_m: 15     # Maximum time before forced deactivation
    
    # Capacity correction for valve opening
    flow_efficiency_factor: 1.2   # High pump pressure compensation
    # Effective capacity = rated × (valve_pct/100) × this factor (capped at 100%)
    
    # Oscillation prevention
    min_activation_duration_m: 5  # Minimum time room stays active
```

### Per-Room Configuration (rooms.yaml)

```yaml
rooms:
  - id: lounge
    name: "Living Room"
    delta_t50: 2290  # Required for load sharing
    # ... existing room config ...
    
    load_sharing:
      # Optional: Override global lookahead window
      schedule_lookahead_m: 90  # This room looks 90 min ahead
      
      # Optional: Fallback priority (lower = higher priority)
      fallback_priority: 1  # First choice when schedules don't help
      # Omit fallback_priority to exclude room from Tier 3
```

---

## Risk Mitigation Strategy

### Per-Phase Safeguards

**Phase 0:** Pure infrastructure, no behavioral changes possible  
**Phase 1:** Master enable flag, start disabled, conservative defaults  
**Phase 2:** Extends working Tier 1, lower valve % reduces risk  
**Phase 3:** Only activates as last resort, 15-min timeout prevents abuse  
**Phase 4:** Refinements to stable system, all parameters configurable  

### Monitoring & Rollback

**Continuous Monitoring:**
- Watch `sensor.pyheat_load_sharing_status` for unexpected activations
- Check AppDaemon logs for errors or warnings
- Analyze `heating_logs/*.csv` for capacity correlation
- Track cycling protection cooldown frequency

**Immediate Rollback Triggers:**
- Rooms heating unexpectedly (not near schedule or target)
- Oscillation detected (rapid activation/deactivation)
- AppDaemon errors or crashes
- User complaints about comfort

**Rollback Procedure:**
1. Set `enabled: false` in boiler.yaml (takes effect next recompute, ~60s)
2. No restart required (feature gracefully disables)
3. Investigate logs and heating data
4. Fix issue and re-enable cautiously

### Testing Best Practices

1. **Baseline First:** Monitor 24-48 hours with `enabled: false`
2. **Enable During Low Load:** First enable during mild weather
3. **Monitor Closely:** Check logs every few hours initially
4. **Gradual Confidence:** Run each phase 48 hours before next
5. **Document Everything:** Log all configuration changes and observations

---

## Success Metrics

### Primary Goals
- ✅ Reduce cycling protection cooldown events by 50%+
- ✅ No unwanted heating (rooms not near schedule/target)
- ✅ System stability (no crashes or errors)

### Secondary Goals
- ✅ Improved comfort (rooms pre-warmed before schedule)
- ✅ Load sharing activates appropriately (not too aggressive)
- ✅ Energy efficiency maintained or improved

### Measurement Approach
1. **Before:** Count cycling cooldown events per day (baseline)
2. **After:** Count events with load sharing enabled
3. **Compare:** Calculate percentage reduction
4. **Energy:** Compare daily boiler runtime (heating_logs analysis)
5. **Comfort:** User subjective feedback

---

## Final Implementation Summary

### Proposal Status: ✅ READY FOR IMPLEMENTATION

**Date:** 26 November 2025  
**Version:** 2.0 (Refined with architectural integration)  
**Approval:** Design validated, conflicts verified clear, all details resolved

### Key Design Points

1. **Modular Architecture:** Two new files (`load_sharing_state.py`, `load_sharing_manager.py`) + 6 file modifications
2. **User Control:** `input_boolean.pyheat_load_sharing_enable` provides runtime on/off switch
3. **Priority System:** Load sharing inserts cleanly between persistence (safety) and corrections
4. **Zero Conflicts:** Verified compatible with all existing PyHeat systems
5. **Phased Rollout:** 6 phases (0-5) with clear checkpoints and rollback procedures
6. **State Machine:** Explicit states, transitions, and single source of truth (LoadSharingContext)
7. **Three-Tier Cascade:** Schedule-aware (Tier 1) → Extended window (Tier 2) → Fallback priority (Tier 3)
8. **Exit Conditions:** Three triggers (A/B/C) based on calling pattern changes, not arbitrary timers
9. **Graceful Degradation:** Skip failures, log warnings, continue operation
10. **Energy Conscious:** Prioritizes schedule-aligned pre-warming over wasteful fallback heating

### Implementation Readiness Checklist

- ✅ Technical feasibility: HIGH (all dependencies available)
- ✅ Algorithm soundness: HIGH (proven approaches, well-mitigated risks)
- ✅ Integration risk: NONE (zero conflicts identified and verified)
- ✅ Configuration complexity: MODERATE (sensible defaults, optional tuning)
- ✅ Performance impact: LOW (negligible computational overhead)
- ✅ Maintenance burden: MODERATE (standard patterns, clear responsibilities)
- ✅ Documentation plan: COMPLETE (Phase 5 deliverables defined)
- ✅ Testing protocols: COMPLETE (per-phase success criteria)
- ✅ Rollback procedures: CLEAR (config flag, no restart required)
- ✅ Internal consistency: VERIFIED (state machine, entry/exit, valve control)

### Risks & Mitigations

**Low Risks (Mitigated):**
- Over-pre-warming → Lower valve % for extended window, per-room lookahead tuning
- Oscillation → 5-minute minimum duration, capacity hysteresis
- Capacity inaccuracy → Exit triggers handle gracefully, alert manager monitoring
- Schedule changes → 60s recompute catches changes, 15-minute Tier 3 timeout

**Negligible Risks:**
- Conflicts with existing systems → Verified none exist
- Performance impact → Negligible (O(n) per room, n~6)
- System stability → Graceful degradation, follows established patterns

### Go/No-Go Decision

**Recommendation:** ✅ **GO - PROCEED WITH PHASE 0**

**Rationale:**
1. All technical prerequisites verified available
2. Zero conflicts with existing systems (comprehensively checked)
3. All implementation details resolved (no open questions)
4. Internal consistency verified (state machine, logic, configuration)
5. Phased approach minimizes risk (each phase independently testable)
6. Clear rollback path (config flag, no code removal required)
7. Follows PyHeat architectural patterns (manager/controller, state machine, YAML config)
8. Positive expected outcome (reduced cycling, minimal energy cost, improved comfort)

**Next Action:** Begin Phase 0 - Infrastructure Preparation (2-3 hours, minimal risk)

---

## Design Review Summary (26 Nov 2025)

### Validated Design Decisions

The following design elements were reviewed and confirmed as appropriate:

1. ✅ **Exit Trigger C - Natural Calling Transition**
   - When load sharing room reaches natural calling threshold → transition to normal hysteresis immediately
   - Room controller takes over since room legitimately needs heat

2. ✅ **Valve Escalation Timing**
   - Escalate immediately based on capacity calculations (no waiting period)
   - Faster response, eliminates unnecessary delay

3. ✅ **TRV Interlock Interaction**
   - Load sharing rooms **count toward** interlock threshold (>50% valve = interlock risk)
   - Intentional - interlock is a safety feature and shouldn't be bypassed
   - Documented explicitly in code and configuration

4. ✅ **Tier 3 No Temperature Check**
   - Tier 3 is ultimate fallback - accepts any room in "auto" mode regardless of temperature
   - Rationale: Preventing boiler cycling takes priority over minor energy waste
   - Only mode restriction: "off" and "manual" modes excluded (respects user intent)

5. ✅ **Capacity Calculation Accuracy (±20-30%)**
   - False activations may occur when actual capacity is sufficient
   - Mitigation: Exit triggers handle this gracefully (deactivates when calling pattern changes)
   - **Monitoring:** Alert manager integration to log WARNING if false activation rate >20%
   - Allows operator to notice and address systematic inaccuracy

6. ✅ **Schedule Changes Mid-Pre-warming**
   - 60s recompute cycle catches changes relatively quickly (acceptable)
   - No additional UI controls needed at this stage

7. ✅ **Flow Efficiency Factor (1.2)**
   - Starting estimate to be used in Phase 1
   - **Requires validation** against real-world cycling correlation
   - If cycling persists with "sufficient" calculated capacity → reduce factor
   - Document tuning results in configuration

8. ✅ **Logging Verbosity**
   - INFO: State changes only (activated, deactivated, room added/removed, tier changes)
   - DEBUG: Continuous evaluation details (prevents log spam)
   - WARN: Tier 3 activation (indicates schedule gap)
   - WARN: High false activation rate (via alert manager)

9. ✅ **Recompute Triggers**
   - **Verified:** RoomController does NOT trigger recompute on call_for_heat state changes
   - Current triggers: sensor changes, mode changes, setpoint changes, timers, periodic (60s)
   - **Analysis:** Temperature sensors trigger recompute → call_for_heat evaluated → exit conditions detected
   - **Worst case:** 60s delay if calling state changes without sensor update (rare edge case)
   - **Conclusion:** No additional trigger needed - existing system is adequate for load sharing exits

10. ✅ **State Machine Architecture**
   - Explicit state management prevents inconsistent internal state
   - Single source of truth: LoadSharingContext dataclass
   - Impossible states are impossible (enforced by enum and transitions)
   - Each state has clear responsibilities and valid transitions
   - Computed properties ensure data consistency (tier1_rooms, tier2_rooms, tier3_rooms)
   - Debuggable: State transitions logged, context can be dumped for inspection

---

## Pre-Implementation Checklist

### Code Verification ✅ COMPLETE

**Existing System Compatibility:**
- ✅ ValveCoordinator uses 3-tier priority: persistence > corrections > normal (confirmed)
- ✅ No other system currently uses 4th priority tier (slot available for load_sharing)
- ✅ Persistence overrides managed via `set_persistence_overrides()` and `clear_persistence_overrides()` (pattern to follow)
- ✅ Boiler state machine independent (doesn't need modification)
- ✅ RoomController doesn't trigger recomputes on call_for_heat changes (verified - no new trigger needed)
- ✅ Recompute triggers: sensors, modes, setpoints, timers, periodic 60s (adequate for exit conditions)
- ✅ CyclingProtection tracks cooldown history (can add `last_cooldown_within()` query method)
- ✅ LoadCalculator has capacity estimation (can add valve adjustment method)
- ✅ Scheduler parses schedules (can add lookahead query method)
- ✅ Master enable OFF handled correctly (all systems respect it)

**Conflict Analysis:**
- ✅ No priority conflicts (load_sharing inserts cleanly between persistence and corrections)
- ✅ No state machine conflicts (independent evaluation)
- ✅ No timing conflicts (uses existing recompute cycle)
- ✅ No entity conflicts (new entities don't overlap)
- ✅ No configuration conflicts (new sections don't overlap)

**Architectural Compliance:**
- ✅ Follows manager/controller pattern (LoadSharingManager in `managers/`)
- ✅ Uses dataclasses for state (LoadSharingContext, RoomActivation)
- ✅ State machine with explicit transitions (established pattern)
- ✅ Configuration via YAML (standard approach)
- ✅ Priority-based overrides (ValveCoordinator pattern)
- ✅ Graceful degradation (skip failures, continue)

### Design Decisions ✅ RESOLVED

**All implementation details resolved:**
1. ✅ Schedule API: Return `(start, end, target)` or `None` (single next block)
2. ✅ Capacity calculation: Per-candidate during selection (accurate)
3. ✅ State persistence: `input_text.pyheat_load_sharing_context` JSON entity
4. ✅ Logging: INFO=state changes, DEBUG=evaluation, WARN=Tier 3
5. ✅ HA exposure: System-level sensor + per-room attributes
6. ✅ Error handling: Skip room, log warning, continue (graceful)
7. ✅ Config validation: Exclude room, log warning (graceful)
8. ✅ User control: `input_boolean.pyheat_load_sharing_enable` (master switch)
9. ✅ Priority insertion: Between persistence and corrections (explicit)
10. ✅ Cycling query: Add `last_cooldown_within(minutes)` to CyclingProtection

**Configuration approach:**
- ✅ Two-level enable: `boiler.yaml` `enabled: false` AND `input_boolean.pyheat_load_sharing_enable: off`
- ✅ Per-room optional: `schedule_lookahead_m`, `fallback_priority` (sensible defaults)
- ✅ Global thresholds: All configurable in `boiler.yaml` (tunable)
- ✅ Validation: Warn on issues, exclude room, continue (graceful)

### Internal Consistency ✅ VERIFIED

**State Machine:**
- ✅ All states defined: DISABLED, INACTIVE, TIER1_ACTIVE, TIER1_ESCALATED, etc.
- ✅ Transitions explicit and logged (debugging friendly)
- ✅ Context is single source of truth (no duplicate state)
- ✅ Helper methods enforce invariants (tier1_rooms, tier2_rooms, tier3_rooms)
- ✅ Exit conditions checked first (prevents spurious escalation)

**Entry/Exit Logic:**
- ✅ Entry requires: low capacity AND cycling risk (both conditions)
- ✅ Exit Trigger A: Original rooms stopped (check trigger set)
- ✅ Exit Trigger B: New caller with sufficient capacity (recalculate)
- ✅ Exit Trigger C: Load sharing room now naturally calling (transition)
- ✅ Minimum duration enforced (5 minutes, prevents oscillation)
- ✅ Tier 3 timeout separate (15 minutes, room-specific)

**Tier Selection:**
- ✅ Tier 1: Schedule within lookahead (per-room configurable)
- ✅ Tier 2: Schedule within 2× lookahead (extended window)
- ✅ Tier 3: Fallback priority list (deterministic)
- ✅ Escalation before adding (maximize existing before new)
- ✅ Mode filtering: Only "auto" mode (respects user intent)
- ✅ Capacity tracking: Valve adjustment applied (flow_efficiency)

**Valve Control:**
- ✅ Tier 1: 70% initial, 80% escalated (active pre-warming)
- ✅ Tier 2: 40% initial, 50% escalated (gentle pre-warming)
- ✅ Tier 3: 50% initial, 60% escalated (fallback)
- ✅ All in 10% increments (consistent)
- ✅ Priority: persistence > load_sharing > corrections > normal (clear)
- ✅ Counts toward interlock (intentional - safety not bypassed)

### Ready for Phase 0 ✅ YES

**All prerequisites met:**
- ✅ Design is sound and comprehensive
- ✅ No conflicts with existing systems
- ✅ All implementation details resolved
- ✅ Internal consistency verified
- ✅ Phased rollout plan ready
- ✅ Documentation structure defined
- ✅ Testing protocols established
- ✅ Rollback procedures clear

**Next steps:**
1. Create `managers/load_sharing_state.py` (enums, dataclasses)
2. Create `managers/load_sharing_manager.py` (skeleton)
3. Update `core/config_loader.py` (parse new sections)
4. Update `ha_yaml/pyheat_package.yaml` (add entities)
5. Extend `core/scheduler.py` (add lookahead API)
6. Test Phase 0 (no behavioral changes)
7. Commit: "feat: Load sharing infrastructure (disabled)"

---

## Open Questions (Post-Implementation Tuning)

### Post-Phase-1 Tuning:
1. Default schedule_lookahead_m: 60 min optimal or adjust?
2. Capacity thresholds: 3500/4000W correct for your system?
3. Valve percentages: 70%/80% appropriate or too aggressive?
4. Flow efficiency factor: 1.2 accurate based on observed behavior?
5. Escalation strategy: Does prioritizing higher % on fewer rooms work better?
6. Minimum activation: 5 minutes sufficient?

### Post-Phase-4 Analysis:
1. Effectiveness: Cycling frequency reduction achieved?
2. Energy impact: Net positive or negative?
3. User comfort: Acceptable or adjustments needed?
4. Configuration complexity: Needs simplification?

