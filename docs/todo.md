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
- [ ] **`sensors.py`** - Temperature sensor fusion
  - Primary/fallback sensor averaging
  - Staleness detection
  - Per-room temperature publishing
  
- [ ] **`scheduler.py`** - Schedule resolution
  - Schedule block evaluation
  - Override/boost application
  - Holiday mode integration

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

**Current Status:** Bootstrap complete, constants defined. Ready to implement domain logic modules.

**Next Steps:** 
1. Implement `sensors.py` for temperature fusion
2. Implement `scheduler.py` for target resolution
3. Wire both into orchestrator for basic functionality
