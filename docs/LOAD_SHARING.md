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

The total capacity of all currently-calling rooms must be below `min_calling_capacity_w` (default: 3500W).

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
- `return_temp` = Current boiler return water temperature (`sensor.opentherm_heating_return_temp`)
- `setpoint` = Current heating setpoint (`input_number.pyheat_opentherm_setpoint`)
- `high_return_delta_c` = Configured threshold (required in boiler.yaml)

**Example:** If setpoint is 70°C and `high_return_delta_c` is 15, load sharing triggers when return temp >= 55°C.

**Rationale:** High return temperature indicates the radiators are saturated and cannot dissipate heat effectively, making cycling imminent.

---

## Room Selection: Two-Tier Cascade

When entry conditions are met, rooms are selected using a two-tier cascade with **one-room-at-a-time** escalation. Each room is escalated to 100% before adding the next room.

### Target Capacity Goal

The system aims to reach `target_capacity_w` (default: 4000W) of total system capacity (calling rooms + load sharing rooms).

---

### Tier 1: Schedule-Aware Pre-Warming (Primary)

**Selection Criteria - ALL must be true:**
- Room is in `auto` mode (not manual or off)
- Room is NOT currently calling for heat
- Room has a scheduled heating block within effective lookahead window
- The scheduled target temperature is higher than current room temperature
- **Both active and passive schedule blocks are considered**

**Lookahead Window:**
```
effective_lookahead = schedule_lookahead_m × 2
```
Default: 60 × 2 = 120 minutes. Per-room overrides apply to `schedule_lookahead_m`.

**Sorting:** Rooms sorted by **closest schedule first** (minutes_until ascending). This ensures rooms that need heat soonest are selected first.

**Initial Valve:** 50%

**One-at-a-Time Escalation:**
1. Add first schedule room at 50%
2. Escalate that room: 50% -> 60% -> 70% -> 80% -> 90% -> 100%
3. Check capacity at each step - stop if target reached
4. Only add next room when current room is at 100% and still insufficient

This minimizes the number of rooms involved.

---

### Tier 2: Fallback (Secondary)

Only evaluated if schedule-aware rooms (including **full escalation to 100%**) are insufficient.

Tier 2 has two phases:

#### Phase A: Passive Rooms (at max_temp)

**Selection Criteria:**
- Room's current `operating_mode == 'passive'` (actively in passive mode right now)
- Room is NOT calling for heat
- Current temperature < max_temp (room can still accept heat)

**Initial Valve:** 50%

**Target Temperature:** Room's current max_temp

**Rationale:** Passive rooms are already configured to accept opportunistic heating up to their max_temp.

#### Phase B: Fallback Priority List (passive rooms prioritized)

If Phase A provides no rooms or insufficient capacity:

**Selection Criteria - ALL must be true:**
- Room is in `auto` mode
- Room is NOT currently calling for heat
- Room is NOT already in schedule tier
- Room has `fallback_priority` configured in rooms.yaml
- Room is NOT in timeout cooldown (see Fallback Timeout below)

**Includes passive rooms:** Passive rooms with `fallback_priority` configured are **re-considered** in Phase B. This allows passive rooms to be heated to `fallback_comfort_target_c` (e.g., 20°C) which may be significantly higher than their normal max_temp.

**NO temperature check** - This is the ultimate fallback. Any eligible room is accepted.

**Selection Order:**
1. **Passive rooms** with `fallback_priority` (sorted by priority ascending)
2. **Non-passive rooms** with `fallback_priority` (sorted by priority ascending)

**Rationale for passive-first ordering:** In Phase B (emergency heat dumping), passive mode indicates user acceptance of opportunistic heating, making these rooms less intrusive choices than active rooms that may already be satisfied. Passive rooms are heated to comfort target regardless of their current max_temp.

**One-at-a-Time Escalation:** Same as schedule tier - add one room at 50%, escalate to 100% before adding another.

**Target Temperature:** Uses `fallback_comfort_target_c` (default: 20°C) for **all Phase B rooms**.

**WARNING Level Logging:** Fallback activation indicates a schedule gap that should be addressed.

---

## Valve Percentages Summary

| Tier | Initial | Max | Escalation | Strategy |
|------|---------|-----|------------|----------|
| Schedule | 50% | 100% | +10% per step | Closest schedule first, one-at-a-time |
| Fallback | 50% | 100% | +10% per step | Priority order, one-at-a-time |

**Key principle:** Minimize rooms involved. Escalate each room to 100% before adding another.

---

## Exit Conditions (Deactivation)

Load sharing evaluates exit conditions on every recompute cycle (typically every 60 seconds).

### Minimum Activation Duration

All exit triggers (except Trigger B) are blocked until the minimum activation duration has elapsed:

```
min_activation_duration_s: 300  # default (5 minutes), configurable
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

**Why it bypasses minimum duration:** This represents the fundamental problem being solved. If natural calling provides sufficient capacity, load sharing is no longer needed immediately.

### Exit Trigger C: Load Sharing Room Now Naturally Calling

If a load sharing room transitions to naturally calling:

```python
if room_states.get(room_id, {}).get('calling', False):
    # Remove from load sharing (room controller takes over)
```

If no load sharing rooms remain, the system deactivates.

### Exit Trigger D: Fallback Timeout

Fallback rooms have a maximum activation duration:

```
fallback_timeout_s: 900  # default (15 minutes), configurable
```

When a fallback room exceeds this timeout:
1. Room is removed from load sharing
2. Room enters cooldown period (`fallback_cooldown_s`, default: 1800s)
3. Room cannot be re-selected until cooldown expires

**Rationale:** Prevents long-term unwanted heating of fallback rooms.

### Exit Trigger E: Room Reached Target Temperature

If a load sharing room reaches/exceeds its pre-warming target:

```python
off_delta = room_config.hysteresis.off_delta_c  # typically 0.1
if temp >= activation.target_temp + off_delta:
    # Remove room (prevents overshoot)
```

### Exit Trigger F: Room Mode Changed from Auto

If a room's mode changes away from `auto`:

```python
if state.get('mode') != 'auto':
    # Remove room (respects user intent)
```

---

## State Machine States

| State | Description |
|-------|-------------|
| `DISABLED` | Master enable is OFF |
| `INACTIVE` | Monitoring but not active |
| `SCHEDULE_ACTIVE` | Schedule-aware rooms active at initial valve % |
| `SCHEDULE_ESCALATED` | Schedule rooms escalated above initial % |
| `FALLBACK_ACTIVE` | Fallback rooms active |
| `FALLBACK_ESCALATED` | Fallback rooms escalated above initial % |

---

## Configuration Reference

### boiler.yaml

```yaml
boiler:
  load_sharing:
    # Entry thresholds
    min_calling_capacity_w: 3500     # Activate when below this
    target_capacity_w: 4000          # Target capacity to reach
    high_return_delta_c: 15          # Return temp risk threshold (REQUIRED)
    
    # Timing
    min_activation_duration_s: 300   # Minimum active time (5 minutes)
    fallback_timeout_s: 900          # Max time for fallback rooms (15 minutes)
    fallback_cooldown_s: 1800        # Cooldown before fallback re-eligible (30 minutes)
    
    # Fallback target
    fallback_comfort_target_c: 20.0  # Pre-warming target for fallback rooms
```

### rooms.yaml (per-room, optional)

```yaml
rooms:
  - id: lounge
    load_sharing:
      schedule_lookahead_m: 90       # Override default 60 minutes (effective = 180 min)
      fallback_priority: 1           # First choice for fallback (lower = higher priority)
```

Rooms without `fallback_priority` are excluded from fallback selection.

---

## Capacity Calculation

Load sharing rooms contribute effective capacity based on their valve opening:

```python
effective_capacity = room_capacity * (valve_pct / 100.0)
```

Where `room_capacity` is the estimated capacity from `LoadCalculator`.

---

## Priority in Valve Coordinator

Load sharing commands are applied with this priority order:

1. **Persistence** (safety: pump overrun, interlock)
2. **Load sharing** (this system)
3. **Corrections** (TRV position corrections)
4. **Normal** (room heating logic)

---

## Key Behaviors Summary

| Scenario | Behavior |
|----------|----------|
| Low capacity but no cycling risk | Load sharing does NOT activate |
| Master enable turned OFF | Immediate deactivation |
| Schedule room reaches schedule time | Transitions to normal control |
| Fallback room exceeds timeout | Removed, enters cooldown |
| All original calling rooms stop | Load sharing deactivates |
| New room starts calling with sufficient capacity | Immediate deactivation |
| Room mode changed to manual/off | Removed from load sharing |

---

## Logging

- **INFO**: State changes (activated, deactivated)
- **DEBUG**: Evaluation details (capacity calculations, candidate selection, escalation steps)
- **WARNING**: Fallback activation (indicates schedule gap - consider improving schedules)

---

## Troubleshooting

### Load sharing not activating

1. Check master enable: `input_boolean.pyheat_load_sharing_enable` must be ON
2. Verify capacity threshold: Is calling capacity actually below `min_calling_capacity_w`?
3. Check cycling risk: Is cycling protection in COOLDOWN or return temp high enough?

### Load sharing activating too aggressively

1. Lower `high_return_delta_c` (requires higher return temp to trigger)
2. Increase `min_calling_capacity_w` (allows more capacity before triggering)

### Fallback rooms heating unnecessarily

1. Decrease `fallback_timeout_s` to shorten activations
2. Remove `fallback_priority` from rooms you don't want used as fallback
3. Improve schedules so schedule-aware tier provides sufficient coverage

### Oscillation (rapid activate/deactivate)

1. Increase `min_activation_duration_s`
2. Check for sensor noise causing capacity fluctuations
