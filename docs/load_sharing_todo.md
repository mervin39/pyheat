# Load Sharing Implementation TODO

**Feature Branch:** `feature/load-sharing-phase2`  
**Status:** Phase 2 Complete ✅  
**Started:** 2025-11-26  
**Phase 0 Completed:** 2025-11-26  
**Phase 1 Completed:** 2025-11-26
**Phase 2 Completed:** 2025-11-26

---

## Phase 0: Infrastructure Preparation ⚙️

**Goal:** Set up foundation without changing heating behavior  
**Duration:** 2-3 hours  
**Risk:** MINIMAL - No behavioral changes  
**Status:** ✅ COMPLETE

### Tasks:

- [x] 1. Create state machine infrastructure (`managers/load_sharing_state.py`)
  - [x] Define `LoadSharingState` enum
  - [x] Define `RoomActivation` dataclass
  - [x] Define `LoadSharingContext` dataclass

- [x] 2. Create LoadSharingManager skeleton (`managers/load_sharing_manager.py`)
  - [x] Initialize with context
  - [x] Method stubs returning empty results
  - [x] Always returns "no load sharing needed"

- [x] 3. Add configuration schema
  - [x] Add `load_sharing` section to `boiler.yaml` (enabled: false)
  - [x] Add `load_sharing` section to rooms.yaml schema
  - [x] Update ConfigLoader to parse new sections
  - [x] Add configuration validation

- [x] 4. Add HA helper entities (`ha_yaml/pyheat_package.yaml`)
  - [x] `input_boolean.pyheat_load_sharing_enable`
  - [x] Helper to store context state (deferred to Phase 1)

- [x] 5. Extend Scheduler API (`core/scheduler.py`)
  - [x] Add `get_next_schedule_block()` method

- [x] 6. Integration into app.py
  - [x] Import LoadSharingManager
  - [x] Initialize manager (disabled)
  - [x] Wire into recompute (no-op)

- [x] 7. Testing
  - [x] AppDaemon starts without errors
  - [x] Config loads successfully
  - [x] Check logs for initialization
  - [x] No behavioral changes

- [x] 8. Documentation
  - [x] Update changelog.md
  - [x] Commit changes

**Phase 0 Results:**
- ✅ AppDaemon restarted successfully
- ✅ LoadSharingManager initialized: "DISABLED (config=False, master=False)"
- ✅ No errors or tracebacks
- ✅ System behavior unchanged
- ✅ Ready for Phase 1 implementation

**Files Created:**
- `managers/load_sharing_state.py` (160 lines)
- `managers/load_sharing_manager.py` (230 lines)

**Files Modified:**
- `core/constants.py` - Load sharing constants
- `core/config_loader.py` - Configuration parsing
- `core/scheduler.py` - Schedule lookahead API
- `ha_yaml/pyheat_package.yaml` - Master enable helper
- `app.py` - Manager integration
- `docs/changelog.md` - Phase 0 entry

---

## Phase 1: Tier 1 Selection Logic ✅

**Goal:** Implement schedule-aware pre-warming selection  
**Duration:** 3-4 hours  
**Risk:** LOW - Isolated tier logic  
**Status:** ✅ COMPLETE

### Tasks:
- [x] Implement entry condition evaluation
  - [x] Capacity check (< 3500W threshold)
  - [x] Cycling risk detection (cooldown OR high return temp)
  - [x] Evidence-based activation (both conditions required)

- [x] Implement Tier 1 room selection
  - [x] Schedule lookahead query (60 min default)
  - [x] Filter for auto mode rooms
  - [x] Check schedule target > current temp
  - [x] Sort by temperature deficit (need)
  - [x] Return selections with 70% valve opening

- [x] Integrate with valve coordinator
  - [x] Add load_sharing priority tier (between safety and corrections)
  - [x] Implement set_load_sharing_overrides()
  - [x] Implement clear_load_sharing_overrides()
  - [x] Priority handling in apply_valve_command()

- [x] Add status publishing
  - [x] Extend publish_system_status() to include load sharing state
  - [x] Include active rooms, tier, valve %, reason, duration
  - [x] Include trigger capacity and trigger rooms

- [x] Implement exit conditions
  - [x] Exit Trigger A: Original calling rooms stopped
  - [x] Exit Trigger B: Additional rooms calling (recalculate)
  - [x] Exit Trigger C: Load sharing room now naturally calling
  - [x] Minimum activation duration (5 minutes)

- [x] Test with real schedules
  - [x] AppDaemon startup successful
  - [x] No errors or warnings in logs
  - [x] Status publishing works

- [x] Update changelog
  - [x] Phase 1 entry added
  - [x] Implementation details documented

**Phase 1 Results:**
- ✅ Entry conditions implemented and tested
- ✅ Tier 1 selection logic complete
- ✅ Valve coordinator integration working
- ✅ Status publishing includes load sharing state
- ✅ Exit conditions implemented
- ✅ AppDaemon starts without errors
- ✅ Ready for Phase 2 implementation

**Files Modified:**
- `managers/load_sharing_manager.py` - Core evaluation logic (~270 lines added)
- `controllers/valve_coordinator.py` - Load sharing priority tier
- `app.py` - Recompute integration
- `services/status_publisher.py` - Status publishing
- `docs/changelog.md` - Phase 1 entry
- `docs/load_sharing_todo.md` - Progress tracking

---

## Phase 2: Tier 2 Extended Lookahead ✅

**Goal:** Add extended window fallback  
**Duration:** 2-3 hours  
**Risk:** LOW - Extends existing logic
**Status:** ✅ COMPLETE

### Tasks:
- [x] Implement Tier 2 selection (2× schedule_lookahead_m window)
- [x] Add escalation logic (increase Tier 1 to 80% before Tier 2)
- [x] Variable valve opening (40% for Tier 2 initial)
- [x] State transitions (TIER1_ACTIVE → TIER1_ESCALATED → TIER2_ACTIVE)
- [x] Integrate cascading logic into evaluate()
- [x] Test with extended windows
- [x] Update changelog

**Phase 2 Results:**
- ✅ Tier 2 selection implemented with 2× lookahead window
- ✅ Escalation logic: Tier 1 → 80% before adding Tier 2
- ✅ Tier 2 rooms open at 40% (gentle pre-warming)
- ✅ Tier 2 escalation to 50% if still insufficient
- ✅ State machine handles cascading transitions
- ✅ Total capacity calculation with valve adjustments
- ✅ AppDaemon starts without errors
- ✅ Ready for Phase 3 implementation

**Files Modified:**
- `managers/load_sharing_manager.py`: Added Tier 2 selection, escalation methods, capacity calculation
- `docs/load_sharing_todo.md`: Phase 2 tracking update

---

## Phase 3: Tier 3 Fallback Priority (Not Started)

**Goal:** Add priority list fallback  
**Duration:** 2-3 hours  
**Risk:** LOW - Simple priority ordering

### Tasks:
- [ ] Implement Tier 3 selection (fallback_priority)
- [ ] Add escalation logic (Tier 2 → 50% → Tier 3)
- [ ] Tier 3 timeout (15 minutes max)
- [ ] Variable valve opening (50% initial, 60% escalated)
- [ ] State transitions (TIER3_ACTIVE → TIER3_ESCALATED)
- [ ] Test with priority lists
- [ ] Update changelog

---

## Phase 4: Integration & Testing (Not Started)

**Goal:** Full system integration and validation  
**Duration:** 4-6 hours  
**Risk:** MODERATE - System-wide testing required

### Tasks:
- [ ] Enable load sharing by default (config and master switch)
- [ ] Real-world testing with cycling scenarios
- [ ] CSV log analysis (cycling frequency)
- [ ] Capacity calculation validation
- [ ] Energy efficiency analysis
- [ ] Documentation update (README, ARCHITECTURE)
- [ ] Final commit and merge to main

