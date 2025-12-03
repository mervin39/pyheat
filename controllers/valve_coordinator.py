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
import constants as C
from persistence import PersistenceManager


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
        self.persistence = PersistenceManager(C.PERSISTENCE_FILE)
        
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
    # Pump Overrun Persistence
    # ========================================================================
    
    def _write_valve_positions_to_persistence(self, positions: Dict[str, int]) -> None:
        """Write valve positions to persistence file.
        
        Args:
            positions: Dict of {room_id: valve_pct}
        """
        try:
            data = self.persistence.load()
            
            # Ensure room_state exists
            if 'room_state' not in data:
                data['room_state'] = {}
            
            # Update valve positions for pump overrun
            for room_id, valve_pct in positions.items():
                if room_id not in data['room_state']:
                    data['room_state'][room_id] = {
                        'valve_percent': 0,
                        'last_calling': False,
                        'passive_valve': 0
                    }
                data['room_state'][room_id]['valve_percent'] = int(valve_pct)
            
            self.persistence.save(data)
            self.ad.log(f"ValveCoordinator: Wrote pump overrun positions: {positions}", level="DEBUG")
        except Exception as e:
            self.ad.log(f"ValveCoordinator: Failed to write valve positions: {e}", level="WARNING")
    
    def _clear_valve_positions_in_persistence(self) -> None:
        """Clear valve positions in persistence file."""
        try:
            data = self.persistence.load()
            
            # Clear all valve positions
            if 'room_state' in data:
                for room_id in data['room_state'].keys():
                    data['room_state'][room_id]['valve_percent'] = 0
            
            self.persistence.save(data)
            self.ad.log("ValveCoordinator: Cleared pump overrun positions", level="DEBUG")
        except Exception as e:
            self.ad.log(f"ValveCoordinator: Failed to clear valve positions: {e}", level="WARNING")
    
    def initialize_from_ha(self) -> None:
        """Initialize valve coordinator state from persistence file.
        
        Restores pump overrun state if AppDaemon restarted during pump overrun period.
        Only restores if the pump overrun timer is still active (not idle).
        """
        try:
            data = self.persistence.load()
            room_state = data.get('room_state', {})
            
            # Check if any valves are persisted (valve_percent > 0)
            persisted_positions = {
                room_id: room_data['valve_percent']
                for room_id, room_data in room_state.items()
                if room_data.get('valve_percent', 0) > 0
            }
            
            if persisted_positions:
                # Check if pump overrun timer is still active
                timer_state = self.ad.get_state(C.HELPER_PUMP_OVERRUN_TIMER)
                if timer_state == "active":
                    # Timer still running - restore pump overrun state
                    self.pump_overrun_active = True
                    self.pump_overrun_snapshot = persisted_positions
                    self.ad.log(
                        f"ValveCoordinator: Restored pump overrun state from persistence: {persisted_positions}",
                        level="INFO"
                    )
                else:
                    # Timer already finished - clear stale persistence and don't restore
                    self.ad.log(
                        f"ValveCoordinator: Pump overrun timer is {timer_state}, clearing stale persistence: {persisted_positions}",
                        level="INFO"
                    )
                    self._clear_valve_positions_in_persistence()
                    self.pump_overrun_active = False
                    self.pump_overrun_snapshot = {}
            else:
                # Normal initialization
                self.pump_overrun_active = False
                self.pump_overrun_snapshot = {}
                self.ad.log("ValveCoordinator: Initialized (no pump overrun active)", level="DEBUG")
        except Exception as e:
            self.ad.log(f"ValveCoordinator: Failed to restore from persistence: {e}", level="WARNING")
            # Normal initialization
            self.pump_overrun_active = False
            self.pump_overrun_snapshot = {}
            self.ad.log("ValveCoordinator: Initialized (no pump overrun active)", level="DEBUG")
    
    def enable_pump_overrun_persistence(self) -> None:
            self.pump_overrun_active = False
            self.pump_overrun_snapshot = {}
            self.ad.log("ValveCoordinator: Initialized (no pump overrun active)", level="DEBUG")
    
    def enable_pump_overrun_persistence(self) -> None:
        """Enable pump overrun persistence.
        
        Snapshots current commanded valve positions and persists to file.
        These positions will be held during pump overrun period.
        """
        # Take snapshot of current commanded positions
        self.pump_overrun_snapshot = self.current_commands.copy()
        self.pump_overrun_active = True
        
        # Persist to file for restart resilience
        self._write_valve_positions_to_persistence(self.pump_overrun_snapshot)
        
        self.ad.log(
            f"ValveCoordinator: Pump overrun enabled, persisting: {self.pump_overrun_snapshot}",
            level="INFO"
        )
    
    def disable_pump_overrun_persistence(self) -> None:
        """Disable pump overrun persistence.
        
        Clears snapshot and persistence file, allowing valves to return to normal control.
        """
        self.pump_overrun_active = False
        self.pump_overrun_snapshot = {}
        
        # Clear persistence file
        self._clear_valve_positions_in_persistence()
        
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
