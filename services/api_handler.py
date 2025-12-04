# -*- coding: utf-8 -*-
"""
api_handler.py - HTTP API endpoints for external access

Responsibilities:
- Register HTTP API endpoints using Appdaemon's register_endpoint()
- Handle HTTP requests from pyheat-web
- Bridge between HTTP requests and internal service handlers
- Provide JSON responses for all service operations
"""

import json
from typing import Any, Dict
import constants as C

class APIHandler:
    """Handles HTTP API endpoints for external access to PyHeat services."""
    
    def __init__(self, ad, service_handler):
        """Initialize the API handler.
        
        Args:
            ad: AppDaemon API reference
            service_handler: ServiceHandler instance for executing operations
        """
        self.ad = ad
        self.service_handler = service_handler
    
    def _strip_time_from_status(self, status: str) -> str:
        """Strip time information from formatted_status for pyheat-web.
        
        Auto mode: Keep full status with times (same as HA)
        Override: Strip " until HH:MM" only (web will show live countdown)
        
        Args:
            status: Formatted status string from status_publisher
            
        Returns:
            Status with times stripped only for Override (not Auto)
        """
        if not status:
            return status
        
        import re
        
        # Strip " until HH:MM" from Override only
        # Auto mode has different structure: "until HH:MM on Day (T°)" - won't match this pattern
        # Override: "Override: T° (ΔD°) until HH:MM" - matches and strips
        if status.startswith("Override:"):
            status = re.sub(r' until \d{2}:\d{2}', '', status)
        
        return status
        
    def register_all(self) -> None:
        """Register all HTTP API endpoints."""
        # Register endpoints for each service operation
        self.ad.register_endpoint(self.api_override, "pyheat_override")
        self.ad.register_endpoint(self.api_cancel_override, "pyheat_cancel_override")
        self.ad.register_endpoint(self.api_set_mode, "pyheat_set_mode")
        self.ad.register_endpoint(self.api_set_passive_settings, "pyheat_set_passive_settings")
        self.ad.register_endpoint(self.api_set_default_target, "pyheat_set_default_target")
        self.ad.register_endpoint(self.api_reload_config, "pyheat_reload_config")
        self.ad.register_endpoint(self.api_get_schedules, "pyheat_get_schedules")
        self.ad.register_endpoint(self.api_get_rooms, "pyheat_get_rooms")
        self.ad.register_endpoint(self.api_replace_schedules, "pyheat_replace_schedules")
        self.ad.register_endpoint(self.api_get_status, "pyheat_get_status")
        self.ad.register_endpoint(self.api_get_history, "pyheat_get_history")
        self.ad.register_endpoint(self.api_get_boiler_history, "pyheat_get_boiler_history")
        
        self.ad.log("Registered PyHeat HTTP API endpoints")
        
    def _handle_request(self, callback, request_body: Dict[str, Any]) -> tuple:
        """Common request handler with error handling.
        
        Args:
            callback: Service callback function to invoke
            request_body: Request parameters from HTTP body
            
        Returns:
            Tuple of (response_dict, status_code)
        """
        try:
            # Service handlers are synchronous
            result = callback("api", "pyheat", "api", request_body)
            
            if isinstance(result, dict):
                if result.get("success", True):
                    return result, 200
                else:
                    return result, 400
            else:
                # If no dict returned, assume success
                return {"success": True}, 200
                
        except Exception as e:
            self.ad.log(f"API request error: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}, 500
    
    def api_override(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_override
        
        Sets temporary temperature override for a room with flexible parameters.
        
        Request body: {
            "room": str,                    # Required
            "target": float,                # Absolute temp (mutually exclusive with delta)
            "delta": float,                 # Temp delta (mutually exclusive with target)
            "minutes": int,                 # Duration in minutes (mutually exclusive with end_time)
            "end_time": str                 # ISO datetime (mutually exclusive with minutes)
        }
        
        Examples:
            {"room": "pete", "target": 21.0, "minutes": 120}
            {"room": "pete", "delta": 2.0, "minutes": 180}
            {"room": "pete", "target": 20.0, "end_time": "2025-11-10T17:30:00"}
        """
        # In Appdaemon, the JSON body is passed as the first parameter (namespace)
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_override, request_body)
    
    def api_cancel_override(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_cancel_override
        
        Cancels active override.
        
        Request body: {
            "room": str
        }
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_cancel_override, request_body)
    
    def api_set_mode(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_mode
        
        Sets room operating mode.
        
        Request body: {
            "room": str,
            "mode": str  # "auto", "manual", "passive", or "off"
        }
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_set_mode, request_body)
    
    def api_set_passive_settings(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_passive_settings
        
        Sets all passive mode settings for a room (batched update).
        
        Request body: {
            "room": str,
            "max_temp": float,      # 10-30°C
            "valve_percent": int,   # 0-100%
            "min_temp": float       # 8-20°C
        }
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_set_passive_settings, request_body)
    
    def api_set_default_target(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_default_target
        
        Updates room's default target temperature in schedules.yaml.
        
        Request body: {
            "room": str,
            "target": float
        }
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_set_default_target, request_body)
    
    def api_reload_config(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_reload_config
        
        Reloads PyHeat configuration from files.
        
        Request body: {} (empty)
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_reload_config, request_body)
    
    def api_get_schedules(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_schedules
        
        Gets current schedules configuration.
        
        Request body: {} (empty)
        Returns: Complete schedules.yaml contents
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        try:
            result = self.service_handler.svc_get_schedules("api", "pyheat", "get_schedules", request_body)
            
            # Format response to match what pyheat-web expects
            if isinstance(result, dict) and not result.get("success") == False:
                # Result is the schedules dict - wrap it in expected format
                # Convert from {room_id: {config}} to {rooms: [{id: room_id, ...config}]}
                rooms_list = []
                for room_id, room_config in result.items():
                    room_data = {"id": room_id}
                    room_data.update(room_config)
                    rooms_list.append(room_data)
                
                return {"rooms": rooms_list}, 200
            else:
                return result, 400
                
        except Exception as e:
            self.ad.log(f"get_schedules API error: {e}", level="ERROR")
            return {"success": False, "error": str(e)}, 500
    
    def api_get_rooms(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_rooms
        
        Gets current rooms configuration.
        
        Request body: {} (empty)
        Returns: Complete rooms.yaml contents
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_get_rooms, request_body)
    
    def api_replace_schedules(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_replace_schedules
        
        Atomically replaces entire schedules.yaml.
        
        Request body: {
            "schedule": {
                "room_id": {
                    "default_target": float,
                    "week": {...}
                },
                ...
            }
        }
        """
        request_body = namespace if isinstance(namespace, dict) else {}
        return self._handle_request(self.service_handler.svc_replace_schedules, request_body)
    
    def api_get_status(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_status
        
        Gets complete system and room status directly from pyheat.
        This eliminates the need for pyheat-web to read individual HA entities.
        
        Request body: {} (empty)
        Returns: {
            "rooms": [{id, name, temp, target, mode, calling_for_heat, valve_percent, ...}],
            "system": {master_enabled, holiday_mode, any_call_for_heat, boiler_state, ...}
        }
        """
        try:
            # Get main status entity which has comprehensive attributes
            import constants as C
            
            status_state = self.ad.get_state(C.STATUS_ENTITY, attribute="all")
            if not status_state:
                return {"success": False, "error": "Status entity not available"}, 500
            
            status_attrs = status_state.get("attributes", {})
            rooms_data = status_attrs.get("rooms", {})
            
            # Build rooms list with additional details
            rooms = []
            for room_id, room_data in rooms_data.items():
                room_cfg = self.service_handler.config.rooms.get(room_id, {})
                
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
                
                # Get base status text and state entity attributes
                state_entity_id = f"sensor.pyheat_{room_id}_state"
                base_status_text = self.ad.get_state(state_entity_id) or "unknown"
                
                # Get state entity attributes for server-formatted status and metadata
                state_entity_full = self.ad.get_state(state_entity_id, attribute="all")
                state_attrs = state_entity_full.get("attributes", {}) if state_entity_full else {}
                
                # Check for active override timer and enhance status_text
                timer_entity = f"timer.pyheat_{room_id}_override"
                status_text = base_status_text
                override_end_time = None  # ISO 8601 timestamp when override finishes
                
                if self.ad.entity_exists(timer_entity):
                    timer_state = self.ad.get_state(timer_entity)
                    if timer_state == "active":
                        # Timer is active - get remaining time and override details
                        timer_attrs = self.ad.get_state(timer_entity, attribute="all")
                        if timer_attrs and "attributes" in timer_attrs:
                            # Get finishes_at for client-side countdown
                            override_end_time = timer_attrs["attributes"].get("finishes_at")
                            
                            remaining_str = timer_attrs["attributes"].get("remaining")
                            if remaining_str:
                                # Parse remaining time (format: "H:MM:SS" or "M:SS")
                                parts = remaining_str.split(":")
                                if len(parts) == 3:  # H:MM:SS
                                    hours, minutes, seconds = map(int, parts)
                                    total_minutes = hours * 60 + minutes + (1 if seconds > 0 else 0)
                                elif len(parts) == 2:  # MM:SS or M:SS
                                    minutes, seconds = map(int, parts)
                                    total_minutes = minutes + (1 if seconds > 0 else 0)
                                else:
                                    total_minutes = 0
                                
                                # Get override target
                                override_target_entity = f"input_number.pyheat_{room_id}_override_target"
                                override_target = None
                                if self.ad.entity_exists(override_target_entity):
                                    override_target_str = self.ad.get_state(override_target_entity)
                                    if override_target_str not in [None, "unknown", "unavailable"]:
                                        try:
                                            override_target = float(override_target_str)
                                        except (ValueError, TypeError):
                                            pass
                                
                                # Build simple override status text
                                if override_target is not None:
                                    status_text = f"override({override_target:.1f}) {total_minutes}m"
                
                # Get actual valve position from TRV feedback sensor
                actual_valve_percent = 0
                room_cfg = self.service_handler.config.rooms.get(room_id, {})
                if room_cfg:
                    fb_valve_entity = room_cfg.get('trv', {}).get('fb_valve')
                    if fb_valve_entity and self.ad.entity_exists(fb_valve_entity):
                        try:
                            fb_state = self.ad.get_state(fb_valve_entity)
                            if fb_state and fb_state not in ['unknown', 'unavailable']:
                                actual_valve_percent = int(float(fb_state))
                        except (ValueError, TypeError):
                            # Fall back to commanded valve percent
                            actual_valve_percent = room_data.get("valve_percent", 0)
                    else:
                        # No feedback sensor, use commanded percent
                        actual_valve_percent = room_data.get("valve_percent", 0)
                
                # Get passive mode settings from input_number helper entities
                passive_max_temp = None
                passive_min_temp = None
                passive_valve_percent = None
                
                passive_max_entity = C.HELPER_ROOM_PASSIVE_MAX_TEMP.format(room=room_id)
                if self.ad.entity_exists(passive_max_entity):
                    try:
                        max_temp_str = self.ad.get_state(passive_max_entity)
                        if max_temp_str not in [None, "unknown", "unavailable"]:
                            passive_max_temp = float(max_temp_str)
                    except (ValueError, TypeError):
                        pass
                
                passive_min_entity = C.HELPER_ROOM_PASSIVE_MIN_TEMP.format(room=room_id)
                if self.ad.entity_exists(passive_min_entity):
                    try:
                        min_temp_str = self.ad.get_state(passive_min_entity)
                        if min_temp_str not in [None, "unknown", "unavailable"]:
                            passive_min_temp = float(min_temp_str)
                    except (ValueError, TypeError):
                        pass
                
                # Get passive valve percent - schedule's default_valve_percent takes precedence
                schedule = self.service_handler.config.schedules.get(room_id, {})
                schedule_valve = schedule.get('default_valve_percent')
                if schedule_valve is not None:
                    passive_valve_percent = int(schedule_valve)
                else:
                    passive_valve_entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room_id)
                    if self.ad.entity_exists(passive_valve_entity):
                        try:
                            valve_str = self.ad.get_state(passive_valve_entity)
                            if valve_str not in [None, "unknown", "unavailable"]:
                                passive_valve_percent = int(float(valve_str))
                        except (ValueError, TypeError):
                            pass
                
                # Build combined room status
                room_status = {
                    "id": room_id,
                    "name": room_cfg.get('name', room_id.replace("_", " ").title()),
                    "temp": room_data.get("temperature"),
                    "target": room_data.get("target"),
                    "mode": room_data.get("mode", "off"),
                    "operating_mode": state_attrs.get("operating_mode", room_data.get("mode", "off")),  # Actual heating mode from state entity
                    "calling_for_heat": room_data.get("calling_for_heat", False),
                    "valve_percent": actual_valve_percent,
                    "is_stale": room_data.get("is_stale", True),
                    "status_text": status_text,
                    "formatted_status": self._strip_time_from_status(state_attrs.get("formatted_status")),  # Strip times for web
                    "manual_setpoint": manual_setpoint,
                    "valve_feedback_consistent": valve_fb_consistent,
                    "override_end_time": override_end_time,  # ISO 8601 timestamp or null
                    # Override metadata from state entity (calculated on-the-fly)
                    "override_remaining_minutes": state_attrs.get("override_remaining_minutes"),
                    "override_target": state_attrs.get("override_target"),
                    "scheduled_temp": state_attrs.get("scheduled_temp"),
                    # Passive mode settings from number helpers
                    "passive_max_temp": passive_max_temp,
                    "passive_min_temp": passive_min_temp,
                    "passive_valve_percent": passive_valve_percent,
                }
                rooms.append(room_status)
            
            # Build system status
            master_enabled = self.ad.get_state(C.HELPER_MASTER_ENABLE) == "on" if self.ad.entity_exists(C.HELPER_MASTER_ENABLE) else True
            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on" if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE) else False
            
            # Get boiler timer end times for client-side countdowns
            boiler_off_delay_end_time = None
            boiler_min_on_end_time = None
            boiler_min_off_end_time = None
            boiler_pump_overrun_end_time = None
            
            # Check boiler state and timers
            boiler_state_val = status_attrs.get("boiler_state", "unknown")
            if C.DEBUG_API_LOGGING:
                self.ad.log(f"API: boiler_state={boiler_state_val}", level="DEBUG")
            
            if self.ad.entity_exists(C.HELPER_BOILER_OFF_DELAY_TIMER):
                timer_state = self.ad.get_state(C.HELPER_BOILER_OFF_DELAY_TIMER)
                if C.DEBUG_API_LOGGING:
                    self.ad.log(f"API: off_delay_timer state={timer_state}", level="DEBUG")
                if timer_state == "active":
                    timer_attrs = self.ad.get_state(C.HELPER_BOILER_OFF_DELAY_TIMER, attribute="all")
                    if C.DEBUG_API_LOGGING:
                        self.ad.log(f"API: off_delay_timer attrs={timer_attrs}", level="DEBUG")
                    if timer_attrs and "attributes" in timer_attrs:
                        boiler_off_delay_end_time = timer_attrs["attributes"].get("finishes_at")
                        if C.DEBUG_API_LOGGING:
                            self.ad.log(f"API: off_delay finishes_at={boiler_off_delay_end_time}", level="DEBUG")
            
            if self.ad.entity_exists(C.HELPER_BOILER_MIN_ON_TIMER):
                timer_state = self.ad.get_state(C.HELPER_BOILER_MIN_ON_TIMER)
                if C.DEBUG_API_LOGGING:
                    self.ad.log(f"API: min_on_timer state={timer_state}", level="DEBUG")
                if timer_state == "active":
                    timer_attrs = self.ad.get_state(C.HELPER_BOILER_MIN_ON_TIMER, attribute="all")
                    if C.DEBUG_API_LOGGING:
                        self.ad.log(f"API: min_on_timer attrs={timer_attrs}", level="DEBUG")
                    if timer_attrs and "attributes" in timer_attrs:
                        boiler_min_on_end_time = timer_attrs["attributes"].get("finishes_at")
                        if C.DEBUG_API_LOGGING:
                            self.ad.log(f"API: min_on finishes_at={boiler_min_on_end_time}", level="DEBUG")
            
            if self.ad.entity_exists(C.HELPER_PUMP_OVERRUN_TIMER):
                timer_state = self.ad.get_state(C.HELPER_PUMP_OVERRUN_TIMER)
                if C.DEBUG_API_LOGGING:
                    self.ad.log(f"API: pump_overrun_timer state={timer_state}", level="DEBUG")
                if timer_state == "active":
                    timer_attrs = self.ad.get_state(C.HELPER_PUMP_OVERRUN_TIMER, attribute="all")
                    if C.DEBUG_API_LOGGING:
                        self.ad.log(f"API: pump_overrun_timer attrs={timer_attrs}", level="DEBUG")
                    if timer_attrs and "attributes" in timer_attrs:
                        boiler_pump_overrun_end_time = timer_attrs["attributes"].get("finishes_at")
                        if C.DEBUG_API_LOGGING:
                            self.ad.log(f"API: pump_overrun finishes_at={boiler_pump_overrun_end_time}", level="DEBUG")
            
            if self.ad.entity_exists(C.HELPER_BOILER_MIN_OFF_TIMER):
                timer_state = self.ad.get_state(C.HELPER_BOILER_MIN_OFF_TIMER)
                if C.DEBUG_API_LOGGING:
                    self.ad.log(f"API: min_off_timer state={timer_state}", level="DEBUG")
                if timer_state == "active":
                    timer_attrs = self.ad.get_state(C.HELPER_BOILER_MIN_OFF_TIMER, attribute="all")
                    if C.DEBUG_API_LOGGING:
                        self.ad.log(f"API: min_off_timer attrs={timer_attrs}", level="DEBUG")
                    if timer_attrs and "attributes" in timer_attrs:
                        boiler_min_off_end_time = timer_attrs["attributes"].get("finishes_at")
                        if C.DEBUG_API_LOGGING:
                            self.ad.log(f"API: min_off finishes_at={boiler_min_off_end_time}", level="DEBUG")
            
            # Get cooldown active state from cycling protection
            cycling_protection = status_attrs.get("cycling_protection")
            if cycling_protection:
                cooldown_active = cycling_protection.get("state") == "COOLDOWN"
            else:
                cooldown_active = False  # No cycling protection available, so not in cooldown

            system = {
                "master_enabled": master_enabled,
                "holiday_mode": holiday_mode,
                "any_call_for_heat": status_attrs.get("any_call_for_heat", False),
                "boiler_state": status_attrs.get("boiler_state", "unknown"),
                "last_recompute": status_attrs.get("last_recompute"),
                "boiler_off_delay_end_time": boiler_off_delay_end_time,
                "boiler_min_on_end_time": boiler_min_on_end_time,
                "boiler_min_off_end_time": boiler_min_off_end_time,
                "boiler_pump_overrun_end_time": boiler_pump_overrun_end_time,
                "cooldown_active": cooldown_active,
                "load_sharing": status_attrs.get("load_sharing"),
            }
            
            return {
                "rooms": rooms,
                "system": system
            }, 200
            
        except Exception as e:
            self.ad.log(f"get_status API error: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}, 500

    def api_get_history(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_get_history
        
        Gets historical temperature, setpoint, mode, and boiler data for a room.
        
        Request body: {
            "room": str,        # Room ID (e.g., "pete", "lounge")
            "period": str       # "today", "yesterday", or "recent_Xh" (X = 1-12)
        }
        
        Returns: {
            "temperature": [{"time": str, "value": float}],
            "setpoint": [{"time": str, "value": float}],
            "mode": [{"time": str, "mode": "auto"|"manual"|"passive"|"off"}],
            "calling_for_heat": [["start_time", "end_time"]]
        }
        """
        from datetime import datetime, timedelta, timezone
        
        request_body = namespace if isinstance(namespace, dict) else {}
        
        try:
            room_id = request_body.get("room")
            period = request_body.get("period", "today")
            
            if not room_id:
                return {"success": False, "error": "room parameter required"}, 400
            
            # Calculate time range
            now = datetime.now(timezone.utc)
            
            # Check for recent_Xh format (e.g., "recent_1h", "recent_3h")
            if period.startswith("recent_"):
                try:
                    hours_str = period.replace("recent_", "").replace("h", "")
                    hours = int(hours_str)
                    if hours < 1 or hours > 12:
                        return {"success": False, "error": "recent hours must be between 1 and 12"}, 400
                    start_time = now - timedelta(hours=hours)
                    end_time = now
                except ValueError:
                    return {"success": False, "error": "invalid recent period format"}, 400
            elif period == "today":
                start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end_time = now
            elif period == "yesterday":
                yesterday = now - timedelta(days=1)
                start_time = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
                end_time = start_time + timedelta(days=1)
            else:
                return {"success": False, "error": "period must be 'today', 'yesterday', or 'recent_Xh'"}, 400
            
            # Build entity IDs for this room
            temp_sensor = f"sensor.pyheat_{room_id}_temperature"
            target_sensor = f"sensor.pyheat_{room_id}_target"
            mode_select = f"input_select.pyheat_{room_id}_mode"
            
            # Fetch history using AppDaemon's get_history
            # get_history(entity_id, start_time=None, end_time=None)
            temperature_data = []
            setpoint_data = []
            mode_data = []
            calling_ranges = []
            
            # Temperature history
            if self.ad.entity_exists(temp_sensor):
                temp_history = self.ad.get_history(
                    entity_id=temp_sensor,
                    start_time=start_time,
                    end_time=end_time
                )
                
                # get_history returns [[{state, last_changed, ...}, ...]]
                if temp_history and len(temp_history) > 0:
                    for state_obj in temp_history[0]:
                        try:
                            temp_value = float(state_obj["state"])
                            temperature_data.append({
                                "time": state_obj["last_changed"],
                                "value": temp_value
                            })
                        except (ValueError, KeyError):
                            continue
            
            # Setpoint history
            if self.ad.entity_exists(target_sensor):
                setpoint_history = self.ad.get_history(
                    entity_id=target_sensor,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if setpoint_history and len(setpoint_history) > 0:
                    for state_obj in setpoint_history[0]:
                        try:
                            setpoint_value = float(state_obj["state"])
                            setpoint_data.append({
                                "time": state_obj["last_changed"],
                                "value": setpoint_value
                            })
                        except (ValueError, KeyError):
                            continue
            
            # Mode history - for mode-aware setpoint coloring in charts
            if self.ad.entity_exists(mode_select):
                mode_history = self.ad.get_history(
                    entity_id=mode_select,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if mode_history and len(mode_history) > 0:
                    for state_obj in mode_history[0]:
                        try:
                            mode_value = state_obj["state"].lower()  # Normalize to lowercase
                            # Only include valid modes
                            if mode_value in ("auto", "manual", "passive", "off"):
                                mode_data.append({
                                    "time": state_obj["last_changed"],
                                    "mode": mode_value
                                })
                        except (KeyError, TypeError, AttributeError):
                            continue
            
            # Operating mode history - tracks actual heating behavior (e.g., auto mode in passive schedule)
            # Also extracts override info for chart coloring (red for heating override, blue for cooling)
            # This is stored as attributes on the state entity
            operating_mode_data = []
            override_data = []  # Tracks override periods with type (heating/cooling)
            state_entity = f"sensor.pyheat_{room_id}_state"
            if self.ad.entity_exists(state_entity):
                state_history = self.ad.get_history(
                    entity_id=state_entity,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if state_history and len(state_history) > 0:
                    for state_obj in state_history[0]:
                        try:
                            attrs = state_obj.get("attributes", {})
                            timestamp = state_obj["last_changed"]
                            
                            # Extract operating mode
                            op_mode = attrs.get("operating_mode", "").lower()
                            # Only include valid operating modes
                            if op_mode in ("auto", "manual", "passive", "off"):
                                operating_mode_data.append({
                                    "time": timestamp,
                                    "operating_mode": op_mode
                                })
                            
                            # Extract override info
                            # override_target exists only when override is active
                            override_target = attrs.get("override_target")
                            scheduled_temp = attrs.get("scheduled_temp")
                            
                            if override_target is not None:
                                # Override is active - determine if heating (positive) or cooling (negative)
                                override_type = "none"
                                if scheduled_temp is not None:
                                    if override_target > scheduled_temp:
                                        override_type = "heating"  # Red - boosting above schedule
                                    elif override_target < scheduled_temp:
                                        override_type = "cooling"  # Blue - reducing below schedule
                                    else:
                                        override_type = "neutral"  # Same as schedule (rare edge case)
                                else:
                                    # No scheduled_temp available, just mark as active override
                                    override_type = "active"
                                
                                override_data.append({
                                    "time": timestamp,
                                    "override_type": override_type,
                                    "override_target": override_target,
                                    "scheduled_temp": scheduled_temp
                                })
                            else:
                                # No override active
                                override_data.append({
                                    "time": timestamp,
                                    "override_type": "none",
                                    "override_target": None,
                                    "scheduled_temp": scheduled_temp
                                })
                        except (KeyError, TypeError, AttributeError):
                            continue
            
            # Calling for heat - use the dedicated binary sensor for this room
            calling_sensor = f"binary_sensor.pyheat_{room_id}_calling_for_heat"
            if self.ad.entity_exists(calling_sensor):
                calling_history = self.ad.get_history(
                    entity_id=calling_sensor,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if calling_history and len(calling_history) > 0:
                    calling_start = None
                    
                    for state_obj in calling_history[0]:
                        try:
                            # Binary sensor states are "on" or "off"
                            is_calling = state_obj.get("state") == "on"
                            timestamp = state_obj["last_changed"]
                            
                            if is_calling and calling_start is None:
                                # Start of a calling period
                                calling_start = timestamp
                            elif not is_calling and calling_start is not None:
                                # End of a calling period
                                calling_ranges.append([calling_start, timestamp])
                                calling_start = None
                        except (KeyError, TypeError):
                            continue
                    
                    # If still calling at end of period
                    if calling_start is not None:
                        calling_ranges.append([calling_start, end_time.isoformat()])
            
            return {
                "temperature": temperature_data,
                "setpoint": setpoint_data,
                "mode": mode_data,
                "operating_mode": operating_mode_data,
                "override": override_data,
                "calling_for_heat": calling_ranges
            }, 200
            
        except Exception as e:
            self.ad.log(f"get_history API error: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}, 500

    def api_get_boiler_history(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_get_boiler_history
        
        Gets historical boiler state data for a given day.
        
        Request body: {
            "days_ago": int  # 0 = today, 1 = yesterday, etc. (max 7)
        }
        
        Returns: {
            "periods": [
                {"start": str (ISO), "end": str (ISO), "state": "on" | "off"},
                ...
            ]
        }
        """
        from datetime import datetime, timedelta, timezone
        
        request_body = namespace if isinstance(namespace, dict) else {}
        
        try:
            days_ago = request_body.get("days_ago", 0)
            
            if not isinstance(days_ago, int) or days_ago < 0 or days_ago > 7:
                return {"success": False, "error": "days_ago must be integer 0-7"}, 400
            
            # Calculate time range for the requested day
            now = datetime.now(timezone.utc)
            target_day = now - timedelta(days=days_ago)
            start_time = target_day.replace(hour=0, minute=0, second=0, microsecond=0)
            end_time = start_time + timedelta(days=1)
            
            # If it's today, only go up to now
            if days_ago == 0:
                end_time = now
            
            # Get boiler state history from status sensor
            status_entity = C.STATUS_ENTITY  # sensor.pyheat_status
            periods = []
            
            if self.ad.entity_exists(status_entity):
                status_history = self.ad.get_history(
                    entity_id=status_entity,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if status_history and len(status_history) > 0:
                    period_start = None
                    period_state = None
                    
                    for state_obj in status_history[0]:
                        try:
                            # Get boiler_state from attributes
                            attrs = state_obj.get("attributes", {})
                            boiler_state = attrs.get("boiler_state")
                            timestamp = state_obj["last_changed"]
                            
                            if not boiler_state:
                                continue
                            
                            # Only track "on" state (actual heating)
                            # Ignore pump_overrun, pending_off, pending_on, etc.
                            is_heating = boiler_state == "on"
                            current_state = "on" if is_heating else "off"
                            
                            # If this is the first state or state changed
                            if period_state is None:
                                # Start first period
                                period_start = timestamp
                                period_state = current_state
                            elif current_state != period_state:
                                # State changed - end previous period and start new one
                                if period_start is not None:
                                    periods.append({
                                        "start": period_start,
                                        "end": timestamp,
                                        "state": period_state
                                    })
                                period_start = timestamp
                                period_state = current_state
                        except (KeyError, TypeError):
                            continue
                    
                    # Close final period
                    if period_start is not None and period_state is not None:
                        periods.append({
                            "start": period_start,
                            "end": end_time.isoformat(),
                            "state": period_state
                        })
            
            return {
                "periods": periods,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat()
            }, 200
            
        except Exception as e:
            self.ad.log(f"get_boiler_history API error: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}, 500


