# PyHeat Implementation TODO

## Status: Production-Ready with HTTP API ✅

**Last Updated**: 2025-11-13  
**Current Phase**: Complete Heating Control with External API Integration

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

### Phase 3: HTTP API Integration ✅
- [x] **API Handler Module** (`api_handler.py`)
  - [x] Register HTTP endpoints via AppDaemon's `register_endpoint()`
  - [x] Bridge HTTP requests to service handlers
  - [x] JSON request/response handling
  - [x] Synchronous endpoint implementation (avoid asyncio.Task issues)
  - [x] Proper parameter extraction from namespace

- [x] **Control Endpoints**
  - [x] `pyheat_override` - Set absolute temperature override ✅
  - [x] `pyheat_boost` - Apply delta boost to target ✅
  - [x] `pyheat_cancel_override` - Cancel active override/boost ✅
  - [x] `pyheat_set_mode` - Set room mode (auto/manual/off) ✅

- [x] **Status Endpoints**
  - [x] `pyheat_get_status` - Complete system and room status ✅
  - [x] `pyheat_get_schedules` - Current schedule configuration ✅
  - [x] Override type tracking with delta storage ✅
  - [x] Override end time (ISO 8601 timestamps) for countdown timers ✅

- [x] **Schedule Management**
  - [x] `pyheat_replace_schedules` - Atomic schedule update ✅
  - [x] `pyheat_reload_config` - Reload from YAML files ✅
  - [x] Handle both legacy and pyheat-web schedule formats ✅
  - [x] Fix double-nested list corruption bug ✅

### Phase 4: Testing & Verification
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
  - [x] 6-state FSM with proper transitions
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
  - [x] boiler_controller.py - Isolated 6-state FSM logic (620 lines)
  - [x] room_controller.py - Per-room heating control (180 lines)
  - [x] trv_controller.py - TRV command and setpoint management (175 lines)
  - [x] sensor_manager.py - Temperature sensor fusion (145 lines)
  - [x] scheduler.py - Schedule parsing and resolution (155 lines)
  - [x] service_handler.py - HA service implementations (245 lines)
  - [x] status_publisher.py - Entity creation and updates (160 lines)
  - [x] config_loader.py - YAML validation and loading (170 lines)
  - [x] api_handler.py - REST API endpoints (580 lines)
  - [x] All functionality verified after refactor
  - [x] Fixed sensor entity creation bug (HTTP 400 with numeric 0)
  - [x] Created debug_monitor.py tool for system testing (280 lines)
  - [x] Documentation cleanup and archiving

### Phase 10: Documentation - COMPLETE ✅
- [x] **Comprehensive Technical Documentation**
  - [x] ARCHITECTURE.md - Complete system architecture with detailed algorithms
  - [x] README.md - Installation, configuration, and usage guide
  - [x] STATUS_FORMAT_SPEC.md - Status text formatting specification
  - [x] TODO.md - Project tracking and implementation history
  - [x] changelog.md - Detailed change tracking
  - [x] BUG_OVERRIDE_HYSTERESIS_TRAP.md - Known issue documentation

---

## Current Status: Production-Ready ✅

The system is fully functional and production-ready with:
- ✅ Complete 6-state boiler FSM with safety interlocks
- ✅ Multi-room heating control with individual schedules
- ✅ TRV setpoint locking and valve control
- ✅ Sensor fusion with staleness detection
- ✅ AppDaemon service interface for Home Assistant
- ✅ REST API for external applications (pyheat-web)
- ✅ Comprehensive documentation

---

## In Progress / Next Steps 🚧

**No active development in progress.** System is stable and feature-complete for current requirements.

---

## Known Issues & Workarounds 🐛

**No known issues!** All discovered bugs have been fixed. System is production-ready.

---

## Deferred / Future Enhancements 📋

### AppDaemon Services - COMPLETE ✅
**Status:** All service handlers implemented and registered with AppDaemon. Services are callable from Home Assistant via `appdaemon.pyheat_*` service calls.

**Available Services:**
- ✅ `appdaemon.pyheat_override` - Set temporary target
- ✅ `appdaemon.pyheat_boost` - Boost by delta from current
- ✅ `appdaemon.pyheat_cancel_override` - Cancel active override
- ✅ `appdaemon.pyheat_set_mode` - Change room mode programmatically
- ✅ `appdaemon.pyheat_set_default_target` - Update default target
- ✅ `appdaemon.pyheat_reload_config` - Reload YAML without restart
- ✅ `appdaemon.pyheat_get_schedules` - Get current schedules (returns dict)
- ✅ `appdaemon.pyheat_get_rooms` - Get current rooms (returns dict)
- ✅ `appdaemon.pyheat_replace_schedules` - Atomically replace schedules

**REST API Endpoints:**
All services also available via HTTP at `/api/appdaemon/pyheat_*` for external applications (pyheat-web).

### Enhanced Features (Inspired by PyScript Version)

- [x] **Persistent Notification System** - COMPLETE ✅ (2025-11-12)
  - [x] Create notification manager for critical/serious errors
  - [x] Spam prevention by tracking active notifications (debouncing + rate limiting)
  - [x] Auto-dismiss when issues resolve
  - [x] Severity levels (CRITICAL, WARNING)
  - [x] Category-based notifications (boiler, TRV, sensor, config, system)
  - [x] User-friendly notifications with emoji indicators
  - **Implementation:** `alert_manager.py` module with comprehensive alert tracking
  - **Documentation:** `docs/ALERT_MANAGER.md`

- [ ] **Enhanced Watchdog and Auto-Recovery**
  - [ ] 1-minute watchdog cron for stuck state detection
  - [ ] Monitor prolonged PENDING_ON or INTERLOCK_BLOCKED states
  - [ ] Auto-recovery from anomalous states
  - [ ] Critical notifications for prolonged issues
  - [ ] Emergency safety valve monitoring
  - **Note:** Some safety features exist; this would enhance them

- [ ] **REST API Documentation**
  - [ ] Complete curl examples for all services
  - [ ] `?return_response=true` usage patterns for AppDaemon
  - [ ] Reading configuration via state attributes
  - [ ] Service response structures with JSON examples
  - [ ] Integration guide for external applications
  - **Note:** PyScript version has extensive REST API docs - adapt for AppDaemon

- [ ] **Advanced Error Handling**
  - [ ] Graceful degradation when entities missing
  - [ ] Invalid state value handling
  - [ ] Configuration validation on load
  - [ ] Service call failure recovery
  - [ ] Persistent error logging

- [ ] **Enhanced State Persistence**
  - [ ] Verify override/boost restoration from timer states works correctly
  - [ ] Document pump overrun valve position persistence
  - [ ] Verify last commanded valve positions tracking
  - [ ] Document boiler state machine state restoration
  - [ ] Add comprehensive state restoration logging
  - **Note:** Basic persistence exists; this would document and enhance it

- [ ] **Architectural Specification Document**
  - [ ] Domain objects and contracts
  - [ ] Event flow and data flow diagrams
  - [ ] File-by-file responsibilities
  - [ ] State model and precedence rules
  - [ ] Startup behavior specifications
  - **Note:** PyScript has detailed `pyheat-spec.md` - useful for future maintainers

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

### Active Bugs

**No active bugs!** 🎉

**Previously Resolved:**

1. **Override Hysteresis Trap** - RESOLVED ✅ (2025-11-10)
   - **Issue**: Override set close to current temp may not trigger heating
   - **Resolution**: Implemented target change detection with hysteresis bypass
   - **Details**: See `docs/BUG_OVERRIDE_HYSTERESIS_TRAP.md`
   - **Fix**: Track `room_last_target` and bypass hysteresis deadband when target changes
   - **Impact**: All target changes (override, schedule, mode) now respond immediately

---

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
