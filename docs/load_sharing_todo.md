# Load Sharing Implementation TODO

**Feature Branch:** `feature/load-sharing-phase0`  
**Status:** Phase 0 Complete ✅  
**Started:** 2025-11-26  
**Phase 0 Completed:** 2025-11-26

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

## Phase 1: Tier 1 Selection Logic (Not Started)

**Goal:** Implement schedule-aware pre-warming selection  
**Duration:** 3-4 hours  
**Risk:** LOW - Isolated tier logic

### Tasks:
- [ ] Implement entry condition evaluation
- [ ] Implement Tier 1 room selection
- [ ] Integrate with valve coordinator
- [ ] Add status publishing
- [ ] Test with real schedules
- [ ] Update changelog

---

## Phase 2: Tier 2 Extended Lookahead (Not Started)

**Goal:** Add extended window fallback  
**Duration:** 2-3 hours  
**Risk:** LOW - Extends existing logic

---

## Phase 3: Tier 3 Fallback Priority (Not Started)

**Goal:** Add priority list fallback  
**Duration:** 2-3 hours  
**Risk:** LOW - Simple priority ordering

---

## Phase 4: Integration & Testing (Not Started)

**Goal:** Full system integration and validation  
**Duration:** 4-6 hours  
**Risk:** MODERATE - System-wide testing required

