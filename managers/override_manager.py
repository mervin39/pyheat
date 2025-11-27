# -*- coding: utf-8 -*-
"""
override_manager.py - Centralized override management

Responsibilities:
- Check if override is active for a room
- Get override target temperature
- Set override (target and timer)
- Cancel override
- Handle timer expiration cleanup
- Encapsulate all timer entity knowledge
"""

from datetime import datetime
from typing import Optional
import constants as C


class OverrideManager:
    """Manages temperature override state and timer entities.
    
    This class centralizes all override-related logic, encapsulating knowledge
    of timer entities and override target entities. It provides a clean interface
    for other components without exposing implementation details.
    """
    
    def __init__(self, ad, config):
        """Initialize the override manager.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
    
    def is_override_active(self, room_id: str) -> bool:
        """Check if an override is currently active for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            True if override timer is active or paused
        """
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if not self.ad.entity_exists(timer_entity):
            return False
        
        timer_state = self.ad.get_state(timer_entity)
        return timer_state in ["active", "paused"]
    
    def get_override_target(self, room_id: str) -> Optional[float]:
        """Get override target temperature if active.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Override target temperature in °C, or None if no active override
        """
        if not self.is_override_active(room_id):
            return None
        
        target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
        if not self.ad.entity_exists(target_entity):
            return None
        
        try:
            override_target = float(self.ad.get_state(target_entity))
            # Sentinel value 0 means cleared (entity min is 5)
            if override_target >= C.TARGET_MIN_C:
                return override_target
        except (ValueError, TypeError):
            self.ad.log(f"Invalid override target for room '{room_id}'", level="WARNING")
        
        return None
    
    def set_override(self, room_id: str, target: float, duration_seconds: int) -> bool:
        """Set temperature override for a room.
        
        Args:
            room_id: Room identifier
            target: Target temperature in °C
            duration_seconds: Override duration in seconds
            
        Returns:
            True if override was set successfully, False otherwise
        """
        # Validate room
        if room_id not in self.config.rooms:
            self.ad.log(f"Cannot set override: room '{room_id}' not found", level="ERROR")
            return False
        
        # Set override target
        override_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
        if self.ad.entity_exists(override_entity):
            self.ad.call_service("input_number/set_value", 
                               entity_id=override_entity, 
                               value=target)
        else:
            self.ad.log(f"Override target entity {override_entity} does not exist", level="WARNING")
            return False
        
        # Start override timer
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            self.ad.call_service("timer/start", 
                               entity_id=timer_entity, 
                               duration=str(duration_seconds))
        else:
            self.ad.log(f"Override timer entity {timer_entity} does not exist", level="WARNING")
            return False
        
        self.ad.log(f"Override set: room={room_id}, target={target:.1f}C, duration={duration_seconds}s")
        return True
    
    def cancel_override(self, room_id: str) -> bool:
        """Cancel active override for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            True if override was cancelled successfully, False otherwise
        """
        # Validate room
        if room_id not in self.config.rooms:
            self.ad.log(f"Cannot cancel override: room '{room_id}' not found", level="ERROR")
            return False
        
        # Cancel timer
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            self.ad.call_service("timer/cancel", entity_id=timer_entity)
            self.ad.log(f"Override cancelled: room={room_id}")
            return True
        else:
            self.ad.log(f"Override timer entity {timer_entity} does not exist", level="WARNING")
            return False
    
    def handle_timer_expired(self, room_id: str) -> None:
        """Handle override timer expiration cleanup.
        
        Called when timer transitions from active/paused to idle.
        Clears the override target to sentinel value.
        
        Args:
            room_id: Room identifier
        """
        target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
        if self.ad.entity_exists(target_entity):
            # Set to sentinel value (entity min is 5, so 0 indicates cleared)
            self.ad.call_service("input_number/set_value",
                               entity_id=target_entity, 
                               value=0)
            self.ad.log(f"Override expired: room={room_id}, target cleared")
        else:
            self.ad.log(f"Override target entity {target_entity} does not exist", level="WARNING")
