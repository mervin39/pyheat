"""
boiler.py - Boiler control module

Simple boiler controller that turns the boiler on/off based on room demand.
Currently uses input_boolean.pyheat_boiler_actor as a dummy actor.

Future enhancements:
- Anti short-cycling (minimum on/off times)
- TRV-open interlock (verify at least one TRV is open)
- Force-off handling for safety
- Connection to real boiler hardware
"""

from typing import Dict, List, Any
from datetime import datetime


class BoilerManager:
    """Manages boiler on/off based on room heating demand.
    
    Current implementation is a simple dummy that:
    - Turns boiler ON if any room calls for heat
    - Turns boiler OFF if no rooms call for heat
    - Uses input_boolean.pyheat_boiler_actor as the control entity
    
    Future: Add anti-cycling, safety interlocks, real hardware control
    """
    
    def __init__(self):
        """Initialize boiler manager."""
        self.boiler_entity = "input_boolean.pyheat_boiler_actor"
        self.boiler_on = False
        self.last_change_time = None
        
        # Read current state
        current_state = state.get(self.boiler_entity)
        self.boiler_on = (current_state == "on")
        
        log.info(f"BoilerManager: initialized with entity {self.boiler_entity}")
        log.info(f"BoilerManager: current state = {'ON' if self.boiler_on else 'OFF'}")
    
    def update(self, rooms_calling_for_heat: List[str]) -> Dict[str, Any]:
        """Update boiler state based on room demand.
        
        Args:
            rooms_calling_for_heat: List of room IDs that are calling for heat
            
        Returns:
            Dict with boiler status info:
            {
                "boiler_on": bool,
                "rooms_calling": List[str],
                "changed": bool,
                "reason": str
            }
        """
        should_be_on = len(rooms_calling_for_heat) > 0
        changed = (should_be_on != self.boiler_on)
        
        if changed:
            # State change needed
            if should_be_on:
                # Turn ON
                log.info(f"BoilerManager: turning boiler ON (demand from {len(rooms_calling_for_heat)} room(s): {', '.join(rooms_calling_for_heat)})")
                state.set(self.boiler_entity, "on")
                self.boiler_on = True
                self.last_change_time = datetime.now()
                reason = f"Heat demand from {len(rooms_calling_for_heat)} room(s)"
            else:
                # Turn OFF
                log.info(f"BoilerManager: turning boiler OFF (no demand)")
                state.set(self.boiler_entity, "off")
                self.boiler_on = False
                self.last_change_time = datetime.now()
                reason = "No heat demand"
        else:
            # No change needed
            if should_be_on:
                reason = f"Already ON - demand from {len(rooms_calling_for_heat)} room(s)"
            else:
                reason = "Already OFF - no demand"
            log.debug(f"BoilerManager: {reason}")
        
        return {
            "boiler_on": self.boiler_on,
            "rooms_calling": rooms_calling_for_heat,
            "changed": changed,
            "reason": reason
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get current boiler status.
        
        Returns:
            Dict with current status:
            {
                "entity": str,
                "on": bool,
                "last_change": datetime or None
            }
        """
        return {
            "entity": self.boiler_entity,
            "on": self.boiler_on,
            "last_change": self.last_change_time
        }
    
    def reload_rooms(self, rooms_cfg: Dict) -> None:
        """Reload configuration (placeholder for consistency with other modules).
        
        Args:
            rooms_cfg: Room configuration dict (not currently used)
            
        Note:
            Boiler configuration is currently hardcoded.
            Future: Could load boiler entity, timing params from config.
        """
        log.info("BoilerManager: reload called (no configuration to reload currently)")
        
        # Re-read current state in case it was changed externally
        current_state = state.get(self.boiler_entity)
        self.boiler_on = (current_state == "on")
        log.debug(f"BoilerManager: re-read state = {'ON' if self.boiler_on else 'OFF'}")


# Module-level instance (initialized by orchestrator)
_boiler_mgr = None


def init():
    """Initialize the boiler manager module.
    
    Returns:
        BoilerManager: Initialized boiler manager instance
    """
    global _boiler_mgr
    
    log.info("BoilerManager: initializing...")
    _boiler_mgr = BoilerManager()
    log.info("BoilerManager: initialization complete")
    
    return _boiler_mgr


def update(rooms_calling_for_heat: List[str]) -> Dict[str, Any]:
    """Update boiler state based on room demand.
    
    Args:
        rooms_calling_for_heat: List of room IDs calling for heat
        
    Returns:
        Dict with boiler status info
    """
    if not _boiler_mgr:
        log.error("BoilerManager: update() called before init()")
        return {"boiler_on": False, "rooms_calling": [], "changed": False, "reason": "Not initialized"}
    
    return _boiler_mgr.update(rooms_calling_for_heat)


def get_status() -> Dict[str, Any]:
    """Get current boiler status.
    
    Returns:
        Dict with current status
    """
    if not _boiler_mgr:
        return {"entity": None, "on": False, "last_change": None}
    
    return _boiler_mgr.get_status()
