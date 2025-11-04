# PyHeat Implementation TODO

## Status: Full Boiler State Machine Implemented ✅

**Last Updated**: 2025-11-04  
**Current Phase**: Production-Ready Heating Control with Advanced Boiler Management

---

## Completed Items ✅

### Phase 1: Foundation & Structure
- [x] **Project Setup**
  - [x] Create `/opt/appdata/appdaemon/conf/apps/pyheat/` directory
  - [x] Initialize git repository
  - [x] Copy configuration files from PyScript version
  - [x] Create `apps.yaml` entry for pyheat app
  - [x] Set up proper .gitignore

- [x] **Core Files Created**
  - [x] `constants.py` - All configuration defaults and constants
  - [x] `app.py` - Main AppDaemon application
  - [x] `__init__.py` - Package initialization
  - [x] `config/rooms.yaml` - Room definitions (6 rooms)
  - [x] `config/schedules.yaml` - Heating schedules
  - [x] `config/boiler.yaml` - Boiler configuration

- [x] **AppDaemon Integration**
  - [x] Implement app class inheriting from `hass.Hass`
  - [x] Configuration loading from YAML files
  - [x] Entity validation and error handling
  - [x] TRV entity derivation from climate entities
  - [x] Callback registration system
  - [x] Periodic recompute scheduling (60s intervals)
  - [x] Startup behavior with delayed recomputes

### Phase 2: Core Heating Logic
- [x] **Sensor Fusion** (`get_room_temperature()`)
  - [x] Average multiple primary sensors per room
  - [x] Fall back to fallback sensors if primaries unavailable
  - [x] Detect stale sensors based on timeout_m
  - [x] Mark rooms as stale if all sensors unavailable

- [x] **Schedule Resolution** (`get_scheduled_target()`)
  - [x] Parse weekly schedule for current day/time
  - [x] Find active schedule block or use default_target
  - [x] Handle holiday mode substitution

- [x] **Target Resolution** (`resolve_room_target()`)
  - [x] Check room mode (off/manual/auto)
  - [x] Apply precedence: off → manual → override → schedule
  - [x] Handle override/boost from timers (basic implementation)

- [x] **Call-for-Heat Logic** (`compute_call_for_heat()`)
  - [x] Calculate error: `e = target - temp`
  - [x] Apply asymmetric hysteresis (on_delta_c, off_delta_c)
  - [x] Maintain previous state in deadband zone

- [x] **Valve Band Calculation** (`compute_valve_percent()`)
  - [x] Map error to valve bands (0%, low%, mid%, max%)
  - [x] Basic band selection based on thresholds
  - [ ] Step hysteresis to prevent band flapping (deferred)
  - [ ] Multi-band jump optimization (deferred)

- [x] **TRV Valve Control** - MAJOR SIMPLIFICATION ✨
  - [x] **TRV Setpoint Locking Strategy** (NEW)
    - [x] Lock all TRV setpoints to 5°C (forces "always open" mode)
    - [x] Automatic setpoint monitoring and correction (5-min intervals)
    - [x] Only control `opening_degree` (not `closing_degree`)
  - [x] **Non-blocking Valve Commands**
    - [x] Use AppDaemon scheduler instead of `time.sleep()`
    - [x] Feedback confirmation with retry logic
    - [x] Rate limiting (30s min interval between updates)
    - [x] Command state tracking in `_valve_command_state` dict
    - [x] 50% faster (2s per room vs 4s)
    - [x] Eliminated AppDaemon callback timeout warnings

- [x] **Boiler Control** (Simplified)
  - [x] Basic on/off control based on room demand
  - [x] Turn on when any room calls for heat
  - [x] Turn off when no rooms calling for heat
  - [x] Track boiler state changes
  - [ ] Full 7-state FSM with anti-cycling (deferred)
  - [ ] TRV-open interlock validation (deferred)
  - [ ] Pump overrun timer (deferred)

- [x] **Status Publishing** (Basic)
  - [x] Create/update `sensor.pyheat_status`
  - [x] Set state string ("heating (N rooms)", "idle")
  - [x] Basic attributes (any_call_for_heat, active_rooms, etc.)
  - [ ] Per-room detailed status (deferred)
  - [ ] Error tracking and reporting (deferred)

### Phase 3: Testing & Verification
- [x] **Initial Testing**
  - [x] App loads and initializes successfully
  - [x] Configuration loading works
  - [x] Sensors read correctly
  - [x] Manual mode with 22°C setpoint works
  - [x] TRV opens to 100% when calling for heat
  - [x] Boiler turns on with room demand
  - [x] TRV setpoint locking verified (locks to 5°C)
  - [x] Non-blocking valve control verified (no warnings)
  - [x] Manual mode with 25°C setpoint test passed

### Phase 4: Documentation
- [x] **Project Documentation**
  - [x] README.md with installation and configuration
  - [x] SYMLINK_SETUP.md for migration from pyscript
  - [x] TRV_SETPOINT_LOCKING.md - Strategy documentation
  - [x] IMPLEMENTATION_PLAN.md → TODO.md conversion
  - [x] Changelog tracking all changes
  - [x] Inline code comments for complex logic

### Phase 5: Full Boiler State Machine
- [x] **Complete State Machine Implementation** ✅
  - [x] 7-state FSM with proper transitions
  - [x] Anti-cycling protection using timer helpers
  - [x] TRV interlock validation and feedback confirmation
  - [x] Valve override calculation for minimum flow requirements
  - [x] Pump overrun with valve position persistence
  - [x] Off-delay for brief demand interruptions
  - [x] Emergency safety valve override
  - [x] Enhanced status publishing with state machine diagnostics
  - [x] Binary control mode (on/off via setpoint)
  - [x] Comprehensive logging and error handling

---

## In Progress / Next Steps 🚧

### Immediate Priority
**✅ Full Boiler State Machine - COMPLETED!** (~4-5 hours actual)
  - [x] Implement 7-state FSM:
    - [x] `STATE_OFF` - Boiler off, no demand
    - [x] `STATE_PENDING_ON` - Waiting for TRV interlock before starting
    - [x] `STATE_ON` - Boiler running normally
    - [x] `STATE_PENDING_OFF` - Brief delay before turning off
    - [x] `STATE_PUMP_OVERRUN` - Boiler off, pump running to dissipate heat
    - [x] `STATE_INTERLOCK_BLOCKED` - Demand present but TRVs not open
    - [x] `STATE_INTERLOCK_FAILED` - (Not implemented - merged with INTERLOCK_BLOCKED)
  - [x] Anti-cycling protection
    - [x] Minimum on time (min_on_time_s)
    - [x] Minimum off time (min_off_time_s)
  - [x] TRV-open interlock
    - [x] Verify sum of all TRV open percentages >= min_valve_open_percent
    - [x] Monitor feedback sensors with timeout
    - [x] Valve override calculation for interlock requirements
  - [x] Pump overrun
    - [x] Keep pump running for pump_overrun_s after boiler off
    - [x] Maintain valve positions during overrun
    - [x] Persist valve positions to helper entity
  - [x] Off-delay
    - [x] 30s delay before turning off for brief demand changes

---

## Deferred / Future Enhancements 📋

### Service Handlers (~2-3 hours)
- [ ] **Room Override/Boost Services**
  - [ ] `pyheat.override(room, target, minutes)` - Set temporary target
  - [ ] `pyheat.boost(room, delta, minutes)` - Boost by delta from current
  - [ ] `pyheat.cancel_override(room)` - Cancel active override
  - [ ] Timer management and cleanup
  - [ ] Persistence across restarts

- [ ] **Configuration Services**
  - [ ] `pyheat.set_mode(room, mode)` - Change room mode programmatically
  - [ ] `pyheat.set_default_target(room, target)` - Update default target
  - [ ] `pyheat.reload_config()` - Reload YAML without restart
  - [ ] `pyheat.replace_schedules(schedule_dict)` - Dynamic schedule updates

- [ ] **Diagnostic Services**
  - [ ] `pyheat.force_valve(room, percent)` - Manual valve control for testing
  - [ ] `pyheat.get_diagnostics()` - Return full system state
  - [ ] `pyheat.force_recompute()` - Immediate recompute trigger

### Enhanced Features
- [ ] **Valve Band Step Hysteresis** (~2 hours)
  - [ ] Implement `step_hysteresis_c` to prevent band oscillation
  - [ ] Multi-band jump logic (e.g., 0% → 100% direct jump)
  - [ ] Track previous band per room

- [ ] **Enhanced Status Publishing** (~1-2 hours)
  - [ ] Per-room detailed status in attributes
  - [ ] Valve position tracking
  - [ ] Error states and warnings
  - [ ] Last action timestamps
  - [ ] Sensor staleness indicators

- [ ] **Advanced Error Handling** (~2-3 hours)
  - [ ] Graceful degradation when entities missing
  - [ ] Invalid state value handling
  - [ ] Configuration validation on load
  - [ ] Service call failure recovery
  - [ ] Persistent error logging

- [ ] **State Persistence** (~1-2 hours)
  - [ ] Restore override/boost from timer states on startup
  - [ ] Pump overrun valve positions via input_text helper
  - [ ] Last commanded valve positions
  - [ ] Boiler state machine state restoration

### Testing & Validation
- [ ] **Comprehensive Integration Tests**
  - [ ] All schedule transitions (day/time boundaries)
  - [ ] Override/boost functionality
  - [ ] Holiday mode activation/deactivation
  - [ ] Master enable/disable
  - [ ] Sensor failure scenarios
  - [ ] TRV feedback timeout scenarios
  - [ ] Boiler state machine edge cases
  - [ ] Multi-room demand scenarios

- [ ] **Performance Testing**
  - [ ] Measure recompute execution time
  - [ ] Verify no callback timeout warnings under load
  - [ ] Monitor memory usage over extended operation
  - [ ] Validate rate limiting effectiveness

### Documentation
- [ ] **User Guide**
  - [ ] Configuration examples
  - [ ] Service usage examples
  - [ ] Troubleshooting guide
  - [ ] FAQ

- [ ] **Developer Documentation**
  - [ ] Architecture overview
  - [ ] State machine diagrams
  - [ ] Control flow diagrams
  - [ ] API reference

---

## Known Issues / Technical Debt 🐛

1. **Valve Band Step Hysteresis Not Implemented**
   - Currently jumps between bands without hysteresis
   - May cause oscillation in edge cases
   - Mitigation: Rate limiting (30s) reduces impact
   - Priority: Low (working well without it)

2. **Override/Boost Timer Handling Incomplete**
   - Basic timer monitoring exists
   - Full restore-from-timer logic not implemented
   - Service handlers not created
   - Priority: Medium

3. **No Service Handlers**
   - Cannot programmatically override/boost rooms
   - Cannot reload config without restart
   - Workaround: Use helper entities directly
   - Priority: Medium

4. **Limited Error Reporting**
   - Status entity has basic state machine detail
   - No per-room error tracking in status
   - Errors only in logs
   - Priority: Low

---

## Development Estimates

### Time Investment So Far: ~20-25 hours
- Foundation & structure: 4 hours
- Core heating logic: 8 hours
- TRV setpoint locking refactor: 3 hours
- Testing & debugging: 2-3 hours
- Full boiler state machine: 4-5 hours
- Documentation: 2-3 hours

### Remaining Work Estimates
- **Service handlers**: 2-3 hours
- **Enhanced features**: 3-5 hours
- **Comprehensive testing**: 2-3 hours
- **Total remaining**: ~7-11 hours

### Total Project: ~27-36 hours
Complete recreation of the PyScript implementation with significant improvements.

---

## Notes

### Design Decisions Made
1. **TRV Setpoint Locking** - Major simplification over dual-command approach
2. **Non-blocking Valve Control** - Follows AppDaemon best practices
3. **Simplified Initial Boiler Logic** - Defer complexity until core proven
4. **Rate Limiting** - 30s minimum between valve updates prevents thrashing
5. **Periodic Recompute** - 60s interval balances responsiveness and efficiency

### Lessons Learned
1. **AppDaemon's scheduler is powerful** - `run_in()` eliminates need for blocking sleeps
2. **TRV setpoint locking is elegant** - Forces predictable valve behavior
3. **Callback timeout warnings matter** - Non-blocking is essential
4. **Entity validation critical** - Missing entities should disable rooms, not crash

### Migration from PyScript
- **Execution model**: Async/await → Callback-based
- **State access**: Direct state.set() → Helper entities + set_state()
- **Performance**: Improved (no GIL issues, true threading)
- **Reliability**: Better (no state consistency issues)
- **Maintainability**: Much better (cleaner code, better debugging)

---

## References

- [AppDaemon Documentation](https://appdaemon.readthedocs.io/)
- [Home Assistant API](https://www.home-assistant.io/developers/rest_api/)
- Original PyScript implementation: `/home/pete/tmp/pyheat_pyscript/`
- TRV Control Strategy: `docs/TRV_SETPOINT_LOCKING.md`
- Migration Guide: `docs/SYMLINK_SETUP.md`
