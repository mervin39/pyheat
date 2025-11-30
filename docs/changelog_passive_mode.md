# Passive Mode Implementation - Changelog Entry

## 2025-11-30: Passive Heating Mode (Phases 1-6) - IN PROGRESS

**Status:** IN PROGRESS 🚧

**Branch:** `feature/passive-mode`

**Summary:**
Adding "passive" heating mode that allows rooms to open valves opportunistically without calling for heat from the boiler. Passive mode enables heating during times when other rooms are calling for heat, or when a small amount of baseline warmth is desired without triggering boiler operation.

**Implementation Progress:**

### ✅ Phase 1: Constants (COMPLETE)
- Added `MODE_PASSIVE`, `VALID_MODES` to constants.py
- Added `HELPER_ROOM_PASSIVE_MAX_TEMP` and `HELPER_ROOM_PASSIVE_VALVE_PERCENT` helper entity templates
- Added `PASSIVE_MAX_TEMP_DEFAULT` (18.0°C) and `PASSIVE_VALVE_PERCENT_DEFAULT` (30%)

### ✅ Phase 2: Home Assistant Entities (COMPLETE)
- Updated `config/ha_yaml/pyheat_package.yaml`:
  - Added "Passive" option to all 6 room mode selectors
  - Added `input_number.pyheat_{room}_passive_max_temp` for each room (10-30°C, default 18°C)
  - Added `input_number.pyheat_{room}_passive_valve_percent` for each room (0-100%, default 30%)
  - Updated `pyheat_set_mode` script to handle passive mode
- Entities confirmed loaded in Home Assistant

### ✅ Phase 3: Scheduler Enhancement (COMPLETE)
- Modified `core/scheduler.py`:
  - Changed `resolve_room_target()` return type from `Optional[float]` to `Optional[Dict]`
  - New dict format: `{'target': float, 'mode': 'active'|'passive', 'valve_percent': Optional[int]}`
  - Added helper methods `_get_passive_max_temp()` and `_get_passive_valve_percent()`
  - Updated `get_scheduled_target()` to return dict format
  - Schedule blocks can now include 'mode' and 'valve_percent' fields
  - Updated `get_next_schedule_change()` to handle dict return
  - Added `Dict` to typing imports

### ✅ Phase 4: Room Controller Enhancement (COMPLETE)
- Modified `controllers/room_controller.py`:
  - Updated `compute_room()` to handle dict return from scheduler
  - Extract target, mode, and valve_percent from scheduler dict
  - Added `'operating_mode'` field to result dict (active/passive/off)
  - Implemented passive mode valve control: binary threshold (valve opens if temp < max_temp)
  - Passive mode never calls for heat (calling always False)
  - Fixed `initialize_room_state()` to extract target float from dict
- Modified `services/status_publisher.py`:
  - Updated `publish_room_entities()` to extract target from scheduled_info dict

### ✅ Phase 6: Load Sharing Enhancement (COMPLETE)
- Modified `managers/load_sharing_manager.py`:
  - Added passive operating_mode check to `_select_tier1_rooms()`
  - Added passive operating_mode check to `_select_tier2_rooms()`
  - Added passive operating_mode check to `_select_tier3_rooms()`
  - Passive rooms now excluded from load sharing (user has manual valve control)

### ✅ Phase 7: Status Publisher Enhancement (COMPLETE)
- Modified `services/status_publisher.py`:
  - Added `operating_mode` field to room attributes in `publish_system_status()`
  - Added `passive_max_temp` field when room in passive mode
  - Status API now exposes passive mode state for pyheat-web integration

### ✅ Phase 10: Heating Logger Enhancement (COMPLETE)
- Modified `services/heating_logger.py`:
  - Added `{room_id}_operating_mode` column to CSV logs
  - Added operating_mode change detection in `should_log()`
  - Updated prev_state cache to track operating_mode changes
- Modified `app.py`:
  - Added `operating_mode` to room data passed to heating logger

### ⏳ Phase 5: Boiler Controller (NO CHANGES NEEDED)
- Verified existing boiler interlock already counts passive room valves
- No code changes required

### ⏳ Remaining Phases:
- Phase 8: API Handler Enhancement (expose passive settings endpoints)
- Phase 9: Alert Manager Enhancement (passive mode alerts)

### ✅ Phase 11: Documentation - COMPLETE
- Updated `README.md` with comprehensive passive mode usage examples
- Updated `docs/ARCHITECTURE.md`:
  - Added passive mode to flow diagrams (target resolution, room heating logic, status publication)
  - Documented passive room exclusion in load sharing (all 3 tiers)
  - Updated target precedence hierarchy with passive mode
  - Enhanced room control processing steps and return values
  - Added passive mode to integration compatibility list
- Documentation fully reflects implemented passive mode behavior

### ✅ Phase 12: State Listeners for Passive Settings - COMPLETE
- Added real-time state listeners for passive mode entities
- Implemented `room_passive_setting_changed()` callback handler
- Changes to `passive_max_temp` and `passive_valve_percent` now trigger immediate recompute
- Resolves 60-second delay issue (entities previously only checked during periodic recompute)
- Consistent UX with other room configuration entities (mode, manual setpoint, override)

**Passive Mode Behavior:**
- **Manual Passive Mode:** User sets room to "passive" mode via input_select
  - Room valve opens to configured percentage when temp < max_temp
  - Room never calls for heat (demand = 0W)
  - Useful for: baseline warmth, opportunistic heating, maintaining minimum temperature
  
- **Scheduled Passive Periods:** Auto mode can include passive schedule blocks
  - Example: `passive 6-8am` (gentle morning warm-up), then `active 8am-10pm` (full heating)
  - Schedule YAML: `{start: "06:00", target: 18.0, mode: "passive", valve_percent: 30}`
  
- **Override Always Active:** When override triggered, always uses active heating (never passive)
  - User needs immediate temperature rise → requires active PID control

**Testing Status:**
- ✅ System running without errors after Phases 1-7, 10, 11, 12
- ✅ Core passive mode functionality implemented
- ✅ Load sharing correctly excludes passive rooms
- ✅ Status API exposes operating_mode for UI integration
- ✅ CSV logs include operating_mode for analysis
- ✅ Passive settings respond immediately to user changes
- ⏳ Need to test: actual passive mode operation with real room

**Commits:**
1. `463f764` - Phase 3: Update scheduler to return dict with mode and valve_percent
2. `2392e32` - Phase 4: Update room_controller and status_publisher for dict-based scheduler
3. `f2b2d6f` - Phase 6: Exclude passive rooms from load sharing tier selection
4. `e754921` - Phases 7 & 10: Status publisher and heating logger enhancements
5. `be80668` - Phase 11: Add passive mode documentation to README.md (partial)
6. `fe8848b` - Phase 11: Complete ARCHITECTURE.md passive mode documentation
7. Pending - Phase 12: Add state listeners for passive settings
6. `fe8848b` - Phase 11: Complete ARCHITECTURE.md passive mode documentation

**Next Steps:**
1. Complete remaining implementation phases (7-11)
2. Test passive mode operation with real rooms
3. Update pyheat-web to display passive mode status
4. Full system testing and validation
