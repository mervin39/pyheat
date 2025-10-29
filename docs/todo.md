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

## 📋 Planned

### Phase 1: Core Domain Logic (continued)
- [ ] **`room_controller.py`** - Room state machine
  - Target resolution (precedence chain)
  - Call-for-heat with hysteresis
  - Valve percentage with bands
  - Status string generation
  
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

**Current Status:** Sensors and scheduler modules implemented and integrated. Both modules loading and configuring successfully.

**Test Results:**
- ✅ SensorManager: 1 room (pete) with 1 sensor configured
- ✅ ScheduleManager: 1 room schedule with 12 blocks loaded
- ✅ Both modules wired into orchestrator
- ✅ Configuration loading on startup working

**Next Steps:** 
1. Implement `room_controller.py` for room state machine and logic
2. Test temperature fusion and schedule resolution
3. Begin entity publishing for visibility
