# PyHeat Implementation Progress

This file tracks progress against the specification in `docs/pyheat-spec.md`.

## ✅ Completed

### Phase 0: Bootstrap & Infrastructure
- [x] **`__init__.py`** - Bootstrap module (loads orchestrator, triggers, services)
  - Relative imports working correctly
  - Safe module loading with error handling
  - Clean startup/shutdown lifecycle
  
- [x] **`ha_triggers.py`** - Event trigger adapter (stub)
  - All trigger decorators registered
  - Debounced recompute working
  - Startup sequence with immediate + delayed recompute
  - Cron tick (1-minute) working
  
- [x] **`config_loader.py`** - YAML configuration loader (working)
  - Loads rooms.yaml and schedules.yaml
  - Builds room registry
  - Validation and error handling
  
- [x] **`core.py`** - Central orchestrator (stub interface)
  - Basic structure with all required methods
  - Service handlers defined (stub)
  - Event handlers defined (stub)
  
- [x] **`constants.py`** - Centralized configuration ✨ NEW
  - All default parameters defined
  - Hysteresis, valve bands, safety defaults
  - Entity ID patterns and derivation
  - Utility functions for entity generation
  - Validation helpers

## 🚧 In Progress

### Phase 1: Core Domain Logic
- [x] **`sensors.py`** - Temperature sensor fusion ✨ NEW
  - Primary/fallback sensor averaging
  - Staleness detection (timeout_m)
  - Per-room temperature publishing
  - Sensor status diagnostics
  - Integrated with orchestrator
  
- [x] **`scheduler.py`** - Schedule resolution ✨ NEW
  - Schedule block evaluation by day/time
  - Override/boost application
  - Holiday mode integration
  - Current block info for status
  - Integrated with orchestrator
  
- [x] **`room_controller.py`** - Room state machine ✨ NEW
  - Room class with first-class state management
  - Target resolution (precedence: off → manual → override → schedule)
  - Call-for-heat with asymmetric hysteresis
  - Valve percentage with stepped bands + step hysteresis
  - Override/boost state tracking with expiry
  - Status string generation
  - Integrated with orchestrator

## 📋 Planned

### Phase 1: Core Domain Logic (continued)
  
- [ ] **`trv.py`** - TRV control adapter
  - Valve command issuing
  - Feedback reading
  - Command/feedback matching

### Phase 2: Boiler & Safety
- [ ] **`boiler.py`** - Boiler control with safety
  - Anti short-cycling
  - TRV-open interlock
  - Force-off handling
  
- [ ] **`status.py`** - Status composition
  - Global status entity
  - Per-room status strings

### Phase 3: Integration
- [ ] **`ha_services.py`** - Service registration
  - All pyheat.* services mapped
  - Argument validation
  - Error handling
  
- [ ] **Core orchestrator implementation** - Replace stubs
  - Wire up all modules
  - Implement recompute_all()
  - Entity publishing

### Phase 4: Testing & Polish
- [ ] End-to-end testing
- [ ] Edge case handling
- [ ] Documentation
- [ ] Performance optimization

## 📝 Notes

**Current Status:** Core domain logic modules (sensors, scheduler, room_controller) implemented and integrated. All modules loading successfully.

**Test Results:**
- ✅ SensorManager: 1 room (pete) with 1 sensor configured
- ✅ ScheduleManager: 1 room schedule with 12 blocks loaded, default 19.5°C
- ✅ RoomControllerManager: 1 room (pete) initialized with hysteresis (0.40/0.10), bands (0.30/0.80/1.50)
- ✅ All three modules wired into orchestrator
- ✅ Configuration loading on startup working cleanly

**Next Steps:** Implement trv.py for TRV control adapter (valve commands and feedback reading) 
1. Implement `room_controller.py` for room state machine and logic
2. Test temperature fusion and schedule resolution
3. Begin entity publishing for visibility
