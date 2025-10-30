"""
ha_services.py - Pyscript service registration and adapter

Responsibilities:
- Register all pyheat.* services with Home Assistant
- Validate service arguments
- Route to orchestrator methods
- Handle errors and return appropriate responses
- All services trigger a recompute after executing

Services:
- pyheat.override(room, target, minutes) - Set absolute target override
- pyheat.boost(room, delta, minutes) - Apply delta boost to current target
- pyheat.cancel_override(room) - Clear any active override/boost
- pyheat.set_mode(room, mode) - Change room mode (auto/manual/off)
- pyheat.set_default_target(room, target) - Update room's default target in schedules.yaml
- pyheat.reload_config() - Reload rooms.yaml and schedules.yaml
- pyheat.get_schedules() - Get current schedules configuration
- pyheat.replace_schedules(schedule) - Atomically replace schedules.yaml
"""

from typing import Dict, Any, Optional


# Module-level state
_orchestrator = None


def init(orchestrator):
    """Initialize service registration with orchestrator reference.
    
    Args:
        orchestrator: The core orchestrator instance
        
    Returns:
        None (services are registered as module-level decorated functions)
    """
    global _orchestrator
    _orchestrator = orchestrator
    
    log.info("ha_services: initializing service registration...")
    
    # Service registration happens via decorators below
    # Pyscript automatically registers @service decorated functions
    
    log.info("ha_services: service registration complete")
    return None


# ============================================================================
# Service Implementations
# ============================================================================

@service("pyheat.override")
async def override(room: str = None, target: float = None, minutes: int = None):
    """Set an absolute target override for a room.
    
    Args:
        room: Room ID (required)
        target: Target temperature in °C (required)
        minutes: Duration in minutes (required)
        
    Behavior:
        - Ignored if room mode is 'manual' or 'off'
        - Starts/extends override timer
        - Triggers immediate recompute
    """
    # Validate required arguments
    if room is None:
        log.error("pyheat.override: 'room' argument is required")
        return {"success": False, "error": "room argument is required"}
    
    if target is None:
        log.error("pyheat.override: 'target' argument is required")
        return {"success": False, "error": "target argument is required"}
    
    if minutes is None:
        log.error("pyheat.override: 'minutes' argument is required")
        return {"success": False, "error": "minutes argument is required"}
    
    # Validate types and ranges
    try:
        target = float(target)
        minutes = int(minutes)
    except (ValueError, TypeError) as e:
        log.error(f"pyheat.override: invalid argument types: {e}")
        return {"success": False, "error": f"invalid argument types: {e}"}
    
    if minutes <= 0:
        log.error("pyheat.override: 'minutes' must be positive")
        return {"success": False, "error": "minutes must be positive"}
    
    # Validate temperature range (reasonable limits)
    if target < 5.0 or target > 35.0:
        log.error(f"pyheat.override: target {target}°C out of range (5-35°C)")
        return {"success": False, "error": f"target {target}°C out of range (5-35°C)"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.override: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info(f"pyheat.override: room={room}, target={target}°C, minutes={minutes}")
    
    try:
        await _orchestrator.svc_override(room=room, target=target, minutes=minutes)
        return {"success": True, "room": room, "target": target, "minutes": minutes}
    except Exception as e:
        log.error(f"pyheat.override failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.boost")
async def boost(room: str = None, delta: float = None, minutes: int = None):
    """Apply a delta boost to the current target for a room.
    
    Args:
        room: Room ID (required)
        delta: Temperature delta in °C (can be negative) (required)
        minutes: Duration in minutes (required)
        
    Behavior:
        - Ignored if room mode is 'manual' or 'off'
        - Adds delta to current resolved target
        - Starts/extends boost timer
        - Triggers immediate recompute
    """
    # Validate required arguments
    if room is None:
        log.error("pyheat.boost: 'room' argument is required")
        return {"success": False, "error": "room argument is required"}
    
    if delta is None:
        log.error("pyheat.boost: 'delta' argument is required")
        return {"success": False, "error": "delta argument is required"}
    
    if minutes is None:
        log.error("pyheat.boost: 'minutes' argument is required")
        return {"success": False, "error": "minutes argument is required"}
    
    # Validate types and ranges
    try:
        delta = float(delta)
        minutes = int(minutes)
    except (ValueError, TypeError) as e:
        log.error(f"pyheat.boost: invalid argument types: {e}")
        return {"success": False, "error": f"invalid argument types: {e}"}
    
    if minutes <= 0:
        log.error("pyheat.boost: 'minutes' must be positive")
        return {"success": False, "error": "minutes must be positive"}
    
    # Validate delta range (reasonable limits)
    if delta < -10.0 or delta > 10.0:
        log.error(f"pyheat.boost: delta {delta}°C out of range (-10 to +10°C)")
        return {"success": False, "error": f"delta {delta}°C out of range (-10 to +10°C)"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.boost: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info(f"pyheat.boost: room={room}, delta={delta:+.1f}°C, minutes={minutes}")
    
    try:
        await _orchestrator.svc_boost(room=room, delta=delta, minutes=minutes)
        return {"success": True, "room": room, "delta": delta, "minutes": minutes}
    except Exception as e:
        log.error(f"pyheat.boost failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.cancel_override")
async def cancel_override(room: str = None):
    """Cancel any active override or boost for a room.
    
    Args:
        room: Room ID (required)
        
    Behavior:
        - Clears override/boost state
        - Stops override timer
        - Returns to schedule/manual mode
        - Safe no-op if no override active
        - Triggers immediate recompute
    """
    # Validate required arguments
    if room is None:
        log.error("pyheat.cancel_override: 'room' argument is required")
        return {"success": False, "error": "room argument is required"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.cancel_override: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info(f"pyheat.cancel_override: room={room}")
    
    try:
        await _orchestrator.svc_cancel_override(room=room)
        return {"success": True, "room": room}
    except Exception as e:
        log.error(f"pyheat.cancel_override failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.set_mode")
async def set_mode(room: str = None, mode: str = None):
    """Set the mode for a room.
    
    Args:
        room: Room ID (required)
        mode: Mode to set - "auto", "manual", or "off" (required)
        
    Behavior:
        - Updates input_select.pyheat_<room>_mode
        - Triggers immediate recompute
    """
    # Validate required arguments
    if room is None:
        log.error("pyheat.set_mode: 'room' argument is required")
        return {"success": False, "error": "room argument is required"}
    
    if mode is None:
        log.error("pyheat.set_mode: 'mode' argument is required")
        return {"success": False, "error": "mode argument is required"}
    
    # Validate mode
    mode = mode.lower()
    if mode not in ["auto", "manual", "off"]:
        log.error(f"pyheat.set_mode: invalid mode '{mode}' (must be auto, manual, or off)")
        return {"success": False, "error": f"invalid mode '{mode}' (must be auto, manual, or off)"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.set_mode: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info(f"pyheat.set_mode: room={room}, mode={mode}")
    
    try:
        await _orchestrator.svc_set_mode(room=room, mode=mode)
        return {"success": True, "room": room, "mode": mode}
    except Exception as e:
        log.error(f"pyheat.set_mode failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.set_default_target")
async def set_default_target(room: str = None, target: float = None):
    """Update the default target temperature for a room in schedules.yaml.
    
    Args:
        room: Room ID (required)
        target: New default target temperature in °C (required)
        
    Behavior:
        - Updates schedules.yaml
        - Reloads configuration
        - Triggers immediate recompute
    """
    # Validate required arguments
    if room is None:
        log.error("pyheat.set_default_target: 'room' argument is required")
        return {"success": False, "error": "room argument is required"}
    
    if target is None:
        log.error("pyheat.set_default_target: 'target' argument is required")
        return {"success": False, "error": "target argument is required"}
    
    # Validate type and range
    try:
        target = float(target)
    except (ValueError, TypeError) as e:
        log.error(f"pyheat.set_default_target: invalid target type: {e}")
        return {"success": False, "error": f"invalid target type: {e}"}
    
    if target < 5.0 or target > 35.0:
        log.error(f"pyheat.set_default_target: target {target}°C out of range (5-35°C)")
        return {"success": False, "error": f"target {target}°C out of range (5-35°C)"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.set_default_target: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info(f"pyheat.set_default_target: room={room}, target={target}°C")
    
    try:
        await _orchestrator.svc_set_default_target(room=room, target=target)
        return {"success": True, "room": room, "target": target}
    except Exception as e:
        log.error(f"pyheat.set_default_target failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.reload_config")
async def reload_config():
    """Reload rooms.yaml and schedules.yaml configuration files.
    
    Behavior:
        - Re-reads config files from disk
        - Validates configuration
        - Applies if valid, keeps last good config if invalid
        - Triggers immediate recompute
    """
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.reload_config: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info("pyheat.reload_config: reloading configuration")
    
    try:
        await _orchestrator.svc_reload_config()
        return {"success": True}
    except Exception as e:
        log.error(f"pyheat.reload_config failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.replace_schedules")
async def replace_schedules(schedule: Dict[str, Any] = None):
    """Atomically replace the schedules.yaml configuration.
    
    Args:
        schedule: Complete schedules.yaml contents as a dict (required)
        
    Behavior:
        - Validates the new schedule
        - If valid, writes to schedules.yaml and reloads
        - If invalid, does nothing and returns error
        - Triggers immediate recompute on success
    """
    # Validate required argument
    if schedule is None:
        log.error("pyheat.replace_schedules: 'schedule' argument is required")
        return {"success": False, "error": "schedule argument is required"}
    
    if not isinstance(schedule, dict):
        log.error("pyheat.replace_schedules: 'schedule' must be a dict")
        return {"success": False, "error": "schedule must be a dict"}
    
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.replace_schedules: orchestrator not available")
        return {"success": False, "error": "orchestrator not available"}
    
    log.info("pyheat.replace_schedules: replacing schedules configuration")
    
    try:
        await _orchestrator.svc_replace_schedules(schedule_dict=schedule)
        return {"success": True}
    except Exception as e:
        log.error(f"pyheat.replace_schedules failed: {e}")
        return {"success": False, "error": str(e)}


@service("pyheat.get_schedules", supports_response="only")
async def get_schedules():
    """Get the current schedules configuration.
    
    Returns:
        Dict with the complete schedules.yaml contents
        
    Behavior:
        - Returns the current in-memory schedules
        - Does NOT reload from disk
        - Use this to read before modifying with replace_schedules
        - Must use ?return_response=true in REST API calls
    """
    # Call orchestrator
    if not _orchestrator:
        log.error("pyheat.get_schedules: orchestrator not available")
        raise ValueError("orchestrator not available")
    
    log.debug("pyheat.get_schedules: retrieving current schedules")
    
    try:
        schedules = await _orchestrator.svc_get_schedules()
        return schedules
    except Exception as e:
        log.error(f"pyheat.get_schedules failed: {e}")
        raise
