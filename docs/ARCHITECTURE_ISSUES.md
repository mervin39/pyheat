# Pyheat Architecture Issues: State Synchronization Problems

**Date**: November 3, 2025  
**Author**: Analysis based on recurring bugs and fixes

## Executive Summary

You're absolutely right to question why we keep having state synchronization bugs. Looking at today's commits alone, we've had **multiple critical issues** where one part of pyheat doesn't know what another part is doing:

1. **Sanity check false positives** - `orchestrator.rooms` empty while `room_controller` has all rooms
2. **Valve closing during OFF-delay** - Valve overrides not applied on the same recompute as state transition
3. **TRV feedback inconsistency checks** - Multiple components tracking valve state
4. **Override persistence** - Room state lost on reload despite HA timers persisting
5. **Pump overrun state** - Lost on reload, had to persist to HA entities

The **root cause** is an architectural mismatch: we have an **event-driven system** with **polling-based state management**.

---

## The Fundamental Problem

### What We Have: Distributed State Without Single Source of Truth

```
┌─────────────────────────────────────────────────────────────┐
│                    PyHeatOrchestrator                       │
│  - owns self.rooms dict (but doesn't populate it!)          │
│  - calls recompute_all() every 60s                          │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┬─────────┐
        │                     │                     │         │
        ▼                     ▼                     ▼         ▼
┌─────────────┐      ┌─────────────┐      ┌─────────────┐   ...
│RoomController│      │BoilerManager│      │TRVManager   │
│ - rooms dict │      │ - state     │      │ - last_cmd  │
│ - override  │      │ - valves    │      │ - feedback  │
│ - targets   │      │ - timers    │      │ - pending   │
└─────────────┘      └─────────────┘      └─────────────┘
```

**Each module maintains its own state independently.** There is NO central state store. State synchronization happens only during `recompute_all()`, which runs periodically (every 60s) or on triggers.

### The Core Issue: Event-Driven Execution + Polling State Management

**Event-Driven Inputs:**
- TRV feedback entity changes (instant)
- Timer finished events (instant)
- User overrides via services (instant)
- Temperature sensor updates (instant)
- Schedule changes (instant)

**Polling State Resolution:**
- `recompute_all()` runs periodically
- Reads current state from each module
- Computes new state
- Writes back to modules
- **Gap between events and state updates**

### Why This Causes Bugs

#### Bug Pattern #1: State Populated After First Use
```python
# core.py __init__
self.rooms = {}  # Empty dict

# room_controller.py
rooms = {"office": Room(...), "bathroom": Room(...)}  # Populated

# core.py recompute_all() - ORIGINAL CODE
all_rooms = self.room_controller.get_all_rooms()
# But self.rooms stays empty! Never synced!

# ha_triggers.py sanity check
for room_id, room in _orchestrator.rooms.items():  # Empty dict!
    if room.call_for_heat:
        rooms_calling_for_heat.append(room_id)
# Result: No rooms found, false positive warning
```

**Why it happened**: Two separate dictionaries (`orchestrator.rooms` and `room_controller.rooms`). No synchronization.

#### Bug Pattern #2: State Changes Apply On NEXT Recompute
```python
# boiler.py update() - STATE_ON → STATE_PENDING_OFF transition
if self._state == self.STATE_ON and not rooms_calling_for_heat:
    self._state = self.STATE_PENDING_OFF  # State changes NOW
    # But valves_must_stay_open NOT set here!
    return {"state": "pending_off", ...}  # Returns to orchestrator

# core.py gets result
boiler_result = self.boiler.update(...)
valve_overrides = boiler_result.get("overridden_valve_percents", {})
# Empty! Because update() didn't set it during transition

# Next recompute 60s later
if self._state == self.STATE_PENDING_OFF:  # NOW it's true
    valves_must_stay_open = True  # Set on NEXT recompute
    # But valves already closed 60s ago!
```

**Why it happened**: State transition happens on recompute N, but side effects (valve overrides) only set on recompute N+1.

#### Bug Pattern #3: Multiple Sources of Truth
```python
# TRV valve position tracked in THREE places:
1. trv.py: self._last_commanded[room_id] = percent
2. boiler.py: self.last_valve_positions[room_id] = percent  
3. HA entity: number.pyheat_{room}_valve_percent

# Which is correct?
- If TRV command in flight, _last_commanded != actual
- If interlock overrides, boiler has different value
- If entity manually changed, all three disagree
```

**Why it happened**: No single source of truth, no state synchronization protocol.

#### Bug Pattern #4: Persistence Requires Manual State Management
```python
# Room override applied
room.override_kind = "boost"
room.override_target = 25.0

# Pyscript reloads
# room_controller reinitializes
room.override_kind = None  # Lost!

# But timer still running in HA
timer.pyheat_bathroom_override: "active", finishes_at: "2025-11-03T14:30:00"

# Solution: Manually persist to HA entities
state.set("input_number.pyheat_bathroom_override_target", value=25.0)
# Then restore on init by reading entity
```

**Why it happened**: Python in-memory state doesn't persist. HA entities do. No automatic state persistence layer.

---

## What We SHOULD Have: Single State, Event-Driven Updates

### Ideal Architecture: Event Sourcing + State Machine

```
┌─────────────────────────────────────────────────────────────┐
│                  SINGLE STATE STORE                          │
│  {                                                           │
│    "rooms": {                                                │
│      "office": {                                             │
│        "temp": 22.9, "target": 25.0,                        │
│        "override": {"kind": "boost", "expires": "..."},     │
│        "valve_commanded": 85, "valve_feedback": 85,         │
│        "call_for_heat": true                                │
│      }                                                       │
│    },                                                        │
│    "boiler": {                                               │
│      "state": "on", "demand": true,                         │
│      "valves_must_stay_open": false,                        │
│      "transition_time": "2025-11-03T23:00:27Z"              │
│    }                                                         │
│  }                                                           │
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │  State Mutations  │
                    │  (Reducers/FSM)   │
                    └─────────┬─────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
   [Temp Event]         [Timer Event]        [User Override]
   temp=22.9°C         "min_off finished"    boost(25.0, 1h)
        │                     │                     │
        └─────────────────────┴─────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Single Recompute │
                    │  ALL state updated│
                    │  NO stale values  │
                    └───────────────────┘
```

**Key Principles:**
1. **Single source of truth**: One state object, not distributed across modules
2. **Immutable state updates**: State changes are atomic, not incremental
3. **Event-driven**: Every input (sensor, timer, user action) triggers immediate recompute
4. **Synchronous state**: All derived values computed in same pass
5. **Persistence layer**: State automatically persisted to HA entities, restored on reload

---

## Why We Don't Have This

### Historical Reasons (Speculation)

1. **Incremental development**: Started with simple polling, added features incrementally
2. **Module separation**: Each module seemed cleanly separated, hiding state dependencies
3. **Pyscript constraints**: No built-in state management framework, hand-rolled everything
4. **Timer helpers**: Event-driven timers added later, bolted onto polling architecture
5. **Complexity**: Full event sourcing is more complex upfront (but simpler long-term)

### Current Code Patterns

**What we do now:**
```python
# Module owns state
class BoilerManager:
    def __init__(self):
        self._state = "off"
        self.last_valve_positions = {}
        
    def update(self, rooms_calling, valve_percents, trv, now):
        # Read own state
        # Compute new state
        # Return partial result
        # Orchestrator applies result
```

**What would be better:**
```python
# Pure state machine, no internal state
class BoilerStateMachine:
    @staticmethod
    def transition(current_state: BoilerState, event: Event) -> BoilerState:
        # Pure function: state + event → new state
        # No side effects
        # All state in BoilerState object
        
# Orchestrator owns ALL state
class PyHeatOrchestrator:
    def __init__(self):
        self.state = PyHeatState()  # Single state object
        
    async def handle_event(self, event):
        # Event triggers recompute
        new_state = self.state_machine.transition(self.state, event)
        self.state = new_state
        await self.apply_state(new_state)
```

---

## The Real Questions

### Q: "Why are these inconsistencies and bugs cropping up?"

**A:** Because we have **distributed stateful modules** that only synchronize during periodic `recompute_all()`. Between recomputes:
- Events happen (timers finish, TRVs update, users override)
- Modules update their own state independently
- State becomes inconsistent across modules
- Next recompute tries to reconcile but may be 60s late

### Q: "Surely at any time there is a single state which we know exactly?"

**A:** **No, there isn't.** At any moment:
- `room_controller.rooms` has one view of room state
- `orchestrator.rooms` has a different (or empty!) view
- `boiler.last_valve_positions` has valve state from last recompute
- `trv._last_commanded` has valve state from last command
- HA entities have what was last published
- Timer helpers have their own state

**These are all snapshots from different times.** There's no guarantee they're consistent.

### Q: "What would fix this?"

**A:** Two options:

#### Option 1: Full Refactor (Ideal but Big)
1. **Single state object** for entire system
2. **Pure state machines** for all logic (no internal state)
3. **Event-driven recompute** on every input
4. **Atomic state updates** (all or nothing)
5. **Automatic persistence** layer

#### Option 2: Tactical Fixes (What We're Doing)
1. **Sync distributed state** (like `orchestrator.rooms = room_controller.rooms`)
2. **Apply side effects immediately** (set `valves_must_stay_open` on transition, not next recompute)
3. **Manual persistence** (save/restore to HA entities)
4. **Defensive checks** (sanity checks, watchdog crons, safety valves)
5. **Add more synchronization points** as bugs discovered

**We're currently doing Option 2.** It works but requires vigilance and generates these recurring bugs.

---

## Concrete Examples From Today

| Bug | Root Cause | Category |
|-----|-----------|----------|
| Sanity check false positives | `orchestrator.rooms` not synced with `room_controller.rooms` | Distributed state |
| Valve closing during OFF-delay | Side effects applied on N+1 recompute, not N | State update lag |
| TRV feedback inconsistency | Multiple sources of truth for valve position | Distributed state |
| Override persistence | Python state doesn't persist, manual save/restore required | No persistence layer |
| Pump overrun lost on reload | Boiler state in memory, not persisted | No persistence layer |

**All share same root cause: No single source of truth, no centralized state management.**

---

## Recommendations

### Short-Term (Tactical)
1. **Document state ownership**: Which module owns which state? Write it down.
2. **Synchronize on recompute**: Every recompute must sync all distributed state
3. **Apply side effects immediately**: State transitions must set ALL derived values in same pass
4. **Defensive programming**: More sanity checks, watchdogs, safety valves
5. **Test state transitions**: Unit test each state transition with all side effects

### Medium-Term (Incremental)
1. **Centralize room state**: Move `rooms` dict ownership to orchestrator, room_controller just manages objects
2. **Centralize valve state**: One source of truth for valve positions (probably boiler or orchestrator)
3. **Event-driven recompute**: Trigger recompute on every event, not just periodic
4. **State snapshot logging**: Log full state on every recompute for debugging
5. **State validation**: Assert state consistency at end of each recompute

### Long-Term (Architectural)
1. **State store**: Single PyHeatState object with all system state
2. **Pure state machines**: Refactor modules to pure functions (state + event → new state)
3. **Event sourcing**: Log all events, replay for debugging/testing
4. **Automatic persistence**: Serialize state to HA entities automatically
5. **State diffing**: Only publish entities that changed

---

## Conclusion

**You're right to be frustrated.** These bugs all have the same root cause: **architectural mismatch between event-driven inputs and polling-based state management**.

The system works ~95% of the time because:
- Recomputes are frequent enough (60s) to catch most state changes
- Timers smooth over timing issues
- Safety features (watchdogs, sanity checks) catch edge cases

But the remaining ~5% generates these recurring bugs because:
- State is distributed across modules
- No synchronization guarantee between events and recomputes
- Side effects can lag by one recompute cycle
- No single source of truth

**The current approach is viable** if we:
1. Accept ongoing bug fixes as state synchronization issues discovered
2. Add more defensive checks and synchronization points
3. Document state ownership clearly
4. Test state transitions thoroughly

**A full refactor would eliminate the root cause** but requires significant effort and risk.

Your call on which path forward.
