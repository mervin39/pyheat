"""
ha_triggers.py - HA/PyScript adapter for state, event, and time triggers

Responsibilities:
- Register all event sources (homeassistant_started, helpers, timers, sensors, TRV feedback, cron)
- Debounce/coalesce bursts to one recompute per tick
- Route events to orchestrator methods
- Startup: immediate recompute + delayed recompute for late-restoring sensors
- Apply "first boiler ON" grace for TRV interlock

Event triggers:
- homeassistant_started
- Helpers: input_select.pyheat_*_mode, input_number.pyheat_*_manual_setpoint,
  input_boolean.pyheat_master_enable, input_boolean.pyheat_holiday_mode
- Timers: timer.pyheat_*_override (started|paused|cancelled|finished)
- Sensors: room temperature sensors from rooms.yaml
- TRV feedback: sensor.<trv_base>_valve_opening_degree_z2m, sensor.<trv_base>_valve_closing_degree_z2m
- 1-minute cron tick
"""

from datetime import datetime, timezone
import asyncio

# Module-level state
_orchestrator = None
_room_registry = None
_debounce_pending = False
_debounce_task = None


def init(orchestrator):
    """Initialize triggers with orchestrator reference.
    
    Args:
        orchestrator: The core orchestrator instance
    
    Returns:
        None (triggers are registered as module-level decorated functions)
    """
    global _orchestrator
    _orchestrator = orchestrator
    
    log.info("ha_triggers: initializing...")
    
    # Load initial room registry for dynamic trigger registration
    _load_room_registry()
    
    log.info("ha_triggers: initialization complete")
    return None


def _load_room_registry():
    """Load room registry from config_loader to know which entities to monitor."""
    global _room_registry
    
    try:
        from . import config_loader
    except ImportError:
        log.warning("ha_triggers: config_loader not available yet")
        return
    
    # We'll trigger the registry load on startup; for now just note it's needed
    _room_registry = {}


async def _debounced_recompute():
    """Debounce helper: wait a short time then trigger recompute."""
    global _debounce_pending, _debounce_task
    
    # Wait for a short debounce period (100ms)
    await task.sleep(0.1)
    
    _debounce_pending = False
    _debounce_task = None
    
    if _orchestrator:
        log.debug("ha_triggers: executing debounced recompute")
        await _orchestrator.recompute_all()
    else:
        log.warning("ha_triggers: orchestrator not available for recompute")


def _request_recompute():
    """Request a debounced recompute (coalesces rapid changes)."""
    global _debounce_pending, _debounce_task
    
    if _debounce_pending:
        # Already scheduled, nothing to do
        return
    
    _debounce_pending = True
    _debounce_task = task.create(_debounced_recompute)


# ============================================================================
# Startup triggers
# ============================================================================

@time_trigger("startup")
async def on_startup():
    """Handle Home Assistant startup.
    
    - Load and log configuration
    - Trigger immediate recompute
    - Schedule delayed recompute for late-restoring sensors
    - Apply first boiler ON grace period
    """
    log.info("=== Pyheat starting up ===")
    
    # Import config_loader
    try:
        from . import config_loader
    except ImportError as e:
        log.error(f"Failed to import config_loader: {e}")
        return
    
    # Load configuration
    log.info("Loading configuration files...")
    ok, err = await config_loader.load_all()
    
    if not ok:
        log.error(f"Configuration load failed: {err}")
        return
    
    # Log the loaded configuration
    log.info("=== Configuration loaded successfully ===")
    
    # Log rooms config
    rooms_cfg, _, _ = await config_loader.load_rooms()
    if rooms_cfg:
        room_count = len(rooms_cfg.get("rooms", []))
        log.info(f"Loaded {room_count} room(s):")
        for room in rooms_cfg.get("rooms", []):
            room_id = room.get("id", "unknown")
            sensor_count = len(room.get("sensors", []))
            trv = room.get("trv", {}).get("entity_id", "none")
            log.info(f"  - {room_id}: {sensor_count} sensor(s), TRV: {trv}")
    
    # Log schedules config
    schedules_cfg, _, _ = await config_loader.load_schedules()
    if schedules_cfg:
        sched_count = len(schedules_cfg.get("rooms", []))
        log.info(f"Loaded {sched_count} schedule(s):")
        for sched in schedules_cfg.get("rooms", []):
            room_id = sched.get("id", "unknown")
            default_target = sched.get("default_target", "?")
            log.info(f"  - {room_id}: default target {default_target}°C")
    
    # Build and log room registry
    global _room_registry
    _room_registry = config_loader.build_room_registry(rooms_cfg, schedules_cfg)
    log.info(f"Room registry built with {len(_room_registry)} room(s)")
    
    # Load configurations into modules
    if _orchestrator:
        log.info("Loading configurations into modules...")
        
        # Load sensors
        if _orchestrator.sensors:
            _orchestrator.sensors.reload_rooms(rooms_cfg)
            log.debug("Sensors module configured")
        
        # Load schedules
        if _orchestrator.scheduler:
            _orchestrator.scheduler.reload(schedules_cfg)
            log.debug("Scheduler module configured")
        
        # Load room controllers
        if _orchestrator.room_controller:
            _orchestrator.room_controller.reload_rooms(rooms_cfg)
            log.debug("Room controller module configured")
        
        # Load TRVs
        if _orchestrator.trv:
            _orchestrator.trv.reload_rooms(rooms_cfg)
            log.debug("TRV module configured")
        
        # Load boiler
        boiler_cfg, _, _ = await config_loader.load_boiler()
        if _orchestrator.boiler:
            _orchestrator.boiler.reload_config(boiler_cfg)
            log.debug("Boiler module configured")
    
    log.info("=== Pyheat configuration ready ===")
    
    # NOTE: Room triggers are registered via @state_trigger decorators below.
    # They cannot be registered dynamically in pyscript, but the decorator approach
    # ensures immediate response to state changes (<1s latency).
    
    # Trigger immediate recompute (if orchestrator exists)
    if _orchestrator:
        log.info("Triggering immediate startup recompute...")
        await _orchestrator.recompute_all()
        
        # Schedule delayed recompute for late-restoring sensors (5 seconds)
        log.info("Scheduling delayed recompute for late-restoring sensors (5s)...")
        await task.sleep(5)
        log.info("Triggering delayed startup recompute...")
        await _orchestrator.recompute_all()
    else:
        log.warning("Orchestrator not available; skipping startup recomputes")


# ============================================================================
# Helper entity triggers (mode, setpoint, master enable, holiday)
# ============================================================================

@state_trigger("input_boolean.pyheat_master_enable")
async def on_master_enable_change(var_name=None, value=None, old_value=None):
    """React to master enable changes."""
    log.info(f"Master enable changed: {old_value} -> {value}")
    _request_recompute()


@state_trigger("input_boolean.pyheat_holiday_mode")
async def on_holiday_mode_change(var_name=None, value=None, old_value=None):
    """React to holiday mode changes."""
    log.info(f"Holiday mode changed: {old_value} -> {value}")
    _request_recompute()


# NOTE: Per-room triggers use multiple @state_trigger decorators on shared functions.
# This is fully supported by pyscript (confirmed in documentation).
# Multiple decorators are OR'ed together - any matching trigger fires the function.
#
# TO ADD A NEW ROOM:
# 1. Add room to config/rooms.yaml and config/schedules.yaml
# 2. Create required Home Assistant helper entities (see README.md)
# 3. Add new @state_trigger lines for the new room to each function below
# 4. Restart pyscript to reload this file


# ============================================================================
# Room mode changes
# ============================================================================

@state_trigger("input_select.pyheat_pete_mode")
@state_trigger("input_select.pyheat_games_mode")
@state_trigger("input_select.pyheat_lounge_mode")
@state_trigger("input_select.pyheat_abby_mode")
@state_trigger("input_select.pyheat_office_mode")
@state_trigger("input_select.pyheat_bathroom_mode")
async def on_room_mode_change(var_name=None, value=None, old_value=None):
    """React to any room mode changes."""
    log.info(f"Room mode changed [{var_name}]: {old_value} -> {value}")
    _request_recompute()


# ============================================================================
# Room manual setpoint changes
# ============================================================================

@state_trigger("input_number.pyheat_pete_manual_setpoint")
@state_trigger("input_number.pyheat_games_manual_setpoint")
@state_trigger("input_number.pyheat_lounge_manual_setpoint")
@state_trigger("input_number.pyheat_abby_manual_setpoint")
@state_trigger("input_number.pyheat_office_manual_setpoint")
@state_trigger("input_number.pyheat_bathroom_manual_setpoint")
async def on_room_setpoint_change(var_name=None, value=None, old_value=None):
    """React to any room manual setpoint changes."""
    log.info(f"Room setpoint changed [{var_name}]: {old_value} -> {value}")
    _request_recompute()


# ============================================================================
# Room timer changes (when boost timers expire)
# ============================================================================

@state_trigger("timer.pyheat_pete_boost_timer")
@state_trigger("timer.pyheat_games_boost_timer")
@state_trigger("timer.pyheat_lounge_boost_timer")
@state_trigger("timer.pyheat_abby_boost_timer")
@state_trigger("timer.pyheat_office_boost_timer")
@state_trigger("timer.pyheat_bathroom_boost_timer")
async def on_room_timer_change(var_name=None, value=None, old_value=None):
    """React to any room boost timer changes."""
    log.info(f"Room timer changed [{var_name}]: {old_value} -> {value}")
    _request_recompute()


# ============================================================================
# Room sensor state changes (temperature updates)
# ============================================================================

@state_trigger("sensor.pete_room_temperature")
@state_trigger("sensor.games_room_temperature")
@state_trigger("sensor.lounge_temperature")
@state_trigger("sensor.abby_room_temperature")
@state_trigger("sensor.office_temperature")
@state_trigger("sensor.bathroom_temperature")
async def on_sensor_change(var_name=None, value=None, old_value=None):
    """React to any temperature sensor changes."""
    log.debug(f"Sensor changed [{var_name}]: {old_value} -> {value}")
    _request_recompute()


# ============================================================================
# TRV local temperature feedback (removed - attribute triggers not supported)
# ============================================================================
# NOTE: Pyscript does not support triggering on entity attributes like
# climate.room_trv.attributes.local_temperature. TRV feedback is handled
# via the 1-minute cron tick instead.


# ============================================================================
# Time trigger (1-minute cron for schedule boundaries and stale checks)
# ============================================================================

@time_trigger("cron(* * * * *)")
async def on_minute_tick():
    """1-minute cron tick for schedule evaluation and stale sensor checks."""
    log.debug("Minute tick")
    
    if _orchestrator:
        now = datetime.now(tz=timezone.utc)
        await _orchestrator.handle_event("cron_tick", {"timestamp": now})
    
    _request_recompute()


# ============================================================================
# Shutdown
# ============================================================================

def shutdown():
    """Cleanup triggers on shutdown (optional)."""
    global _orchestrator, _room_registry, _debounce_task
    
    log.info("ha_triggers: shutting down...")
    
    # Cancel any pending debounce task
    if _debounce_task:
        try:
            _debounce_task.cancel()
        except Exception:
            pass
    
    _orchestrator = None
    _room_registry = None
    _debounce_task = None
    
    log.info("ha_triggers: shutdown complete")
