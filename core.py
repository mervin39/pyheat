"""
core.py - Central orchestrator for Pyheat

Responsibilities:
- Central orchestrator for all app logic and data flow
- Owns room registry and coordinates modules (scheduler, sensors, room_controller, trv, boiler)
- Applies state precedence, runs recomputes, and enforces safety/interlocks
- Routes events from ha_triggers to appropriate handlers

This is currently a STUB implementation to allow triggers to fire and log.
Full implementation will follow as other modules are built.
"""

from datetime import datetime, timezone
from typing import Dict, Optional, Any


class PyHeatOrchestrator:
    """Central orchestrator for Pyheat heating control.
    
    This is a stub implementation that provides the interface expected by
    ha_triggers.py but doesn't yet implement the full logic.
    """
    
    def __init__(self):
        """Initialize the orchestrator."""
        log.info("PyHeatOrchestrator: initializing...")
        
        # Room registry (will be populated by config_loader)
        self.rooms = {}
        
        # Module references (to be implemented)
        self.scheduler = None
        self.sensors = None
        self.room_controllers = {}
        self.trv = None
        self.boiler = None
        
        # State tracking
        self.last_recompute = None
        self.recompute_count = 0
        
        log.info("PyHeatOrchestrator: initialized (stub)")
    
    async def recompute_all(self):
        """Recompute all room states, valve positions, and boiler demand.
        
        This is the main orchestration function that:
        1. Reads current sensor values
        2. Resolves targets (schedule/override/manual)
        3. Applies hysteresis to determine call-for-heat
        4. Computes valve percentages
        5. Aggregates room demands
        6. Controls boiler with safety checks
        
        Currently a STUB that just logs.
        """
        self.recompute_count += 1
        self.last_recompute = datetime.now(tz=timezone.utc)
        
        log.info(f"[STUB] recompute_all() called (count: {self.recompute_count})")
        log.debug(f"  Timestamp: {self.last_recompute.isoformat()}")
        log.debug(f"  Rooms in registry: {len(self.rooms)}")
        
        # TODO: Implement full recompute logic:
        # - Get current temps from sensors module
        # - Resolve targets from scheduler module
        # - Update room_controller for each room
        # - Aggregate call-for-heat states
        # - Check TRV feedback for interlock
        # - Control boiler via boiler module
        # - Publish status entities
    
    async def handle_state_change(self, entity_id: str, old_value: Any, new_value: Any):
        """Handle state change from Home Assistant.
        
        Called by ha_triggers when a monitored entity changes state.
        
        Args:
            entity_id: The entity that changed (e.g., "sensor.roomtemp_pete")
            old_value: Previous state value
            new_value: New state value
        """
        log.debug(f"[STUB] handle_state_change: {entity_id}")
        log.debug(f"  Old: {old_value}")
        log.debug(f"  New: {new_value}")
        
        # TODO: Implement state change handling:
        # - Route to appropriate module (sensors, trv, etc.)
        # - Update internal state
        # - May trigger recompute via ha_triggers debounce
    
    async def handle_event(self, event_type: str, data: Dict):
        """Handle generic events.
        
        Args:
            event_type: Type of event (e.g., "cron_tick")
            data: Event data dictionary
        """
        log.debug(f"[STUB] handle_event: {event_type}")
        
        if event_type == "cron_tick":
            # Periodic tick for schedule evaluation and stale checks
            now = data.get("timestamp", datetime.now(tz=timezone.utc))
            log.debug(f"  Cron tick at {now.strftime('%H:%M:%S')}")
            
            # TODO: Implement:
            # - Check for schedule boundary crossings
            # - Mark stale sensors
            # - Update time-based state
        else:
            log.debug(f"  Unhandled event type: {event_type}")
    
    async def handle_timer(self, room_id: str):
        """Handle timer events for room overrides/boosts.
        
        Args:
            room_id: ID of the room whose timer changed
        """
        log.info(f"[STUB] handle_timer: {room_id}")
        
        # TODO: Implement:
        # - Read timer state (active/paused/idle)
        # - Read remaining time
        # - Update room_controller override state
        # - Trigger recompute
    
    # Service handlers (called by ha_services.py when implemented)
    
    async def svc_override(self, room: str, target: float, minutes: int):
        """Service: Set absolute target override for a room.
        
        Args:
            room: Room ID
            target: Target temperature in °C
            minutes: Duration in minutes
        """
        log.info(f"[STUB] svc_override: room={room}, target={target}°C, minutes={minutes}")
        
        # TODO: Implement:
        # - Validate room exists
        # - Start/restart timer.pyheat_{room}_override
        # - Update room_controller with override
        # - Trigger recompute
    
    async def svc_boost(self, room: str, delta: float, minutes: int):
        """Service: Apply delta boost to current target.
        
        Args:
            room: Room ID
            delta: Temperature delta in °C (can be negative)
            minutes: Duration in minutes
        """
        log.info(f"[STUB] svc_boost: room={room}, delta={delta:+.1f}°C, minutes={minutes}")
        
        # TODO: Implement similar to override but with delta logic
    
    async def svc_cancel_override(self, room: str):
        """Service: Cancel any active override/boost.
        
        Args:
            room: Room ID
        """
        log.info(f"[STUB] svc_cancel_override: room={room}")
        
        # TODO: Implement:
        # - Stop timer.pyheat_{room}_override
        # - Clear room_controller override
        # - Trigger recompute
    
    async def svc_set_mode(self, room: str, mode: str):
        """Service: Set room mode (auto/manual/off).
        
        Args:
            room: Room ID
            mode: One of "auto", "manual", "off"
        """
        log.info(f"[STUB] svc_set_mode: room={room}, mode={mode}")
        
        # TODO: Implement:
        # - Validate mode
        # - Update input_select.pyheat_{room}_mode
        # - Mode change will trigger state_change -> recompute
    
    async def svc_set_default_target(self, room: str, target: float):
        """Service: Update default_target in schedules.yaml.
        
        Args:
            room: Room ID
            target: New default target in °C
        """
        log.info(f"[STUB] svc_set_default_target: room={room}, target={target}°C")
        
        # TODO: Implement:
        # - Load current schedules
        # - Update room's default_target
        # - Write back to schedules.yaml via config_loader
        # - Reload scheduler module
        # - Trigger recompute
    
    async def svc_reload_config(self):
        """Service: Reload configuration from disk."""
        log.info(f"[STUB] svc_reload_config")
        
        # TODO: Implement:
        # - Call config_loader.reload_configs()
        # - Rebuild room registry
        # - Reload scheduler
        # - Reload sensors
        # - Trigger recompute
    
    async def svc_replace_schedules(self, schedule_dict: Dict):
        """Service: Replace schedules with new dict and save.
        
        Args:
            schedule_dict: New schedules dictionary
        """
        log.info(f"[STUB] svc_replace_schedules")
        log.debug(f"  Schedules for {len(schedule_dict.get('rooms', []))} room(s)")
        
        # TODO: Implement:
        # - Validate schedule_dict
        # - Write via config_loader.write_schedules()
        # - Reload scheduler
        # - Trigger recompute
    
    def shutdown(self):
        """Cleanup on shutdown."""
        log.info("PyHeatOrchestrator: shutting down...")
        
        # TODO: Cleanup resources
        
        log.info("PyHeatOrchestrator: shutdown complete")


# Factory function for __init__.py
def create_orchestrator():
    """Create and return orchestrator instance.
    
    Returns:
        PyHeatOrchestrator instance
    """
    return PyHeatOrchestrator()
