# Short-Cycling Protection Implementation Plan

**Status:** Ready for Implementation  
**Date:** 2025-11-21  
**Analysis Period:** 2025-11-21 heating logs

---

## **1. OVERVIEW**

**Problem:** Single-room heating causes boiler short-cycling due to insufficient heat extraction. Return temperature stays too high (64-67°C), causing rapid flame OFF/ON cycles (9-11 seconds per burn).

**Solution:** Proactive cooldown detection - when flame goes OFF with high return temp, drop boiler setpoint to 30°C to prevent re-ignition until system cools to safe threshold.

**Validated Approach:** Manual intervention on 2025-11-21 demonstrated 100% success - return temp cooled from 67°C to 49°C over 10 minutes, then achieved 23.4-minute stable burn.

---

## **2. NEW COMPONENT: CyclingProtection Class**

Create new file: `controllers/cycling_protection.py`

### **2.1 Responsibilities**
- Monitor flame OFF events via `binary_sensor.opentherm_flame` state listener
- Detect DHW interruptions (ignore these) vs CH shutdowns (analyze these)
- Evaluate return temperature risk after 2-second sensor stabilization delay
- Drop boiler setpoint to 30°C when high return temp detected
- Monitor return temp during cooldown every 10 seconds
- Restore original setpoint when return temp reaches safe threshold
- Track cooldown history for excessive cycling alerts
- Persist state across AppDaemon restarts

### **2.2 State Machine**
- **NORMAL**: No cooldown active, monitoring flame status
- **COOLDOWN**: Setpoint dropped to 30°C, monitoring return temp for recovery
- **TIMEOUT**: Forced recovery after 30 minutes (alert user)

### **2.3 Key Methods**
```python
class CyclingProtection:
    def __init__(ad, config, alert_manager)
    def initialize_from_ha()  # Restore state from persistence entity
    def on_flame_off(entity, attribute, old, new, kwargs)  # Flame OFF event
    def _evaluate_cooldown_need(kwargs)  # Delayed check after 2s
    def _enter_cooldown(original_setpoint)  # Drop setpoint, save state
    def _check_recovery(kwargs)  # Monitor return temp every 10s
    def _exit_cooldown()  # Restore setpoint, clear state
    def _get_return_temp() -> float
    def _get_current_setpoint() -> float  # Read from climate entity
    def _set_setpoint(temperature)  # climate.set_temperature service
    def _get_recovery_threshold() -> float  # saved_setpoint - 15°C, min 35°C
    def _save_state()  # Persist to input_text helper
    def get_state_dict() -> Dict  # For logging/status publishing
```

---

## **3. CONFIGURATION ADDITIONS**

### **3.1 New Constants** (`core/constants.py`)
```python
# ============================================================================
# Short-Cycling Protection (OpenTherm Return Temperature Monitoring)
# ============================================================================

# DHW detection - sensor delay for state stabilization
CYCLING_SENSOR_DELAY_S = 2  # Wait for OpenTherm sensors to update after flame OFF

# High return temp detection threshold (delta from setpoint)
# When return_temp >= (setpoint - delta), cooldown is triggered
CYCLING_HIGH_RETURN_DELTA_C = 10  # e.g., 60°C when setpoint is 70°C

# Cooldown setpoint (must be climate entity minimum)
CYCLING_COOLDOWN_SETPOINT = 30  # °C - prevents re-ignition during cooldown

# Recovery threshold calculation
CYCLING_RECOVERY_DELTA_C = 15  # °C below saved setpoint
CYCLING_RECOVERY_MIN_C = 35    # °C absolute minimum (safety margin above cooldown)

# Recovery monitoring interval
CYCLING_RECOVERY_MONITORING_INTERVAL_S = 10  # Check every 10 seconds

# Timeout protection (force recovery if stuck)
CYCLING_COOLDOWN_MAX_DURATION_S = 1800  # 30 minutes

# Excessive cycling detection
CYCLING_EXCESSIVE_COUNT = 3      # Cooldowns to trigger alert
CYCLING_EXCESSIVE_WINDOW_S = 3600  # Time window (1 hour)

# Helper entities
HELPER_OPENTHERM_SETPOINT = "input_number.pyheat_opentherm_setpoint"
HELPER_CYCLING_STATE = "input_text.pyheat_cycling_protection_state"
```

### **3.2 New Helper Entities** (`ha_yaml/pyheat_package.yaml`)
```yaml
input_number:
  pyheat_opentherm_setpoint:
    name: "PyHeat OpenTherm Flow Temperature Setpoint"
    min: 30
    max: 80
    step: 1
    unit_of_measurement: "°C"
    mode: slider
    initial: 70
    icon: mdi:thermometer

input_text:
  pyheat_cycling_protection_state:
    name: "PyHeat Cycling Protection State"
    initial: '{"mode":"NORMAL","saved_setpoint":null,"cooldown_start":null}'
    max: 255
```

### **3.3 Alert Manager Constants** (`managers/alert_manager.py`)
```python
ALERT_CYCLING_PROTECTION_TIMEOUT = "cycling_protection_timeout"
ALERT_CYCLING_PROTECTION_EXCESSIVE = "cycling_protection_excessive"
```

---

## **4. INTEGRATION POINTS**

### **4.1 app.py Modifications**

**Import:**
```python
from cycling_protection import CyclingProtection
```

**Initialize in __init__():**
```python
self.cycling = CyclingProtection(self, self.config, self.alerts)
```

**Initialize state after config load:**
```python
# After self.config.load_all()
self.cycling.initialize_from_ha()
```

**Register flame sensor callback in setup_callbacks():**
```python
# In setup_callbacks(), after OpenTherm sensor registration:
# Flame sensor for cycling protection (triggers cooldown detection)
if self.entity_exists(C.OPENTHERM_FLAME):
    self.listen_state(self.cycling.on_flame_off, C.OPENTHERM_FLAME)
    self.log("Registered flame sensor for cycling protection")
```

**No changes needed to:**
- BoilerController (operates independently)
- RoomController (operates independently)
- TRVController (operates independently)
- Status publisher (can optionally add cycling state to attributes)

### **4.2 Heating Logger Integration** (`services/heating_logger.py`)

**Add CSV columns:**
```python
# In _get_csv_columns():
'cycling_state',              # NORMAL, COOLDOWN, TIMEOUT
'cycling_cooldown_count',     # Count in last hour
'cycling_saved_setpoint',     # Original setpoint (during cooldown)
'cycling_recovery_threshold'  # Target return temp for recovery
```

**Collect data in log_state():**
```python
# Add parameter: cycling_data: Dict
def log_state(self, trigger: str, opentherm_data: Dict, boiler_state: str, 
              pump_overrun_active: bool, room_data: Dict, total_valve_pct: int,
              cycling_data: Dict):
    # ... existing code ...
    
    # Cycling protection state
    'cycling_state': cycling_data.get('state', 'NORMAL'),
    'cycling_cooldown_count': cycling_data.get('cooldown_count', 0),
    'cycling_saved_setpoint': round_temp(cycling_data.get('saved_setpoint', '')),
    'cycling_recovery_threshold': round_temp(cycling_data.get('recovery_threshold', '')),
```

**Update callers in app.py:**
```python
# In _log_heating_state():
cycling_data = self.cycling.get_state_dict()  # Add this method to CyclingProtection
self.heating_logger.log_state(trigger, opentherm_data, boiler_state,
                              pump_overrun_active, room_data, total_valve_pct,
                              cycling_data)
```

### **4.3 Status Publisher (Optional Enhancement)** (`services/status_publisher.py`)

**Add to system status attributes:**
```python
# In publish_status():
attributes['cycling_protection'] = {
    'state': self.ad.cycling.state,
    'cooldown_start': self.ad.cycling.cooldown_entry_time.isoformat() if self.ad.cycling.cooldown_entry_time else None,
    'saved_setpoint': self.ad.cycling.saved_setpoint,
    'recovery_threshold': self.ad.cycling._get_recovery_threshold() if self.ad.cycling.state == 'COOLDOWN' else None,
    'cooldowns_last_hour': len([e for e in self.ad.cycling.cooldown_history if (now - e[0]).seconds < 3600])
}
```

---

## **5. IMPLEMENTATION SEQUENCE**

### **Phase 1: Core Infrastructure**
1. Add constants to `core/constants.py`
2. Add helper entities to `ha_yaml/pyheat_package.yaml`
3. Deploy helper entities to Home Assistant
4. Add alert constants to `managers/alert_manager.py`

### **Phase 2: CyclingProtection Class**
1. Create `controllers/cycling_protection.py` with full implementation
2. Implement state machine (NORMAL, COOLDOWN, TIMEOUT)
3. Implement flame OFF detection with 2-second delay
4. Implement DHW vs CH discrimination (100% accurate flag check)
5. Implement cooldown entry/exit logic
6. Implement recovery monitoring with threshold calculation
7. Implement state persistence (save/restore)
8. Implement timeout protection
9. Implement excessive cycling detection

### **Phase 3: Integration**
1. Import and initialize in `app.py`
2. Register flame sensor callback in `app.py`
3. Add CSV columns to `heating_logger.py`
4. Update logging calls in `app.py` to include cycling data
5. (Optional) Add cycling state to status publisher

### **Phase 4: Testing & Validation**
1. Test with single-room heating scenario
2. Verify DHW interruptions are correctly ignored
3. Verify cooldown activates when return temp high
4. Verify recovery at correct threshold
5. Verify state persistence across restarts
6. Verify timeout protection after 30 minutes
7. Verify excessive cycling alerts

---

## **6. KEY DESIGN DECISIONS**

### **6.1 DHW Detection Method**
- **Approach:** Check `binary_sensor.opentherm_dhw == 'on'` before evaluating cooldown
- **Validation:** 100% accuracy across 39 flame OFF events on 2025-11-21
- **Timing:** 2-second delay ensures flag has updated before check
- **Rejected alternatives:** DHW burner counter (71.8%), temperature drop rate (71.8%)

### **6.2 Setpoint Control Method**
- **Service:** `climate.set_temperature` on `climate.opentherm_heating`
- **Tested:** Successfully changes setpoint 70°C → 65°C → 70°C
- **Cooldown setpoint:** 30°C (hardware minimum)
- **Normal setpoint:** User-configurable via `input_number.pyheat_opentherm_setpoint`

### **6.3 Recovery Threshold**
- **Formula:** `recovery_temp = max(saved_setpoint - 15, 35)`
- **Rationale:** 
  - Scales with operating setpoint (higher setpoint = deeper cooldown)
  - 35°C minimum ensures 5°C safety margin above 30°C cooldown setpoint
  - Manual intervention showed 49°C successful → 15°C delta conservative

### **6.4 Independence from BoilerController**
- **No coordination needed** - operates via setpoint manipulation only
- BoilerController continues normal FSM operation (unaware of cooldown)
- When setpoint at 30°C, boiler won't fire (current temp > 30°C)
- Pump continues running (climate entity stays "heat" mode)
- Normal boiler shutdown logic works correctly if demand ceases

### **6.5 State Persistence**
- **Purpose:** Survive AppDaemon restarts mid-cooldown
- **Method:** JSON serialization to `input_text.pyheat_cycling_protection_state`
- **Fields:** mode, saved_setpoint, cooldown_start timestamp
- **Recovery:** On init, restore state and resume cooldown monitoring if needed

### **6.6 Timeout Protection**
- **Duration:** 30 minutes (3x observed cooldown time)
- **Behavior:** Force exit cooldown, restore setpoint, alert user
- **Rationale:** Prevents infinite cooldown if recovery threshold unreachable

### **6.7 Excessive Cycling**
- **Threshold:** 3 cooldowns in 1 hour
- **Behavior:** Alert user, continue trying (no lockout)
- **Rationale:** Conditions may improve; user can adjust thresholds; system stays operational

---

## **7. LOGGING STRATEGY**

### **7.1 AppDaemon Logs**
```python
# INFO - Normal operation
"🔥 Flame OFF | DHW: off | Return: 67.0°C | Setpoint: 70.0°C | Delta: 3.0°C"
"❄️ COOLDOWN STARTED | Return: 67.0°C >= Threshold: 60.0°C | Saved setpoint: 70.0°C → New: 30°C"
"✅ COOLDOWN ENDED | Duration: 612s | Return: 49.0°C | Restored setpoint: 70.0°C"

# DEBUG - Monitoring
"Cooldown check: 55.0°C (target: 55.0°C) [120s elapsed]"
"Flame OFF: DHW active - ignoring (DHW flag: on)"

# WARNING/ERROR - Problems
"🚨 COOLDOWN TIMEOUT: 1825s elapsed! Return: 58.0°C, Target: 55.0°C"
"⚠️ EXCESSIVE CYCLING: 3 cooldowns in 3600s"
```

### **7.2 CSV Logging**
- State captured every log entry for trend analysis
- Enables correlation with room states, valve positions, boiler behavior
- Foundation for future threshold tuning and optimization

---

## **8. SUCCESS CRITERIA**

### **8.1 Functional Requirements**
- ✅ Detect high return temp at flame OFF (threshold: setpoint - 10°C)
- ✅ Ignore DHW interruptions (100% accuracy requirement)
- ✅ Drop setpoint to 30°C during cooldown
- ✅ Monitor return temp every 10 seconds
- ✅ Recover when return temp ≤ (setpoint - 15°C), min 35°C
- ✅ Restore original setpoint on recovery
- ✅ Persist state across AppDaemon restarts
- ✅ Timeout after 30 minutes with alert
- ✅ Alert on excessive cycling (3 in 1 hour)

### **8.2 Performance Requirements**
- No impact on recompute performance (flame monitoring is passive)
- No race conditions (setpoint changes are atomic service calls)
- Minimal memory overhead (~200 bytes state + history)

### **8.3 Safety Requirements**
- Never leave setpoint stuck at 30°C (timeout protection)
- Never apply cooldown to DHW events (validated detection)
- Always restore setpoint on recovery or timeout
- System continues operating even if cycling protection fails

---

## **9. FUTURE ENHANCEMENTS**

### **9.1 Adaptive Thresholds**
- Track success/failure of cooldowns (did next burn last >5 minutes?)
- Auto-tune `CYCLING_RECOVERY_DELTA_C` based on success rate
- Adjust per number of calling rooms (single room = deeper cooldown)

### **9.2 Predictive Prevention**
- Analyze room demand patterns (1 room vs 2+ rooms)
- Proactively lower setpoint when single room calling
- Avoid reaching high return temp condition in first place

### **9.3 Dashboard Integration**
- Custom card showing cycling protection state
- Historical cooldown frequency graph
- Current return temp vs recovery threshold gauge

---

## **10. TESTING PLAN**

### **10.1 Unit Tests (if feasible)**
- Test DHW flag detection logic
- Test recovery threshold calculation
- Test state serialization/deserialization
- Test timeout calculation

### **10.2 Integration Tests**
- Single-room heating scenario (known to cause cycling)
- DHW interruption during CH burn (should ignore)
- Multiple rapid DHW cycles (all should be ignored)
- AppDaemon restart mid-cooldown (state recovery)
- 30-minute timeout scenario (manual test)

### **10.3 Field Validation**
- Monitor for 1 week after deployment
- Verify cooldown activates when expected
- Verify no false positives (DHW ignored)
- Verify recovery thresholds appropriate
- Adjust thresholds based on observed behavior

---

## **11. ROLLBACK PLAN**

If issues arise:
1. Set `ENABLE_CYCLING_PROTECTION = False` in constants.py (add this flag)
2. Remove flame sensor callback registration in app.py
3. System reverts to original behavior (no functional changes to existing code)
4. Helper entities remain but are unused (no harm)

---

## **12. DOCUMENTATION UPDATES**

### **12.1 Required Updates**
- `docs/ARCHITECTURE.md`: Add CyclingProtection component section
- `docs/changelog.md`: Document implementation with full details
- `README.md`: Add cycling protection to features list
- `heating_logs/README.md`: Document new CSV columns

### **12.2 New Documentation**
- `debug/short-cycling-protection/IMPLEMENTATION.md`: Implementation outcomes and results
- `debug/short-cycling-protection/TESTING.md`: Test results and validation

---

## **13. REFERENCE DOCUMENTS**

This implementation plan synthesizes the following analysis documents:

1. **dhw_detection_solution.md** - DHW vs CH discrimination method (100% accuracy)
2. **dhw_interruption_analysis_2025-11-21.md** - Detailed event timeline showing DHW behavior
3. **implementation_decisions.md** - Architectural decisions for all 10 major issues
4. **log_analysis_2025-11-21.md** - Short-cycling behavior analysis (Periods 1 & 2)
5. **log_analysis_manual_intervention_2025-11-21.md** - Successful cooldown validation
6. **proposed_fix.md** - High-level solution approach

---

## **IMPLEMENTATION STATUS**

- [ ] Phase 1: Core Infrastructure
- [ ] Phase 2: CyclingProtection Class
- [ ] Phase 3: Integration
- [ ] Phase 4: Testing & Validation

**Next Step:** Begin Phase 1 - Add constants and helper entities
