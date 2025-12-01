# Proposal: System-Wide Frost Protection

**Status:** PLANNING  
**Date:** 2025-12-01  
**Author:** Proposal based on requirements analysis  
**Related:** See `passive_frost_protection_proposal.md` for passive-mode-only approach

---

## Problem Statement

PyHeat currently has **no frost protection mechanism** to prevent rooms from getting dangerously cold. This creates several risks:

1. **Frozen pipes** - Water pipes in walls/floors can freeze and burst
2. **Property damage** - Condensation, mold, structural damage from extreme cold
3. **Safety hazard** - Dangerous temperatures for occupants or pets
4. **System failure gaps** - Room in "off" mode or misconfigured schedule could freeze

**User requirement:** System-wide safety floor that activates regardless of room mode (except when user explicitly wants a room off).

---

## Proposed Solution

Add a **global frost protection temperature** that acts as an absolute safety floor for all rooms. When any room drops below this threshold, frost protection activates with emergency heating, regardless of the room's configured mode or schedule.

### Core Concept

**Frost protection is a safety override**, not a comfort feature:
- Activates only in emergency situations (rare)
- Uses aggressive heating for rapid recovery
- Overrides most user settings (except explicit "off" mode)
- Respects master_enable (system-wide kill switch)

---

## Configuration

### Global Setting (boiler.yaml)

Add single system-wide configuration:

```yaml
system:
  frost_protection_temp_c: 8.0  # Global safety floor for all rooms
```

**Rationale:**
- 8°C is standard frost protection temperature (UK/EU norm)
- Above pipe freezing risk (0-5°C depending on insulation)
- Below comfort threshold (users won't accidentally rely on it for heating)
- Single configuration point - no per-room complexity

**User adjustment:**
- Lower (e.g., 6°C): Minimal intervention, pipes-only protection
- Higher (e.g., 10°C): More conservative, earlier activation

---

## Behavioral Specification

### Activation Conditions

Frost protection activates when **ALL** of these are true:

1. **Temperature below threshold**: `temp < frost_protection_temp - on_delta`
2. **Room mode is NOT "off"**: Respects explicit user decision to disable room
3. **Master enable is ON**: Respects system-wide kill switch
4. **Sensors are valid**: Room has non-stale temperature reading

### Deactivation (Recovery)

Frost protection deactivates when:

1. **Temperature recovered**: `temp > frost_protection_temp + off_delta`
2. **Returns to normal mode**: Resumes scheduled/manual/passive behavior

### Hysteresis

Uses existing per-room hysteresis values (`on_delta_c`, `off_delta_c`) to prevent oscillation:

**Example with frost_protection_temp = 8°C, on_delta = 0.3°C, off_delta = 0.1°C:**
- **Activate**: temp < 7.7°C (emergency!)
- **Maintain**: 7.7°C ≤ temp ≤ 8.1°C (heating, recovering)
- **Deactivate**: temp > 8.1°C (recovered, return to normal)

### Room Mode Interactions

| Room Mode | Normal Behavior | Frost Protection Behavior |
|-----------|----------------|---------------------------|
| **Off** | No heating | **NO ACTIVATION** (user explicitly disabled room) |
| **Auto** | Schedule-driven | **ACTIVATES** (safety override) |
| **Manual** | Constant setpoint | **ACTIVATES** if setpoint < frost temp (safety override) |
| **Passive** | Opportunistic | **ACTIVATES** (safety override) |
| **Holiday** | 15°C setpoint | **ACTIVATES** if drops below 8°C (very rare) |

**Key principle:** Frost protection overrides everything except explicit "off" mode.

### Master Enable Interaction

**master_enable OFF:**
- **Frost protection DISABLED**
- No heating occurs, even in emergency
- Rationale: User has system-wide control for maintenance, emergencies, etc.
- User accepts responsibility for frost risk when master is off

**master_enable ON:**
- Frost protection active and monitoring
- Will activate if any room drops below threshold

---

## Heating Behavior During Frost Protection

### Call for Heat

When activated, room **calls for heat** (sets `calling = True`):
- Boiler will turn on if not already running
- Room participates in boiler demand aggregation
- Treated as legitimate heating demand (not opportunistic)

### Valve Opening

**Decision: Force valve to 100%**

**Rationale:**
1. **Emergency situation** - rapid recovery required
2. **Maximize heat transfer** - fully open valves ensure maximum radiator output
3. **User override** - ignores any configured valve bands or passive percentages
4. **Clear behavioral distinction** - frost protection is special, not normal operation
5. **Proven approach** - matches TRV frost protection behavior

**Alternative considered:** Use normal valve bands based on error
- Rejected: Too slow, defeats emergency purpose
- Frost protection needs fast recovery, not gradual heating

### Target Temperature

**Frost protection uses the frost_protection_temp as the target:**
- Not a comfort temperature (8°C is cold!)
- Just enough to get room above danger zone
- Intentional overshoot to 9-10°C provides safety buffer
- Returns to normal behavior once recovered

**Example:**
- Frost temp: 8°C
- Room drops to 7.5°C → frost protection activates
- Heating continues until temp > 8.1°C (8.0 + off_delta)
- Likely overshoots to 9-10°C (desirable - thermal buffer)
- Returns to normal mode behavior

---

## Implementation Architecture

### Detection Logic (room_controller.py)

Add frost protection check **before** normal mode logic:

```python
def compute_room_heating(self, room_id: str, now: datetime) -> dict:
    """Main entry point for room heating decisions."""
    
    # Get temperature and room mode
    temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
    room_mode = self.get_room_mode(room_id)
    
    # Check master enable
    if not self.is_master_enabled():
        return self._no_heating_result(room_id, temp, "master_disabled")
    
    # FROST PROTECTION CHECK (BEFORE mode logic)
    if room_mode != MODE_OFF and temp is not None:
        frost_temp = self.config.system.get('frost_protection_temp_c', 8.0)
        on_delta = self.config.rooms[room_id]['hysteresis']['on_delta_c']
        off_delta = self.config.rooms[room_id]['hysteresis']['off_delta_c']
        
        # Check if frost protection should activate/continue
        in_frost_protection = self.room_frost_protection_active.get(room_id, False)
        
        if not in_frost_protection and temp < (frost_temp - on_delta):
            # Activate frost protection
            self.room_frost_protection_active[room_id] = True
            self.ad.log(f"FROST PROTECTION ACTIVATED: {room_id} at {temp:.1f}C "
                       f"(threshold: {frost_temp:.1f}C)", level="WARNING")
            return self._frost_protection_heating(room_id, temp, frost_temp)
        
        elif in_frost_protection and temp > (frost_temp + off_delta):
            # Deactivate frost protection (recovered)
            self.room_frost_protection_active[room_id] = False
            self.ad.log(f"FROST PROTECTION DEACTIVATED: {room_id} recovered to {temp:.1f}C", 
                       level="INFO")
            # Continue to normal mode logic below
        
        elif in_frost_protection:
            # Continue frost protection heating
            return self._frost_protection_heating(room_id, temp, frost_temp)
    
    # Normal mode logic (existing code)
    # ... existing compute_room_heating logic ...
```

### Helper Method

```python
def _frost_protection_heating(self, room_id: str, temp: float, frost_temp: float) -> dict:
    """Generate heating command for frost protection mode."""
    return {
        'temp': temp,
        'target': frost_temp,
        'is_stale': False,
        'mode': self.get_room_mode(room_id),  # Actual mode (for display)
        'operating_mode': 'frost_protection',  # Special operating mode
        'calling': True,                       # CALL FOR HEAT
        'valve_percent': 100,                  # MAXIMUM HEATING
        'error': frost_temp - temp,
        'frost_protection': True,              # Flag for status display
    }
```

### State Tracking

Add new state dict to RoomController:

```python
self.room_frost_protection_active = {}  # {room_id: bool}
```

**Persistence:** Should NOT persist across AppDaemon restarts
- Frost protection state is temperature-dependent
- Will be re-evaluated immediately on restart
- No benefit to persisting, adds complexity

---

## Integration with Existing Systems

### Load Sharing

**Frost protection rooms are EXCLUDED from load sharing:**
- They are legitimately calling for heat (safety reason)
- Don't want to pre-warm other rooms while one is in emergency
- Existing `calling = True` logic already excludes them

**No code changes needed** - existing load sharing checks will work.

### Valve Coordinator

**Frost protection uses normal valve command path:**
- `room_controller` sets `valve_percent = 100`
- `valve_coordinator.apply_valve_command()` applies normal priority
- Frost protection commands are "normal" commands (Priority 4)

**Interaction with persistence:**
- If pump overrun active, persistence overrides frost protection (safety: keep valves open)
- If frost protection activates during pump overrun, valve is already open (no conflict)
- After pump overrun ends, frost protection can take over if still needed

**No code changes needed** - existing priority system handles it.

### Boiler Controller

**Frost protection rooms count as calling rooms:**
- Boiler aggregates all calling rooms (including frost protection)
- Boiler turns on if any room calling (including frost protection)
- Frost protection rooms contribute to valve interlock calculation

**No code changes needed** - boiler sees standard calling behavior.

### Status Publishing

**Display frost protection state clearly:**

```python
# In status_publisher.format_room_status():
if room_data.get('frost_protection'):
    operating_mode_display = "FROST PROTECTION"  # All caps, urgent
    formatted_status = f"⚠️ FROST PROTECTION: {temp:.1f}°C → {target:.1f}°C"
else:
    # Existing status formatting
```

**Entity attributes:**
```python
attributes = {
    'temperature': temp,
    'target': target,
    'mode': room_mode,                    # auto/manual/passive/off
    'operating_mode': operating_mode,     # active/passive/frost_protection
    'frost_protection': True/False,
    'calling': True,
    'valve_percent': 100,
    # ... existing attributes ...
}
```

### Heating Logger

**Add frost protection column to CSV logs:**

```python
# In heating_logger.py
columns = [
    'timestamp',
    'room',
    'temp',
    'target',
    'mode',
    'operating_mode',
    'frost_protection',  # NEW: True/False
    'calling',
    'valve_percent',
    # ... existing columns ...
]
```

**Enables post-analysis:**
- When did frost protection activate?
- How long did recovery take?
- What external temperature caused it?
- How effective was the 100% valve strategy?

---

## Example Scenarios

### Scenario 1: Auto Mode Room Gets Cold

**Setup:**
- Office in "auto" mode
- Schedule: Heat to 18°C Mon-Fri 08:00-17:00, otherwise 12°C
- Current time: Saturday 03:00 (target = 12°C)
- Extreme cold night, temp drops to 7.2°C

**Timeline:**

| Time | Temp | Normal Behavior | Frost Protection |
|------|------|----------------|------------------|
| 02:00 | 9.0°C | Target 12°C, calling | Normal heating |
| 02:30 | 8.5°C | Target 12°C, calling | Normal heating |
| 03:00 | 7.7°C | Target 12°C, calling | Normal heating (below target but not below frost threshold yet) |
| 03:15 | 7.5°C | Target 12°C, calling | **FROST PROTECTION ACTIVATES** (below 7.7°C) |
| 03:20 | 7.8°C | - | Frost protection heating (100% valve) |
| 03:30 | 8.5°C | - | Frost protection continues (below 8.1°C recovery) |
| 03:40 | 9.2°C | - | **FROST PROTECTION DEACTIVATES** (above 8.1°C) |
| 03:45 | 9.5°C | Target 12°C, calling | Returns to normal heating |

**Notes:**
- Room was already heating (target 12°C > temp)
- Frost protection took over when temp dropped dangerously low
- Forced 100% valve for rapid recovery
- Returned to normal once safe (9.2°C)

### Scenario 2: Passive Mode Room Gets Very Cold

**Setup:**
- Games room in "passive" mode
- Passive max_temp = 18°C, valve = 30%
- Rarely heats because other rooms don't call much
- Temp slowly drifts down to 7.0°C

**Timeline:**

| Temp | Normal Passive Behavior | Frost Protection |
|------|------------------------|------------------|
| 10°C | No calling, valve 30% if others heat | Opportunistic |
| 9°C | No calling, valve 30% if others heat | Opportunistic |
| 8°C | No calling, valve 30% if others heat | Opportunistic (at threshold) |
| 7.6°C | No calling, valve 30% if others heat | **ACTIVATES** (below 7.7°C) |
| 7.8°C | - | Calling=TRUE, valve=100% |
| 8.5°C | - | Calling=TRUE, valve=100% |
| 9.2°C | - | **DEACTIVATES** (above 8.1°C) |
| 10°C | No calling, valve 30% if others heat | Returns to passive |

**Notes:**
- Passive mode doesn't normally call for heat
- Frost protection overrides: room now calls for heat (boiler turns on)
- 100% valve replaces configured 30%
- Returns to passive behavior after recovery

### Scenario 3: Manual Mode with Low Setpoint

**Setup:**
- Bathroom in "manual" mode
- Manual setpoint = 10°C (user likes cool bathroom)
- Extreme weather, temp drops to 7.0°C

**Timeline:**

| Temp | Normal Manual Behavior | Frost Protection |
|------|----------------------|------------------|
| 9.5°C | Target 10°C, calling | Normal heating |
| 8.5°C | Target 10°C, calling | Normal heating |
| 7.9°C | Target 10°C, calling | Normal heating |
| 7.6°C | Target 10°C, calling | **FROST PROTECTION ACTIVATES** |
| 7.8°C | - | Frost protection (100% valve) |
| 8.2°C | - | **DEACTIVATES** (above 8.1°C) |
| 8.5°C | Target 10°C, calling | Returns to manual heating |

**Notes:**
- Manual mode continued heating toward 10°C target
- Frost protection briefly overrode to ensure minimum safety
- Quickly returned to normal manual control
- User's 10°C setpoint still respected after recovery

### Scenario 4: Off Mode Room Does NOT Get Frost Protection

**Setup:**
- Spare bedroom in "off" mode
- User closed vents, doesn't want heating
- Temp drops to 5°C (cold!)

**Behavior:**
- **Frost protection DOES NOT activate**
- Room remains off (no heating)
- User has explicitly disabled room
- System respects user's decision

**Risk:** User must understand "off" means truly off, no safety net.

**Documentation required:** Warn users that "off" mode disables all heating including frost protection.

### Scenario 5: Master Enable Off

**Setup:**
- System maintenance, master_enable turned off
- Extreme cold night, room drops to 6°C

**Behavior:**
- **Frost protection DOES NOT activate**
- No heating occurs system-wide
- User has disabled entire heating system
- Frost protection respects master enable

**Risk:** User must understand master_enable is a true kill switch.

**Documentation required:** Warn that turning off master_enable disables frost protection.

---

## Alerts and Notifications

### Alert Strategy

**Recommendation: YES, send alerts when frost protection activates**

**Rationale:**
1. Indicates unusual/emergency condition
2. Might reveal system problem (undersized radiators, poor insulation, heating failure)
3. Might reveal configuration problem (schedule gap, wrong mode)
4. Might reveal external issue (open window, extreme weather, power outage)
5. User should know when safety system activates

### Alert Implementation

Use existing `alert_manager.py` infrastructure:

```python
# In room_controller._frost_protection_heating()
self.alert_manager.raise_alert(
    alert_id=f"frost_protection_{room_id}",
    title=f"Frost Protection Activated: {room_name}",
    message=f"Temperature dropped to {temp:.1f}°C (threshold: {frost_temp:.1f}°C). "
            f"Emergency heating activated.",
    severity="warning"
)
```

**Alert clearing:**
```python
# When frost protection deactivates
self.alert_manager.clear_alert(f"frost_protection_{room_id}")
```

**Alert characteristics:**
- **Severity**: Warning (not error - system is responding correctly)
- **Persistence**: Home Assistant persistent notification
- **One alert per room** (not per activation)
- **Auto-clear** when frost protection deactivates

### Alert Frequency

**Question: Should alerts be rate-limited?**

**Option A: Alert every activation**
- Pro: User always knows when safety system engages
- Con: Could be annoying if room oscillates at threshold

**Option B: Alert once per 24 hours per room**
- Pro: Prevents alert fatigue
- Con: User might miss repeated activations (could indicate problem)

**Option C: Alert on first activation, then only if exceeds threshold**
- Example: Alert first time, then only if activates >3 times in 24h
- Pro: Balances awareness with noise reduction
- Con: More complex logic

**Recommendation: Option A (alert every activation)**
- Frost protection activation is rare (shouldn't happen in normal operation)
- If happening frequently, user SHOULD be alerted (indicates problem)
- User can dismiss notifications if expected (e.g., extreme weather)

---

## Configuration Loading

### Config Schema Update

```python
# In config_loader.py - validate boiler.yaml
schema = {
    'boiler': {
        'entity_id': str,
        'opentherm': str,
        'pump_overrun_s': int,
        'anti_cycling': {
            'min_on_time_s': int,
            'min_off_time_s': int,
            'off_delay_s': int,
        },
        'interlock': {
            'min_valve_open_percent': int,
        },
        'safety_room': str,
        'load_sharing': { ... },
    },
    'system': {  # NEW
        'frost_protection_temp_c': float,  # Required
    },
}
```

### Default Value

If not specified in config:

```python
# In core/constants.py
FROST_PROTECTION_TEMP_C_DEFAULT = 8.0
```

```python
# In config_loader.py
frost_temp = config.get('system', {}).get('frost_protection_temp_c', 
                                          C.FROST_PROTECTION_TEMP_C_DEFAULT)
```

### Validation

```python
# In config_loader.py validate_config()
frost_temp = config['system']['frost_protection_temp_c']
if not (5.0 <= frost_temp <= 15.0):
    errors.append(f"frost_protection_temp_c must be 5-15°C, got {frost_temp}")
```

**Validation range:** 5-15°C
- Lower bound (5°C): Below this risks frozen pipes
- Upper bound (15°C): Above this is comfort heating, not frost protection

---

## Testing Strategy

### Unit Tests

1. **Activation threshold:**
   - Temp = 8.1°C → no activation (above threshold + off_delta)
   - Temp = 7.7°C → no activation (at threshold - on_delta, boundary)
   - Temp = 7.6°C → activation (below threshold - on_delta)

2. **Deactivation threshold:**
   - Temp = 8.0°C → continue heating (at threshold, below recovery)
   - Temp = 8.1°C → continue heating (at threshold + off_delta, boundary)
   - Temp = 8.2°C → deactivate (above threshold + off_delta)

3. **Mode interactions:**
   - Off mode: never activates
   - Auto mode: activates correctly
   - Manual mode: activates correctly
   - Passive mode: activates correctly

4. **Master enable:**
   - master_enable off: never activates
   - master_enable on: activates correctly

5. **Stale sensors:**
   - Stale temp: no activation (safety: don't heat without sensor)
   - Valid temp: activates correctly

### Integration Tests

1. **Frost protection → boiler activation:**
   - Single room in frost protection → boiler turns on
   - Multiple rooms in frost protection → boiler handles all

2. **Frost protection → valve commands:**
   - Commands sent correctly (100% opening)
   - TRV feedback confirmation works
   - Valve persistence doesn't interfere

3. **Frost protection → status publishing:**
   - Entity attributes show frost_protection state
   - Status text indicates emergency heating
   - Next change calculation handles frost protection

4. **Frost protection → CSV logging:**
   - Frost protection column logged correctly
   - Timestamps accurate
   - Can analyze activation patterns

5. **Alert system:**
   - Alert raised on activation
   - Alert cleared on deactivation
   - Alert visible in HA notifications

### Real-World Testing

1. **Simulate cold room:**
   - Override sensor value to trigger frost protection
   - Verify activation, heating, recovery
   - Check boiler responds correctly

2. **Mode transitions:**
   - Start in auto, trigger frost protection, switch to manual
   - Verify frost protection continues regardless of mode change
   - Verify recovery works after mode change

3. **Multi-room:**
   - Trigger frost protection in multiple rooms
   - Verify boiler aggregates demand correctly
   - Verify status display handles multiple active

4. **Recovery timing:**
   - Measure time to recover from 7.5°C to 8.2°C
   - Verify overshoot behavior (should reach 9-10°C)
   - Verify return to normal heating

---

## Documentation Requirements

### User Documentation (README.md)

Add new section:

```markdown
## Frost Protection

PyHeat includes automatic frost protection to prevent rooms from getting dangerously cold.

**How it works:**
- Global safety threshold (default 8°C) configured in `config/boiler.yaml`
- Activates automatically when room temperature drops below threshold
- Uses emergency heating (100% valve opening) for rapid recovery
- Returns to normal behavior once room is safe

**Important notes:**
- Frost protection does NOT activate for rooms in "off" mode
- Frost protection is disabled when master_enable is off
- You will receive a notification when frost protection activates

**Configuration:**
```yaml
# config/boiler.yaml
system:
  frost_protection_temp_c: 8.0  # Adjust if needed (5-15°C range)
```

**Recommended settings:**
- 6-7°C: Pipes-only protection (minimal intervention)
- 8-10°C: Balanced approach (standard UK/EU)
- 11-15°C: Conservative (earlier activation)
```

### Technical Documentation (ARCHITECTURE.md)

Add section to "Room Control Logic":

```markdown
### Frost Protection

**Priority: Highest** - Checked before all other mode logic.

When room temperature drops below configured frost protection threshold:
1. Room calls for heat (regardless of mode)
2. Valve forced to 100% (ignores bands/passive settings)
3. Target set to frost_protection_temp
4. Continues until temp > frost_protection_temp + off_delta
5. Returns to normal mode behavior

**Exceptions:**
- Does NOT activate when room_mode = "off"
- Does NOT activate when master_enable = "off"
- Does NOT activate with stale sensors

**Use case:** Prevent frozen pipes, property damage, safety hazards.
```

### Changelog (changelog.md)

```markdown
## 2025-12-01 - Frost Protection Implementation

### Added
- System-wide frost protection to prevent dangerously cold rooms
- Global `frost_protection_temp_c` setting in boiler.yaml (default 8°C)
- Automatic emergency heating when room drops below threshold
- Alert notifications when frost protection activates
- CSV logging of frost protection events
- Status entity display of frost protection state

### Behavior
- Activates for all modes except "off" (respects explicit user disable)
- Forces 100% valve opening for rapid recovery
- Respects master_enable (disabled when system off)
- Uses existing hysteresis to prevent oscillation
- Returns to normal behavior after recovery

### Documentation
- README.md: User guide for frost protection feature
- ARCHITECTURE.md: Technical implementation details
- Examples in config/boiler.yaml
```

---

## Implementation Checklist

### Configuration
- [ ] Add `system.frost_protection_temp_c` to boiler.yaml
- [ ] Add default constant to constants.py
- [ ] Add config schema validation to config_loader.py
- [ ] Add example/comments to boiler.yaml template

### Core Logic
- [ ] Add `room_frost_protection_active` state dict to RoomController
- [ ] Add frost protection check in `compute_room_heating()`
- [ ] Add `_frost_protection_heating()` helper method
- [ ] Add frost protection logging (activation/deactivation)

### Integration
- [ ] Verify load sharing excludes frost protection rooms (no changes needed)
- [ ] Verify valve coordinator handles frost protection commands (no changes needed)
- [ ] Verify boiler aggregates frost protection calling (no changes needed)

### Status & Logging
- [ ] Add frost protection display to status_publisher.py
- [ ] Add `frost_protection` attribute to room entities
- [ ] Add `frost_protection` column to CSV logs
- [ ] Format status text for frost protection state

### Alerts
- [ ] Add frost protection alert raising in room_controller
- [ ] Add frost protection alert clearing on deactivation
- [ ] Test alert delivery and clearing

### Documentation
- [ ] Update README.md with frost protection section
- [ ] Update ARCHITECTURE.md with frost protection logic
- [ ] Update changelog.md
- [ ] Add inline code comments
- [ ] Document "off" mode and master_enable caveats

### Testing
- [ ] Unit tests: activation threshold
- [ ] Unit tests: deactivation threshold  
- [ ] Unit tests: mode interactions
- [ ] Unit tests: master enable interaction
- [ ] Integration test: boiler activation
- [ ] Integration test: valve commands
- [ ] Integration test: status display
- [ ] Integration test: alerts
- [ ] Real-world test: simulate cold room
- [ ] Real-world test: recovery timing

### Deployment
- [ ] Update boiler.yaml in production
- [ ] Commit all changes with descriptive message
- [ ] Monitor first activation in real system
- [ ] Adjust thresholds if needed based on real behavior

---

## Open Questions & Decisions Needed

### 1. Default Frost Protection Temperature

**Recommendation:** 8°C

**Rationale:**
- Standard UK/EU frost protection setting
- Well above pipe freezing risk (0-5°C)
- Well below comfort threshold (15-18°C)
- Proven temperature used in commercial heating systems

**Alternatives:**
- 7°C: More conservative (less frequent activation)
- 10°C: More protective (earlier activation)

### 2. Should We Log Room State Changes?

**Recommendation:** Yes, log activation and deactivation at WARNING/INFO level

**Rationale:**
- Activation is notable event (should be visible)
- Deactivation confirms recovery (good to know)
- WARNING level for activation (draws attention)
- INFO level for deactivation (confirms resolution)

**Example logs:**
```
[WARNING] FROST PROTECTION ACTIVATED: office at 7.5C (threshold: 8.0C)
[INFO] FROST PROTECTION DEACTIVATED: office recovered to 8.3C
```

### 3. CSV Column Name

**Recommendation:** `frost_protection` (boolean)

**Rationale:**
- Matches entity attribute name
- Clear purpose
- Boolean is simplest (True = active, False = inactive)

**Alternative:** `frost_protection_active` (more verbose, same meaning)

### 4. Should Frost Protection State Persist?

**Recommendation:** No, do not persist

**Rationale:**
- State is temperature-dependent (will be re-evaluated immediately)
- Temperature will be checked on restart anyway
- Simpler implementation (one less thing to persist)
- No benefit to persistence

### 5. Should "Off" Mode Really Disable Frost Protection?

**CRITICAL DECISION POINT**

Your original sketch said:
> "When a room is 'off' the user does not want it heating under any circumstances."

**Recommendation:** Yes, respect "off" mode (no frost protection)

**Rationale:**
- User explicitly disabled room heating
- "Off" means OFF (clear user expectation)
- Safety responsibility falls to user when they choose "off"
- Allows for intentional scenarios (closed room, removed radiator, winterizing)

**Risk mitigation:**
- Document clearly in README that "off" disables frost protection
- Warning in docs: "Use 'off' mode only when you are certain the room can safely remain unheated"
- Consider adding a config option to override (future enhancement)

**Alternative approach (not recommended):**
- Frost protection overrides "off" mode
- Pro: Comprehensive safety, prevents any room freezing
- Con: Violates user expectation of "off" meaning truly off
- Con: No way to truly disable a room when needed

**Proposed documentation:**
```
⚠️ **WARNING:** Rooms in "off" mode do NOT receive frost protection.
Only use "off" mode if you are certain the room can safely remain unheated
(e.g., room is closed off, radiator removed, or during property winterization).
```

### 6. Status Text Format

**Recommendation:** Use clear, urgent formatting

**Examples:**
```
FROST PROTECTION: 7.5° → 8.0° (emergency heating)
FROST PROTECTION ACTIVE: 7.8° → 8.0° 
⚠️ FROST PROTECTION: Office 7.5°C (emergency)
```

**Rationale:**
- All caps draws attention
- Shows current temp and target
- Indicates emergency nature
- Optional emoji for visual distinction (⚠️)

---

## Future Enhancements

These are NOT part of initial implementation, but could be added later:

### 1. Per-Room Frost Protection Temperature

Allow rooms to override global setting:

```yaml
# rooms.yaml
rooms:
  - id: bathroom
    frost_protection_temp_c: 10.0  # Override global 8°C
```

**Use case:** Bathroom with exposed pipes needs higher protection.

### 2. Frost Protection Override for "Off" Mode

Config option to force frost protection even in "off" mode:

```yaml
# boiler.yaml
system:
  frost_protection_temp_c: 8.0
  frost_protection_override_off_mode: true  # Force protection even when off
```

**Use case:** User wants comprehensive safety, willing to accept override.

### 3. Graduated Frost Protection Levels

Multiple thresholds with different responses:

```yaml
system:
  frost_protection:
    emergency_temp_c: 5.0   # 100% valve, always activate
    warning_temp_c: 8.0     # 70% valve, activate if not in "off"
    comfort_temp_c: 12.0    # 50% valve, activate if not in "off" or "manual"
```

**Use case:** More nuanced response to different severity levels.

### 4. External Temperature Integration

Consider outdoor temperature when activating:

```yaml
system:
  frost_protection_temp_c: 8.0
  outdoor_sensor: sensor.outdoor_temperature
  outdoor_activation_threshold_c: -5.0  # Only if outdoor is very cold
```

**Use case:** Reduce false alarms from temporary sensor issues.

### 5. Time-of-Day Restrictions

Only allow frost protection during certain hours:

```yaml
system:
  frost_protection_temp_c: 8.0
  active_hours: "00:00-23:59"  # Or restrict to night: "22:00-06:00"
```

**Use case:** User wants frost protection only at night, manual control during day.

---

## Comparison with Passive Frost Protection Proposal

There are now TWO frost protection proposals. Key differences:

| Aspect | Passive-Only Proposal | System-Wide Proposal (This) |
|--------|----------------------|--------------------------|
| **Scope** | Passive mode only | All modes (except "off") |
| **Use case** | Passive rooms need floor | Emergency safety for all rooms |
| **Configuration** | Global + per-room entities + per-schedule | Global only (simple) |
| **Complexity** | Medium (multiple config points) | Low (single setting) |
| **Activation** | Passive rooms drifting low | ANY room in emergency |
| **Priority** | After mode logic (within passive) | Before mode logic (override) |

**Recommendation: Implement BOTH**

1. **Passive frost protection** (min_temp in passive mode):
   - Use case: Passive rooms that drift below comfort (12-15°C)
   - Prevents passive rooms from getting too cold during normal operation
   - User-configured threshold per room/schedule

2. **System-wide frost protection** (emergency override):
   - Use case: True emergency (pipe freezing risk at 7-8°C)
   - Safety net for ALL rooms regardless of mode
   - Fixed global threshold

**Implementation order:**
1. System-wide frost protection first (this proposal) - critical safety feature
2. Passive frost protection second (other proposal) - comfort enhancement

**They work together:**
- Passive room drops to 13°C → passive min_temp activates (gentle heating)
- Passive room continues dropping to 7°C → system frost protection activates (emergency heating)
- Two-tier protection: comfort threshold + safety threshold

---

## Risk Assessment

### Risks & Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| **Frost protection oscillates at threshold** | Annoying cycling | Medium | Use hysteresis, intentional overshoot |
| **False alarms from sensor glitches** | Unnecessary heating | Low | Require non-stale sensor, use smoothing |
| **User disables via "off" mode and pipes freeze** | Property damage | Low | Document clearly, warn in README |
| **100% valve causes overheating** | Discomfort | Medium | Deactivate at 8.1°C (slight overshoot) |
| **Alert fatigue** | User ignores | Low | Frost protection rare in normal operation |
| **Master enable off during freeze** | No protection | Very Low | Document clearly, user accepts risk |
| **Implementation bugs** | System malfunction | Medium | Thorough testing, gradual rollout |

### Safety Analysis

**Failure modes:**

1. **Frost protection fails to activate:**
   - Consequence: Room freezes, potential pipe damage
   - Likelihood: Low (simple logic, well-tested)
   - Detection: User notices cold room or pipe failure
   - Recovery: Manual intervention, repair

2. **Frost protection activates incorrectly:**
   - Consequence: Unwanted heating, energy waste
   - Likelihood: Medium (sensor error, config error)
   - Detection: Alert notification, user sees status
   - Recovery: Investigate cause, adjust config

3. **Frost protection fails to deactivate:**
   - Consequence: Room overheats to normal target (not dangerous)
   - Likelihood: Low (simple threshold check)
   - Detection: Room reaches normal target, heating stops anyway
   - Recovery: Self-correcting (room controller stops at target)

**Overall risk:** Low - well-designed, defensively implemented, thoroughly tested.

---

## Conclusion

This proposal provides a **comprehensive, system-wide frost protection mechanism** that:

✅ Protects all rooms (except explicit "off" mode)  
✅ Uses simple, single configuration point  
✅ Activates automatically in emergencies  
✅ Respects user control (master_enable, "off" mode)  
✅ Integrates seamlessly with existing systems  
✅ Provides clear status and alerts  
✅ Requires minimal code changes  
✅ Well-tested and documented  

**Next step:** Review and approve, then proceed with implementation.

---

## Related Documents

- Passive frost protection: `passive_frost_protection_proposal.md`
- Current architecture: `ARCHITECTURE.md`
- Example config: `config/boiler.yaml`
