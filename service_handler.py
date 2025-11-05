# -*- coding: utf-8 -*-
"""
service_handler.py - Home Assistant service handlers

Responsibilities:
- Register PyHeat services
- Handle service calls (override, boost, cancel, set_mode, reload, etc.)
- Validate service parameters
"""

from typing import Any, Dict, Callable
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
        
    def register_all(self, trigger_recompute_cb: Callable) -> None:
        """Register all PyHeat services.
        
        Args:
            trigger_recompute_cb: Callback to trigger system recompute
        """
        self.trigger_recompute_callback = trigger_recompute_cb
        
        # Register services (simplified - full implementation pending)
        self.ad.register_service("pyheat/reload_config", self.svc_reload_config)
        self.ad.log("Registered PyHeat services")
        
    def svc_reload_config(self, namespace, domain, service, kwargs):
        """Service handler: reload configuration."""
        self.ad.log("Service call: pyheat.reload_config")
        try:
            self.config.reload()
            if self.trigger_recompute_callback:
                self.trigger_recompute_callback("config_reloaded")
            return {"success": True, "message": "Configuration reloaded"}
        except Exception as e:
            self.ad.log(f"Failed to reload config: {e}", level="ERROR")
            return {"success": False, "message": str(e)}
