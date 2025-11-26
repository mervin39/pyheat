# -*- coding: utf-8 -*-
"""
load_sharing_manager.py - Load sharing manager for PyHeat

Responsibilities:
- Evaluate load sharing needs based on capacity and cycling risk
- Select rooms using three-tier cascading strategy
- Manage state transitions and exit conditions
- Provide valve commands for load sharing rooms
- Track activation context and timing

Phase 0: Skeleton implementation - always returns "no load sharing needed"
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from load_sharing_state import LoadSharingState, RoomActivation, LoadSharingContext
import constants as C


class LoadSharingManager:
    """Manages intelligent load sharing to reduce boiler short-cycling.
    
    Uses schedule-aware pre-warming as primary strategy, with extended
    lookahead and explicit fallback priorities for comprehensive coverage.
    
    Phase 0: Infrastructure only - no behavioral changes
    """
    
    def __init__(self, ad, config, scheduler, load_calculator, sensors):
        """Initialize the load sharing manager.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            scheduler: Scheduler instance for schedule lookahead
            load_calculator: LoadCalculator instance for capacity calculations
            sensors: SensorManager instance for current temperatures
        """
        self.ad = ad
        self.config = config
        self.scheduler = scheduler
        self.load_calculator = load_calculator
        self.sensors = sensors
        
        # State machine context
        self.context = LoadSharingContext()
        
        # Configuration (loaded from boiler.yaml)
        self.enabled = False
        self.min_calling_capacity_w = 3500  # Activation threshold
        self.target_capacity_w = 4000       # Target capacity to reach
        self.min_activation_duration_s = 300  # 5 minutes minimum
        self.tier3_timeout_s = 900          # 15 minutes max for Tier 3
        
        # Master enable switch (HA helper)
        self.master_enable_entity = C.HELPER_LOAD_SHARING_ENABLE
        
    def initialize_from_ha(self) -> None:
        """Load configuration and initial state from Home Assistant.
        
        Phase 0: Reads config but stays disabled.
        """
        # Load load_sharing config from boiler.yaml
        ls_config = self.config.boiler_config.get('load_sharing', {})
        self.enabled = ls_config.get('enabled', False)
        self.min_calling_capacity_w = ls_config.get('min_calling_capacity_w', 3500)
        self.target_capacity_w = ls_config.get('target_capacity_w', 4000)
        self.min_activation_duration_s = ls_config.get('min_activation_duration_s', 300)
        self.tier3_timeout_s = ls_config.get('tier3_timeout_s', 900)
        
        # Check master enable switch
        master_enabled = self._is_master_enabled()
        
        if not self.enabled or not master_enabled:
            self.context.state = LoadSharingState.DISABLED
            self.ad.log(
                f"LoadSharingManager: DISABLED (config={self.enabled}, master={master_enabled})",
                level="INFO"
            )
        else:
            self.context.state = LoadSharingState.INACTIVE
            self.ad.log(
                f"LoadSharingManager: Initialized (inactive) - "
                f"capacity threshold={self.min_calling_capacity_w}W, "
                f"target={self.target_capacity_w}W",
                level="INFO"
            )
    
    def _is_master_enabled(self) -> bool:
        """Check if master enable switch is ON.
        
        Returns:
            True if input_boolean.pyheat_load_sharing_enable is 'on'
        """
        try:
            state = self.ad.get_state(self.master_enable_entity)
            return state == 'on'
        except Exception as e:
            self.ad.log(
                f"LoadSharingManager: Failed to read master enable: {e}",
                level="WARNING"
            )
            return False
    
    def evaluate(self, room_states: Dict[str, Dict], boiler_state: str) -> Dict[str, int]:
        """Evaluate load sharing needs and return valve commands.
        
        Phase 0: Always returns empty dict (no load sharing active).
        
        Args:
            room_states: Dict of room states from room_controller
                         {room_id: {temp, target, calling, valve_percent, error, mode}}
            boiler_state: Current boiler state machine state
            
        Returns:
            Dict of valve commands for load sharing rooms {room_id: valve_pct}
            Phase 0: Always returns {}
        """
        # Phase 0: Always disabled, no evaluation
        if self.context.state == LoadSharingState.DISABLED:
            return {}
        
        # Check master enable (in case it was toggled)
        if not self._is_master_enabled():
            if self.context.is_active():
                self._deactivate("master enable toggled off")
            return {}
        
        # Phase 0: Even if enabled, return empty (no logic implemented yet)
        self.context.last_evaluation = datetime.now()
        return {}
    
    def _deactivate(self, reason: str) -> None:
        """Deactivate load sharing and reset context.
        
        Args:
            reason: Human-readable reason for deactivation
        """
        self.ad.log(
            f"LoadSharingManager: Deactivating - {reason}",
            level="INFO"
        )
        self.context.reset()
        self.context.state = LoadSharingState.INACTIVE
    
    def get_status(self) -> Dict:
        """Get current status for publishing to Home Assistant.
        
        Returns:
            Dict with state, active_rooms, reason, capacities, etc.
        """
        return {
            'state': self.context.state.value,
            'active_rooms': [
                {
                    'room_id': room.room_id,
                    'tier': room.tier,
                    'valve_pct': room.valve_pct,
                    'reason': room.reason,
                    'duration_s': (datetime.now() - room.activated_at).total_seconds()
                }
                for room in self.context.active_rooms.values()
            ],
            'trigger_capacity': self.context.trigger_capacity,
            'trigger_rooms': list(self.context.trigger_calling_rooms),
            'enabled': self.enabled,
            'master_enabled': self._is_master_enabled()
        }
    
    # ========================================================================
    # Phase 1+ Methods (Stubs)
    # ========================================================================
    
    def _evaluate_entry_conditions(self, room_states: Dict) -> bool:
        """Check if load sharing should activate.
        
        Phase 1: Implement capacity + cycling risk logic.
        Phase 0: Stub returns False.
        """
        return False
    
    def _select_tier1_rooms(self, room_states: Dict) -> List[Tuple[str, int, str]]:
        """Select Tier 1 (schedule-aware) rooms.
        
        Phase 1: Implement schedule lookahead logic.
        Phase 0: Stub returns empty list.
        
        Returns:
            List of (room_id, valve_pct, reason) tuples
        """
        return []
    
    def _select_tier2_rooms(self, room_states: Dict) -> List[Tuple[str, int, str]]:
        """Select Tier 2 (extended lookahead) rooms.
        
        Phase 2: Implement extended window logic.
        Phase 0: Stub returns empty list.
        
        Returns:
            List of (room_id, valve_pct, reason) tuples
        """
        return []
    
    def _select_tier3_rooms(self, room_states: Dict) -> List[Tuple[str, int, str]]:
        """Select Tier 3 (fallback priority) rooms.
        
        Phase 3: Implement priority list logic.
        Phase 0: Stub returns empty list.
        
        Returns:
            List of (room_id, valve_pct, reason) tuples
        """
        return []
    
    def _evaluate_exit_conditions(self, room_states: Dict) -> bool:
        """Check if load sharing should deactivate.
        
        Phase 1: Implement exit trigger logic.
        Phase 0: Stub returns False.
        """
        return False
