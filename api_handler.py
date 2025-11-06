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
        
    def register_all(self) -> None:
        """Register all HTTP API endpoints."""
        # Register endpoints for each service operation
        self.ad.register_endpoint(self.api_override, "pyheat_override")
        self.ad.register_endpoint(self.api_boost, "pyheat_boost")
        self.ad.register_endpoint(self.api_cancel_override, "pyheat_cancel_override")
        self.ad.register_endpoint(self.api_set_mode, "pyheat_set_mode")
        self.ad.register_endpoint(self.api_set_default_target, "pyheat_set_default_target")
        self.ad.register_endpoint(self.api_reload_config, "pyheat_reload_config")
        self.ad.register_endpoint(self.api_get_schedules, "pyheat_get_schedules")
        self.ad.register_endpoint(self.api_get_rooms, "pyheat_get_rooms")
        self.ad.register_endpoint(self.api_replace_schedules, "pyheat_replace_schedules")
        self.ad.register_endpoint(self.api_get_status, "pyheat_get_status")
        
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
            return {"success": False, "error": str(e)}, 500
    
    async def api_override(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_override
        
        Sets absolute target override for a room.
        
        Request body: {
            "room": str,
            "target": float,
            "minutes": int
        }
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_override, request_body)
    
    async def api_boost(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_boost
        
        Applies delta boost to current target.
        
        Request body: {
            "room": str,
            "delta": float,
            "minutes": int
        }
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_boost, request_body)
    
    async def api_cancel_override(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_cancel_override
        
        Cancels active override/boost.
        
        Request body: {
            "room": str
        }
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_cancel_override, request_body)
    
    async def api_set_mode(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_mode
        
        Sets room operating mode.
        
        Request body: {
            "room": str,
            "mode": str  # "auto", "manual", or "off"
        }
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_set_mode, request_body)
    
    async def api_set_default_target(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_default_target
        
        Updates room's default target temperature in schedules.yaml.
        
        Request body: {
            "room": str,
            "target": float
        }
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_set_default_target, request_body)
    
    async def api_reload_config(self, data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_reload_config
        
        Reloads PyHeat configuration from files.
        
        Request body: {} (empty)
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_reload_config, request_body)
    
    async def api_get_schedules(self, namespace, data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_schedules
        
        Gets current schedules configuration.
        
        Request body: {} (empty)
        Returns: Complete schedules.yaml contents
        """
        request_body = data.get("body", {})
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
    
    async def api_get_rooms(self, data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_rooms
        
        Gets current rooms configuration.
        
        Request body: {} (empty)
        Returns: Complete rooms.yaml contents
        """
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_get_rooms, request_body)
    
    async def api_replace_schedules(self, data: Dict[str, Any]) -> tuple:
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
        request_body = data.get("body", {})
        return self._handle_request(self.service_handler.svc_replace_schedules, request_body)
    
    async def api_get_status(self, namespace, data: Dict[str, Any]) -> tuple:
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
            import pyheat.constants as C
            
            status_state = await self.ad.get_state(C.STATUS_ENTITY, attribute="all")
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
                if await self.ad.entity_exists(manual_setpoint_entity):
                    manual_setpoint_str = await self.ad.get_state(manual_setpoint_entity)
                    if manual_setpoint_str not in [None, "unknown", "unavailable"]:
                        try:
                            manual_setpoint = float(manual_setpoint_str)
                        except (ValueError, TypeError):
                            pass
                
                # Get valve feedback consistency if available
                valve_fb_consistent = None
                fb_valve_entity_id = f"binary_sensor.pyheat_{room_id}_valve_feedback_consistent"
                if await self.ad.entity_exists(fb_valve_entity_id):
                    fb_state = await self.ad.get_state(fb_valve_entity_id)
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
                    "status_text": await self.ad.get_state(f"sensor.pyheat_{room_id}_state") or "unknown",
                    "manual_setpoint": manual_setpoint,
                    "valve_feedback_consistent": valve_fb_consistent
                }
                rooms.append(room_status)
            
            # Build system status
            master_enabled = await self.ad.get_state(C.HELPER_MASTER_ENABLE) == "on" if await self.ad.entity_exists(C.HELPER_MASTER_ENABLE) else True
            holiday_mode = await self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on" if await self.ad.entity_exists(C.HELPER_HOLIDAY_MODE) else False
            
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
            }, 200
            
        except Exception as e:
            self.ad.log(f"get_status API error: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {"success": False, "error": str(e)}, 500

