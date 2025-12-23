# -*- coding: utf-8 -*-
"""
service_handler.py - Service registration and callbacks

Responsibilities:
- Register Appdaemon services for pyheat
- Handle service calls with validation
- Bridge service calls to internal logic
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Callable
import os
import yaml
import json
import constants as C


class ServiceHandler:
    """Handles PyHeat service registration and callbacks."""
    
    def __init__(self, ad, config, override_manager=None):
        """Initialize the service handler.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            override_manager: OverrideManager instance (optional, for override operations)
        """
        self.ad = ad
        self.config = config
        self.override_manager = override_manager
        self.trigger_recompute_callback = None  # Set by main app
        self.scheduler_ref = None  # Set by main app for override delta calculation
        
    def register_all(self, trigger_recompute_cb: Callable, scheduler_ref=None) -> None:
        """Register all PyHeat services.
        
        Args:
            trigger_recompute_cb: Callback to trigger system recompute
            scheduler_ref: Reference to Scheduler instance (for override delta calculation)
        """
        self.trigger_recompute_callback = trigger_recompute_cb
        self.scheduler_ref = scheduler_ref
        
        # Register all services
        self.ad.register_service("pyheat/override", self.svc_override)
        self.ad.register_service("pyheat/override_passive", self.svc_override_passive)
        self.ad.register_service("pyheat/cancel_override", self.svc_cancel_override)
        self.ad.register_service("pyheat/set_mode", self.svc_set_mode)
        self.ad.register_service("pyheat/set_passive_settings", self.svc_set_passive_settings)
        self.ad.register_service("pyheat/set_default_target", self.svc_set_default_target)
        self.ad.register_service("pyheat/reload_config", self.svc_reload_config)
        self.ad.register_service("pyheat/get_schedules", self.svc_get_schedules)
        self.ad.register_service("pyheat/get_rooms", self.svc_get_rooms)
        self.ad.register_service("pyheat/replace_schedules", self.svc_replace_schedules)
        self.ad.register_service("pyheat/get_status", self.svc_get_status)
        self.ad.register_service("pyheat/get_settings", self.svc_get_settings)
        self.ad.register_service("pyheat/set_settings", self.svc_set_settings)

        self.ad.log("Registered PyHeat services")
        
    def svc_override(self, namespace, domain, service, kwargs):
        """Service: pyheat.override - Set temporary temperature override for a room.
        
        Unified override mechanism supporting both absolute and delta temperature modes,
        with flexible duration specification (relative or absolute end time).
        
        Args:
            room (str): Room ID (required)
            target (float): Absolute target temperature in °C (mutually exclusive with delta)
            delta (float): Temperature delta from scheduled target in °C (mutually exclusive with target)
            minutes (int): Duration in minutes (mutually exclusive with end_time)
            end_time (str): ISO datetime string for override end (mutually exclusive with minutes)
            
        Temperature mode (exactly one required):
            - target: Set explicit temperature (e.g., 21.0°C)
            - delta: Adjust from current schedule (e.g., +2.0°C or -1.5°C)
            
        Duration mode (exactly one required):
            - minutes: Relative duration (e.g., 120 for 2 hours)
            - end_time: Absolute end time (e.g., "2025-11-10T17:30:00")
            
        Returns:
            Dict with success, room, target (absolute), duration_seconds, end_time (ISO)
        """
        room = kwargs.get('room')
        target = kwargs.get('target')
        delta = kwargs.get('delta')
        minutes = kwargs.get('minutes')
        end_time = kwargs.get('end_time')
        
        # Validate room
        if room is None:
            self.ad.log("pyheat.override: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.override: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        # Validate temperature mode (exactly one of target or delta)
        if (target is None and delta is None):
            self.ad.log("pyheat.override: must provide either 'target' or 'delta'", level="ERROR")
            return {"success": False, "error": "must provide either 'target' or 'delta'"}
        
        if (target is not None and delta is not None):
            self.ad.log("pyheat.override: cannot provide both 'target' and 'delta'", level="ERROR")
            return {"success": False, "error": "cannot provide both 'target' and 'delta'"}
        
        # Validate duration mode (exactly one of minutes or end_time)
        if (minutes is None and end_time is None):
            self.ad.log("pyheat.override: must provide either 'minutes' or 'end_time'", level="ERROR")
            return {"success": False, "error": "must provide either 'minutes' or 'end_time'"}
        
        if (minutes is not None and end_time is not None):
            self.ad.log("pyheat.override: cannot provide both 'minutes' and 'end_time'", level="ERROR")
            return {"success": False, "error": "cannot provide both 'minutes' and 'end_time'"}
        
        try:
            # Calculate absolute target temperature
            if delta is not None:
                # Delta mode: calculate from current scheduled target
                delta = float(delta)
                if delta < -10.0 or delta > 10.0:
                    self.ad.log(f"pyheat.override: delta {delta}C out of range (-10 to +10C)", level="ERROR")
                    return {"success": False, "error": f"delta {delta}C out of range (-10 to +10C)"}
                
                # Get current scheduled target (without any existing override)
                now = datetime.now()
                mode_entity = C.HELPER_ROOM_MODE.format(room=room)
                room_mode = self.ad.get_state(mode_entity) if self.ad.entity_exists(mode_entity) else "auto"
                room_mode = room_mode.lower() if room_mode else "auto"
                
                holiday_mode = False
                if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                    holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                
                # Get scheduled target (ignores any existing override)
                if self.scheduler_ref:
                    scheduled_target = self.scheduler_ref.get_scheduled_target(room, now, holiday_mode)
                    if scheduled_target is None:
                        self.ad.log(f"pyheat.override: could not determine scheduled target for room '{room}'", level="ERROR")
                        return {"success": False, "error": "could not determine scheduled target"}
                else:
                    self.ad.log("pyheat.override: scheduler reference not available", level="ERROR")
                    return {"success": False, "error": "scheduler reference not available"}
                
                absolute_target = scheduled_target + delta
                self.ad.log(f"pyheat.override: delta mode: scheduled={scheduled_target:.1f}C, delta={delta:+.1f}C, target={absolute_target:.1f}C")
            else:
                # Absolute target mode
                absolute_target = float(target)
                self.ad.log(f"pyheat.override: absolute mode: target={absolute_target:.1f}C")
            
            # Clamp to valid temperature range
            if absolute_target < 10.0 or absolute_target > 35.0:
                self.ad.log(f"pyheat.override: calculated target {absolute_target:.1f}C out of valid range (10-35C), clamping", level="WARNING")
                absolute_target = max(10.0, min(35.0, absolute_target))
            
            # Calculate duration and end time
            if minutes is not None:
                # Relative duration mode
                minutes = int(minutes)
                if minutes <= 0:
                    self.ad.log("pyheat.override: 'minutes' must be positive", level="ERROR")
                    return {"success": False, "error": "minutes must be positive"}
                
                duration_seconds = minutes * 60
                end_dt = datetime.now() + timedelta(seconds=duration_seconds)
                end_time_iso = end_dt.isoformat()
                self.ad.log(f"pyheat.override: duration mode: {minutes} minutes ({duration_seconds}s), ends at {end_time_iso}")
            else:
                # Absolute end time mode
                try:
                    # Parse ISO datetime
                    end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00') if 'Z' in end_time else end_time)
                    now_dt = datetime.now(end_dt.tzinfo if end_dt.tzinfo else None)
                    
                    if end_dt <= now_dt:
                        self.ad.log(f"pyheat.override: end_time {end_time} is not in the future", level="ERROR")
                        return {"success": False, "error": "end_time must be in the future"}
                    
                    duration_seconds = int((end_dt - now_dt).total_seconds())
                    end_time_iso = end_dt.isoformat()
                    self.ad.log(f"pyheat.override: end_time mode: {end_time_iso} ({duration_seconds}s from now)")
                except (ValueError, AttributeError) as e:
                    self.ad.log(f"pyheat.override: invalid end_time format: {e}", level="ERROR")
                    return {"success": False, "error": f"invalid end_time format: {e}"}
            
            # Set override via override manager
            if self.override_manager:
                success = self.override_manager.set_override(room, absolute_target, duration_seconds)
                if not success:
                    return {"success": False, "error": "Failed to set override"}
            else:
                self.ad.log("pyheat.override: override_manager not available", level="ERROR")
                return {"success": False, "error": "override_manager not available"}
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("override_service"), 1)
            
            result = {
                "success": True,
                "room": room,
                "target": absolute_target,
                "duration_seconds": duration_seconds,
                "end_time": end_time_iso
            }
            
            self.ad.log(f"pyheat.override: SUCCESS - room={room}, target={absolute_target:.1f}C, duration={duration_seconds}s")
            return result
        
        except Exception as e:
            self.ad.log(f"pyheat.override failed: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}


    def svc_override_passive(self, namespace, domain, service, kwargs):
        """Service: pyheat.override_passive - Create passive override.

        Args:
            room (str): Room ID (required)
            min_temp (float): Minimum temperature (°C, comfort floor) (required)
            max_temp (float): Maximum temperature (°C, upper limit) (required)
            valve_percent (float): Valve opening percentage (0-100%) (required)
            minutes (int): Duration in minutes (optional, mutually exclusive with end_time)
            end_time (str): ISO 8601 end time (optional, mutually exclusive with minutes)
        """
        from datetime import datetime, timedelta, timezone

        room = kwargs.get('room')
        min_temp = kwargs.get('min_temp')
        max_temp = kwargs.get('max_temp')
        valve_percent = kwargs.get('valve_percent')
        minutes = kwargs.get('minutes')
        end_time_str = kwargs.get('end_time')

        # Validate room exists
        if room not in self.config.rooms:
            return {'success': False, 'error': f'Unknown room: {room}'}

        # Validate room is in auto mode
        mode_entity = C.HELPER_ROOM_MODE.format(room=room)
        if self.ad.entity_exists(mode_entity):
            room_mode = self.ad.get_state(mode_entity)
            if room_mode.lower() != "auto":
                return {'success': False, 'error': f'Passive overrides only work in auto mode (room is in {room_mode} mode)'}

        # Validate all three passive params provided
        if min_temp is None or max_temp is None or valve_percent is None:
            return {'success': False, 'error': 'min_temp, max_temp, and valve_percent are required'}

        # Validate exactly one of minutes/end_time
        if (minutes is None) == (end_time_str is None):
            return {'success': False, 'error': 'Must provide exactly one of: minutes or end_time'}

        # Validate ranges using constants
        if not (C.PASSIVE_OVERRIDE_MIN_TEMP_MIN <= min_temp <= C.PASSIVE_OVERRIDE_MIN_TEMP_MAX):
            return {'success': False, 'error': f'min_temp must be between {C.PASSIVE_OVERRIDE_MIN_TEMP_MIN}-{C.PASSIVE_OVERRIDE_MIN_TEMP_MAX}°C'}
        if not (C.PASSIVE_OVERRIDE_MAX_TEMP_MIN <= max_temp <= C.PASSIVE_OVERRIDE_MAX_TEMP_MAX):
            return {'success': False, 'error': f'max_temp must be between {C.PASSIVE_OVERRIDE_MAX_TEMP_MIN}-{C.PASSIVE_OVERRIDE_MAX_TEMP_MAX}°C'}
        if not (C.PASSIVE_OVERRIDE_VALVE_MIN <= valve_percent <= C.PASSIVE_OVERRIDE_VALVE_MAX):
            return {'success': False, 'error': f'valve_percent must be between {C.PASSIVE_OVERRIDE_VALVE_MIN}-{C.PASSIVE_OVERRIDE_VALVE_MAX}%'}

        # Validate minimum gap between min and max
        temp_range = max_temp - min_temp
        if temp_range < C.PASSIVE_OVERRIDE_MIN_RANGE:
            return {'success': False, 'error': f'max_temp must be at least {C.PASSIVE_OVERRIDE_MIN_RANGE}°C above min_temp (gap: {temp_range:.1f}°C)'}

        # Log warnings for edge cases
        if valve_percent == 0:
            self.ad.log(f"Warning: Passive override for '{room}' has valve_percent=0 (room will not receive heat)", level="WARNING")
        if temp_range < 2.0:
            self.ad.log(f"Warning: Passive override for '{room}' has narrow range ({temp_range:.1f}°C) - may cause oscillation", level="WARNING")

        # Calculate duration
        if minutes is not None:
            duration_seconds = int(minutes) * 60
        else:
            try:
                # Parse ISO datetime, assume local time if no timezone provided
                end_dt = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                if end_dt.tzinfo is None:
                    # Naive datetime - assume local time
                    end_dt = end_dt.replace(tzinfo=None)
                    now = datetime.now()
                else:
                    # Timezone-aware datetime
                    now = datetime.now(end_dt.tzinfo)

                if end_dt <= now:
                    return {'success': False, 'error': 'end_time must be in the future'}
                duration_seconds = int((end_dt - now).total_seconds())
            except Exception as e:
                return {'success': False, 'error': f'Invalid end_time format: {e}'}

        # Set passive override
        success = self.override_manager.set_passive_override(
            room, min_temp, max_temp, valve_percent, duration_seconds
        )

        if success:
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("passive_override_service"), 1)

            # Calculate end_time for response
            end_dt = datetime.now() + timedelta(seconds=duration_seconds)

            return {
                'success': True,
                'room': room,
                'min_temp': min_temp,
                'max_temp': max_temp,
                'valve_percent': valve_percent,
                'duration_seconds': duration_seconds,
                'end_time': end_dt.isoformat()
            }
        else:
            return {'success': False, 'error': 'Failed to set passive override'}

    def svc_cancel_override(self, namespace, domain, service, kwargs):
        """Service: pyheat.cancel_override - Cancel active override (both active and passive).

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
            # Cancel override via override manager
            if self.override_manager:
                success = self.override_manager.cancel_override(room)
                if not success:
                    return {"success": False, "error": "Failed to cancel override"}
            else:
                self.ad.log("pyheat.cancel_override: override_manager not available", level="ERROR")
                return {"success": False, "error": "override_manager not available"}

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
            manual_setpoint (float): Manual setpoint temperature (optional, for manual mode)
        """
        room = kwargs.get('room')
        mode = kwargs.get('mode')
        manual_setpoint = kwargs.get('manual_setpoint')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.set_mode: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if mode is None:
            self.ad.log("pyheat.set_mode: 'mode' argument is required", level="ERROR")
            return {"success": False, "error": "mode argument is required"}
        
        # Validate mode
        mode = mode.lower()
        if mode not in ["auto", "manual", "passive", "off"]:
            self.ad.log(f"pyheat.set_mode: invalid mode '{mode}' (must be auto, manual, passive, or off)", level="ERROR")
            return {"success": False, "error": f"invalid mode '{mode}' (must be auto, manual, passive, or off)"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.set_mode: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        self.ad.log(f"pyheat.set_mode: room={room}, mode={mode}, manual_setpoint={manual_setpoint}")
        
        try:
            # If manual_setpoint provided, set it first (before changing mode)
            if manual_setpoint is not None and mode == "manual":
                manual_setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room)
                if self.ad.entity_exists(manual_setpoint_entity):
                    self.ad.call_service(
                        "input_number/set_value",
                        entity_id=manual_setpoint_entity,
                        value=manual_setpoint
                    )
                    self.ad.log(f"Set {manual_setpoint_entity} to {manual_setpoint}C")
            
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

    def svc_set_passive_settings(self, namespace, domain, service, kwargs):
        """Service: pyheat.set_passive_settings - Set all passive mode settings for a room.
        
        Batched update of passive mode configuration to ensure atomic changes.
        
        Args:
            room (str): Room ID (required)
            max_temp (float): Maximum temperature threshold in °C (10-30°C, required)
            valve_percent (int): Valve opening percentage (0-100%, required)
            min_temp (float): Minimum temperature (comfort floor) in °C (8-20°C, required)
        
        Returns:
            Dict with success status
        """
        room = kwargs.get('room')
        max_temp = kwargs.get('max_temp')
        valve_percent = kwargs.get('valve_percent')
        min_temp = kwargs.get('min_temp')
        
        # Validate required arguments
        if room is None:
            self.ad.log("pyheat.set_passive_settings: 'room' argument is required", level="ERROR")
            return {"success": False, "error": "room argument is required"}
        
        if max_temp is None:
            self.ad.log("pyheat.set_passive_settings: 'max_temp' argument is required", level="ERROR")
            return {"success": False, "error": "max_temp argument is required"}
        
        if valve_percent is None:
            self.ad.log("pyheat.set_passive_settings: 'valve_percent' argument is required", level="ERROR")
            return {"success": False, "error": "valve_percent argument is required"}
        
        if min_temp is None:
            self.ad.log("pyheat.set_passive_settings: 'min_temp' argument is required", level="ERROR")
            return {"success": False, "error": "min_temp argument is required"}
        
        # Validate room exists
        if room not in self.config.rooms:
            self.ad.log(f"pyheat.set_passive_settings: room '{room}' not found", level="ERROR")
            return {"success": False, "error": f"room '{room}' not found"}
        
        # Validate ranges
        try:
            max_temp = float(max_temp)
            min_temp = float(min_temp)
            valve_percent = int(valve_percent)
            
            if max_temp < 10.0 or max_temp > 30.0:
                self.ad.log(f"pyheat.set_passive_settings: max_temp {max_temp}C out of range (10-30C)", level="ERROR")
                return {"success": False, "error": f"max_temp {max_temp}C out of range (10-30C)"}
            
            if min_temp < 8.0 or min_temp > 20.0:
                self.ad.log(f"pyheat.set_passive_settings: min_temp {min_temp}C out of range (8-20C)", level="ERROR")
                return {"success": False, "error": f"min_temp {min_temp}C out of range (8-20C)"}
            
            if valve_percent < 0 or valve_percent > 100:
                self.ad.log(f"pyheat.set_passive_settings: valve_percent {valve_percent}% out of range (0-100%)", level="ERROR")
                return {"success": False, "error": f"valve_percent {valve_percent}% out of range (0-100%)"}
            
            # Validate min < max
            if min_temp >= max_temp:
                self.ad.log(f"pyheat.set_passive_settings: min_temp ({min_temp}C) must be less than max_temp ({max_temp}C)", level="ERROR")
                return {"success": False, "error": f"min_temp ({min_temp}C) must be less than max_temp ({max_temp}C)"}
            
        except (ValueError, TypeError) as e:
            self.ad.log(f"pyheat.set_passive_settings: invalid argument types: {e}", level="ERROR")
            return {"success": False, "error": f"invalid argument types: {e}"}
        
        self.ad.log(f"pyheat.set_passive_settings: room={room}, max_temp={max_temp}C, valve_percent={valve_percent}%, min_temp={min_temp}C")
        
        try:
            # Update all three input_number entities
            max_temp_entity = C.HELPER_ROOM_PASSIVE_MAX_TEMP.format(room=room)
            valve_entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room)
            min_temp_entity = C.HELPER_ROOM_PASSIVE_MIN_TEMP.format(room=room)
            
            if self.ad.entity_exists(max_temp_entity):
                self.ad.call_service(
                    "input_number/set_value",
                    entity_id=max_temp_entity,
                    value=max_temp
                )
                self.ad.log(f"Set {max_temp_entity} to {max_temp}C")
            else:
                self.ad.log(f"Entity {max_temp_entity} not found", level="WARNING")
            
            if self.ad.entity_exists(valve_entity):
                self.ad.call_service(
                    "input_number/set_value",
                    entity_id=valve_entity,
                    value=valve_percent
                )
                self.ad.log(f"Set {valve_entity} to {valve_percent}%")
            else:
                self.ad.log(f"Entity {valve_entity} not found", level="WARNING")
            
            if self.ad.entity_exists(min_temp_entity):
                self.ad.call_service(
                    "input_number/set_value",
                    entity_id=min_temp_entity,
                    value=min_temp
                )
                self.ad.log(f"Set {min_temp_entity} to {min_temp}C")
            else:
                self.ad.log(f"Entity {min_temp_entity} not found", level="WARNING")
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("set_passive_settings_service"), 1)
            
            return {
                "success": True,
                "room": room,
                "max_temp": max_temp,
                "valve_percent": valve_percent,
                "min_temp": min_temp
            }
        
        except Exception as e:
            self.ad.log(f"pyheat.set_passive_settings failed: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
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
            # Go up two levels: services/ -> pyheat/
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_dir = os.path.join(app_dir, "config")
            schedules_file = os.path.join(config_dir, "schedules.yaml")
            
            # Save as 'rooms' key structure to match format
            schedules_data = {'rooms': list(self.config.schedules.values())}

            with open(schedules_file, 'w') as f:
                yaml.dump(schedules_data, f, default_flow_style=False, sort_keys=False)

            # Set permissions to 0o666 (rw-rw-rw-) for easy inspection/debugging
            os.chmod(schedules_file, 0o666)

            self.ad.log(f"Updated schedules.yaml: {room} default_target = {target}C")
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("set_default_target_service"), 1)
            
            return {"success": True, "room": room, "target": target}
        
        except Exception as e:
            self.ad.log(f"pyheat.set_default_target failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}
        
    def svc_reload_config(self, namespace, domain, service, kwargs):
        """Service handler: reload configuration.
        
        Note: This service performs a hot reload of all config files.
        For structural changes (rooms, sensors), automatic file watching will
        trigger an app restart instead. Manual service calls always hot reload.
        """
        self.ad.log("Service call: pyheat.reload_config")
        try:
            self.config.reload()
            if self.trigger_recompute_callback:
                self.trigger_recompute_callback("config_reloaded")
            return {
                "success": True,
                "message": "Configuration reloaded (hot reload)",
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
                Expected format: {"rooms": [{"id": "...", "default_target": ..., "week": {...}}, ...]}
            
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
            # Handle two possible formats:
            # 1. {"rooms": [...]} - from pyheat-web (preferred)
            # 2. {"room_id": {...}, ...} - legacy format
            
            if 'rooms' in schedule and isinstance(schedule['rooms'], list):
                # Format 1: Already has 'rooms' list
                rooms_list = schedule['rooms']
            else:
                # Format 2: Dict keyed by room_id - convert to list
                rooms_list = list(schedule.values())
            
            # Validate schedule structure and count blocks
            total_blocks = 0
            room_ids = []
            for room_data in rooms_list:
                if 'id' in room_data:
                    room_ids.append(room_data['id'])
                if 'week' in room_data and isinstance(room_data['week'], dict):
                    for day, blocks in room_data['week'].items():
                        if isinstance(blocks, list):
                            total_blocks += len(blocks)
            
            # Write to schedules.yaml
            # Go up two levels: services/ -> pyheat/
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_dir = os.path.join(app_dir, "config")
            schedules_file = os.path.join(config_dir, "schedules.yaml")
            
            # Save with 'rooms' list structure
            schedules_data = {'rooms': rooms_list}

            with open(schedules_file, 'w') as f:
                yaml.dump(schedules_data, f, default_flow_style=False, sort_keys=False)

            # Set permissions to 0o666 (rw-rw-rw-) for easy inspection/debugging
            os.chmod(schedules_file, 0o666)

            # Reload configuration
            self.config.reload()
            
            # Trigger immediate recompute
            if self.trigger_recompute_callback:
                self.ad.run_in(lambda kwargs: self.trigger_recompute_callback("replace_schedules_service"), 1)
            
            result = {
                "success": True,
                "rooms_saved": len(rooms_list),
                "total_blocks": total_blocks,
                "room_ids": room_ids
            }
            self.ad.log(f"Schedules replaced: {result}")
            return result
        
        except Exception as e:
            self.ad.log(f"pyheat.replace_schedules failed: {e}", level="ERROR")
            return {"success": False, "error": str(e)}
    
    def svc_get_status(self, namespace, domain, service, kwargs):
        """Service: pyheat.get_status - Get current system and room status.
        
        Returns complete status including room temperatures, targets, modes,
        valve positions, boiler state, etc. This eliminates the need for
        pyheat-web to read individual HA entities.
        
        Returns:
            {
                "rooms": [
                    {
                        "id": str,
                        "name": str,
                        "temp": float or null,
                        "target": float or null,
                        "mode": str,
                        "calling_for_heat": bool,
                        "valve_percent": int,
                        "is_stale": bool,
                        "status_text": str,
                        "manual_setpoint": float or null,
                        "valve_feedback_consistent": bool or null
                    },
                    ...
                ],
                "system": {
                    "master_enabled": bool,
                    "holiday_mode": bool,
                    "any_call_for_heat": bool,
                    "boiler_state": str,
                    "last_recompute": str (ISO datetime)
                }
            }
        """
        try:
            # Get main status entity which has comprehensive attributes
            status_state = self.ad.get_state(C.STATUS_ENTITY, attribute="all")
            if not status_state:
                return {"success": False, "error": "Status entity not available"}
            
            status_attrs = status_state.get("attributes", {})
            rooms_data = status_attrs.get("rooms", {})
            
            # Build rooms list with additional details
            rooms = []
            for room_id, room_data in rooms_data.items():
                room_cfg = self.config.rooms.get(room_id, {})
                
                # Get manual setpoint from input_number entity
                manual_setpoint_entity = f"input_number.pyheat_{room_id}_manual_setpoint"
                manual_setpoint = None
                if self.ad.entity_exists(manual_setpoint_entity):
                    manual_setpoint_str = self.ad.get_state(manual_setpoint_entity)
                    if manual_setpoint_str not in [None, "unknown", "unavailable"]:
                        try:
                            manual_setpoint = float(manual_setpoint_str)
                        except (ValueError, TypeError):
                            pass
                
                # Get valve feedback consistency if available
                valve_fb_consistent = None
                fb_valve_entity_id = f"binary_sensor.pyheat_{room_id}_valve_feedback_consistent"
                if self.ad.entity_exists(fb_valve_entity_id):
                    fb_state = self.ad.get_state(fb_valve_entity_id)
                    valve_fb_consistent = (fb_state == "on") if fb_state else None
                
                # Build combined room status
                room_status = {
                    "id": room_id,
                    "name": room_cfg.get('name', room_id.replace("_", " ").title()),
                    "temp": room_data.get("temperature"),
                    "target": room_data.get("target"),
                    "mode": room_data.get("mode", "off"),
                    "calling_for_heat": room_data.get("calling_for_heat", False),
                    "valve_percent": room_data.get("valve_percent", 0),
                    "is_stale": room_data.get("is_stale", True),
                    "status_text": self.ad.get_state(f"sensor.pyheat_{room_id}_state") or "unknown",
                    "manual_setpoint": manual_setpoint,
                    "valve_feedback_consistent": valve_fb_consistent
                }
                rooms.append(room_status)
            
            # Build system status
            master_enabled = self.ad.get_state(C.HELPER_MASTER_ENABLE) == "on" if self.ad.entity_exists(C.HELPER_MASTER_ENABLE) else True
            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on" if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE) else False
            
            system = {
                "master_enabled": master_enabled,
                "holiday_mode": holiday_mode,
                "any_call_for_heat": status_attrs.get("any_call_for_heat", False),
                "boiler_state": status_attrs.get("boiler_state", "unknown"),
                "last_recompute": status_attrs.get("last_recompute")
            }
            
            return {
                "rooms": rooms,
                "system": system
            }
            
        except Exception as e:
            self.ad.log(f"pyheat.get_status failed: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_get_settings(self, namespace, domain, service, kwargs):
        """Service: pyheat.get_settings - Get current system settings.

        Returns current values of all configurable system settings.

        Returns:
            Dict with settings: {
                "master_enable": bool,
                "holiday_mode": bool,
                "opentherm_setpoint": float,
                "setpoint_ramp_enable": bool,
                "setpoint_ramp_max": float,
                "load_sharing_mode": str
            }
        """
        try:
            # Get all settings from HA input entities
            master_enable = self.ad.get_state(C.HELPER_MASTER_ENABLE) == "on" if self.ad.entity_exists(C.HELPER_MASTER_ENABLE) else False
            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on" if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE) else False

            # Get numeric settings
            opentherm_setpoint = 0.0
            if self.ad.entity_exists(C.HELPER_OPENTHERM_SETPOINT):
                try:
                    opentherm_setpoint = float(self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT))
                except (ValueError, TypeError):
                    opentherm_setpoint = 0.0

            setpoint_ramp_enable = self.ad.get_state(C.HELPER_SETPOINT_RAMP_ENABLE) == "on" if self.ad.entity_exists(C.HELPER_SETPOINT_RAMP_ENABLE) else False

            setpoint_ramp_max = 0.0
            if self.ad.entity_exists(C.HELPER_SETPOINT_RAMP_MAX):
                try:
                    setpoint_ramp_max = float(self.ad.get_state(C.HELPER_SETPOINT_RAMP_MAX))
                except (ValueError, TypeError):
                    setpoint_ramp_max = 0.0

            # Get load sharing mode
            load_sharing_mode = "Off"
            if self.ad.entity_exists(C.HELPER_LOAD_SHARING_MODE):
                load_sharing_mode = self.ad.get_state(C.HELPER_LOAD_SHARING_MODE) or "Off"

            return {
                "master_enable": master_enable,
                "holiday_mode": holiday_mode,
                "opentherm_setpoint": opentherm_setpoint,
                "setpoint_ramp_enable": setpoint_ramp_enable,
                "setpoint_ramp_max": setpoint_ramp_max,
                "load_sharing_mode": load_sharing_mode
            }

        except Exception as e:
            self.ad.log(f"pyheat.get_settings failed: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}

    def svc_set_settings(self, namespace, domain, service, kwargs):
        """Service: pyheat.set_settings - Update system settings.

        Updates one or more system settings. Only provided values will be updated.

        Args:
            master_enable (bool): Global system on/off (optional)
            holiday_mode (bool): Holiday/away mode (optional)
            opentherm_setpoint (float): Boiler water temperature 30-80°C (optional)
            setpoint_ramp_enable (bool): Enable setpoint ramping (optional)
            setpoint_ramp_max (float): Max ramp setpoint 30-80°C (optional)
            load_sharing_mode (str): Off/Conservative/Balanced/Aggressive (optional)

        Returns:
            Dict with success and updated settings
        """
        try:
            updated = []

            # Update master enable
            if "master_enable" in kwargs:
                value = kwargs["master_enable"]
                if self.ad.entity_exists(C.HELPER_MASTER_ENABLE):
                    service_name = "input_boolean/turn_on" if value else "input_boolean/turn_off"
                    self.ad.call_service(service_name, entity_id=C.HELPER_MASTER_ENABLE)
                    updated.append("master_enable")
                    self.ad.log(f"Set master_enable to {value}")

            # Update holiday mode
            if "holiday_mode" in kwargs:
                value = kwargs["holiday_mode"]
                if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                    service_name = "input_boolean/turn_on" if value else "input_boolean/turn_off"
                    self.ad.call_service(service_name, entity_id=C.HELPER_HOLIDAY_MODE)
                    updated.append("holiday_mode")
                    self.ad.log(f"Set holiday_mode to {value}")

            # Update OpenTherm setpoint
            if "opentherm_setpoint" in kwargs:
                value = float(kwargs["opentherm_setpoint"])
                if value < 30.0 or value > 80.0:
                    return {"success": False, "error": "opentherm_setpoint must be between 30 and 80"}
                if self.ad.entity_exists(C.HELPER_OPENTHERM_SETPOINT):
                    self.ad.call_service("input_number/set_value",
                                       entity_id=C.HELPER_OPENTHERM_SETPOINT,
                                       value=value)
                    updated.append("opentherm_setpoint")
                    self.ad.log(f"Set opentherm_setpoint to {value}")

            # Update setpoint ramp enable
            if "setpoint_ramp_enable" in kwargs:
                value = kwargs["setpoint_ramp_enable"]
                if self.ad.entity_exists(C.HELPER_SETPOINT_RAMP_ENABLE):
                    service_name = "input_boolean/turn_on" if value else "input_boolean/turn_off"
                    self.ad.call_service(service_name, entity_id=C.HELPER_SETPOINT_RAMP_ENABLE)
                    updated.append("setpoint_ramp_enable")
                    self.ad.log(f"Set setpoint_ramp_enable to {value}")

            # Update setpoint ramp max
            if "setpoint_ramp_max" in kwargs:
                value = float(kwargs["setpoint_ramp_max"])
                if value < 30.0 or value > 80.0:
                    return {"success": False, "error": "setpoint_ramp_max must be between 30 and 80"}
                if self.ad.entity_exists(C.HELPER_SETPOINT_RAMP_MAX):
                    self.ad.call_service("input_number/set_value",
                                       entity_id=C.HELPER_SETPOINT_RAMP_MAX,
                                       value=value)
                    updated.append("setpoint_ramp_max")
                    self.ad.log(f"Set setpoint_ramp_max to {value}")

            # Update load sharing mode
            if "load_sharing_mode" in kwargs:
                value = kwargs["load_sharing_mode"]
                valid_modes = ["Off", "Conservative", "Balanced", "Aggressive"]
                if value not in valid_modes:
                    return {"success": False, "error": f"load_sharing_mode must be one of: {', '.join(valid_modes)}"}
                if self.ad.entity_exists(C.HELPER_LOAD_SHARING_MODE):
                    self.ad.call_service("input_select/select_option",
                                       entity_id=C.HELPER_LOAD_SHARING_MODE,
                                       option=value)
                    updated.append("load_sharing_mode")
                    self.ad.log(f"Set load_sharing_mode to {value}")

            if not updated:
                return {"success": False, "error": "No valid settings provided"}

            # Trigger recompute if needed (changes to master_enable or holiday_mode especially)
            if self.trigger_recompute_callback:
                self.trigger_recompute_callback()

            return {
                "success": True,
                "updated": updated
            }

        except Exception as e:
            self.ad.log(f"pyheat.set_settings failed: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}

