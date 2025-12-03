# Load Sharing: Logic Reference

This document provides a definitive reference for how the load sharing system works, including room selection logic, activation conditions, and exit criteria.

---

## Purpose

Load sharing prevents boiler short-cycling when primary calling rooms have insufficient radiator capacity to dissipate the boiler's minimum output. It intelligently opens additional room valves to create more heat sink capacity.

---

## Master Enable Control

Load sharing is controlled by a single Home Assistant input boolean:

```
input_boolean.pyheat_load_sharing_enable
```

- **ON**: Load sharing will evaluate and activate when conditions are met
- **OFF**: Load sharing is disabled; any active load sharing immediately deactivates

---

## Entry Conditions (Activation)

Load sharing activates when **ALL** of the following conditions are true:

### 1. Low Calling Capacity

The total capacity of all currently-calling rooms must be below `min_calling_capacity_w` (default: 2000W).

```
total_calling_capacity < min_calling_capacity_w
```

**How capacity is calculated:**
- Each room's capacity comes from `LoadCalculator.get_all_estimated_capacities()`
- Based on `delta_t50` ratings from room config and current temperature differential
- Only rooms with `calling = True` are summed

### 2. Cycling Risk Present

At least ONE of the following must be true:

#### A. Cycling Protection Active
```
cycling_protection_state == 'COOLDOWN'
```
The cycling protection system has detected recent short-cycling and is in cooldown.

#### B. High Return Temperature
```
return_temp >= (setpoint - high_return_delta_c)
```

Where:
- `return_temp` = Current boiler return water temperature (`sensor.opentherm_heating_return_water_temp`)
- `setpoint` = Current heating setpoint (`input_number.pyheat_opentherm_setpoint`)
- `high_return_delta_c` = Configured threshold (default: 6°C)

**Example:** If setpoint is 70°C and `high_return_delta_c` is 6, load sharing triggers when return temp >= 64°C.

**Rationale:** High return temperature indicates the radiators are saturated and cannot dissipate heat effectively, making cycling imminent.

---

## Room Selection: Three-Tier Cascade

When entry conditions are met, rooms are selected using a cascading three-tier strategy. The system progresses through tiers until target capacity is reached.

### Target Capacity Goal

The system aims to reach `target_capacity_w` (default: 2500W) of total system capacity (calling rooms + load sharing rooms).

### Tier 1: Schedule-Aware Pre-Warming (Primary)

**Selection Criteria - ALL must be true:**
- Room is in `auto` mode (not manual or off)
- Room is NOT currently calling for heat
- Room has a scheduled heating block within `schedule_lookahead_m` minutes (default: 60)
- The scheduled target temperature is higher than current room temperature
- Block must have `target > default_target` (indicates heating is needed)
- **Both active and passive schedule blocks are considered** - any upcoming block that needs heat is eligible

**Sorting:** Rooms sorted by need (scheduled_target - current_temp) descending. Neediest rooms selected first.

**Initial Valve:** 70%

**Escalation:** Tier 1 rooms escalate by 10% increments **up to 100%** before moving to Tier 2. A room with an upcoming schedule at 100% is preferred over adding Tier 2/3 rooms.

**Schedule Block Detection:**
```python
next_block = scheduler.get_next_schedule_block(room_id, now, within_minutes=60)
# Returns: (start_time, end_time, target_temp, block_mode) or None
# block_mode is 'active' or 'passive' - both are eligible for pre-warming
```

### Tier 2: Extended Lookahead (Secondary)

Only evaluated if Tier 1 (including **full escalation to 100%**) is insufficient.

**Selection Criteria:** Same as Tier 1, but with 2x the lookahead window:
- If room's `schedule_lookahead_m` is 60, searches within 120 minutes
- If room's `schedule_lookahead_m` is 90, searches within 180 minutes
- **Both active and passive schedule blocks are considered**

**Initial Valve:** 40% (gentler pre-warming for more distant schedules)

**Escalation:** Tier 2 rooms escalate by 10% increments **up to 100%** before moving to Tier 3.

### Tier 3: Fallback Priority (Tertiary)

Only evaluated if Tier 1+2 (including **full escalation to 100%**) are insufficient.

Tier 3 has two phases:

#### Phase A: Passive Rooms (at max_temp)

**Selection Criteria:**
- Room's current `operating_mode == 'passive'` (actively in passive mode right now)
- Room is NOT calling for heat
- Current temperature < max_temp (room can still accept heat)

**Initial Valve:** 50%

**Target Temperature:** Room's current max_temp

**Rationale:** Passive rooms are already configured to accept opportunistic heating up to their max_temp.

#### Phase B: Fallback Priority List (including passive rooms at comfort target)

If Phase A provides no rooms or insufficient capacity:

**Selection Criteria - ALL must be true:**
- Room is in `auto` mode
- Room is NOT currently calling for heat
- Room is NOT already in Tier 1 or Tier 2
- Room has `fallback_priority` configured in rooms.yaml
- Room is NOT in timeout cooldown (see Tier 3 Timeout below)

**Includes passive rooms:** Passive rooms with `fallback_priority` configured are **re-considered** in Phase B. This allows passive rooms to be heated to `tier3_comfort_target_c` (e.g., 20°C) which may be significantly higher than their normal max_temp. This provides more heat sink capacity when needed.

**NO temperature check** - This is the ultimate fallback. Any eligible room is accepted regardless of whether it's above target temperature.

**Sorting:** Rooms sorted by `fallback_priority` ascending. Lower number = higher priority (1 selected before 2).

**Selection Strategy:** Maximize existing rooms before adding new ones:
1. Add ONE room at initial 50%
2. Escalate that room (50% -> 60% -> 70% -> 80% -> 90% -> 100%) before adding another
3. Only add next priority room when current room is at 100%

**Target Temperature:** Uses `tier3_comfort_target_c` (default: 20°C) for **all Phase B rooms** (including passive rooms reconsidered here). This bypasses low parking temperatures and passive max_temps.

---

## Valve Percentages Summary

| Tier | Initial | Max | Escalation | Notes |
|------|---------|-----|------------|-------|
| Tier 1 | 70% | 100% | +10% per step | Schedule-aware pre-warming, fully exhaust before Tier 2 |
| Tier 2 | 40% | 100% | +10% per step | Extended lookahead (gentle), fully exhaust before Tier 3 |
| Tier 3 | 50% | 100% | +10% per step | Escalate existing rooms before adding new ones |

**Key principle:** A room that will want heat soon (Tier 1/2) at 100% valve is better than opening a room that doesn't want heat (Tier 3). Exhaust each tier completely before moving to the next.

---

## Exit Conditions (Deactivation)

Load sharing evaluates exit conditions on every recompute cycle (typically every 60 seconds).

### Minimum Activation Duration

All exit triggers (except Trigger B) are blocked until the minimum activation duration has elapsed:

```
min_activation_duration_s: 60  # default, configurable
```

This prevents rapid oscillation.

### Exit Trigger A: Original Calling Rooms Stopped

If **none** of the rooms that originally triggered load sharing are still calling:

```python
trigger_still_calling = trigger_calling_rooms & current_calling
if not trigger_still_calling:
    # DEACTIVATE
```

**Rationale:** The original need for load sharing is gone.

### Exit Trigger B: Additional Rooms Started Calling (Bypasses Minimum Duration)

If new rooms join the calling set AND total capacity now meets target:

```python
new_calling = current_calling - trigger_calling_rooms
if new_calling:
    new_total_capacity = calculate_capacity(current_calling)
    if new_total_capacity >= target_capacity_w:
        # DEACTIVATE (bypasses minimum duration!)
```

**Why it bypasses minimum duration:** This exit represents the fundamental problem being solved - insufficient capacity. If natural calling provides sufficient capacity, load sharing is no longer needed immediately.

If new rooms join but capacity is still insufficient, the trigger set is updated and load sharing continues.

### Exit Trigger C: Load Sharing Room Now Naturally Calling

If a load sharing room transitions to naturally calling (reaches its own heat demand):

```python
if room_states.get(room_id, {}).get('calling', False):
    # Remove from load sharing (room controller takes over)
```

The room is removed from load sharing control. If no load sharing rooms remain, the system deactivates.

### Exit Trigger D: Tier 3 Timeout

Tier 3 fallback rooms have a maximum activation duration:

```
tier3_timeout_s: 180  # default, configurable
```

When a Tier 3 room exceeds this timeout:
1. Room is removed from load sharing
2. Room enters cooldown period (`tier3_cooldown_s`, default: 300s)
3. Room cannot be re-selected until cooldown expires

If all Tier 3 rooms time out and no other rooms remain, load sharing deactivates.

**Rationale:** Prevents long-term unwanted heating of fallback rooms.

### Exit Trigger E: Room Reached Target Temperature

If a load sharing room reaches/exceeds its pre-warming target:

```python
off_delta = room_config.hysteresis.off_delta_c  # typically 0.3
if temp >= activation.target_temp + off_delta:
    # Remove room (prevents overshoot)
```

Uses the same hysteresis logic as normal heating control.

### Exit Trigger F: Room Mode Changed from Auto

If a room's mode changes away from `auto`:

```python
if state.get('mode') != 'auto':
    # Remove room (respects user intent)
```

---

## State Machine States

The load sharing system uses an explicit state machine:

| State | Description |
|-------|-------------|
| `DISABLED` | Master enable is OFF |
| `INACTIVE` | Monitoring but not active (entry conditions not met) |
| `TIER1_ACTIVE` | Tier 1 rooms active at initial valve % |
| `TIER1_ESCALATED` | Tier 1 rooms escalated to higher valve % |
| `TIER2_ACTIVE` | Tier 1+2 rooms active |
| `TIER2_ESCALATED` | Tier 2 rooms escalated |
| `TIER3_ACTIVE` | Tier 1+2+3 rooms active |
| `TIER3_ESCALATED` | Tier 3 rooms escalated |

---

## Configuration Reference

### boiler.yaml

```yaml
boiler:
  load_sharing:
    # Entry thresholds
    min_calling_capacity_w: 2000     # Activate when below this
    target_capacity_w: 2500          # Stop adding rooms at this
    high_return_delta_c: 6           # Return temp risk threshold
    
    # Timing
    min_activation_duration_s: 60    # Minimum active time
    tier3_timeout_s: 180             # Max time for Tier 3 rooms
    tier3_cooldown_s: 300            # Cooldown before Tier 3 re-eligible
    
    # Tier 3 target
    tier3_comfort_target_c: 20.0     # Pre-warming target for fallback rooms
```

### rooms.yaml (per-room, optional)

```yaml
rooms:
  - id: lounge
    load_sharing:
      schedule_lookahead_m: 90       # Override default 60 minutes
      fallback_priority: 1           # First choice for Tier 3 (lower = higher priority)
```

Rooms without `fallback_priority` are excluded from Tier 3 Phase B selection.

---

## Capacity Calculation

Load sharing rooms contribute effective capacity based on their valve opening:

```python
effective_capacity = room_capacity * (valve_pct / 100.0)
```

Where `room_capacity` is the estimated capacity from `LoadCalculator` (based on `delta_t50` and temperature differential).

---

## Priority in Valve Coordinator

Load sharing commands are applied with this priority order:

1. **Persistence** (safety: pump overrun, interlock)
2. **Load sharing** (this system)
3. **Corrections** (TRV position corrections)
4. **Normal** (room heating logic)

Safety always wins. Load sharing can be overridden by persistence but overrides normal heating commands.

---

## Key Behaviors Summary

| Scenario | Behavior |
|----------|----------|
| Low capacity but no cycling risk | Load sharing does NOT activate |
| Master enable turned OFF | Immediate deactivation |
| Tier 1 room reaches schedule time | Transitions to normal control |
| Tier 3 room exceeds timeout | Removed, enters cooldown |
| All original calling rooms stop | Load sharing deactivates |
| New room starts calling with sufficient capacity | Immediate deactivation |
| Room mode changed to manual/off | Removed from load sharing |

---

## Logging

- **INFO**: State changes (activated, deactivated, tier transitions)
- **DEBUG**: Evaluation details (capacity calculations, candidate selection)
- **WARNING**: Tier 3 activation (indicates schedule gap - consider improving schedules)

---

## Troubleshooting

### Load sharing not activating

1. Check master enable: `input_boolean.pyheat_load_sharing_enable` must be ON
2. Verify capacity threshold: Is calling capacity actually below `min_calling_capacity_w`?
3. Check cycling risk: Is cycling protection in COOLDOWN or return temp high enough?

### Load sharing activating too aggressively

1. Lower `high_return_delta_c` (requires higher return temp to trigger)
2. Increase `min_calling_capacity_w` (allows more capacity before triggering)

### Tier 3 rooms heating unnecessarily

1. Increase `tier3_timeout_s` to allow shorter activations
2. Remove `fallback_priority` from rooms you don't want used as fallback
3. Improve schedules so Tier 1/2 provide sufficient coverage

### Oscillation (rapid activate/deactivate)

1. Increase `min_activation_duration_s`
2. Check for sensor noise causing capacity fluctuations
