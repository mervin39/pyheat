# -*- coding: utf-8 -*-
"""
boiler_controller.py - Boiler state machine and control

Responsibilities:
- Manage boiler on/off state machine
- Anti-cycling protection (min on/off times)
- TRV interlock checking
- Pump overrun management
"""

from datetime import datetime
from typing import List, Dict, Optional, Tuple
import pyheat.constants as C


class BoilerController:
    """Manages boiler state machine and safety interlocks."""
    
    def __init__(self, ad, config):
        """Initialize the boiler controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        self.boiler_state = C.STATE_OFF
        self.boiler_state_entry_time = None
        self.boiler_last_on = None
        self.boiler_last_off = None
        self.boiler_last_valve_positions = {}  # For pump overrun
        
    def update_state(self, any_calling: bool, active_rooms: List[str], 
                    room_data: Dict, now: datetime) -> Tuple[str, str]:
        """Update boiler state based on room demands.
        
        Simplified implementation - full state machine pending.
        
        Args:
            any_calling: Whether any room is calling for heat
            active_rooms: List of room IDs that are calling
            room_data: Dict of room states {room_id: room_dict}
            now: Current datetime
            
        Returns:
            Tuple of (boiler_state, boiler_reason)
        """
        # Simplified logic: turn on if any room calling, off otherwise
        # Full state machine with interlocks and pump overrun to be implemented
        
        if any_calling:
            # Should be on
            if self.boiler_state == C.STATE_OFF:
                self.boiler_last_on = now
                self.boiler_state = C.STATE_ON
                self.boiler_state_entry_time = now
                self._set_boiler_on()
                reason = f"Heating {len(active_rooms)} room(s): {', '.join(active_rooms)}"
            else:
                reason = f"Heating {len(active_rooms)} room(s): {', '.join(active_rooms)}"
        else:
            # Should be off
            if self.boiler_state == C.STATE_ON:
                self.boiler_last_off = now
                self.boiler_state = C.STATE_OFF
                self.boiler_state_entry_time = now
                self._set_boiler_off()
                reason = "No rooms calling for heat"
            else:
                reason = "Idle"
        
        return self.boiler_state, reason
    
    def _set_boiler_on(self) -> None:
        """Turn boiler on."""
        setpoint = self.config.boiler_config.get('binary_on_setpoint', C.BOILER_BINARY_ON_SETPOINT_DEFAULT)
        self._set_boiler_setpoint(setpoint)
        self.ad.log(f"Boiler ON (setpoint={setpoint}°C)")
    
    def _set_boiler_off(self) -> None:
        """Turn boiler off."""
        setpoint = self.config.boiler_config.get('binary_off_setpoint', C.BOILER_BINARY_OFF_SETPOINT_DEFAULT)
        self._set_boiler_setpoint(setpoint)
        self.ad.log(f"Boiler OFF (setpoint={setpoint}°C)")
    
    def _set_boiler_setpoint(self, setpoint: float) -> None:
        """Set boiler climate entity setpoint.
        
        Args:
            setpoint: Target temperature in °C
        """
        boiler_entity = self.config.boiler_config.get('entity_id')
        if not boiler_entity:
            self.ad.log("No boiler entity configured", level="ERROR")
            return
        
        try:
            self.ad.call_service('climate/set_temperature',
                            entity_id=boiler_entity,
                            temperature=setpoint)
        except Exception as e:
            self.ad.log(f"Failed to set boiler setpoint: {e}", level="ERROR")
