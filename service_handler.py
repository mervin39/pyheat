# -*- coding: utf-8 -*-
"""
service_handler.py - Home Assistant service handlers

Responsibilities:
- Register PyHeat services
- Handle service calls (override, boost, cancel, set_mode, reload, etc.)
- Validate service parameters
"""

from typing import Any, Dict, Callable
from datetime import datetime
import os
import yaml
import pyheat.constants as C


class ServiceHandler:
    """Handles PyHeat service registration and callbacks."""
    
    def __init__(self, ad, config):
        """Initialize the service handler.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        self.trigger_recompute_callback = None  # Set by main app
        self.scheduler_ref = None  # Set by main app for boost service
        
    def register_all(self, trigger_recompute_cb: Callable, scheduler_ref=None) -> None:
        """Register all PyHeat services.
        
        Args:
            trigger_recompute_cb: Callback to trigger system recompute
            scheduler_ref: Reference to Scheduler instance (for boost service)
        """
        self.trigger_recompute_callback = trigger_recompute_cb
        self.scheduler_ref = scheduler_ref
        
        # Register all services
        self.ad.register_service("pyheat/override", self.svc_override)
        self.ad.register_service("pyheat/boost", self.svc_boost)
        self.ad.register_service("pyheat/cancel_override", self.svc_cancel_override)
        self.ad.register_service("pyheat/set_mode", self.svc_set_mode)
        self.ad.register_service("pyheat/set_default_target", self.svc_set_default_target)
        self.ad.register_service("pyheat/reload_config", self.svc_reload_config)
        self.ad.register_service("pyheat/get_schedules", self.svc_get_schedules)
        self.ad.register_service("pyheat/get_rooms", self.svc_get_rooms)
        self.ad.register_service("pyheat/replace_schedules", self.svc_replace_schedules)
        
        self.ad.log("Registered PyHeat services")
        
    def svc_override(self, namespace, domain, service, kwargs):
        """Service: pyheat.override - Set absolute target override for a room.
        
        Args:
            room (str): Room ID (required)
            target (float): Target temperature in C (required)
            minutes (int): Duration in minutes (required)
        """
        room = kwargs.get('room')
        target = kwargs.get('target')
        minutes = kwargs.get('minutes')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.override: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if target is None:
            self.ad.log("pyheat.override: 'target' argument is required", level="ERROR")
            return {"success": False, "error": "target argument is required"}
        
        if minutes is None:
            self.ad.log("pyheat.override: 'minutes' argument is required", level="ERROR")
            return {"success": False, "error": "minutes argument is required"}
        
        # Validate types and ranges
        try:
            target = float(target)
            minutes = int(minutes)
        except (ValueError, TypeError) as e:
            self.ad.log(f"pyheat.override: invalid argument types: {e}", level="ERROR")
            return {"success": False, "error": f"invalid argument types: {e}"}
        
        if minutes <= 0:
            self.ad.log("pyheat.override: 'minutes' must be positive", level="ERROR")
            return {"success": False, "error": "minutes must be positive"}
        
        if target < 10.0 or target > 35.0:
            self.ad.log(f"pyheat.override: target {target}C out of range (10-35C)", level="ERROR")
            return {"success": False, "error": f"target {target}C out of range (10-35C)"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.override: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        self.ad.log(f"pyheat.override: room={room}, target={target}C, minutes={minutes}")
        
        try:
            # Set override target in helper
            override_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room)
            if self.ad.entity_exists(override_entity):
                self.ad.call_service("input_number/set_value", entity_id=override_entity, value=target)
            
            # Start override timer
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room)
            if self.ad.entity_exists(timer_entity):
                duration = f"{minutes * 60}"  # Convert to seconds
                self.ad.call_service("timer/start", entity_id=timer_entity, duration=duration)
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("override_service"), 1)
            
            return {"success": True, "room": room, "target": target, "minutes": minutes}
        
        except Exception as e:
            self.ad.log(f"pyheat.override failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_boost(self, namespace, domain, service, kwargs):
        """Service: pyheat.boost - Apply delta boost to current target.
        
        Args:
            room (str): Room ID (required)
            delta (float): Temperature delta in C (can be negative) (required)
            minutes (int): Duration in minutes (required)
        """
        room = kwargs.get('room')
        delta = kwargs.get('delta')
        minutes = kwargs.get('minutes')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.boost: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if delta is None:
            self.ad.log("pyheat.boost: 'delta' argument is required", level="ERROR")
            return {"success": False, "error": "delta argument is required"}
        
        if minutes is None:
            self.ad.log("pyheat.boost: 'minutes' argument is required", level="ERROR")
            return {"success": False, "error": "minutes argument is required"}
        
        # Validate types and ranges
        try:
            delta = float(delta)
            minutes = int(minutes)
        except (ValueError, TypeError) as e:
            self.ad.log(f"pyheat.boost: invalid argument types: {e}", level="ERROR")
            return {"success": False, "error": f"invalid argument types: {e}"}
        
        if minutes <= 0:
            self.ad.log("pyheat.boost: 'minutes' must be positive", level="ERROR")
            return {"success": False, "error": "minutes must be positive"}
        
        if delta < -10.0 or delta > 10.0:
            self.ad.log(f"pyheat.boost: delta {delta}C out of range (-10 to +10C)", level="ERROR")
            return {"success": False, "error": f"delta {delta}C out of range (-10 to +10C)"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.boost: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        self.ad.log(f"pyheat.boost: room={room}, delta={delta:+.1f}C, minutes={minutes}")
        
        try:
            # Get current target to calculate boost target
            now = datetime.now()
            mode_entity = C.HELPER_ROOM_MODE.format(room=room)
            room_mode = self.ad.get_state(mode_entity) if self.ad.entity_exists(mode_entity) else "auto"
            room_mode = room_mode.lower() if room_mode else "auto"
            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on" if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE) else False
            
            # Resolve current target (without override/boost) using scheduler
            current_target = None
            if self.scheduler_ref:
                current_target = self.scheduler_ref.resolve_room_target(room, now, room_mode, holiday_mode, False)
            
            if current_target is None:
                self.ad.log(f"pyheat.boost: could not resolve current target for room '{room}'", level="ERROR")
                return {"success": False, "error": "could not resolve current target"}
            
            # Calculate boost target
            boost_target = current_target + delta
            
            # Clamp to valid range
            boost_target = max(10.0, min(35.0, boost_target))
            
            # Set override target
            override_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room)
            if self.ad.entity_exists(override_entity):
                self.ad.call_service("input_number/set_value", entity_id=override_entity, value=boost_target)
            
            # Start override timer
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room)
            if self.ad.entity_exists(timer_entity):
                duration = f"{minutes * 60}"  # Convert to seconds
                self.ad.call_service("timer/start", entity_id=timer_entity, duration=duration)
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("boost_service"), 1)
            
            return {"success": True, "room": room, "delta": delta, "boost_target": boost_target, "minutes": minutes}
        
        except Exception as e:
            self.ad.log(f"pyheat.boost failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_cancel_override(self, namespace, domain, service, kwargs):
        """Service: pyheat.cancel_override - Cancel active override/boost.
        
        Args:
            room (str): Room ID (required)
        """
        room = kwargs.get('room')
        
        # Validate required argument
        if room is None:
            self.ad.log("pyheat.cancel_override: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.cancel_override: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        self.ad.log(f"pyheat.cancel_override: room={room}")
        
        try:
            # Cancel override timer
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room)
            if self.ad.entity_exists(timer_entity):
                self.ad.call_service("timer/cancel", entity_id=timer_entity)
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("cancel_override_service"), 1)
            
            return {"success": True, "room": room}
        
        except Exception as e:
            self.ad.log(f"pyheat.cancel_override failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_set_mode(self, namespace, domain, service, kwargs):
        """Service: pyheat.set_mode - Set room mode.
        
        Args:
            room (str): Room ID (required)
            mode (str): Mode to set - "auto", "manual", or "off" (required)
        """
        room = kwargs.get('room')
        mode = kwargs.get('mode')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.set_mode: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if mode is None:
            self.ad.log("pyheat.set_mode: 'mode' argument is required", level="ERROR")
            return {"success": False, "error": "mode argument is required"}
        
        # Validate mode
        mode = mode.lower()
        if mode not in ["auto", "manual", "off"]:
            self.ad.log(f"pyheat.set_mode: invalid mode '{mode}' (must be auto, manual, or off)", level="ERROR")
            return {"success": False, "error": f"invalid mode '{mode}' (must be auto, manual, or off)"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.set_mode: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        self.ad.log(f"pyheat.set_mode: room={room}, mode={mode}")
        
        try:
            # Set mode via helper
            mode_entity = C.HELPER_ROOM_MODE.format(room=room)
            if self.ad.entity_exists(mode_entity):
                # Capitalize first letter for input_select
                mode_display = mode.capitalize()
                self.ad.call_service("input_select/select_option", entity_id=mode_entity, option=mode_display)
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("set_mode_service"), 1)
            
            return {"success": True, "room": room, "mode": mode}
        
        except Exception as e:
            self.ad.log(f"pyheat.set_mode failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_set_default_target(self, namespace, domain, service, kwargs):
        """Service: pyheat.set_default_target - Update default target in schedules.yaml.
        
        Args:
            room (str): Room ID (required)
            target (float): New default target temperature in C (required)
        """
        room = kwargs.get('room')
        target = kwargs.get('target')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.set_default_target: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if target is None:
            self.ad.log("pyheat.set_default_target: 'target' argument is required", level="ERROR")
            return {"success": False, "error": "target argument is required"}
        
        # Validate type and range
        try:
            target = float(target)
        except (ValueError, TypeError) as e:
            self.ad.log(f"pyheat.set_default_target: invalid target type: {e}", level="ERROR")
            return {"success": False, "error": f"invalid target type: {e}"}
        
        if target < 5.0 or target > 35.0:
            self.ad.log(f"pyheat.set_default_target: target {target}C out of range (5-35C)", level="ERROR")
            return {"success": False, "error": f"target {target}C out of range (5-35C)"}
        
        # Validate room exists
        if room not in self.config.schedules:
            self.ad.log(f"pyheat.set_default_target: room '{room}' not found in schedules", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found in schedules"}
        
        self.ad.log(f"pyheat.set_default_target: room={room}, target={target}C")
        
        try:
            # Update schedules in memory
            self.config.schedules[room]['default_target'] = target
            
            # Write to schedules.yaml
            app_dir = os.path.dirname(os.path.abspath(__file__))
            config_dir = os.path.join(app_dir, "config")
            schedules_file = os.path.join(config_dir, "schedules.yaml")
            
            # Save as 'rooms' key structure to match format
            schedules_data = {'rooms': list(self.config.schedules.values())}
            
            with open(schedules_file, 'w') as f:
                yaml.dump(schedules_data, f, default_flow_style=False, sort_keys=False)
            
            self.ad.log(f"Updated schedules.yaml: {room} default_target = {target}C")
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("set_default_target_service"), 1)
            
            return {"success": True, "room": room, "target": target}
        
        except Exception as e:
            self.ad.log(f"pyheat.set_default_target failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}
        
    def svc_reload_config(self, namespace, domain, service, kwargs):
        """Service handler: reload configuration."""
        self.ad.log("Service call: pyheat.reload_config")
        try:
            self.config.reload()
            if self.trigger_recompute_callback:
                self.trigger_recompute_callback("config_reloaded")
            return {
                "success": True,
                "message": "Configuration reloaded",
                "room_count": len(self.config.rooms),
                "schedule_count": len(self.config.schedules)
            }
        except Exception as e:
            self.ad.log(f"Failed to reload config: {e}", level="ERROR")
            return {"success": False, "message": str(e)}

    def svc_get_schedules(self, namespace, domain, service, kwargs):
        """Service: pyheat.get_schedules - Get current schedules configuration.
        
        Returns:
            Dict with complete schedules.yaml contents
        """
        self.ad.log("pyheat.get_schedules: retrieving current schedules", level="DEBUG")
        
        try:
            return self.config.schedules
        except Exception as e:
            self.ad.log(f"pyheat.get_schedules failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_get_rooms(self, namespace, domain, service, kwargs):
        """Service: pyheat.get_rooms - Get current rooms configuration.
        
        Returns:
            Dict with complete rooms.yaml contents
        """
        self.ad.log("pyheat.get_rooms: retrieving current rooms", level="DEBUG")
        
        try:
            return self.config.rooms
        except Exception as e:
            self.ad.log(f"pyheat.get_rooms failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_replace_schedules(self, namespace, domain, service, kwargs):
        """Service: pyheat.replace_schedules - Atomically replace schedules.yaml.
        
        Args:
            schedule (dict): Complete schedules.yaml contents (required)
            
        Returns:
            Dict with success, rooms_saved, total_blocks, room_ids
        """
        schedule = kwargs.get('schedule')
        
        # Validate required argument
        if schedule is None:
            self.ad.log("pyheat.replace_schedules: 'schedule' argument is required", level="ERROR")
            return {"success": False, "error": "schedule argument is required"}
        
        if not isinstance(schedule, dict):
            self.ad.log("pyheat.replace_schedules: 'schedule' must be a dict", level="ERROR")
            return {"success": False, "error": "schedule must be a dict"}
        
        self.ad.log("pyheat.replace_schedules: processing request")
        
        try:
            # Validate schedule structure
            total_blocks = 0
            for room_id, room_schedule in schedule.items():
                if 'week' in room_schedule and isinstance(room_schedule['week'], dict):
                    for day, blocks in room_schedule['week'].items():
                        if isinstance(blocks, list):
                            total_blocks += len(blocks)
            
            # Write to schedules.yaml
            app_dir = os.path.dirname(os.path.abspath(__file__))
            config_dir = os.path.join(app_dir, "config")
            schedules_file = os.path.join(config_dir, "schedules.yaml")
            
            # Save as 'rooms' key structure to match format
            schedules_data = {'rooms': list(schedule.values())}
            
            with open(schedules_file, 'w') as f:
                yaml.dump(schedules_data, f, default_flow_style=False, sort_keys=False)
            
            # Reload configuration
            self.config.reload()
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("replace_schedules_service"), 1)
            
            result = {
                "success": True,
                "rooms_saved": len(schedule),
                "total_blocks": total_blocks,
                "room_ids": list(schedule.keys())
            }
            self.ad.log(f"Schedules replaced: {result}")
            return result
        
        except Exception as e:
            self.ad.log(f"pyheat.replace_schedules failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}

