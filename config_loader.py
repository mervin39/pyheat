"""
config_loader.py - YAML configuration loader for Pyheat

Responsibilities:
- Load rooms.yaml and schedules.yaml from hass.config.path(...)
- Validate both files; on failure, keep last good config and return error
- Build in-memory room registry by matching room IDs across both files
- Warn for rooms without schedules and schedules referencing unknown rooms
- Provide current configs to orchestrator; perform atomic writes for updates
- Implement read/write using task.executor(...) to avoid blocking event loop
- Normalize TRV entity_id (climate.*) → derive number.*/sensor.* IDs; verify existence

File I/O notes (PyScript + Home Assistant):
- Paths: always resolve with hass.config.path("config/<file>.yaml")
- Non-blocking: ALL disk I/O via await task.executor(...)
- Read: executor→ read text → yaml.safe_load → normalize None to {} → validate → return dict
- Write: validate first → yaml.safe_dump → executor writes temp file → os.replace(temp, final)
- Concurrency: guard reads/writes with a lock (used inside executor too)
- Error handling: on parse/validation failure, KEEP last good config; log one clear error
- Existence/permissions: ensure parent directory exists; treat missing file as empty YAML
- No extra deps: use stdlib + PyYAML shipped with HA
"""

import asyncio
import os
import tempfile
from typing import Dict, List, Tuple, Optional, Any

import yaml

# Concurrency lock for file operations
_lock = asyncio.Lock()

# Last good configs (kept when validation fails)
_last_rooms_cfg = {}
_last_schedules_cfg = {}


# ============================================================================
# Path resolution
# ============================================================================

def _get_config_path(filename: str) -> str:
    """Resolve config path using hass.config.path().
    
    Args:
        filename: Relative path like "pyscript/apps/pyheat/config/rooms.yaml"
    
    Returns:
        Absolute path to the config file
    """
    # Build path relative to HA config directory (hass is global in pyscript)
    return hass.config.path(f"pyscript/apps/pyheat/config/{filename}")


# ============================================================================
# Non-blocking file I/O (executor pattern)
# ============================================================================

@pyscript_executor
def _read_file_sync(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Synchronous file read (runs in executor thread).
    
    Args:
        path: Absolute file path
    
    Returns:
        (content_str, error_str) where one is None
    """
    try:
        # Ensure parent directory exists
        parent = os.path.dirname(path)
        if not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        
        # Missing file is treated as empty YAML
        if not os.path.exists(path):
            return "", None
        
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content, None
    except Exception as exc:
        return None, str(exc)


async def _read_yaml_file(path: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Read and parse YAML file (non-blocking).
    
    Args:
        path: Absolute file path
    
    Returns:
        (parsed_dict, error_str) where one is None
    """
    async with _lock:
        content, err = _read_file_sync(path)
        if err:
            return None, f"Failed to read {path}: {err}"
        
        try:
            # Parse YAML; treat None (empty file) as {}
            data = yaml.safe_load(content)
            if data is None:
                data = {}
            return data, None
        except yaml.YAMLError as exc:
            return None, f"YAML parse error in {path}: {exc}"


@pyscript_executor
def _write_file_sync(path: str, content: str) -> Optional[str]:
    """Synchronous atomic file write (runs in executor thread).
    
    Args:
        path: Absolute file path
        content: String content to write
    
    Returns:
        error_str or None on success
    """
    try:
        # Ensure parent directory exists
        parent = os.path.dirname(path)
        if not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        
        # Atomic write: temp file in same directory → os.replace
        temp_fd, temp_path = tempfile.mkstemp(dir=parent, prefix=".tmp_", suffix=".yaml")
        try:
            os.write(temp_fd, content.encode("utf-8"))
            os.close(temp_fd)
            
            # Set permissions to 644 (readable by all, writable by owner)
            os.chmod(temp_path, 0o644)
            
            os.replace(temp_path, path)
            return None
        except Exception:
            os.close(temp_fd)
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
    except Exception as exc:
        return str(exc)


async def _write_yaml_file(path: str, data: Dict) -> Optional[str]:
    """Write dict to YAML file atomically (non-blocking).
    
    Args:
        path: Absolute file path
        data: Dict to serialize
    
    Returns:
        error_str or None on success
    """
    try:
        content = yaml.safe_dump(data, default_flow_style=False, allow_unicode=True)
    except Exception as exc:
        return f"YAML serialization error: {exc}"
    
    async with _lock:
        err = _write_file_sync(path, content)
        if err:
            return f"Failed to write {path}: {err}"
        return None


# ============================================================================
# Validation helpers
# ============================================================================

def _validate_rooms(data: Dict) -> Tuple[bool, Optional[str]]:
    """Validate rooms.yaml structure and content.
    
    Rules:
    - Schema: each room requires id, at least one sensor, exactly one trv.entity_id
    - Duplicates: duplicate room id → fail
    - Bounds: precision ∈ {0,1,2}, timeout_m ≥ 1
    - IDs: entity IDs must be lowercase HA IDs
    - Roles: each sensor needs a role (primary/fallback)
    
    Args:
        data: Parsed rooms.yaml dict
    
    Returns:
        (ok: bool, error: str|None)
    """
    if not isinstance(data, dict):
        return False, "rooms.yaml must be a dict"
    
    rooms_list = data.get("rooms", [])
    if not isinstance(rooms_list, list):
        return False, "rooms.yaml: 'rooms' must be a list"
    
    seen_ids = set()
    
    for idx, room in enumerate(rooms_list):
        if not isinstance(room, dict):
            return False, f"rooms.yaml: room #{idx} is not a dict"
        
        # Required: id
        room_id = room.get("id")
        if not room_id or not isinstance(room_id, str):
            return False, f"rooms.yaml: room #{idx} missing or invalid 'id'"
        
        # Check for duplicates
        if room_id in seen_ids:
            return False, f"rooms.yaml: duplicate room id '{room_id}'"
        seen_ids.add(room_id)
        
        # Required: sensors (at least one)
        sensors = room.get("sensors", [])
        if not isinstance(sensors, list) or len(sensors) == 0:
            return False, f"rooms.yaml: room '{room_id}' has no sensors"
        
        for sidx, sensor in enumerate(sensors):
            if not isinstance(sensor, dict):
                return False, f"rooms.yaml: room '{room_id}' sensor #{sidx} is not a dict"
            
            entity_id = sensor.get("entity_id")
            if not entity_id or not isinstance(entity_id, str):
                return False, f"rooms.yaml: room '{room_id}' sensor #{sidx} missing 'entity_id'"
            
            # Must be lowercase
            if entity_id != entity_id.lower():
                return False, f"rooms.yaml: room '{room_id}' sensor entity_id must be lowercase"
            
            # Role must be primary or fallback
            role = sensor.get("role")
            if role not in ("primary", "fallback"):
                return False, f"rooms.yaml: room '{room_id}' sensor #{sidx} role must be 'primary' or 'fallback'"
        
        # Required: trv.entity_id (exactly one)
        trv = room.get("trv")
        if not isinstance(trv, dict):
            return False, f"rooms.yaml: room '{room_id}' missing 'trv' dict"
        
        trv_entity_id = trv.get("entity_id")
        if not trv_entity_id or not isinstance(trv_entity_id, str):
            return False, f"rooms.yaml: room '{room_id}' trv missing 'entity_id'"
        
        # Must be lowercase and start with "climate."
        if trv_entity_id != trv_entity_id.lower():
            return False, f"rooms.yaml: room '{room_id}' trv entity_id must be lowercase"
        if not trv_entity_id.startswith("climate."):
            return False, f"rooms.yaml: room '{room_id}' trv entity_id must be a climate.* entity"
        
        # Optional: precision ∈ {0,1,2}
        precision = room.get("precision", 1)
        if precision not in (0, 1, 2):
            return False, f"rooms.yaml: room '{room_id}' precision must be 0, 1, or 2"
        
        # Optional: timeout_m ≥ 1
        timeout_m = room.get("timeout_m", 180)
        if not isinstance(timeout_m, (int, float)) or timeout_m < 1:
            return False, f"rooms.yaml: room '{room_id}' timeout_m must be ≥ 1"
    
    return True, None


def _validate_schedules(data: Dict) -> Tuple[bool, Optional[str]]:
    """Validate schedules.yaml structure and content.
    
    This function is RESILIENT: it skips invalid blocks but loads valid ones.
    Only structural errors (like missing 'rooms' list) cause complete failure.
    
    Rules:
    - Schema: each room entry requires id, default_target, week.mon..sun keys
    - Targets: numeric °C in range 5-35
    - Times: "HH:MM" 00:00-23:59; start < end; same-day only
    - Overlaps: overlapping blocks for a day are removed (both blocks)
    - Ordering: blocks are sorted by start time on load
    - Invalid blocks: logged as warnings and skipped (not fatal)
    
    Args:
        data: Parsed schedules.yaml dict (will be MODIFIED in-place to remove invalid blocks)
    
    Returns:
        (ok: bool, error: str|None)
        - ok=True even if some blocks were skipped (check logs for warnings)
        - ok=False only for structural errors that prevent loading
    """
    if not isinstance(data, dict):
        return False, "schedules.yaml must be a dict"
    
    rooms_list = data.get("rooms", [])
    if not isinstance(rooms_list, list):
        return False, "schedules.yaml: 'rooms' must be a list"
    
    weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    
    for idx, room in enumerate(rooms_list):
        if not isinstance(room, dict):
            log.warning(f"schedules.yaml: room #{idx} is not a dict - skipping")
            continue
        
        # Required: id
        room_id = room.get("id")
        if not room_id or not isinstance(room_id, str):
            log.warning(f"schedules.yaml: room #{idx} missing or invalid 'id' - skipping")
            continue
        
        # Required: default_target (numeric, 5-35)
        default_target = room.get("default_target")
        if not isinstance(default_target, (int, float)):
            log.warning(f"schedules.yaml: room '{room_id}' default_target must be numeric - using 16.0")
            room["default_target"] = 16.0
        elif not (5 <= default_target <= 35):
            log.warning(f"schedules.yaml: room '{room_id}' default_target {default_target}°C out of range - using 16.0")
            room["default_target"] = 16.0
        
        # Required: week (dict with all weekdays)
        week = room.get("week")
        if not isinstance(week, dict):
            log.warning(f"schedules.yaml: room '{room_id}' missing 'week' dict - creating empty week")
            week = {}
            room["week"] = week
        
        # Ensure all weekdays exist
        for day in weekdays:
            if day not in week:
                week[day] = []
            
            blocks = week[day]
            if not isinstance(blocks, list):
                log.warning(f"schedules.yaml: room '{room_id}' week.{day} not a list - resetting to empty")
                blocks = []
                week[day] = blocks
            
            # Validate blocks and collect valid ones
            valid_blocks = []
            parsed_blocks = []  # (start_mins, end_mins, block_dict, original_index)
            
            for bidx, block in enumerate(blocks):
                if not isinstance(block, dict):
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} not a dict - skipping")
                    continue
                
                start = block.get("start")
                end = block.get("end")  # Optional: if missing, means block runs until midnight
                target = block.get("target")
                
                # Validate start time (required)
                if not start or not isinstance(start, str):
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} missing 'start' - skipping")
                    continue
                
                parts = start.split(":")
                if len(parts) != 2:
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} start must be HH:MM - skipping")
                    continue
                
                try:
                    hh, mm = int(parts[0]), int(parts[1])
                    if not (0 <= hh <= 23 and 0 <= mm <= 59):
                        raise ValueError()
                    start_mins = hh * 60 + mm
                except ValueError:
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} start '{start}' invalid time - skipping")
                    continue
                
                # Validate end time (optional - if missing, defaults to midnight = 24:00 = 1440 minutes)
                if end is not None:
                    if not isinstance(end, str):
                        log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} end must be a string - skipping")
                        continue
                    
                    parts = end.split(":")
                    if len(parts) != 2:
                        log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} end must be HH:MM - skipping")
                        continue
                    
                    try:
                        hh, mm = int(parts[0]), int(parts[1])
                        if not (0 <= hh <= 23 and 0 <= mm <= 59):
                            raise ValueError()
                        end_mins = hh * 60 + mm
                    except ValueError:
                        log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} end '{end}' invalid time - skipping")
                        continue
                else:
                    # No end time specified = runs until midnight (1440 minutes = 24:00)
                    end_mins = 1440
                
                # start < end (same-day only; if end is midnight it's treated as 1440 > start)
                if start_mins >= end_mins:
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} start >= end ({start} >= {end or '24:00'}) - skipping")
                    continue
                
                # Validate target
                if not isinstance(target, (int, float)):
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} target must be numeric - skipping")
                    continue
                if not (5 <= target <= 35):
                    log.warning(f"schedules.yaml: room '{room_id}' {day} block #{bidx} target {target}°C out of range (5-35) - skipping")
                    continue
                
                # This block is valid so far
                parsed_blocks.append((start_mins, end_mins, block, bidx))
            
            # Check for overlaps and remove overlapping blocks
            parsed_blocks.sort()
            skip_indices = set()
            
            for i in range(len(parsed_blocks) - 1):
                _, end_i, _, idx_i = parsed_blocks[i]
                start_next, _, _, idx_next = parsed_blocks[i + 1]
                if start_next < end_i:
                    log.warning(
                        f"schedules.yaml: room '{room_id}' {day} has overlapping blocks "
                        f"(block #{idx_i} and #{idx_next}) - skipping both"
                    )
                    skip_indices.add(i)
                    skip_indices.add(i + 1)
            
            # Collect valid non-overlapping blocks
            for i, (_, _, block, _) in enumerate(parsed_blocks):
                if i not in skip_indices:
                    valid_blocks.append(block)
            
            # Replace the day's blocks with only valid ones
            week[day] = valid_blocks
            
            if len(valid_blocks) < len(blocks):
                log.info(f"schedules.yaml: room '{room_id}' {day}: kept {len(valid_blocks)}/{len(blocks)} blocks")
    
    return True, None


# ============================================================================
# Entity existence checks (HA state checks)
# ============================================================================

def _check_entity_exists(entity_id: str) -> bool:
    """Check if an entity exists in Home Assistant.
    
    Args:
        entity_id: Entity ID to check
    
    Returns:
        True if entity exists, False otherwise
    """
    try:
        return state.exist(entity_id)
    except Exception:
        return False


def _derive_trv_entities(trv_base: str) -> Dict[str, str]:
    """Derive TRV control/feedback entity IDs from climate base.
    
    Args:
        trv_base: The part after "climate." (e.g., "living_trv")
    
    Returns:
        Dict with keys: opening_cmd, closing_cmd, opening_fb, closing_fb
    """
    return {
        "opening_cmd": f"number.{trv_base}_valve_opening_degree",
        "closing_cmd": f"number.{trv_base}_valve_closing_degree",
        "opening_fb": f"sensor.{trv_base}_valve_opening_degree_z2m",
        "closing_fb": f"sensor.{trv_base}_valve_closing_degree_z2m",
    }


def _validate_entity_existence(rooms_cfg: Dict) -> List[str]:
    """Check entity existence and return warnings for missing entities.
    
    Args:
        rooms_cfg: Validated rooms config dict
    
    Returns:
        List of warning messages (empty if all OK)
    """
    warnings = []
    
    for room in rooms_cfg.get("rooms", []):
        room_id = room["id"]
        
        # Check sensors
        for sensor in room.get("sensors", []):
            entity_id = sensor["entity_id"]
            if not _check_entity_exists(entity_id):
                warnings.append(
                    f"Room '{room_id}': sensor '{entity_id}' does not exist in HA; "
                    "room may not function until sensor is available"
                )
        
        # Check TRV and derived entities
        trv_entity_id = room["trv"]["entity_id"]
        if not _check_entity_exists(trv_entity_id):
            warnings.append(
                f"Room '{room_id}': TRV climate entity '{trv_entity_id}' does not exist in HA"
            )
        
        # Derive and check TRV control/feedback entities
        trv_base = trv_entity_id.replace("climate.", "")
        derived = _derive_trv_entities(trv_base)
        
        for key, entity_id in derived.items():
            if not _check_entity_exists(entity_id):
                warnings.append(
                    f"Room '{room_id}': derived TRV entity '{entity_id}' ({key}) does not exist; "
                    "TRV control will fail until entity is available"
                )
    
    return warnings


# ============================================================================
# Cross-validation (rooms ↔ schedules)
# ============================================================================

def _cross_validate(rooms_cfg: Dict, schedules_cfg: Dict) -> List[str]:
    """Cross-validate rooms and schedules; return warnings.
    
    Args:
        rooms_cfg: Validated rooms config
        schedules_cfg: Validated schedules config
    
    Returns:
        List of warning messages
    """
    warnings = []
    
    room_ids = {r["id"] for r in rooms_cfg.get("rooms", [])}
    schedule_ids = {r["id"] for r in schedules_cfg.get("rooms", [])}
    
    # Rooms without schedules
    for room_id in room_ids - schedule_ids:
        warnings.append(f"Room '{room_id}' has no schedule defined in schedules.yaml")
    
    # Schedules without rooms
    for room_id in schedule_ids - room_ids:
        warnings.append(f"Schedule for '{room_id}' has no corresponding room in rooms.yaml")
    
    return warnings


# ============================================================================
# Public API
# ============================================================================

async def load_rooms() -> Tuple[Dict, bool, Optional[str]]:
    """Load and validate rooms.yaml.
    
    Returns:
        (rooms_dict, ok: bool, err: str|None)
    """
    global _last_rooms_cfg
    
    path = _get_config_path("rooms.yaml")
    data, err = await _read_yaml_file(path)
    
    if err:
        log.error(f"Failed to load rooms.yaml: {err}")
        return _last_rooms_cfg, False, err
    
    ok, err = _validate_rooms(data)
    if not ok:
        log.error(f"Validation failed for rooms.yaml: {err}")
        return _last_rooms_cfg, False, err
    
    # Check entity existence (warnings only)
    warnings = _validate_entity_existence(data)
    for warn in warnings:
        log.warning(warn)
    
    # Success: update last good config
    _last_rooms_cfg = data
    return data, True, None


async def load_schedules() -> Tuple[Dict, bool, Optional[str]]:
    """Load and validate schedules.yaml.
    
    Returns:
        (sched_dict, ok: bool, err: str|None)
    """
    global _last_schedules_cfg
    
    path = _get_config_path("schedules.yaml")
    data, err = await _read_yaml_file(path)
    
    if err:
        log.error(f"Failed to load schedules.yaml: {err}")
        return _last_schedules_cfg, False, err
    
    ok, err = _validate_schedules(data)
    if not ok:
        log.error(f"Validation failed for schedules.yaml: {err}")
        return _last_schedules_cfg, False, err
    
    # Success: update last good config
    _last_schedules_cfg = data
    return data, True, None


async def load_all() -> Tuple[bool, Optional[str]]:
    """Load and validate both rooms.yaml and schedules.yaml.
    
    Performs cross-validation and logs warnings for mismatches.
    
    Returns:
        (ok: bool, err: str|None)
    """
    rooms_cfg, rooms_ok, rooms_err = await load_rooms()
    schedules_cfg, scheds_ok, scheds_err = await load_schedules()
    
    if not rooms_ok:
        return False, f"rooms.yaml: {rooms_err}"
    
    if not scheds_ok:
        return False, f"schedules.yaml: {scheds_err}"
    
    # Cross-validate
    warnings = _cross_validate(rooms_cfg, schedules_cfg)
    for warn in warnings:
        log.warning(warn)
    
    return True, None


async def write_schedules(new_sched: Dict) -> Tuple[bool, Optional[str]]:
    """Validate and atomically write new schedules.yaml.
    
    On validation failure, keeps last good file and returns error.
    
    Args:
        new_sched: New schedule dict to validate and write
    
    Returns:
        (ok: bool, err: str|None)
    """
    global _last_schedules_cfg
    
    # Validate in-memory first
    ok, err = _validate_schedules(new_sched)
    if not ok:
        log.error(f"Cannot write schedules.yaml: validation failed: {err}")
        return False, err
    
    # Atomic write
    path = _get_config_path("schedules.yaml")
    write_err = await _write_yaml_file(path, new_sched)
    
    if write_err:
        log.error(f"Failed to write schedules.yaml: {write_err}")
        return False, write_err
    
    # Success: update last good config
    _last_schedules_cfg = new_sched
    log.info("Successfully wrote schedules.yaml")
    return True, None


async def reload_configs() -> Tuple[bool, Optional[str]]:
    """Re-read from disk and re-validate; non-destructive on failure.
    
    Returns:
        (ok: bool, err: str|None)
    """
    log.info("Reloading configuration from disk...")
    return await load_all()


# ============================================================================
# Room registry builder
# ============================================================================

def build_room_registry(rooms_cfg: Dict, schedules_cfg: Dict) -> Dict[str, Dict]:
    """Build in-memory room registry by matching room IDs.
    
    Each room entry contains:
    - All data from rooms.yaml
    - Schedule data from schedules.yaml (if present)
    - Derived TRV entity IDs
    
    Args:
        rooms_cfg: Validated rooms config
        schedules_cfg: Validated schedules config
    
    Returns:
        Dict mapping room_id → room_data
    """
    registry = {}
    
    # Index schedules by room ID
    schedule_map = {s["id"]: s for s in schedules_cfg.get("rooms", [])}
    
    for room in rooms_cfg.get("rooms", []):
        room_id = room["id"]
        
        # Derive TRV entities
        trv_base = room["trv"]["entity_id"].replace("climate.", "")
        derived_entities = _derive_trv_entities(trv_base)
        
        # Build room entry
        registry[room_id] = {
            "id": room_id,
            "sensors": room.get("sensors", []),
            "trv": {
                "entity_id": room["trv"]["entity_id"],
                "base": trv_base,
                **derived_entities,
            },
            "precision": room.get("precision", 1),
            "timeout_m": room.get("timeout_m", 180),
            "hysteresis": room.get("hysteresis", {}),
            "valve_bands": room.get("valve_bands", {}),
            "valve_update": room.get("valve_update", {}),
            "schedule": schedule_map.get(room_id),  # None if no schedule
        }
    
    return registry
