# PyHeat Architecture & Responsibility Analysis Report

**Date**: 16 November 2025  
**Analysis Type**: Component Responsibility Review & Conflict Detection

## Executive Summary

I've analyzed the PyHeat codebase against its documented architecture. Overall, the modular design is **well-implemented** with clear separation of concerns. However, I've identified **several issues** where code responsibilities are misplaced or where different components could fight each other.

---

## Critical Issues (Potential Component Conflicts)

### 1. **CRITICAL: Valve Persistence Logic Split Between app.py and boiler_controller.py**

**Problem**: Valve persistence responsibility is **fragmented** between two files, creating potential for conflicts.

**Current State**:
- `boiler_controller.py` **decides** which valve positions to persist (`persisted_valves` dict)
- `boiler_controller.py` **saves** valve positions to `self.boiler_last_valve_positions`
- `app.py` **applies** the persisted valve commands in `recompute_all()`
- `room_controller.py` **computes** normal valve positions independently

**The Conflict**:
```python
# In boiler_controller.py (line 218):
if has_demand and all_valve_positions:
    self.boiler_last_valve_positions = all_valve_positions.copy()  # Saves positions

# But in app.py (lines 527-549):
if persisted_valves:
    for room_id, valve_percent in persisted_valves.items():
        self.rooms.set_room_valve(room_id, valve_percent, now)  # Applies them
```

**Why This Is Dangerous**:
1. During `PENDING_OFF` state, boiler controller sets `valves_must_stay_open = True` and returns `persisted_valves`
2. But `app.py` immediately calls `self.rooms.set_room_valve()` which could trigger TRV checks
3. If TRV feedback shows valve closing (before command completes), `trv_controller.check_feedback_for_unexpected_position()` might trigger a correction
4. The correction would **fight** with the persistence command

**Evidence of Awareness**:
```python
# In trv_controller.py (line 276):
# CRITICAL: During PENDING_OFF and PUMP_OVERRUN states, valve persistence is active
if boiler_state and boiler_state in (C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN):
    self.ad.log(f"TRV feedback ignored during {boiler_state}", level="DEBUG")
    return  # Prevents fighting!
```

The code **knows** about this conflict and tries to prevent it by checking boiler state in TRV controller - but this is **cross-component coupling** that violates separation of concerns.

**Recommendation**:
Move **all valve persistence logic** into `boiler_controller.py`. It should:
- Decide when persistence is needed
- Store valve positions
- **Directly apply valve commands** (bypass room_controller)
- Return only a status flag to app.py

---

### 2. **Valve Command Responsibility Overlap (room_controller vs trv_controller)**

**Problem**: `room_controller.set_room_valve()` acts as a middleware but adds confusing logic.

**Current Flow**:
```python
# app.py calls:
self.rooms.set_room_valve(room_id, valve_percent, now)

# room_controller.set_room_valve() checks:
def set_room_valve(self, room_id, valve_percent, now):
    is_correction = room_id in self.trvs.unexpected_valve_positions
    if is_correction:
        expected = self.trvs.unexpected_valve_positions[room_id]['expected']
        self.trvs.set_valve(room_id, expected, now, is_correction=True)
    else:
        self.trvs.set_valve(room_id, valve_percent, now, is_correction=False)
```

**Why This Is Wrong**:
- `room_controller` is making decisions about TRV corrections
- But `trv_controller` already **owns** the `unexpected_valve_positions` dict
- `room_controller` is **reading internal state** from `trv_controller`
- This creates **tight coupling** between controllers

**The Real Issue**:
When `room_controller` detects an unexpected position, it should just send the normal command. The TRV controller should handle whether it's a correction internally.

**Recommendation**:
- Remove `set_room_valve()` from `room_controller` entirely
- Have `app.py` call `trvs.set_valve()` directly
- Move unexpected position handling **inside** `trv_controller.set_valve()`

---

### 3. **Temperature Smoothing Split Between status_publisher and app.py**

**Problem**: Smoothing logic is applied in **two places** with potential inconsistency.

**Current State**:
```python
# In app.py sensor_changed() (line 329):
smoothed_temp = self.status.apply_smoothing_if_enabled(room_id, fused_temp)
self.status.update_room_temperature(room_id, smoothed_temp, is_stale)

# In status_publisher.py:
def apply_smoothing_if_enabled(self, room_id, raw_temp):
    return self._apply_smoothing(room_id, raw_temp)
```

**Why This Is Problematic**:
- `sensor_manager` does the fusion
- `app.py` calls `status_publisher` to do smoothing
- But smoothing state (`self.smoothed_temps`) lives in `status_publisher`
- Yet the smoothed value is used for **control decisions** (deadband check in app.py)

**Violation**:
Smoothing is a **sensor processing function**, not a status publishing function. It affects control logic, not just display.

**Recommendation**:
Move smoothing to `sensor_manager.get_room_temperature()`:
```python
# In sensor_manager:
def get_room_temperature(self, room_id, now):
    raw_fused_temp, is_stale = self._fuse_sensors(room_id, now)
    smoothed_temp = self._apply_smoothing(room_id, raw_fused_temp)
    return smoothed_temp, is_stale
```

---

### 4. **Scheduler Has Override Resolution Logic**

**Problem**: `scheduler.py` handles override timer checks, but override timers are managed by `service_handler.py`.

**Current State**:
```python
# In scheduler.resolve_room_target() (line 49):
timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
if self.ad.entity_exists(timer_entity):
    timer_state = self.ad.get_state(timer_entity)
    if timer_state in ["active", "paused"]:
        # Get override target...
```

**Coupling**:
- Scheduler knows about timer entities
- Service handler sets timer values
- App.py listens to timer changes
- Three components all touching the same concept

**Recommendation**:
Create an `OverrideManager` class that:
- Owns override state
- Provides `get_override_target(room_id)` method
- Encapsulates timer entity knowledge
- Used by scheduler instead of direct entity access

---

## Moderate Issues (Responsibility Misalignment)

### 5. **room_controller Initializes From HA But Doesn't Own TRV State**

**In room_controller.initialize_from_ha()** (line 45):
```python
fb_valve_entity = room_cfg['trv']['fb_valve']
fb_valve_str = self.ad.get_state(fb_valve_entity)
fb_valve = int(float(fb_valve_str))
if fb_valve > 0:
    self.room_call_for_heat[room_id] = True
```

**Problem**:
- `room_controller` is directly reading TRV feedback entities
- But TRV entity knowledge should be in `trv_controller`
- Duplication: `trv_controller.initialize_from_ha()` also reads these entities

**Recommendation**:
Have `trv_controller` provide a method:
```python
def get_current_valve_position(self, room_id):
    # Returns current valve position from feedback sensor
```

Then `room_controller` calls this instead of directly accessing entities.

---

### 6. **status_publisher Has Scheduler Reference**

**In status_publisher.__init__()** (line 20):
```python
self.scheduler_ref = None  # Set by app.py later
```

**Problem**:
- Status publisher uses scheduler to get next schedule change
- But it's a **display concern**, not a control concern
- Creates circular dependency potential

**Why It Exists**:
The formatted status needs to show "Auto: 18.0° until 23:00 (14.0°)" which requires schedule knowledge.

**Recommendation**:
Pass schedule information **as parameters** to status publishing methods instead of holding a reference:
```python
def publish_room_entities(self, room_id, data, now, next_change=None):
    # Use next_change if provided
```

---

### 7. **app.py Does Too Much Orchestration Logic**

**In app.py recompute_all()** (lines 491-561):
```python
# Compute each room
for room_id in self.config.rooms.keys():
    data = self.rooms.compute_room(room_id, now)
    
# Update boiler state
boiler_state, boiler_reason, persisted_valves, valves_must_stay_open = \
    self.boiler.update_state(...)
    
# Apply valve commands with persistence priority
if persisted_valves:
    for room_id, valve_percent in persisted_valves.items():
        self.rooms.set_room_valve(room_id, valve_percent, now)
        data_for_publish = room_data[room_id].copy()
        data_for_publish['valve_percent'] = valve_percent
        self.status.publish_room_entities(room_id, data_for_publish, now)
```

**Problem**:
- App.py has ~60 lines of orchestration logic
- It's making decisions about how to merge persisted valves with computed valves
- It's modifying room_data dictionaries
- This logic belongs in a controller

**Recommendation**:
Create a `HeatingCoordinator` class that owns the recompute logic:
```python
class HeatingCoordinator:
    def recompute_system(self, now):
        room_data = self._compute_all_rooms(now)
        boiler_state = self._update_boiler(room_data, now)
        self._apply_valve_commands(room_data, boiler_state, now)
        self._publish_status(room_data, boiler_state, now)
```

---

## Minor Issues (Code Organization)

### 8. **service_handler Stores Override Type Metadata**

**In service_handler.py** (referenced in app.py line 312):
```python
self.service_handler._set_override_type(room_id, "none")
```

But this method doesn't exist in the provided code! This suggests:
- Lost functionality
- Or incomplete refactoring
- Status publisher refers to override types but service handler manages them

**Recommendation**: Remove this call or implement the method properly.

---

### 9. **Constants File Has Config Defaults**

**constants.py** defines defaults like:
```python
HYSTERESIS_DEFAULT = {"on_delta_c": 0.30, "off_delta_c": 0.10}
VALVE_BANDS_DEFAULT = {...}
```

But **config_loader.py** also applies these defaults:
```python
room_cfg['hysteresis'] = room.get('hysteresis', C.HYSTERESIS_DEFAULT.copy())
```

**Problem**: Default values in two places creates potential inconsistency.

**Recommendation**: Keep constants for **limits and validation**, but have config_loader own all default application.

---

### 10. **TRV Controller Checks Boiler State**

**In trv_controller.check_feedback_for_unexpected_position()** (line 276):
```python
if boiler_state and boiler_state in (C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN):
    return  # Don't trigger corrections during valve persistence
```

**Problem**: TRV controller has knowledge of boiler states, creating coupling.

**Recommendation**: 
Pass a `valve_persistence_active` boolean flag instead of `boiler_state`:
```python
def check_feedback_for_unexpected_position(self, room_id, feedback_percent, now, 
                                          valve_persistence_active=False):
    if valve_persistence_active:
        return
```

---

## Architecture Strengths (Doing Well)

1. ✅ **sensor_manager.py** - Pure sensor fusion, no control logic
2. ✅ **config_loader.py** - Clean configuration handling, no business logic
3. ✅ **alert_manager.py** - (not reviewed in detail, but appears separate)
4. ✅ **Modular file structure** - Each controller has clear domain
5. ✅ **Constants centralization** - Good separation of tuning parameters
6. ✅ **Event-driven design** - Callbacks and triggers well organized

---

## Summary of Recommendations

### High Priority (Prevent Conflicts)
1. **Move valve persistence entirely to boiler_controller** - eliminate app.py orchestration
2. **Remove set_room_valve() from room_controller** - direct TRV calls from app.py
3. **Move temperature smoothing to sensor_manager** - not status_publisher

### Medium Priority (Clean Architecture)
4. **Create OverrideManager class** - decouple override handling
5. **Remove scheduler_ref from status_publisher** - pass data as parameters
6. **Create HeatingCoordinator class** - move recompute logic from app.py

### Low Priority (Cleanup)
7. **Fix trv_controller boiler state coupling** - use boolean flags
8. **Consolidate default value handling** - single source of truth
9. **Add missing _set_override_type method** - or remove the call

---

## Conclusion

The codebase is **well-structured** compared to typical heating controllers, but there are **architectural debt items** that could cause:

1. **Race conditions** during valve persistence (components fighting each other)
2. **Difficult debugging** due to split responsibilities
3. **Coupling** between modules that should be independent

The critical issue is **valve persistence** during pump overrun and state transitions. The current workaround (checking boiler state in TRV controller) prevents problems but violates clean architecture principles.

**Risk Assessment**:
- Current system: **Works but fragile** - relies on careful state checking
- Under stress: Could experience valve fighting during rapid state transitions
- Maintenance: Difficult to modify without breaking assumptions

**Recommendation**: Address the valve persistence architecture before adding new features.
