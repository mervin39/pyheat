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
        
        self.ad.log("Registered PyHeat HTTP API endpoints")
        
    def _handle_request(self, callback, request_data: Dict[str, Any]) -> tuple:
        """Common request handler with error handling.
        
        Args:
            callback: Service callback function to invoke
            request_data: Request parameters from HTTP body
            
        Returns:
            Tuple of (response_dict, status_code)
        """
        try:
            result = callback("api", "pyheat", "api", request_data)
            
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
    
    async def api_override(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_override
        
        Sets absolute target override for a room.
        
        Request body: {
            "room": str,
            "target": float,
            "minutes": int
        }
        """
        return self._handle_request(self.service_handler.svc_override, request_data)
    
    async def api_boost(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_boost
        
        Applies delta boost to current target.
        
        Request body: {
            "room": str,
            "delta": float,
            "minutes": int
        }
        """
        return self._handle_request(self.service_handler.svc_boost, request_data)
    
    async def api_cancel_override(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_cancel_override
        
        Cancels active override/boost.
        
        Request body: {
            "room": str
        }
        """
        return self._handle_request(self.service_handler.svc_cancel_override, request_data)
    
    async def api_set_mode(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_mode
        
        Sets room operating mode.
        
        Request body: {
            "room": str,
            "mode": str  # "auto", "manual", or "off"
        }
        """
        return self._handle_request(self.service_handler.svc_set_mode, request_data)
    
    async def api_set_default_target(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_set_default_target
        
        Updates room's default target temperature in schedules.yaml.
        
        Request body: {
            "room": str,
            "target": float
        }
        """
        return self._handle_request(self.service_handler.svc_set_default_target, request_data)
    
    async def api_reload_config(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: POST /api/appdaemon/pyheat_reload_config
        
        Reloads PyHeat configuration from files.
        
        Request body: {} (empty)
        """
        return self._handle_request(self.service_handler.svc_reload_config, request_data)
    
    async def api_get_schedules(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_schedules
        
        Gets current schedules configuration.
        
        Request body: {} (empty)
        Returns: Complete schedules.yaml contents
        """
        try:
            result = self.service_handler.svc_get_schedules("api", "pyheat", "get_schedules", request_data)
            
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
    
    async def api_get_rooms(self, request_data: Dict[str, Any]) -> tuple:
        """API endpoint: GET/POST /api/appdaemon/pyheat_get_rooms
        
        Gets current rooms configuration.
        
        Request body: {} (empty)
        Returns: Complete rooms.yaml contents
        """
        return self._handle_request(self.service_handler.svc_get_rooms, request_data)
    
    async def api_replace_schedules(self, request_data: Dict[str, Any]) -> tuple:
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
        return self._handle_request(self.service_handler.svc_replace_schedules, request_data)
