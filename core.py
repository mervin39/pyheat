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
from . import boiler


class PyHeatOrchestrator:
    """Central orchestrator for Pyheat heating control."""
    
    def __init__(self):
        """Initialize the orchestrator."""
        log.info("PyHeatOrchestrator: initializing...")
        
        # Room registry (will be populated by config_loader)
        self.rooms = {}
        
        # Load boiler configuration synchronously (needed for initialization)
        # We use task.executor to make this non-blocking
        boiler_config = {}
        try:
            from . import config_loader
            # Async config loading is handled at startup trigger
            # For now, use empty config with defaults
            boiler_config = {"boiler": {}}  # Will be loaded in first reload
        except Exception as e:
            log.warning(f"Failed to load boiler config during init: {e}, using defaults")
            boiler_config = {"boiler": {}}
        
        # Initialize module singletons
        self.sensors = sensors.init()
        self.scheduler = scheduler.init()
        self.room_controller = room_controller.init()
        self.trv = trv.init()
        self.boiler = boiler.init(boiler_config)
        
        # State tracking
        self.last_recompute = None
        self.recompute_count = 0
        
        log.info("PyHeatOrchestrator: initialized with sensors, scheduler, room_controller, trv, and boiler modules")
    
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
            try:
                mode_val = state.get(mode_entity)
                mode = mode_val.lower() if mode_val else "auto"
            except NameError:
                log.warning(f"Entity {mode_entity} does not exist, defaulting to auto mode")
                mode = "auto"
            
            manual_setpoint = None
            if mode == "manual":
                setpoint_entity = "input_number.pyheat_" + room_id + "_manual_setpoint"
                try:
                    manual_val = state.get(setpoint_entity)
                    if manual_val is not None:
                        try:
                            manual_setpoint = float(manual_val)
                        except (ValueError, TypeError):
                            log.warning(f"Invalid manual setpoint for {room_id}: {manual_val}")
                except NameError:
                    log.warning(f"Entity {setpoint_entity} does not exist")
            
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
        
        # Aggregate rooms calling for heat and their valve percentages
        rooms_calling_for_heat = []
        room_valve_percents = {}
        
        for room_status in room_statuses:
            room_id = room_status["room_id"]
            if room_status.get("call_for_heat", False):
                rooms_calling_for_heat.append(room_id)
                # Store the valve percent calculated from bands (before interlock override)
                room_valve_percents[room_id] = room_status.get("valve_percent", 0)
        
        # Update boiler with interlock safety (may override valve percents)
        boiler_status = None
        valve_overrides = {}
        if self.boiler:
            boiler_result = self.boiler.update(rooms_calling_for_heat, room_valve_percents)
            boiler_status = {
                "on": boiler_result["boiler_on"],
                "rooms_calling": len(boiler_result["rooms_calling"]),
                "reason": boiler_result["reason"],
                "interlock_ok": boiler_result.get("interlock_ok", True),
                "total_valve_percent": boiler_result.get("total_valve_percent", 0)
            }
            
            # Get overridden valve percents (if interlock kicked in)
            valve_overrides = boiler_result.get("overridden_valve_percents", {})
        
        # Apply valve overrides to TRVs
        if self.trv:
            for room_id, room_status in zip(
                [rs["room_id"] for rs in room_statuses],
                room_statuses
            ):
                # Use overridden valve percent if interlock applied, otherwise use room's calculated percent
                final_valve_percent = valve_overrides.get(room_id, room_status.get("valve_percent", 0))
                
                # Command TRV to the final valve position
                await self.trv.set_valve_percent(room_id, final_valve_percent)
        
                # Read global flags
        try:
            master_enable = state.get("input_boolean.pyheat_master_enable") == "on"
        except NameError:
            log.warning("Entity input_boolean.pyheat_master_enable does not exist, defaulting to True")
            master_enable = True
        
        try:
            holiday = state.get("input_boolean.pyheat_holiday_mode") == "on"
        except NameError:
            log.warning("Entity input_boolean.pyheat_holiday_mode does not exist, defaulting to False")
            holiday = False
        
        # Build and publish global status
        state_str, attributes = status.build_status(
            master_enable=master_enable,
            holiday=holiday,
            rooms=room_statuses,
            boiler=boiler_status
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
        # Get room name for friendly_name (capitalize room_id as fallback)
        room_name = room_status.get("room_name", room_id.replace("_", " ").title())
        
        # Build room status entity
        status_str, status_attrs = status.build_room_status(room_status)
        
        # Publish room status
        status_entity = "sensor.pyheat_" + room_id + "_status"
        status_attrs["friendly_name"] = room_name + " Status"
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
                new_attributes={
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                    "friendly_name": room_name + " Temperature"
                }
            )
        
        # Publish room target
        target = room_status.get("target")
        if target is not None:
            target_entity = "sensor.pyheat_" + room_id + "_target"
            state.set(
                target_entity,
                value=round(target, 1),
                new_attributes={
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                    "friendly_name": room_name + " Target"
                }
            )
        
        # Publish call for heat
        cfh_entity = "binary_sensor.pyheat_" + room_id + "_calling_for_heat"
        state.set(
            cfh_entity,
            value="on" if room_status.get("call_for_heat") else "off",
            new_attributes={
                "device_class": "heat",
                "friendly_name": room_name + " Calling For Heat"
            }
        )
        
        # Publish valve percent
        valve_entity = "number.pyheat_" + room_id + "_valve_percent"
        valve_percent = room_status.get("valve_percent", 0)
        state.set(
            valve_entity,
            value=valve_percent,
            new_attributes={
                "unit_of_measurement": "%",
                "min": 0,
                "max": 100,
                "friendly_name": room_name + " Valve"
            }
        )
        
        # Control TRV valve
        await self.trv.set_valve_percent(room_id, valve_percent)
        
        # Publish room state
        state_entity = "sensor.pyheat_" + room_id + "_state"
        state.set(
            state_entity,
            value=room_status.get("state", "unknown"),
            new_attributes={"friendly_name": room_name + " State"}
        )
    
    async def handle_state_change(self, entity_id: str, old_value: Any, new_value: Any):
        """Handle state change from Home Assistant.
        
        Called by ha_triggers when a monitored entity changes state.
        
        Args:
            entity_id: The entity that changed (e.g., "sensor.roomtemp_pete")
            old_value: Previous state value
            new_value: New state value
        """
        # State change handling is done by ha_triggers which calls recompute
        # This method exists for logging and potential future per-entity handling
        log.debug(f"State change: {entity_id}: {old_value} -> {new_value}")
    
    async def handle_event(self, event_type: str, data: Dict):
        """Handle generic events.
        
        Args:
            event_type: Type of event (e.g., "cron_tick")
            data: Event data dictionary
        """
        if event_type == "cron_tick":
            # Periodic tick for schedule evaluation and stale checks
            # Recompute is triggered by ha_triggers after this returns
            now = data.get("timestamp", datetime.now(tz=timezone.utc))
            log.debug(f"Cron tick at {now.strftime('%H:%M:%S')}")
        else:
            log.debug(f"Unhandled event type: {event_type}")
    
    async def handle_timer(self, room_id: str):
        """Handle timer events for room overrides/boosts.
        
        Args:
            room_id: ID of the room whose timer changed
        """
        # Timer state is read during recompute from HA timer entity
        # This method exists for logging and potential future timer-specific handling
        log.debug(f"Timer event for room: {room_id}")
    
    # Service handlers (called by ha_services.py when implemented)
    
    async def svc_override(self, room: str, target: float, minutes: int):
        """Service: Set absolute target override for a room.
        
        Args:
            room: Room ID
            target: Target temperature in °C
            minutes: Duration in minutes
        """
        log.info(f"svc_override: room={room}, target={target}°C, minutes={minutes}")
        
        # Get room controller
        room_obj = self.room_controller.get_room(room) if self.room_controller else None
        if not room_obj:
            log.error(f"svc_override: room '{room}' not found")
            raise ValueError(f"Room '{room}' not found")
        
        # Apply override to room
        now = datetime.now(tz=timezone.utc)
        room_obj.apply_override(target=target, minutes=minutes, now=now)
        
        # Start/restart timer
        timer_entity = f"timer.pyheat_{room}_override"
        duration = minutes * 60  # Convert to seconds
        service.call("timer", "start", entity_id=timer_entity, duration=duration)
        
        log.info(f"Override applied to {room}: {target}°C for {minutes}m")
        
        # Trigger recompute
        await self.recompute_all()
    
    async def svc_boost(self, room: str, delta: float, minutes: int):
        """Service: Apply delta boost to current target.
        
        Args:
            room: Room ID
            delta: Temperature delta in °C (can be negative)
            minutes: Duration in minutes
        """
        log.info(f"svc_boost: room={room}, delta={delta:+.1f}°C, minutes={minutes}")
        
        # Get room controller
        room_obj = self.room_controller.get_room(room) if self.room_controller else None
        if not room_obj:
            log.error(f"svc_boost: room '{room}' not found")
            raise ValueError(f"Room '{room}' not found")
        
        # Apply boost to room
        now = datetime.now(tz=timezone.utc)
        room_obj.apply_boost(delta=delta, minutes=minutes, now=now)
        
        # Start/restart timer
        timer_entity = f"timer.pyheat_{room}_override"
        duration = minutes * 60  # Convert to seconds
        service.call("timer", "start", entity_id=timer_entity, duration=duration)
        
        log.info(f"Boost applied to {room}: {delta:+.1f}°C for {minutes}m")
        
        # Trigger recompute
        await self.recompute_all()
    
    async def svc_cancel_override(self, room: str):
        """Service: Cancel any active override/boost.
        
        Args:
            room: Room ID
        """
        log.info(f"svc_cancel_override: room={room}")
        
        # Get room controller
        room_obj = self.room_controller.get_room(room) if self.room_controller else None
        if not room_obj:
            log.error(f"svc_cancel_override: room '{room}' not found")
            raise ValueError(f"Room '{room}' not found")
        
        # Clear override
        room_obj.clear_override()
        
        # Cancel timer
        timer_entity = f"timer.pyheat_{room}_override"
        service.call("timer", "cancel", entity_id=timer_entity)
        
        log.info(f"Override/boost cancelled for {room}")
        
        # Trigger recompute
        await self.recompute_all()
    
    async def svc_set_mode(self, room: str, mode: str):
        """Service: Set room mode (auto/manual/off).
        
        Args:
            room: Room ID
            mode: One of "auto", "manual", "off"
        """
        log.info(f"svc_set_mode: room={room}, mode={mode}")
        
        # Capitalize mode for input_select (Auto, Manual, Off)
        mode_capitalized = mode.capitalize()
        
        # Update input_select
        mode_entity = f"input_select.pyheat_{room}_mode"
        service.call("input_select", "select_option", entity_id=mode_entity, option=mode_capitalized)
        
        log.info(f"Mode set to {mode} for {room}")
        
        # Trigger recompute (will also happen via state_change trigger)
        await self.recompute_all()
    
    async def svc_set_default_target(self, room: str, target: float):
        """Service: Update default_target in schedules.yaml.
        
        Args:
            room: Room ID
            target: New default target in °C
        """
        log.info(f"svc_set_default_target: room={room}, target={target}°C")
        
        # Import config_loader
        from . import config_loader
        
        # Load current schedules
        schedules_cfg, path, err = await config_loader.load_schedules()
        if not schedules_cfg:
            log.error(f"svc_set_default_target: failed to load schedules: {err}")
            raise ValueError(f"Failed to load schedules: {err}")
        
        # Find and update room
        found = False
        for room_sched in schedules_cfg.get("rooms", []):
            if room_sched.get("id") == room:
                room_sched["default_target"] = target
                found = True
                log.info(f"Updated default_target for {room} to {target}°C")
                break
        
        if not found:
            log.error(f"svc_set_default_target: room '{room}' not found in schedules")
            raise ValueError(f"Room '{room}' not found in schedules")
        
        # Write back to file
        ok, err = await config_loader.write_schedules(schedules_cfg)
        if not ok:
            log.error(f"svc_set_default_target: failed to save schedules.yaml: {err}")
            raise RuntimeError(f"Failed to save schedules.yaml: {err}")
        
        # Reload scheduler module
        if self.scheduler:
            self.scheduler.reload(schedules_cfg)
        
        log.info(f"Schedules saved and reloaded")
        
        # Trigger recompute
        await self.recompute_all()
    
    async def svc_reload_config(self):
        """Service: Reload configuration from disk.
        
        Returns:
            Dict with reload statistics (if supports_response used)
        """
        log.info("svc_reload_config: reloading configuration from disk")
        
        # Import config_loader
        from . import config_loader
        
        # Reload configs from disk
        ok, err = await config_loader.reload_configs()
        if not ok:
            log.error(f"svc_reload_config: failed to reload: {err}")
            raise RuntimeError(f"Failed to reload configuration: {err}")
        
        # Get reloaded configs
        rooms_cfg, _, _ = await config_loader.load_rooms()
        schedules_cfg, _, _ = await config_loader.load_schedules()
        boiler_cfg, _, _ = await config_loader.load_boiler()
        
        # Reload modules
        if self.sensors:
            self.sensors.reload_rooms(rooms_cfg)
            log.info("Sensors reloaded")
        
        if self.scheduler:
            self.scheduler.reload(schedules_cfg)
            log.info("Scheduler reloaded")
        
        if self.room_controller:
            self.room_controller.reload_rooms(rooms_cfg)
            log.info("Room controllers reloaded")
        
        if self.trv:
            self.trv.reload_rooms(rooms_cfg)
            log.info("TRV controllers reloaded")
        
        if self.boiler:
            self.boiler.reload_config(boiler_cfg)
            log.info("Boiler controller reloaded")
        
        log.info("Configuration reloaded successfully")
        
        # Trigger recompute
        await self.recompute_all()
        
        # Return stats for optional response
        return {
            "room_count": len(rooms_cfg.get("rooms", [])),
            "schedule_count": len(schedules_cfg.get("rooms", []))
        }
    
    async def svc_get_schedules(self) -> Dict:
        """Service: Get current schedules configuration.
        
        Returns:
            Dict containing the current schedules (schedules.yaml format)
        """
        log.debug("svc_get_schedules: retrieving current schedules")
        
        # Import config_loader
        from . import config_loader
        
        # Load current schedules from disk (most up-to-date)
        schedules_cfg, ok, err = await config_loader.load_schedules()
        
        if not ok:
            log.error(f"svc_get_schedules: failed to load schedules: {err}")
            raise ValueError(f"Failed to load schedules: {err}")
        
        log.debug(f"svc_get_schedules: returning {len(schedules_cfg.get('rooms', []))} room schedule(s)")
        return schedules_cfg
    
    async def svc_get_rooms(self) -> Dict:
        """Service: Get current rooms configuration.
        
        Returns:
            Dict containing the current rooms (rooms.yaml format)
        """
        log.debug("svc_get_rooms: retrieving current rooms")
        
        # Import config_loader
        from . import config_loader
        
        # Load current rooms from disk (most up-to-date)
        rooms_cfg, ok, err = await config_loader.load_rooms()
        
        if not ok:
            log.error(f"svc_get_rooms: failed to load rooms: {err}")
            raise ValueError(f"Failed to load rooms: {err}")
        
        log.debug(f"svc_get_rooms: returning {len(rooms_cfg.get('rooms', []))} room(s)")
        return rooms_cfg
    
    async def svc_replace_schedules(self, schedule_dict: Dict):
        """Service: Replace schedules with new dict and save.
        
        Args:
            schedule_dict: New schedules dictionary
            
        Returns:
            Dict with success status and metadata for verification
        """
        log.info("svc_replace_schedules: replacing schedules")
        log.debug(f"  New schedules for {len(schedule_dict.get('rooms', []))} room(s)")
        
        # Import config_loader
        from . import config_loader
        
        # Write schedules (validates first)
        ok, err = await config_loader.write_schedules(schedule_dict)
        if not ok:
            log.error(f"svc_replace_schedules: validation/write failed: {err}")
            return {
                "success": False,
                "error": err,
                "rooms_saved": 0
            }
        
        # Reload scheduler
        if self.scheduler:
            self.scheduler.reload(schedule_dict)
            log.info("Scheduler reloaded with new schedules")
        
        # Return success with metadata for verification
        rooms = schedule_dict.get("rooms", [])
        total_blocks = 0
        for room in rooms:
            week = room.get("week", {})
            for day_blocks in week.values():
                total_blocks += len(day_blocks)
        
        result = {
            "success": True,
            "rooms_saved": len(rooms),
            "total_blocks": total_blocks,
            "room_ids": [r.get("id") for r in rooms]
        }
        
        log.info(f"Schedules replaced successfully: {result}")
        
        # Trigger recompute
        await self.recompute_all()
        
        return result
    
    def shutdown(self):
        """Cleanup on shutdown."""
        log.info("PyHeatOrchestrator: shutting down...")
        log.info("PyHeatOrchestrator: shutdown complete")


# Factory function for __init__.py
def create_orchestrator():
    """Create and return orchestrator instance.
    
    Returns:
        PyHeatOrchestrator instance
    """
    return PyHeatOrchestrator()
