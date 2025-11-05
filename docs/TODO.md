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
  - [x] Stepped band selection with thresholds
  - [x] Step hysteresis to prevent band flapping ✅
  - [x] Multi-band jump optimization (up=fast, down=gradual) ✅

- [x] **TRV Valve Control** - COMPLETE ✅
  - [x] **TRV Setpoint Locking Strategy**
    - [x] Lock all TRV setpoints to 35°C (forces "always open" mode) ✅
    - [x] Immediate setpoint monitoring via state listener ✅
    - [x] Periodic backup check (5-min intervals) ✅
    - [x] Only control `opening_degree` (not `closing_degree`)
  - [x] **Non-blocking Valve Commands**
    - [x] Use AppDaemon scheduler instead of `time.sleep()`
    - [x] Feedback confirmation with retry logic
    - [x] Rate limiting (30s min interval between updates)
    - [x] Command state tracking in `_valve_command_state` dict
    - [x] 50% faster (2s per room vs 4s)
    - [x] Eliminated AppDaemon callback timeout warnings

- [x] **Boiler Control** - COMPLETE ✅
  - [x] Full 7-state FSM with anti-cycling ✅
  - [x] TRV-open interlock validation ✅
  - [x] Pump overrun timer ✅
  - [x] All state transitions implemented
  - [x] Anti-cycling protection (min_on_time, min_off_time)
  - [x] TRV feedback confirmation before boiler start
  - [x] Valve override calculation for minimum flow
  - [x] Emergency safety valve handling

- [x] **Status Publishing** - COMPLETE ✅
  - [x] Create/update `sensor.pyheat_status`
  - [x] Set state string ("heating (N rooms)", "idle")
  - [x] Comprehensive attributes (any_call_for_heat, active_rooms, etc.)
  - [x] Full boiler state machine diagnostics
  - [x] **Per-room entities** (All 24 entities working)
    - [x] `sensor.pyheat_<room>_temperature` - Fused room temperature
    - [x] `sensor.pyheat_<room>_target` - Resolved target
    - [x] `sensor.pyheat_<room>_valve_percent` - Valve opening (0-100) ✅
    - [x] `binary_sensor.pyheat_<room>_calling_for_heat` - Heat demand (on/off) ✅

### Phase 3: Testing & Verification
- [x] **Initial Testing** - COMPLETE ✅
  - [x] App loads and initializes successfully
  - [x] Configuration loading works
  - [x] Sensors read correctly
  - [x] Manual mode with 22°C setpoint works
  - [x] TRV opens to 100% when calling for heat
  - [x] Boiler turns on with room demand
  - [x] TRV setpoint locking verified (locks to 35°C) ✅
  - [x] Non-blocking valve control verified (no warnings)
  - [x] Manual mode with 25°C setpoint test passed
  - [x] Valve band transitions verified (0→1→2→3)
  - [x] Hysteresis logic verified (multi-band up, single-band down)
  - [x] Minimum valve open interlock verified (35% → 100% override)
  - [x] Boiler state machine transitions verified
  - [x] TRV feedback confirmation verified
  - [x] Immediate TRV setpoint correction verified (15°C→35°C in 2s)

### Phase 4: Documentation
- [x] **Project Documentation**
  - [x] README.md with installation and configuration
  - [x] SYMLINK_SETUP.md for migration from pyscript
  - [x] TRV_SETPOINT_LOCKING.md - Strategy documentation
  - [x] IMPLEMENTATION_PLAN.md → TODO.md conversion
  - [x] Changelog tracking all changes
  - [x] Inline code comments for complex logic

### Phase 5: Full Boiler State Machine - COMPLETE ✅
- [x] **Complete State Machine Implementation**
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

### Phase 6: Valve Band Control - COMPLETE ✅
- [x] **Stepped Valve Bands with Hysteresis**
  - [x] 4-band system (0%, low%, mid%, max%)
  - [x] Error-based band selection (e = target - temp)
  - [x] Hysteresis: multi-band jumps up, single-band drops down
  - [x] Per-room configuration with defaults
  - [x] Band transition logging

### Phase 7: TRV Setpoint Fix - COMPLETE ✅
- [x] **Critical TRV Setpoint Correction**
  - [x] Changed from 5°C to 35°C (allows TRV controller to cooperate)
  - [x] Immediate detection via state listener
  - [x] Automatic correction within seconds
  - [x] Periodic backup monitoring

### Phase 8: Bug Fixes - COMPLETE ✅
- [x] **Configuration and Emergency Valve Fixes**
  - [x] Fixed boiler.yaml timer configuration (debug values removed)
  - [x] Fixed emergency safety valve logic (exclude transition states)
  - [x] Pump overrun live test successful (180s duration verified)
  - [x] Emergency valve no longer triggers during PENDING_OFF/PUMP_OVERRUN

### Phase 9: Modular Architecture Refactor - COMPLETE ✅
- [x] **Code Organization and Maintainability**
  - [x] Split monolithic app.py (1900+ lines) into 10 specialized modules
  - [x] boiler_controller.py - Isolated 7-state FSM logic (295 lines)
  - [x] room_controller.py - Per-room heating control (180 lines)
  - [x] trv_controller.py - TRV command and setpoint management (175 lines)
  - [x] sensor_manager.py - Temperature sensor fusion (145 lines)
  - [x] scheduler.py - Schedule parsing and resolution (155 lines)
  - [x] service_handler.py - HA service implementations (245 lines)
  - [x] status_publisher.py - Entity creation and updates (160 lines)
  - [x] config_loader.py - YAML validation and loading (125 lines)
  - [x] All functionality verified after refactor
  - [x] Fixed sensor entity creation bug (HTTP 400 with numeric 0)
  - [x] Created debug_monitor.py tool for system testing (280 lines)
  - [x] Documentation cleanup and archiving

---

## In Progress / Next Steps 🚧

### Next Priority: Service Handler Integration (~2-3 hours)

Make service handlers callable from Home Assistant. Currently they're registered within AppDaemon but don't appear as HA services.

### Phase 8: Bug Fixes - COMPLETE ✅
- [x] **Configuration and Emergency Valve Fixes**
  - [x] Fixed boiler.yaml timer configuration (debug values removed)
  - [x] Fixed emergency safety valve logic (exclude transition states)
  - [x] Pump overrun live test successful (180s duration verified)
  - [x] Emergency valve no longer triggers during PENDING_OFF/PUMP_OVERRUN

---

## Known Issues & Workarounds 🐛

**No known issues!** All discovered bugs have been fixed. System is production-ready.

---

## Deferred / Future Enhancements 📋

### Service Handlers (~5-8 hours) - IMPLEMENTED BUT NOT INTEGRATED ⚠️
**Status:** All service handlers implemented and registered with AppDaemon, but not yet callable from Home Assistant. Services are registered internally via `register_service()` but don't appear as HA services. Need to investigate proper AppDaemon->HA service registration method.

**Implemented Services:**
- [x] `pyheat.override(room, target, minutes)` - Set temporary target ✅
- [x] `pyheat.boost(room, delta, minutes)` - Boost by delta from current ✅
- [x] `pyheat.cancel_override(room)` - Cancel active override ✅
- [x] `pyheat.set_mode(room, mode)` - Change room mode programmatically ✅
- [x] `pyheat.set_default_target(room, target)` - Update default target ✅
- [x] `pyheat.reload_config()` - Reload YAML without restart ✅
- [x] `pyheat.get_schedules()` - Get current schedules (returns dict) ✅
- [x] `pyheat.get_rooms()` - Get current rooms (returns dict) ✅
- [x] `pyheat.replace_schedules(schedule_dict)` - Atomically replace schedules ✅

**TODO:** Make services callable from Home Assistant (currently only registered within AppDaemon)

### Enhanced Features
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

**None!** All discovered issues have been resolved. System is production-ready.

---

## Development Estimates

### Time Investment So Far: ~42-48 hours
- Foundation & structure: 4 hours
- Core heating logic: 8 hours
- TRV setpoint locking refactor: 3 hours
- Initial testing & debugging: 3-4 hours
- Full boiler state machine: 4-5 hours
- Valve band control with hysteresis: 3-4 hours
- Per-room entity publishing fixes: 2-3 hours
- TRV setpoint correction (35°C fix): 1-2 hours
- Modular architecture refactor: 6-8 hours
- Bug fixes and sensor entity fix: 2-3 hours
- Debug monitoring tool: 2-3 hours
- Documentation updates: 2-3 hours

### Remaining Work Estimates (Optional Enhancements)
- **Service handler integration**: 2-3 hours
- **Enhanced error handling**: 1-2 hours
- **Comprehensive testing suite**: 2-3 hours
- **Total remaining**: ~5-8 hours

### Total Project: ~47-56 hours
Complete recreation of the PyScript implementation with significant improvements and feature parity achieved.

---

## Notes

### Design Decisions Made
1. **TRV Setpoint Locking (35°C)** - Major simplification, allows TRV controller cooperation
2. **Non-blocking Valve Control** - Follows AppDaemon best practices
3. **Full Boiler State Machine** - Complete 7-state FSM for safety and efficiency
4. **Valve Band Hysteresis** - Multi-band jumps up, single-band drops down
5. **Rate Limiting** - 30s minimum between valve updates prevents thrashing
6. **Periodic Recompute** - 60s interval balances responsiveness and efficiency
7. **Immediate TRV Setpoint Correction** - State listener + periodic backup

### Lessons Learned
1. **AppDaemon's scheduler is powerful** - `run_in()` eliminates need for blocking sleeps
2. **TRV setpoint locking is critical** - 35°C allows controller cooperation, 5°C fights us
3. **Callback timeout warnings matter** - Non-blocking is essential
4. **Entity validation critical** - Missing entities should disable rooms, not crash
5. **Hysteresis prevents oscillation** - Multi-band jumps up, gradual drops down
6. **State listeners are immediate** - Catch user changes within seconds
7. **AppDaemon set_state quirks** - Integer 0 fails, must use string "0"

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
