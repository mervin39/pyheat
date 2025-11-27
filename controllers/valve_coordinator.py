# -*- coding: utf-8 -*-
"""
valve_coordinator.py - Central authority for valve command decisions

Responsibilities:
- Apply persistence overrides from boiler (pump overrun, interlock)
- Apply load sharing overrides from load sharing manager
- Apply corrections from TRV feedback
- Coordinate with TRV controller to send actual commands
- Manage priority: safety > load_sharing > corrections > normal
"""

from datetime import datetime
from typing import Dict, Optional
import json
import constants as C


class ValveCoordinator:
    """Central authority for final valve command decisions.
    
    This class acts as the single point of authority for determining what
    valve commands should be sent to TRVs, considering all overrides and
    priorities:
    
    Priority Order (highest to lowest):
    1. Persistence overrides (boiler safety: pump overrun, interlock)
    2. Load sharing overrides (intelligent load balancing)
    3. Correction overrides (unexpected TRV positions)
    4. Normal desired values (from room heating logic)
    """
    
    def __init__(self, ad, trv_controller):
        """Initialize the valve coordinator.
        
        Args:
            ad: AppDaemon API reference
            trv_controller: TRVController instance for sending actual commands
        """
        self.ad = ad
        self.trvs = trv_controller
        
        # Persistence overrides from boiler controller (DEPRECATED - kept for compatibility)
        self.persistence_overrides = {}  # {room_id: valve_percent}
        self.persistence_reason = None
        self.persistence_active = False
        
        # Pump overrun persistence (NEW - managed by valve coordinator)
        self.pump_overrun_active = False
        self.pump_overrun_snapshot = {}  # {room_id: valve_pct}
        
        # Track current commanded positions (for all rooms)
        self.current_commands = {}  # {room_id: valve_pct}
        
        # Load sharing overrides from load sharing manager
        self.load_sharing_overrides = {}  # {room_id: valve_percent}
        
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
    
    def set_load_sharing_overrides(self, overrides: Dict[str, int]) -> None:
        """Set load sharing overrides from load sharing manager.
        
        Called by app.py after evaluating load sharing needs.
        
        Args:
            overrides: Dict mapping room_id -> valve_percent for load sharing rooms
        """
        self.load_sharing_overrides = overrides.copy() if overrides else {}
        
        if self.load_sharing_overrides:
            rooms_str = ', '.join(f"{rid}={pct}%" for rid, pct in overrides.items())
            self.ad.log(
                f"Load sharing overrides ACTIVE: [{rooms_str}]",
                level="DEBUG"
            )
    
    def clear_load_sharing_overrides(self) -> None:
        """Clear load sharing overrides.
        
        Called when load sharing deactivates.
        """
        if self.load_sharing_overrides:
            self.ad.log("Load sharing overrides CLEARED", level="DEBUG")
        
        self.load_sharing_overrides = {}
    
    # ========================================================================
    # Pump Overrun Persistence (NEW - replaces boiler controller logic)
    # ========================================================================
    
    def _read_persistence_entity(self) -> Dict:
        """Read room persistence entity.
        
        Returns:
            Dict in format: {room_id: [valve_pct, calling_state], ...}
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            return {}
        
        data_str = self.ad.get_state(C.HELPER_ROOM_PERSISTENCE)
        if not data_str or data_str in ['unknown', 'unavailable', '']:
            return {}
        
        try:
            return json.loads(data_str)
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}
    
    def _write_valve_positions_to_entity(self, positions: Dict[str, int]) -> None:
        """Write valve positions to persistence entity (index 0).
        
        Preserves calling state (index 1) from room controller.
        
        Args:
            positions: Dict of {room_id: valve_pct}
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            return
        
        try:
            # Read current data to preserve calling states
            data = self._read_persistence_entity()
            
            # Update valve positions (index 0), preserve calling state (index 1)
            for room_id, valve_pct in positions.items():
                if room_id not in data:
                    data[room_id] = [0, 0]
                data[room_id][0] = int(valve_pct)
                # Keep index 1 (calling state) unchanged
            
            # Write back
            self.ad.call_service("input_text/set_value",
                entity_id=C.HELPER_ROOM_PERSISTENCE,
                value=json.dumps(data, separators=(',', ':'))
            )
            self.ad.log(f"ValveCoordinator: Wrote pump overrun positions to entity: {positions}", level="DEBUG")
        except Exception as e:
            self.ad.log(f"ValveCoordinator: Failed to write valve positions: {e}", level="WARNING")
    
    def _clear_valve_positions_in_entity(self) -> None:
        """Clear valve positions in persistence entity (set index 0 to 0).
        
        Preserves calling state (index 1).
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            return
        
        try:
            data = self._read_persistence_entity()
            
            # Clear all valve positions (set index 0 to 0)
            for room_id in data.keys():
                data[room_id][0] = 0
                # Keep index 1 unchanged
            
            self.ad.call_service("input_text/set_value",
                entity_id=C.HELPER_ROOM_PERSISTENCE,
                value=json.dumps(data, separators=(',', ':'))
            )
            self.ad.log("ValveCoordinator: Cleared pump overrun positions in entity", level="DEBUG")
        except Exception as e:
            self.ad.log(f"ValveCoordinator: Failed to clear valve positions: {e}", level="WARNING")
    
    def initialize_from_ha(self) -> None:
        """Initialize valve coordinator state from Home Assistant.
        
        Restores pump overrun state if AppDaemon restarted during pump overrun period.
        """
        # Read persistence entity
        data = self._read_persistence_entity()
        
        # Check if any valves are persisted (index 0 > 0)
        persisted_positions = {
            room_id: room_data[0]
            for room_id, room_data in data.items()
            if len(room_data) >= 2 and room_data[0] > 0
        }
        
        if persisted_positions:
            # We were in pump overrun when AppDaemon restarted
            self.pump_overrun_active = True
            self.pump_overrun_snapshot = persisted_positions
            self.ad.log(
                f"ValveCoordinator: Restored pump overrun state from entity: {persisted_positions}",
                level="INFO"
            )
        else:
            # Normal initialization
            self.pump_overrun_active = False
            self.pump_overrun_snapshot = {}
            self.ad.log("ValveCoordinator: Initialized (no pump overrun active)", level="DEBUG")
    
    def enable_pump_overrun_persistence(self) -> None:
        """Enable pump overrun persistence.
        
        Snapshots current commanded valve positions and persists to HA entity.
        These positions will be held during pump overrun period.
        """
        # Take snapshot of current commanded positions
        self.pump_overrun_snapshot = self.current_commands.copy()
        self.pump_overrun_active = True
        
        # Persist to HA entity for restart resilience
        self._write_valve_positions_to_entity(self.pump_overrun_snapshot)
        
        self.ad.log(
            f"ValveCoordinator: Pump overrun enabled, persisting: {self.pump_overrun_snapshot}",
            level="INFO"
        )
    
    def disable_pump_overrun_persistence(self) -> None:
        """Disable pump overrun persistence.
        
        Clears snapshot and persistence entity, allowing valves to return to normal control.
        """
        self.pump_overrun_active = False
        self.pump_overrun_snapshot = {}
        
        # Clear persistence entity
        self._clear_valve_positions_in_entity()
        
        self.ad.log("ValveCoordinator: Pump overrun disabled", level="INFO")
    
    def get_total_valve_opening(self) -> int:
        """Get total valve opening from current commanded positions.
        
        Returns:
            Sum of all valve percentages currently commanded
        """
        return sum(self.current_commands.values())
    
    def get_persisted_valves(self) -> Dict[str, int]:
        """Get current pump overrun persisted valves.
        
        Returns:
            Dict of {room_id: valve_pct} for persisted valves, or empty dict if not active
        """
        if self.pump_overrun_active:
            return self.pump_overrun_snapshot.copy()
        return {}
    
    def apply_valve_command(self, room_id: str, desired_percent: int, 
                           now: datetime) -> int:
        """Apply final valve command with all overrides considered.
        
        This method determines the final valve position to command based on:
        1. Legacy persistence overrides (compatibility - deprecated)
        2. Pump overrun persistence (NEW - safety during cooling)
        3. Load sharing overrides (intelligent load balancing)
        4. Correction overrides (unexpected positions)
        5. Normal desired value (default)
        
        Args:
            room_id: Room identifier
            desired_percent: Desired valve percentage from room heating logic
            now: Current datetime
            
        Returns:
            Final valve percentage that was commanded
        """
        final_percent = desired_percent
        reason = "normal"
        
        # Priority 1: Legacy persistence overrides (compatibility - deprecated)
        if room_id in self.persistence_overrides:
            final_percent = self.persistence_overrides[room_id]
            reason = f"persistence: {self.persistence_reason}"
        
        # Priority 2: Pump overrun persistence (NEW)
        elif self.pump_overrun_active and room_id in self.pump_overrun_snapshot:
            # Use snapshot position, BUT allow new demand to override
            # If room is calling for MORE than snapshot, use that (new demand during pump overrun)
            snapshot_valve = self.pump_overrun_snapshot[room_id]
            if desired_percent > snapshot_valve:
                # New demand wants more - allow it and update snapshot
                final_percent = desired_percent
                self.pump_overrun_snapshot[room_id] = desired_percent
                reason = "pump_overrun_updated"
                self.ad.log(
                    f"ValveCoordinator: Pump overrun - room '{room_id}' new demand {desired_percent}% > snapshot {snapshot_valve}%, updating",
                    level="INFO"
                )
            else:
                # Hold snapshot position
                final_percent = snapshot_valve
                reason = "pump_overrun"
        
        # Priority 3: Load sharing overrides
        elif room_id in self.load_sharing_overrides:
            final_percent = self.load_sharing_overrides[room_id]
            reason = "load_sharing"
        
        # Priority 4: Correction overrides
        elif room_id in self.trvs.unexpected_valve_positions:
            final_percent = self.trvs.unexpected_valve_positions[room_id]['expected']
            reason = "correction"
        
        # Track commanded position (for pump overrun snapshot)
        self.current_commands[room_id] = final_percent
        
        # Send the command to TRV controller
        # Pass persistence_active flag so TRV controller can skip feedback checks
        # (Both legacy persistence and pump overrun count as persistence)
        is_correction = (reason == "correction")
        persistence_for_trv = self.persistence_active or self.pump_overrun_active
        self.trvs.set_valve(
            room_id, 
            final_percent, 
            now, 
            is_correction=is_correction,
            persistence_active=persistence_for_trv
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
