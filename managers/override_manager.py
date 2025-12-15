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
        """Set active mode temperature override for a room.

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

        # Set override mode to active
        mode_entity = C.HELPER_ROOM_OVERRIDE_MODE.format(room=room_id)
        if self.ad.entity_exists(mode_entity):
            self.ad.call_service("input_select/select_option",
                               entity_id=mode_entity,
                               option=C.OVERRIDE_MODE_ACTIVE)
        else:
            self.ad.log(f"Override mode entity {mode_entity} does not exist", level="WARNING")
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

        self.ad.log(f"Active override set: room={room_id}, target={target:.1f}C, duration={duration_seconds}s")
        return True
    
    def cancel_override(self, room_id: str) -> bool:
        """Cancel active override for a room (both active and passive).

        Args:
            room_id: Room identifier

        Returns:
            True if override was cancelled successfully, False otherwise
        """
        # Validate room
        if room_id not in self.config.rooms:
            self.ad.log(f"Cannot cancel override: room '{room_id}' not found", level="ERROR")
            return False

        # Check override mode before canceling (for CSV logging)
        override_mode = self.get_override_mode(room_id)

        # Set override mode to none
        mode_entity = C.HELPER_ROOM_OVERRIDE_MODE.format(room=room_id)
        if self.ad.entity_exists(mode_entity):
            self.ad.call_service("input_select/select_option",
                               entity_id=mode_entity,
                               option=C.OVERRIDE_MODE_NONE)

        # Cancel timer
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            self.ad.call_service("timer/cancel", entity_id=timer_entity)
            self.ad.log(f"Override cancelled: room={room_id}")

            # Log CSV event for passive overrides
            if override_mode == C.OVERRIDE_MODE_PASSIVE:
                if hasattr(self.ad, 'queue_csv_event'):
                    self.ad.queue_csv_event(f"passive_override_ended_{room_id}")

            return True
        else:
            self.ad.log(f"Override timer entity {timer_entity} does not exist", level="WARNING")
            return False
    
    def handle_timer_expired(self, room_id: str) -> None:
        """Handle override timer expiration cleanup.

        Called when timer transitions from active/paused to idle.
        Clears the override target to sentinel value and resets mode.

        Args:
            room_id: Room identifier
        """
        # Check override mode before expiring (for CSV logging)
        override_mode = self.get_override_mode(room_id)

        # Set override mode to none
        mode_entity = C.HELPER_ROOM_OVERRIDE_MODE.format(room=room_id)
        if self.ad.entity_exists(mode_entity):
            self.ad.call_service("input_select/select_option",
                               entity_id=mode_entity,
                               option=C.OVERRIDE_MODE_NONE)

        target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
        if self.ad.entity_exists(target_entity):
            # Set to sentinel value (entity min is 5, so 0 indicates cleared)
            self.ad.call_service("input_number/set_value",
                               entity_id=target_entity,
                               value=0)
            self.ad.log(f"Override expired: room={room_id}, target cleared")

            # Log CSV event for passive overrides
            if override_mode == C.OVERRIDE_MODE_PASSIVE:
                if hasattr(self.ad, 'queue_csv_event'):
                    self.ad.queue_csv_event(f"passive_override_ended_{room_id}")
        else:
            self.ad.log(f"Override target entity {target_entity} does not exist", level="WARNING")

    def get_override_mode(self, room_id: str) -> str:
        """Get the current override mode for a room.

        CRITICAL: Timer state is the source of truth. If timer is inactive,
        mode is always "none" regardless of input_select value. This prevents
        race conditions and ensures consistent state.

        Args:
            room_id: Room identifier

        Returns:
            "active", "passive", or "none"
        """
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)

        # Timer state is source of truth - check first
        if not self.ad.entity_exists(timer_entity):
            return C.OVERRIDE_MODE_NONE

        timer_state = self.ad.get_state(timer_entity)
        if timer_state not in ["active", "paused"]:
            return C.OVERRIDE_MODE_NONE

        # Timer is active - read mode from input_select
        mode_entity = C.HELPER_ROOM_OVERRIDE_MODE.format(room=room_id)
        if self.ad.entity_exists(mode_entity):
            mode = self.ad.get_state(mode_entity)
            if mode in [C.OVERRIDE_MODE_ACTIVE, C.OVERRIDE_MODE_PASSIVE]:
                return mode

        # Timer active but invalid/missing mode - default to active for backward compatibility
        self.ad.log(f"Timer active for {room_id} but mode entity invalid - defaulting to active", level="WARNING")
        return C.OVERRIDE_MODE_ACTIVE

    def set_passive_override(self, room_id: str, min_temp: float, max_temp: float,
                            valve_percent: float, duration_seconds: int) -> bool:
        """Set passive mode temperature override for a room.

        Args:
            room_id: Room identifier
            min_temp: Comfort floor temperature (8-20°C)
            max_temp: Upper limit temperature (10-30°C)
            valve_percent: Valve opening percentage (0-100%)
            duration_seconds: Override duration in seconds

        Returns:
            True if override was set successfully, False otherwise
        """
        # Validate room
        if room_id not in self.config.rooms:
            self.ad.log(f"Cannot set passive override: room '{room_id}' not found", level="ERROR")
            return False

        # Set override mode to passive
        mode_entity = C.HELPER_ROOM_OVERRIDE_MODE.format(room=room_id)
        if self.ad.entity_exists(mode_entity):
            self.ad.call_service("input_select/select_option",
                               entity_id=mode_entity,
                               option=C.OVERRIDE_MODE_PASSIVE)
        else:
            self.ad.log(f"Override mode entity {mode_entity} does not exist", level="WARNING")
            return False

        # Set passive override parameters
        min_temp_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_MIN_TEMP.format(room=room_id)
        max_temp_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_MAX_TEMP.format(room=room_id)
        valve_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_VALVE_PERCENT.format(room=room_id)

        if not all([self.ad.entity_exists(min_temp_entity),
                   self.ad.entity_exists(max_temp_entity),
                   self.ad.entity_exists(valve_entity)]):
            self.ad.log(f"One or more passive override entities do not exist for room {room_id}", level="WARNING")
            return False

        self.ad.call_service("input_number/set_value", entity_id=min_temp_entity, value=min_temp)
        self.ad.call_service("input_number/set_value", entity_id=max_temp_entity, value=max_temp)
        self.ad.call_service("input_number/set_value", entity_id=valve_entity, value=valve_percent)

        # Start override timer
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            self.ad.call_service("timer/start",
                               entity_id=timer_entity,
                               duration=str(duration_seconds))
        else:
            self.ad.log(f"Override timer entity {timer_entity} does not exist", level="WARNING")
            return False

        self.ad.log(f"Passive override set: room={room_id}, min={min_temp:.1f}C, max={max_temp:.1f}C, valve={valve_percent}%, duration={duration_seconds}s")

        # Log CSV event for observability
        if hasattr(self.ad, 'queue_csv_event'):
            self.ad.queue_csv_event(f"passive_override_started_{room_id}")

        return True

    def get_passive_override_params(self, room_id: str) -> Optional[dict]:
        """Get passive override parameters if a passive override is active.

        Args:
            room_id: Room identifier

        Returns:
            dict with keys: min_temp, max_temp, valve_percent
            None if no passive override is active
        """
        # Check if override mode is passive
        if self.get_override_mode(room_id) != C.OVERRIDE_MODE_PASSIVE:
            return None

        # Read passive override entities
        min_temp_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_MIN_TEMP.format(room=room_id)
        max_temp_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_MAX_TEMP.format(room=room_id)
        valve_entity = C.HELPER_ROOM_OVERRIDE_PASSIVE_VALVE_PERCENT.format(room=room_id)

        if not all([self.ad.entity_exists(min_temp_entity),
                   self.ad.entity_exists(max_temp_entity),
                   self.ad.entity_exists(valve_entity)]):
            return None

        try:
            min_temp = float(self.ad.get_state(min_temp_entity))
            max_temp = float(self.ad.get_state(max_temp_entity))
            valve_percent = float(self.ad.get_state(valve_entity))

            return {
                'min_temp': min_temp,
                'max_temp': max_temp,
                'valve_percent': valve_percent
            }
        except (ValueError, TypeError):
            self.ad.log(f"Invalid passive override parameters for room '{room_id}'", level="WARNING")
            return None
