"""
core.py - Central orchestrator for Pyheat

Responsibilities:
- Central orchestrator for all app logic and data flow
- Owns room registry and coordinates modules (scheduler, sensors, room_controller, trv, boiler)
- Applies state precedence, runs recomputes, and enforces safety/interlocks
- Routes events from ha_triggers to appropriate handlers

Partial implementation: sensors and scheduler modules are now integrated.
"""

from datetime import datetime, timezone
from typing import Dict, Optional, Any
from . import sensors
from . import scheduler
from . import room_controller
from . import trv
from . import status


class PyHeatOrchestrator:
    """Central orchestrator for Pyheat heating control."""
    
    def __init__(self):
        """Initialize the orchestrator."""
        log.info("PyHeatOrchestrator: initializing...")
        
        # Room registry (will be populated by config_loader)
        self.rooms = {}
        
        # Initialize module singletons
        self.sensors = sensors.init()
        self.scheduler = scheduler.init()
        self.room_controller = room_controller.init()
        self.trv = trv.init()
        
        # Module references (to be implemented)
        self.boiler = None
        
        # State tracking
        self.last_recompute = None
        self.recompute_count = 0
        
        log.info("PyHeatOrchestrator: initialized with sensors, scheduler, room_controller, and trv modules")
    
    async def recompute_all(self):
        """Recompute all room states, valve positions, and boiler demand.
        
        This is the main orchestration function that:
        1. Reads current sensor values
        2. Resolves targets (schedule/override/manual)
        3. Applies hysteresis to determine call-for-heat
        4. Computes valve percentages
        5. Aggregates room demands
        6. Controls boiler with safety checks
        7. Publishes status entities
        """
        self.recompute_count += 1
        now = datetime.now(tz=timezone.utc)
        self.last_recompute = now
        
        log.debug(f"recompute_all() called (count: {self.recompute_count}) at {now.strftime('%H:%M:%S')}")
        
        # Get all room controllers
        all_rooms = self.room_controller.get_all_rooms() if self.room_controller else {}
        
        if not all_rooms:
            log.debug("No rooms configured yet")
            return
        
        # Collect room status from each room
        room_statuses = []
        
        for room_id, room in all_rooms.items():
            # Get current temperature from sensors
            temp = None
            is_stale = True
            if self.sensors:
                temp, is_stale = self.sensors.get_room_temp(room_id, now)
            
            # Get scheduled target from scheduler
            schedule_target = None
            if self.scheduler:
                schedule_target = self.scheduler.get_scheduled_target(room_id, now)
            
            # Get mode and manual setpoint from HA
            mode_entity = "input_select.pyheat_" + room_id + "_mode"
            mode_val = state.get(mode_entity)
            mode = mode_val.lower() if mode_val else "auto"
            manual_setpoint = None
            if mode == "manual":
                setpoint_entity = "input_number.pyheat_" + room_id + "_manual_setpoint"
                manual_val = state.get(setpoint_entity)
                if manual_val is not None:
                    try:
                        manual_setpoint = float(manual_val)
                    except (ValueError, TypeError):
                        log.warning(f"Invalid manual setpoint for {room_id}: {manual_val}")
            
            # Update room inputs
            room.update_inputs(
                temp=temp,
                is_stale=is_stale,
                mode=mode,
                schedule_target=schedule_target,
                manual_setpoint=manual_setpoint,
            )
            
            # Compute room state
            room_status = room.compute(now)
            room_statuses.append(room_status)
            
            # Publish per-room entities
            await self._publish_room_entities(room_id, room_status)
        
        # Get master enable and holiday mode
        master_enable = state.get("input_boolean.pyheat_master_enable") == "on"
        holiday = state.get("input_boolean.pyheat_holiday_mode") == "on"
        
        # Build and publish global status
        state_str, attributes = status.build_status(
            master_enable=master_enable,
            holiday=holiday,
            rooms=room_statuses,
            boiler=None  # TODO: Add boiler status when implemented
        )
        
        # Publish global status entity
        state.set("sensor.pyheat_status", value=state_str, new_attributes=attributes)
        
        log.debug(f"Published status: {state_str} ({len(room_statuses)} rooms)")
    
    async def _publish_room_entities(self, room_id: str, room_status: Dict[str, Any]):
        """Publish per-room entities.
        
        Args:
            room_id: Room identifier
            room_status: Room status dict from room.compute()
        """
        # Build room status entity
        status_str, status_attrs = status.build_room_status(room_status)
        
        # Publish room status
        status_entity = "sensor.pyheat_" + room_id + "_status"
        state.set(
            status_entity,
            value=status_str,
            new_attributes=status_attrs
        )
        
        # Publish room temperature
        temp = room_status.get("temp")
        if temp is not None:
            temp_entity = "sensor.pyheat_" + room_id + "_temperature"
            state.set(
                temp_entity,
                value=round(temp, 1),
                new_attributes={"unit_of_measurement": "°C", "device_class": "temperature"}
            )
        
        # Publish room target
        target = room_status.get("target")
        if target is not None:
            target_entity = "sensor.pyheat_" + room_id + "_target"
            state.set(
                target_entity,
                value=round(target, 1),
                new_attributes={"unit_of_measurement": "°C", "device_class": "temperature"}
            )
        
        # Publish call for heat
        cfh_entity = "binary_sensor.pyheat_" + room_id + "_calling_for_heat"
        state.set(
            cfh_entity,
            value="on" if room_status.get("call_for_heat") else "off",
            new_attributes={"device_class": "heat"}
        )
        
        # Publish valve percent
        valve_entity = "number.pyheat_" + room_id + "_valve_percent"
        state.set(
            valve_entity,
            value=room_status.get("valve_percent", 0),
            new_attributes={"unit_of_measurement": "%", "min": 0, "max": 100}
        )
        
        # Publish room state
        state_entity = "sensor.pyheat_" + room_id + "_state"
        state.set(
            state_entity,
            value=room_status.get("state", "unknown")
        )
    
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
