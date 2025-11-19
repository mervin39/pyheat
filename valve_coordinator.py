# -*- coding: utf-8 -*-
"""
valve_coordinator.py - Central authority for valve command decisions

Responsibilities:
- Apply persistence overrides from boiler (pump overrun, interlock)
- Apply corrections from TRV feedback
- Coordinate with TRV controller to send actual commands
- Manage priority: safety > corrections > normal
"""

from datetime import datetime
from typing import Dict, Optional
import pyheat.constants as C


class ValveCoordinator:
    """Central authority for final valve command decisions.
    
    This class acts as the single point of authority for determining what
    valve commands should be sent to TRVs, considering all overrides and
    priorities:
    
    Priority Order (highest to lowest):
    1. Persistence overrides (boiler safety: pump overrun, interlock)
    2. Correction overrides (unexpected TRV positions)
    3. Normal desired values (from room heating logic)
    """
    
    def __init__(self, ad, trv_controller):
        """Initialize the valve coordinator.
        
        Args:
            ad: AppDaemon API reference
            trv_controller: TRVController instance for sending actual commands
        """
        self.ad = ad
        self.trvs = trv_controller
        
        # Persistence overrides from boiler controller
        self.persistence_overrides = {}  # {room_id: valve_percent}
        self.persistence_reason = None
        self.persistence_active = False
        
    def set_persistence_overrides(self, overrides: Dict[str, int], reason: str) -> None:
        """Set persistence overrides from boiler controller.
        
        Called by boiler controller when valve persistence is needed for safety
        (e.g., pump overrun, interlock requirements).
        
        Args:
            overrides: Dict mapping room_id -> valve_percent to persist
            reason: Human-readable explanation (for logging)
        """
        self.persistence_overrides = overrides.copy() if overrides else {}
        self.persistence_reason = reason
        self.persistence_active = bool(overrides)
        
        if self.persistence_active:
            rooms_str = ', '.join(f"{rid}={pct}%" for rid, pct in overrides.items())
            self.ad.log(
                f"Valve persistence ACTIVE: {reason} [{rooms_str}]",
                level="INFO"
            )
        else:
            self.ad.log("Valve persistence CLEARED", level="DEBUG")
    
    def clear_persistence_overrides(self) -> None:
        """Clear persistence overrides.
        
        Called when boiler exits persistence states.
        """
        if self.persistence_active:
            self.ad.log("Valve persistence cleared", level="DEBUG")
        
        self.persistence_overrides = {}
        self.persistence_reason = None
        self.persistence_active = False
    
    def is_persistence_active(self) -> bool:
        """Check if valve persistence is currently active.
        
        Returns:
            True if persistence overrides are active
        """
        return self.persistence_active
    
    def apply_valve_command(self, room_id: str, desired_percent: int, 
                           now: datetime) -> int:
        """Apply final valve command with all overrides considered.
        
        This method determines the final valve position to command based on:
        1. Persistence overrides (highest priority)
        2. Correction overrides (unexpected positions)
        3. Normal desired value (default)
        
        Args:
            room_id: Room identifier
            desired_percent: Desired valve percentage from room heating logic
            now: Current datetime
            
        Returns:
            Final valve percentage that was commanded
        """
        final_percent = desired_percent
        reason = "normal"
        
        # Priority 1: Persistence overrides (safety)
        if room_id in self.persistence_overrides:
            final_percent = self.persistence_overrides[room_id]
            reason = f"persistence: {self.persistence_reason}"
        
        # Priority 2: Correction overrides (if no persistence)
        elif room_id in self.trvs.unexpected_valve_positions:
            final_percent = self.trvs.unexpected_valve_positions[room_id]['expected']
            reason = "correction"
        
        # Send the command to TRV controller
        # Pass persistence_active flag so TRV controller can skip feedback checks
        is_correction = (reason == "correction")
        self.trvs.set_valve(
            room_id, 
            final_percent, 
            now, 
            is_correction=is_correction,
            persistence_active=self.persistence_active
        )
        
        # Log decision
        if reason != "normal":
            self.ad.log(
                f"Room '{room_id}': valve={final_percent}% ({reason})",
                level="DEBUG"
            )
        
        return final_percent
    
    def apply_all_valve_commands(self, room_valve_data: Dict[str, int], 
                                now: datetime) -> Dict[str, int]:
        """Apply valve commands for all rooms.
        
        Convenience method for batch processing all rooms.
        
        Args:
            room_valve_data: Dict mapping room_id -> desired_valve_percent
            now: Current datetime
            
        Returns:
            Dict mapping room_id -> final_valve_percent (after overrides)
        """
        final_valves = {}
        
        for room_id, desired_percent in room_valve_data.items():
            final_percent = self.apply_valve_command(room_id, desired_percent, now)
            final_valves[room_id] = final_percent
        
        return final_valves
