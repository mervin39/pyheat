# -*- coding: utf-8 -*-
"""
load_sharing_manager.py - Load sharing manager for PyHeat

Responsibilities:
- Evaluate load sharing needs based on capacity and cycling risk
- Select rooms using three-tier cascading strategy
- Manage state transitions and exit conditions
- Provide valve commands for load sharing rooms
- Track activation context and timing

Phase 1: Entry conditions and Tier 1 selection implemented
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
        
        # Track rooms that need explicit closure on deactivation (Bug #6 fix)
        self.last_deactivated_rooms = []
        
        # Configuration (loaded from boiler.yaml)
        self.min_calling_capacity_w = 3500  # Activation threshold
        self.target_capacity_w = 4000       # Target capacity to reach
        self.min_activation_duration_s = 300  # 5 minutes minimum
        self.tier3_timeout_s = 900          # 15 minutes max for Tier 3
        self.high_return_delta_c = 15       # Return temp delta for cycling risk detection
        
        # Master enable switch (HA helper)
        self.master_enable_entity = C.HELPER_LOAD_SHARING_ENABLE
        
    def initialize_from_ha(self) -> None:
        """Load configuration and initial state from Home Assistant.
        
        Load sharing is controlled solely by input_boolean.pyheat_load_sharing_enable.
        No config file enable flag - just load thresholds and parameters.
        """
        # Load load_sharing config from boiler.yaml (thresholds and parameters only)
        ls_config = self.config.boiler_config.get('load_sharing', {})
        
        # Required configuration - no defaults
        if 'high_return_delta_c' not in ls_config:
            raise ValueError(
                "Missing required config: load_sharing.high_return_delta_c must be defined in boiler.yaml. "
                "This sets the return temperature delta threshold for cycling risk detection. "
                "Example: 15 means load sharing activates when return temp is within 15°C of setpoint."
            )
        
        self.min_calling_capacity_w = ls_config.get('min_calling_capacity_w', 3500)
        self.target_capacity_w = ls_config.get('target_capacity_w', 4000)
        self.min_activation_duration_s = ls_config.get('min_activation_duration_s', 300)
        self.tier3_timeout_s = ls_config.get('tier3_timeout_s', 900)
        self.high_return_delta_c = ls_config['high_return_delta_c']
        
        # Check master enable switch (single source of truth)
        master_enabled = self._is_master_enabled()
        
        if not master_enabled:
            self.context.state = LoadSharingState.DISABLED
            self.ad.log(
                f"LoadSharingManager: DISABLED (master switch off)",
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
            self.ad.log(
                f"LoadSharingManager: Master enable check - entity={self.master_enable_entity}, state={state}",
                level="DEBUG"
            )
            return state == 'on'
        except Exception as e:
            self.ad.log(
                f"LoadSharingManager: Failed to read master enable: {e}",
                level="WARNING"
            )
            return False
    
    def evaluate(self, room_states: Dict[str, Dict], boiler_state: str, cycling_protection_state: str) -> Dict[str, int]:
        """Evaluate load sharing needs and return valve commands.
        
        Phase 1: Implements entry conditions and Tier 1 selection.
        
        Args:
            room_states: Dict of room states from room_controller
                         {room_id: {temp, target, calling, valve_percent, error, mode}}
            boiler_state: Current boiler state machine state
            cycling_protection_state: Current cycling protection state (NORMAL, COOLDOWN, TIMEOUT)
            
        Returns:
            Dict of valve commands for load sharing rooms {room_id: valve_pct}
        """
        now = datetime.now()
        self.context.last_evaluation = now
        
        # Phase 1: Always disabled, no evaluation
        if self.context.state == LoadSharingState.DISABLED:
            return {}
        
        # Check master enable (in case it was toggled)
        if not self._is_master_enabled():
            if self.context.is_active():
                self._deactivate("master enable toggled off")
            return {}
        
        # If currently inactive, check entry conditions
        if not self.context.is_active():
            if self._evaluate_entry_conditions(room_states, cycling_protection_state):
                # Entry conditions met - start with Tier 1 selection
                tier1_selections = self._select_tier1_rooms(room_states, now)
                
                if tier1_selections:
                    # Activate load sharing with Tier 1
                    self._activate_tier1(room_states, tier1_selections, now)
                    
                    # Check if Tier 1 capacity is sufficient
                    total_capacity = self._calculate_total_system_capacity(room_states)
                    
                    if total_capacity >= self.target_capacity_w:
                        self.ad.log(
                            f"Load sharing: Tier 1 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                            level="INFO"
                        )
                    else:
                        # Tier 1 insufficient - escalate to 80%
                        self.ad.log(
                            f"Load sharing: Tier 1 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                            level="INFO"
                        )
                        self._escalate_tier1_rooms()
                        
                        # Recalculate with escalated Tier 1
                        total_capacity = self._calculate_total_system_capacity(room_states)
                        
                        if total_capacity >= self.target_capacity_w:
                            self.ad.log(
                                f"Load sharing: Tier 1 escalated sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                level="INFO"
                            )
                        else:
                            # Still insufficient - try Tier 2
                            self.ad.log(
                                f"Load sharing: Tier 1 escalated insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - trying Tier 2",
                                level="INFO"
                            )
                            tier2_selections = self._select_tier2_rooms(room_states, now)
                            
                            if tier2_selections:
                                self._activate_tier2(tier2_selections, now)
                                
                                # Recalculate with Tier 2 added
                                total_capacity = self._calculate_total_system_capacity(room_states)
                                
                                if total_capacity >= self.target_capacity_w:
                                    self.ad.log(
                                        f"Load sharing: Tier 2 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                        level="INFO"
                                    )
                                else:
                                    # Escalate Tier 2
                                    self.ad.log(
                                        f"Load sharing: Tier 2 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                        level="INFO"
                                    )
                                    self._escalate_tier2_rooms()
                                    
                                    # Recalculate with escalated Tier 2
                                    total_capacity = self._calculate_total_system_capacity(room_states)
                                    
                                    if total_capacity >= self.target_capacity_w:
                                        self.ad.log(
                                            f"Load sharing: Tier 2 escalated sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                            level="INFO"
                                        )
                                    else:
                                        # Still insufficient - try Tier 3 (fallback priority)
                                        self.ad.log(
                                            f"Load sharing: Tier 2 escalated insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - trying Tier 3",
                                            level="INFO"
                                        )
                                        tier3_selections = self._select_tier3_rooms(room_states)
                                        
                                        if tier3_selections:
                                            self._activate_tier3(tier3_selections, now)
                                            
                                            # Recalculate with Tier 3 added
                                            total_capacity = self._calculate_total_system_capacity(room_states)
                                            
                                            if total_capacity >= self.target_capacity_w:
                                                self.ad.log(
                                                    f"Load sharing: Tier 3 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                                    level="INFO"
                                                )
                                            else:
                                                # Escalate Tier 3 rooms progressively (50% → 60% → 70% → ... → 100%)
                                                # Or add next priority room if all maxed out
                                                self.ad.log(
                                                    f"Load sharing: Tier 3 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                                    level="INFO"
                                                )
                                                
                                                # Keep escalating until we reach target or exhaust all options
                                                while total_capacity < self.target_capacity_w:
                                                    escalated = self._escalate_tier3_rooms(room_states)
                                                    if not escalated:
                                                        # No more escalation possible
                                                        break
                                                    
                                                    # Recalculate capacity after escalation
                                                    total_capacity = self._calculate_total_system_capacity(room_states)
                                                
                                                # Final status
                                                if total_capacity >= self.target_capacity_w:
                                                    self.ad.log(
                                                        f"Load sharing: Tier 3 escalated sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                                        level="INFO"
                                                    )
                                                else:
                                                    self.ad.log(
                                                        f"Load sharing: All tiers exhausted ({total_capacity:.0f}W < {self.target_capacity_w}W) - "
                                                        f"accepting cycling as lesser evil",
                                                        level="INFO"
                                                    )
                                        else:
                                            self.ad.log(
                                                f"Load sharing: No Tier 3 rooms available - all tiers exhausted",
                                                level="INFO"
                                            )
                            else:
                                # No Tier 2 rooms - try Tier 3 directly
                                self.ad.log(
                                    f"Load sharing: No Tier 2 rooms available - trying Tier 3",
                                    level="INFO"
                                )
                                tier3_selections = self._select_tier3_rooms(room_states)
                                
                                if tier3_selections:
                                    self._activate_tier3(tier3_selections, now)
                                    
                                    # Recalculate with Tier 3 added
                                    total_capacity = self._calculate_total_system_capacity(room_states)
                                    
                                    if total_capacity >= self.target_capacity_w:
                                        self.ad.log(
                                            f"Load sharing: Tier 3 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                            level="INFO"
                                        )
                                    else:
                                        # Escalate Tier 3
                                        self.ad.log(
                                            f"Load sharing: Tier 3 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                            level="INFO"
                                        )
                                        self._escalate_tier3_rooms()
                                        
                                        # Final capacity check
                                        total_capacity = self._calculate_total_system_capacity(room_states)
                                        self.ad.log(
                                            f"Load sharing: Tier 3 escalated - final capacity {total_capacity:.0f}W",
                                            level="INFO"
                                        )
                                else:
                                    self.ad.log(
                                        f"Load sharing: No Tier 3 rooms available - all tiers exhausted",
                                        level="INFO"
                                    )
                    
                    # Return valve commands for activated rooms
                    return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
                else:
                    # Tier 1 empty - initialize trigger context and try Tier 2
                    self.ad.log("Load sharing: No Tier 1 rooms available - trying Tier 2", level="INFO")
                    self._initialize_trigger_context(room_states, now)
                    tier2_selections = self._select_tier2_rooms(room_states, now)
                    
                    if tier2_selections:
                        self._activate_tier2(tier2_selections, now)
                        
                        # Recalculate with Tier 2 added
                        total_capacity = self._calculate_total_system_capacity(room_states)
                        
                        if total_capacity >= self.target_capacity_w:
                            self.ad.log(
                                f"Load sharing: Tier 2 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                level="INFO"
                            )
                        else:
                            # Escalate Tier 2
                            self.ad.log(
                                f"Load sharing: Tier 2 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                level="INFO"
                            )
                            self._escalate_tier2_rooms()
                            
                            # Recalculate with escalated Tier 2
                            total_capacity = self._calculate_total_system_capacity(room_states)
                            
                            if total_capacity >= self.target_capacity_w:
                                self.ad.log(
                                    f"Load sharing: Tier 2 escalated sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                    level="INFO"
                                )
                            else:
                                # Still insufficient - try Tier 3
                                self.ad.log(
                                    f"Load sharing: Tier 2 escalated insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - trying Tier 3",
                                    level="INFO"
                                )
                                tier3_selections = self._select_tier3_rooms(room_states)
                                
                                if tier3_selections:
                                    self._activate_tier3(tier3_selections, now)
                                    
                                    # Recalculate with Tier 3 added
                                    total_capacity = self._calculate_total_system_capacity(room_states)
                                    
                                    if total_capacity >= self.target_capacity_w:
                                        self.ad.log(
                                            f"Load sharing: Tier 3 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                            level="INFO"
                                        )
                                    else:
                                        # Escalate Tier 3
                                        self.ad.log(
                                            f"Load sharing: Tier 3 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                            level="INFO"
                                        )
                                        self._escalate_tier3_rooms()
                                        
                                        # Final capacity check
                                        total_capacity = self._calculate_total_system_capacity(room_states)
                                        self.ad.log(
                                            f"Load sharing: Tier 3 escalated - final capacity {total_capacity:.0f}W",
                                            level="INFO"
                                        )
                                else:
                                    self.ad.log(
                                        f"Load sharing: No Tier 3 rooms available - all tiers exhausted",
                                        level="INFO"
                                    )
                        
                        # Return valve commands for activated rooms
                        return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
                    else:
                        # No Tier 2 rooms - try Tier 3 directly
                        self.ad.log(
                            f"Load sharing: No Tier 2 rooms available - trying Tier 3",
                            level="INFO"
                        )
                        tier3_selections = self._select_tier3_rooms(room_states)
                        
                        if tier3_selections:
                            self._activate_tier3(tier3_selections, now)
                            
                            # Recalculate with Tier 3 added
                            total_capacity = self._calculate_total_system_capacity(room_states)
                            
                            if total_capacity >= self.target_capacity_w:
                                self.ad.log(
                                    f"Load sharing: Tier 3 sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                                    level="INFO"
                                )
                            else:
                                # Escalate Tier 3 progressively (maximize existing before adding new)
                                self.ad.log(
                                    f"Load sharing: Tier 3 insufficient ({total_capacity:.0f}W < {self.target_capacity_w}W) - escalating",
                                    level="INFO"
                                )
                                
                                # Keep escalating until we reach target or exhaust all options
                                while total_capacity < self.target_capacity_w:
                                    escalated = self._escalate_tier3_rooms(room_states)
                                    if not escalated:
                                        break
                                    total_capacity = self._calculate_total_system_capacity(room_states)
                                
                                self.ad.log(
                                    f"Load sharing: Tier 3 escalated - final capacity {total_capacity:.0f}W",
                                    level="INFO"
                                )
                            
                            # Return valve commands for activated rooms
                            return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
                        else:
                            self.ad.log(
                                f"Load sharing: No rooms available in any tier - accepting cycling as lesser evil",
                                level="INFO"
                            )
            
            return {}
        
        # If currently active, check exit conditions
        if self._evaluate_exit_conditions(room_states, now):
            self._deactivate("exit conditions met")
            return {}
        
        # Load sharing is active - return current valve commands
        return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
    
    def _deactivate(self, reason: str) -> None:
        """Deactivate load sharing and reset context.
        
        Tracks which rooms were opened by load sharing for explicit closure.
        This prevents Bug #6 (valve persistence after deactivation).
        
        Args:
            reason: Human-readable reason for deactivation
        """
        # Track rooms that need explicit closure (Bug #6 fix)
        self.last_deactivated_rooms = list(self.context.active_rooms.keys())
        
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
            'master_enabled': self._is_master_enabled(),
            'decision_explanation': self._build_decision_explanation(),
            'decision_details': self._build_decision_details()
        }
    
    def _build_decision_explanation(self) -> str:
        """Build concise human-readable explanation of load sharing decision.
        
        Returns single-line summary suitable for general display (80-120 chars).
        
        Returns:
            Human-readable explanation string
        """
        if self.context.state == LoadSharingState.DISABLED:
            return "Load sharing disabled (master switch off)"
        
        if not self.context.is_active():
            return "Load sharing inactive (sufficient capacity or no cycling risk)"
        
        # Active state - explain the activation
        trigger_rooms = ", ".join(sorted(self.context.trigger_calling_rooms))
        num_trigger = len(self.context.trigger_calling_rooms)
        
        # Build tier summary
        tier_counts = {}
        for room in self.context.active_rooms.values():
            tier_counts[room.tier] = tier_counts.get(room.tier, 0) + 1
        
        tier_summary = []
        for tier in sorted(tier_counts.keys()):
            count = tier_counts[tier]
            tier_name = {1: "schedule-aware", 2: "extended", 3: "fallback"}[tier]
            tier_summary.append(f"{count} {tier_name}")
        
        tier_str = ", ".join(tier_summary)
        
        return (
            f"Active: {num_trigger} room(s) calling ({trigger_rooms}) "
            f"with {self.context.trigger_capacity:.0f}W < {self.min_calling_capacity_w}W threshold. "
            f"Added {tier_str} room(s) to reach {self.target_capacity_w}W target."
        )
    
    def _build_decision_details(self) -> Dict:
        """Build detailed structured breakdown of load sharing decision.
        
        Provides comprehensive data for debugging and detailed display.
        
        Returns:
            Dict with activation_reason, room_selections, capacity_status
        """
        if self.context.state == LoadSharingState.DISABLED:
            return {
                'status': 'disabled',
                'reason': 'Master switch (input_boolean.pyheat_load_sharing_enable) is off'
            }
        
        if not self.context.is_active():
            return {
                'status': 'inactive',
                'reason': 'Load sharing not needed (sufficient capacity or no cycling risk)'
            }
        
        # Active state - provide detailed breakdown
        now = datetime.now()
        
        # Activation reason details
        activation_reason = {
            'type': 'low_capacity_with_cycling_risk',
            'trigger_rooms': sorted(self.context.trigger_calling_rooms),
            'trigger_capacity_w': round(self.context.trigger_capacity, 0),
            'capacity_threshold_w': self.min_calling_capacity_w,
            'activated_at': self.context.trigger_timestamp.isoformat() if self.context.trigger_timestamp else None,
            'duration_s': round(self.context.activation_duration(now), 0) if self.context.trigger_timestamp else 0
        }
        
        # Room selection details
        room_selections = []
        for room in sorted(self.context.active_rooms.values(), key=lambda r: (r.tier, r.room_id)):
            tier_names = {
                1: 'Schedule-aware pre-warming',
                2: 'Extended lookahead',
                3: 'Fallback priority list'
            }
            
            duration_s = (now - room.activated_at).total_seconds()
            
            room_selections.append({
                'room_id': room.room_id,
                'tier': room.tier,
                'tier_name': tier_names.get(room.tier, f'Tier {room.tier}'),
                'selection_reason': room.reason,
                'valve_pct': room.valve_pct,
                'activated_at': room.activated_at.isoformat(),
                'duration_s': round(duration_s, 0)
            })
        
        # Capacity status
        # Note: Total capacity calculation would require room_states, so we provide counts
        capacity_status = {
            'target_capacity_w': self.target_capacity_w,
            'active_room_count': len(self.context.active_rooms),
            'tier_breakdown': {
                'tier1_count': len(self.context.tier1_rooms),
                'tier2_count': len(self.context.tier2_rooms),
                'tier3_count': len(self.context.tier3_rooms)
            }
        }
        
        return {
            'status': 'active',
            'state': self.context.state.value,
            'activation_reason': activation_reason,
            'room_selections': room_selections,
            'capacity_status': capacity_status
        }
    
    # ========================================================================
    # Phase 1+ Methods (Stubs)
    # ========================================================================
    
    def _evaluate_entry_conditions(self, room_states: Dict, cycling_protection_state: str) -> bool:
        """Check if load sharing should activate.
        
        Entry conditions (ALL must be true):
        1. Total calling capacity < min_calling_capacity_w (default 3500W)
        2. Cycling risk present (cooldown active OR high return temp)
        
        Args:
            room_states: Room state dict from room_controller
            cycling_protection_state: Current cycling protection state
            
        Returns:
            True if load sharing should activate
        """
        # Get calling rooms
        calling_rooms = [rid for rid, state in room_states.items() if state.get('calling', False)]
        
        if not calling_rooms:
            # No rooms calling - no need for load sharing
            return False
        
        # Calculate total calling capacity
        all_capacities = self.load_calculator.get_all_estimated_capacities()
        total_capacity = 0.0
        for room_id in calling_rooms:
            capacity = all_capacities.get(room_id)
            if capacity is not None:
                total_capacity += capacity
        
        # Check capacity threshold
        if total_capacity >= self.min_calling_capacity_w:
            # Sufficient capacity - no need for load sharing
            return False
        
        # Check cycling risk
        # Option 1: Cycling protection is active (COOLDOWN state)
        if cycling_protection_state == 'COOLDOWN':
            self.ad.log(
                f"Load sharing entry: Low capacity ({total_capacity:.0f}W < {self.min_calling_capacity_w}W) + "
                f"cycling protection active",
                level="INFO"
            )
            return True
        
        # Option 2: High return temperature risk (check return temp delta)
        try:
            return_temp = self.ad.get_state(C.OPENTHERM_HEATING_RETURN_TEMP)
            setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
            
            if return_temp not in ['unknown', 'unavailable', None] and \
               setpoint not in ['unknown', 'unavailable', None]:
                return_temp = float(return_temp)
                setpoint = float(setpoint)
                
                # High risk if return temp is within configured delta of setpoint
                threshold = setpoint - self.high_return_delta_c
                if return_temp >= threshold:
                    self.ad.log(
                        f"Load sharing entry: Low capacity ({total_capacity:.0f}W < {self.min_calling_capacity_w}W) + "
                        f"high return temp ({return_temp:.1f}C >= {threshold:.1f}C threshold, setpoint {setpoint:.1f}C)",
                        level="INFO"
                    )
                    return True
        except (ValueError, TypeError) as e:
            self.ad.log(f"Failed to check return temp for load sharing: {e}", level="DEBUG")
        
        # Capacity is low but no cycling risk detected - don't activate yet
        self.ad.log(
            f"Load sharing: Low capacity ({total_capacity:.0f}W) but no cycling risk - monitoring",
            level="DEBUG"
        )
        return False
    
    def _select_tier1_rooms(self, room_states: Dict, now: datetime) -> List[Tuple[str, int, str]]:
        """Select Tier 1 (schedule-aware) rooms.
        
        Selection criteria:
        - Room in "auto" mode
        - Not currently calling for heat
        - Has schedule block within lookahead window
        - Schedule target > current temperature
        
        Sorted by: (scheduled_target - current_temp) DESC (neediest first)
        
        Args:
            room_states: Room state dict from room_controller
            now: Current datetime
            
        Returns:
            List of (room_id, valve_pct, reason) tuples
        """
        candidates = []
        
        for room_id, state in room_states.items():
            # Skip if not in auto mode
            if state.get('mode') != 'auto':
                continue
            
            # Skip if already calling
            if state.get('calling', False):
                continue
            
            # Get room config for lookahead window
            room_cfg = self.config.rooms.get(room_id, {})
            load_sharing_cfg = room_cfg.get('load_sharing', {})
            lookahead_m = load_sharing_cfg.get('schedule_lookahead_m', C.LOAD_SHARING_SCHEDULE_LOOKAHEAD_M_DEFAULT)
            
            # Check for schedule block within lookahead window
            next_block = self.scheduler.get_next_schedule_block(room_id, now, within_minutes=lookahead_m)
            
            if next_block is None:
                # No schedule block within window
                continue
            
            start_time, end_time, target_temp = next_block
            current_temp = state.get('temp')
            
            if current_temp is None:
                # No temperature data - skip
                continue
            
            # Only pre-warm if schedule target is higher than current temp
            if target_temp <= current_temp:
                continue
            
            # Calculate need (temperature deficit)
            need = target_temp - current_temp
            
            # Calculate time until schedule
            minutes_until = (start_time - now).total_seconds() / 60
            
            # Determine reason string
            reason = f"schedule_{int(minutes_until)}m"
            
            candidates.append((room_id, need, target_temp, minutes_until, reason))
        
        # Sort by need (descending) - neediest rooms first
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Return as list of (room_id, valve_pct, reason, target_temp)
        # Initial valve opening: 70% for Tier 1 (C.LOAD_SHARING_TIER1_INITIAL_PCT)
        selections = []
        for room_id, need, target, minutes, reason in candidates:
            selections.append((room_id, C.LOAD_SHARING_TIER1_INITIAL_PCT, reason, target))
            self.ad.log(
                f"Load sharing Tier 1 candidate: {room_id} - need={need:.1f}C, target={target:.1f}C, "
                f"minutes_until={minutes:.0f}, valve={C.LOAD_SHARING_TIER1_INITIAL_PCT}%",
                level="DEBUG"
            )
        
        return selections
    
    def _select_tier2_rooms(self, room_states: Dict, now: datetime) -> List[Tuple[str, int, str]]:
        """Select Tier 2 (extended lookahead) rooms.
        
        Phase 2: Extended window selection (2× schedule_lookahead_m).
        Same criteria as Tier 1 but with wider time window.
        
        Selection criteria:
        - Room in "auto" mode
        - Not currently calling for heat
        - Not already in Tier 1
        - Has schedule block within 2× lookahead window
        - Schedule target > current temperature
        
        Sorted by: (scheduled_target - current_temp) DESC (neediest first)
        
        Args:
            room_states: Room state dict from room_controller
            now: Current datetime
            
        Returns:
            List of (room_id, valve_pct, reason) tuples
        """
        candidates = []
        tier1_room_ids = set(self.context.active_rooms.keys())
        
        for room_id, state in room_states.items():
            # Skip if not in auto mode
            if state.get('mode') != 'auto':
                continue
            
            # Skip if already calling
            if state.get('calling', False):
                continue
            
            # Skip if already in Tier 1
            if room_id in tier1_room_ids:
                continue
            
            # Get room config for lookahead window (2× the configured window)
            room_cfg = self.config.rooms.get(room_id, {})
            load_sharing_cfg = room_cfg.get('load_sharing', {})
            base_lookahead_m = load_sharing_cfg.get('schedule_lookahead_m', C.LOAD_SHARING_SCHEDULE_LOOKAHEAD_M_DEFAULT)
            extended_lookahead_m = base_lookahead_m * 2
            
            # Check for schedule block within extended window
            next_block = self.scheduler.get_next_schedule_block(room_id, now, within_minutes=extended_lookahead_m)
            
            if next_block is None:
                # No schedule block within extended window
                continue
            
            start_time, end_time, target_temp = next_block
            current_temp = state.get('temp')
            
            if current_temp is None:
                # No temperature data - skip
                continue
            
            # Only pre-warm if schedule target is higher than current temp
            if target_temp <= current_temp:
                continue
            
            # Calculate need (temperature deficit)
            need = target_temp - current_temp
            
            # Calculate time until schedule
            minutes_until = (start_time - now).total_seconds() / 60
            
            # Determine reason string
            reason = f"schedule_{int(minutes_until)}m_ext"
            
            candidates.append((room_id, need, target_temp, minutes_until, reason))
        
        # Sort by need (descending) - neediest rooms first
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Return as list of (room_id, valve_pct, reason, target_temp)
        # Initial valve opening: 40% for Tier 2 (gentle pre-warming for extended window)
        selections = []
        for room_id, need, target, minutes, reason in candidates:
            selections.append((room_id, C.LOAD_SHARING_TIER2_INITIAL_PCT, reason, target))
            self.ad.log(
                f"Load sharing Tier 2 candidate: {room_id} - need={need:.1f}C, target={target:.1f}C, "
                f"minutes_until={minutes:.0f}, valve={C.LOAD_SHARING_TIER2_INITIAL_PCT}%",
                level="DEBUG"
            )
        
        return selections
    
    def _select_tier3_rooms(self, room_states: Dict) -> List[Tuple[str, int, str]]:
        """Select Tier 3 (fallback priority) rooms.
        
        Phase 3: Priority list fallback - deterministic selection when schedules don't help.
        
        STRATEGY: Maximize existing rooms before adding new ones.
        - Add ONE room at a time in priority order
        - Room will be escalated (50% → 60% → 70% → 80% → 90% → 100%) before next room is added
        - This minimizes the number of rooms heated (energy efficiency)
        
        Selection criteria:
        - Room in "auto" mode (respects user intent)
        - Not currently calling for heat
        - Not already in Tier 1 or Tier 2
        - Has fallback_priority configured (rooms without this are excluded)
        - NO temperature check - ultimate fallback accepts any auto mode room
        
        Sorted by: fallback_priority ASC (lower number = higher priority)
        
        Args:
            room_states: Room state dict from room_controller
            
        Returns:
            List of (room_id, valve_pct, reason) tuples (returns ONE room, will be escalated later)
        """
        candidates = []
        active_room_ids = set(self.context.active_rooms.keys())
        
        for room_id, state in room_states.items():
            # Skip if not in auto mode
            if state.get('mode') != 'auto':
                continue
            
            # Skip if already calling
            if state.get('calling', False):
                continue
            
            # Skip if already in Tier 1 or Tier 2
            if room_id in active_room_ids:
                continue
            
            # Get room config for fallback priority
            room_cfg = self.config.rooms.get(room_id, {})
            load_sharing_cfg = room_cfg.get('load_sharing', {})
            fallback_priority = load_sharing_cfg.get('fallback_priority')
            
            # Skip if no fallback_priority configured (explicit exclusion)
            if fallback_priority is None:
                continue
            
            # Add to candidates
            reason = f"fallback_p{fallback_priority}"
            candidates.append((room_id, fallback_priority, reason))
            
            self.ad.log(
                f"Load sharing Tier 3 candidate: {room_id} - priority={fallback_priority}",
                level="DEBUG"
            )
        
        # Sort by priority (ascending - lower number = higher priority)
        candidates.sort(key=lambda x: x[1])
        
        # Return ONLY the highest priority room (will be escalated before adding more)
        # Initial valve opening: 50% for Tier 3 (compromise between flow and energy)
        if candidates:
            room_id, priority, reason = candidates[0]
            all_capacities = self.load_calculator.get_all_estimated_capacities()
            room_capacity = all_capacities.get(room_id)
            
            if room_capacity is None:
                self.ad.log(
                    f"Load sharing Tier 3: Skipping {room_id} - no capacity estimate",
                    level="DEBUG"
                )
                return []
            
            # Get comfort target for Tier 3 pre-warming
            # Uses global comfort target (default 20C) to bypass low parking temperatures
            ls_config = self.config.boiler_config.get('load_sharing', {})
            tier3_target = ls_config.get('tier3_comfort_target_c', 20.0)
            
            valve_pct = C.LOAD_SHARING_TIER3_INITIAL_PCT
            effective_room_capacity = room_capacity * (valve_pct / 100.0)
            current_capacity = self._calculate_total_system_capacity(room_states)
            new_total_capacity = current_capacity + effective_room_capacity
            
            self.ad.log(
                f"Load sharing Tier 3 selection: {room_id} - priority={priority}, "
                f"valve={valve_pct}%, target={tier3_target:.1f}C, adds {effective_room_capacity:.0f}W "
                f"(total: {new_total_capacity:.0f}W)",
                level="DEBUG"
            )
            
            return [(room_id, valve_pct, reason, tier3_target)]
        
        return []
    
    def _initialize_trigger_context(self, room_states: Dict, now: datetime) -> None:
        """Initialize the trigger context for load sharing activation.
        
        This must be called before any tier activation (_activate_tier1/2/3).
        Records which rooms triggered load sharing and their capacity.
        
        Args:
            room_states: Room state dict
            now: Current datetime
        """
        calling_rooms = [rid for rid, state in room_states.items() if state.get('calling', False)]
        self.context.trigger_calling_rooms = set(calling_rooms)
        
        # Calculate trigger capacity
        all_capacities = self.load_calculator.get_all_estimated_capacities()
        trigger_capacity = 0.0
        for room_id in calling_rooms:
            capacity = all_capacities.get(room_id)
            if capacity is not None:
                trigger_capacity += capacity
        
        self.context.trigger_capacity = trigger_capacity
        self.context.trigger_timestamp = now
    
    def _activate_tier1(self, room_states: Dict, tier1_selections: List[Tuple[str, int, str]], now: datetime) -> None:
        """Activate load sharing with Tier 1 rooms.
        
        Args:
            room_states: Room state dict
            tier1_selections: List of (room_id, valve_pct, reason) tuples
            now: Current datetime
        """
        # Initialize trigger context (calling rooms and capacity)
        self._initialize_trigger_context(room_states, now)
        
        # Activate selected rooms
        for room_id, valve_pct, reason, target_temp in tier1_selections:
            activation = RoomActivation(
                room_id=room_id,
                tier=1,
                valve_pct=valve_pct,
                activated_at=now,
                reason=reason,
                target_temp=target_temp
            )
            self.context.active_rooms[room_id] = activation
        
        # Update state
        self.context.state = LoadSharingState.TIER1_ACTIVE
        
        # Log activation
        calling_rooms = [rid for rid, state in room_states.items() if state.get('calling', False)]
        trigger_capacity = self.context.trigger_capacity
        room_list = ', '.join([f"{rid}={vpct}%" for rid, vpct, _, _ in tier1_selections])
        self.ad.log(
            f"Load sharing ACTIVATED (Tier 1): {len(tier1_selections)} room(s) [{room_list}] | "
            f"Trigger: {len(calling_rooms)} room(s) at {trigger_capacity:.0f}W",
            level="INFO"
        )
    
    def _activate_tier2(self, tier2_selections: List[Tuple[str, int, str]], now: datetime) -> None:
        """Activate Tier 2 rooms (add to existing Tier 1).
        
        Args:
            tier2_selections: List of (room_id, valve_pct, reason) tuples
            now: Current datetime
        """
        # Activate selected Tier 2 rooms
        for room_id, valve_pct, reason, target_temp in tier2_selections:
            activation = RoomActivation(
                room_id=room_id,
                tier=2,
                valve_pct=valve_pct,
                activated_at=now,
                reason=reason,
                target_temp=target_temp
            )
            self.context.active_rooms[room_id] = activation
        
        # Update state
        self.context.state = LoadSharingState.TIER2_ACTIVE
        
        # Log activation
        room_list = ', '.join([f"{rid}={vpct}%" for rid, vpct, _, _ in tier2_selections])
        self.ad.log(
            f"Load sharing: Added {len(tier2_selections)} Tier 2 room(s) [{room_list}]",
            level="INFO"
        )
    
    def _evaluate_exit_conditions(self, room_states: Dict, now: datetime) -> bool:
        """Check if load sharing should deactivate.
        
        Exit conditions (any triggers exit):
        A. Original calling room(s) stopped (none still calling)
        B. Additional room(s) started calling (recalculate capacity) - BYPASSES minimum duration
        C. Load sharing room now naturally calling (remove from load sharing)
        D. Tier 3 rooms exceeded timeout (15 minutes max for fallback rooms)
        E. Room reached/exceeded target temperature (NEW - prevents overshoot)
        F. Room mode changed from auto (NEW - respects user mode changes)
        
        Minimum activation duration enforced (5 minutes default) for all triggers EXCEPT B.
        Exit Trigger B bypasses minimum duration because it solves the fundamental problem
        (insufficient capacity) that load sharing was activated for.
        
        Args:
            room_states: Room state dict
            now: Current datetime
            
        Returns:
            True if load sharing should deactivate
        """
        # Get current calling rooms (needed for Exit Trigger B)
        current_calling = set([rid for rid, state in room_states.items() if state.get('calling', False)])
        
        # Exit Trigger B: Additional rooms started calling - CHECK FIRST, BYPASSES MINIMUM DURATION
        # This represents the fundamental problem being solved by new naturally-calling rooms
        new_calling = current_calling - self.context.trigger_calling_rooms
        if new_calling:
            # Calculate new total capacity
            all_capacities = self.load_calculator.get_all_estimated_capacities()
            new_total_capacity = 0.0
            for room_id in current_calling:
                capacity = all_capacities.get(room_id)
                if capacity is not None:
                    new_total_capacity += capacity
            
            if new_total_capacity >= self.target_capacity_w:
                self.ad.log(
                    f"Load sharing exit: Additional rooms calling ({list(new_calling)}), "
                    f"capacity now sufficient ({new_total_capacity:.0f}W >= {self.target_capacity_w}W) - "
                    f"bypassing minimum duration",
                    level="INFO"
                )
                return True
            else:
                # Capacity still insufficient - update trigger set and continue
                self.ad.log(
                    f"Load sharing: Additional rooms calling ({list(new_calling)}), "
                    f"but capacity still insufficient ({new_total_capacity:.0f}W < {self.target_capacity_w}W) - "
                    f"updating trigger set",
                    level="INFO"
                )
                self.context.trigger_calling_rooms = current_calling
        
        # For ALL OTHER exit triggers, enforce minimum activation duration
        if not self.context.can_exit(now, self.min_activation_duration_s):
            return False
        
        # Exit Trigger D: Check Tier 3 timeouts FIRST (before other conditions)
        # Remove Tier 3 rooms that have exceeded their timeout
        tier3_rooms_to_remove = []
        for room_id, activation in list(self.context.active_rooms.items()):
            if activation.tier == 3:
                duration = (now - activation.activated_at).total_seconds()
                if duration >= self.tier3_timeout_s:
                    self.ad.log(
                        f"Load sharing: Tier 3 room '{room_id}' exceeded timeout "
                        f"({duration:.0f}s >= {self.tier3_timeout_s}s) - removing",
                        level="INFO"
                    )
                    tier3_rooms_to_remove.append(room_id)
        
        # Remove timed-out Tier 3 rooms
        for room_id in tier3_rooms_to_remove:
            del self.context.active_rooms[room_id]
        
        # If only Tier 3 rooms were active and all timed out, deactivate
        if not self.context.active_rooms:
            self.ad.log("Load sharing exit: All Tier 3 rooms timed out", level="INFO")
            return True
        
        # Exit Trigger F: Room mode changed from auto (NEW)
        # Remove rooms that are no longer in auto mode
        mode_changed_rooms = []
        for room_id, activation in list(self.context.active_rooms.items()):
            state = room_states.get(room_id, {})
            if state.get('mode') != 'auto':
                self.ad.log(
                    f"Load sharing: Room '{room_id}' mode changed from auto - removing",
                    level="INFO"
                )
                mode_changed_rooms.append(room_id)
        
        # Remove rooms with mode changes
        for room_id in mode_changed_rooms:
            del self.context.active_rooms[room_id]
        
        # Check if any rooms remain after mode change removals
        if not self.context.active_rooms:
            self.ad.log("Load sharing exit: No load sharing rooms remain after mode changes", level="INFO")
            return True
        
        # Exit Trigger E: Room reached/exceeded target temperature (NEW - prevents overshoot)
        # Remove rooms that have reached their pre-warming target
        temp_reached_rooms = []
        for room_id, activation in list(self.context.active_rooms.items()):
            state = room_states.get(room_id, {})
            temp = state.get('temp')
            
            # Only check if we have valid temperature data
            if temp is None:
                continue
            
            # Get hysteresis off_delta to prevent oscillation (same as normal control)
            room_cfg = self.config.rooms.get(room_id, {})
            off_delta = room_cfg.get('hysteresis', {}).get('off_delta_c', 0.3)
            
            # Check if room reached/exceeded the target it was pre-warming for
            # Use target + off_delta to match normal hysteresis behavior
            if temp >= activation.target_temp + off_delta:
                self.ad.log(
                    f"Load sharing: Room '{room_id}' exceeded target "
                    f"({temp:.1f}C >= {activation.target_temp + off_delta:.1f}C, target={activation.target_temp:.1f}C) - removing",
                    level="INFO"
                )
                temp_reached_rooms.append(room_id)
        
        # Remove rooms that reached target
        for room_id in temp_reached_rooms:
            del self.context.active_rooms[room_id]
        
        # Check if any rooms remain after temperature-based removals
        if not self.context.active_rooms:
            self.ad.log("Load sharing exit: No load sharing rooms remain after temperature exits", level="INFO")
            return True
        
        # Exit Trigger A: Original calling rooms stopped
        # (current_calling already calculated at top of function for Exit Trigger B)
        trigger_still_calling = self.context.trigger_calling_rooms & current_calling
        if not trigger_still_calling:
            self.ad.log(
                f"Load sharing exit: Original calling rooms stopped (trigger={list(self.context.trigger_calling_rooms)})",
                level="INFO"
            )
            return True
        
        # Exit Trigger B was already checked at the top of this function (bypasses minimum duration)
        # If we reach here, either no new rooms joined OR they didn't provide sufficient capacity
        
        # Exit Trigger C: Load sharing room now naturally calling
        rooms_to_remove = []
        for room_id in list(self.context.active_rooms.keys()):
            if room_states.get(room_id, {}).get('calling', False):
                self.ad.log(
                    f"Load sharing: Room '{room_id}' now naturally calling - removing from load sharing control",
                    level="INFO"
                )
                rooms_to_remove.append(room_id)
        
        # Remove rooms that are now naturally calling
        for room_id in rooms_to_remove:
            del self.context.active_rooms[room_id]
        
        # If no load sharing rooms remain, deactivate
        if not self.context.active_rooms:
            self.ad.log("Load sharing exit: No load sharing rooms remain", level="INFO")
            return True
        
        return False
    
    def _calculate_total_system_capacity(self, room_states: Dict) -> float:
        """Calculate total system capacity including calling rooms and load sharing rooms.
        
        Includes:
        - All naturally calling rooms at their current capacity
        - All load sharing rooms at their effective capacity (valve adjusted)
        
        Args:
            room_states: Room state dict
            
        Returns:
            Total system capacity in watts
        """
        total = 0.0
        all_capacities = self.load_calculator.get_all_estimated_capacities()
        
        # Add calling rooms
        for room_id, state in room_states.items():
            if state.get('calling', False):
                capacity = all_capacities.get(room_id)
                if capacity is not None:
                    total += capacity
        
        # Add load sharing rooms (with valve adjustment)
        for room_id, activation in self.context.active_rooms.items():
            capacity = all_capacities.get(room_id)
            if capacity is not None:
                # Apply valve adjustment - rough estimate
                # valve_pct / 100 gives flow factor (e.g., 70% = 0.7)
                # Apply flow efficiency multiplier (assume ~1.0 for simplicity)
                effective_capacity = capacity * (activation.valve_pct / 100.0)
                total += effective_capacity
        
        return total
    
    def _escalate_tier1_rooms(self) -> None:
        """Escalate Tier 1 rooms from 70% to 80% valve opening.
        
        Called when Tier 1 rooms alone are insufficient to reach target capacity.
        Updates context state and room valve percentages.
        """
        for room_id, activation in self.context.active_rooms.items():
            if activation.tier == 1:
                activation.valve_pct = C.LOAD_SHARING_TIER1_ESCALATED_PCT
        
        self.context.state = LoadSharingState.TIER1_ESCALATED
        
        self.ad.log(
            f"Load sharing: Escalating {len(self.context.tier1_rooms)} Tier 1 rooms to "
            f"{C.LOAD_SHARING_TIER1_ESCALATED_PCT}%",
            level="INFO"
        )
    
    def _escalate_tier2_rooms(self) -> None:
        """Escalate Tier 2 rooms from 40% to 50% valve opening.
        
        Called when Tier 1 escalated + Tier 2 initial are insufficient.
        Updates context state and room valve percentages.
        """
        for room_id, activation in self.context.active_rooms.items():
            if activation.tier == 2:
                activation.valve_pct = C.LOAD_SHARING_TIER2_ESCALATED_PCT
        
        self.context.state = LoadSharingState.TIER2_ESCALATED
        
        self.ad.log(
            f"Load sharing: Escalating {len(self.context.tier2_rooms)} Tier 2 rooms to "
            f"{C.LOAD_SHARING_TIER2_ESCALATED_PCT}%",
            level="INFO"
        )
    
    def _activate_tier3(self, tier3_selections: List[Tuple[str, int, str]], now: datetime) -> None:
        """Activate Tier 3 fallback rooms (add to existing Tier 1+2).
        
        Tier 3 is the ultimate fallback - only activates when schedules don't help.
        Uses WARN level logging to indicate schedule gap that should be addressed.
        
        Args:
            tier3_selections: List of (room_id, valve_pct, reason) tuples
            now: Current datetime
        """
        # Activate selected Tier 3 rooms
        for room_id, valve_pct, reason, target_temp in tier3_selections:
            activation = RoomActivation(
                room_id=room_id,
                tier=3,
                valve_pct=valve_pct,
                activated_at=now,
                reason=reason,
                target_temp=target_temp
            )
            self.context.active_rooms[room_id] = activation
        
        # Update state
        self.context.state = LoadSharingState.TIER3_ACTIVE
        
        # Log activation with WARN level (indicates schedule gap)
        room_list = ', '.join([f"{rid}={vpct}%" for rid, vpct, _, _ in tier3_selections])
        self.ad.log(
            f"Load sharing: Added {len(tier3_selections)} Tier 3 fallback room(s) [{room_list}] - "
            f"WARNING: Tier 3 activated (indicates schedule gap - consider improving schedules)",
            level="WARNING"
        )
    
    def _escalate_tier3_rooms(self, room_states: Dict) -> bool:
        """Escalate Tier 3 rooms progressively or add next room if maxed out.
        
        MAXIMIZE EXISTING STRATEGY:
        - Escalate current rooms: 50% → 60% → 70% → 80% → 90% → 100%
        - Only add next priority room when all existing rooms are at 100%
        
        Called when all tiers are active but still insufficient capacity.
        Updates context state and room valve percentages.
        
        Args:
            room_states: Room state dict (needed for adding next room)
            
        Returns:
            True if rooms were escalated or new room added, False if all maxed out
        """
        tier3_rooms = self.context.tier3_rooms  # Property returns list of Tier 3 activations
        
        if not tier3_rooms:
            return False
        
        # Check if all existing Tier 3 rooms are at 100%
        all_maxed = all(room.valve_pct >= 100 for room in tier3_rooms)
        
        if all_maxed:
            # All existing rooms maxed out - try to add next priority room
            self.ad.log(
                f"Load sharing Tier 3: All {len(tier3_rooms)} room(s) at 100%, attempting to add next priority room",
                level="INFO"
            )
            
            # Select next priority room
            next_room = self._select_tier3_rooms(room_states)
            if next_room:
                room_id, valve_pct, reason = next_room[0]
                now = datetime.now()
                activation = RoomActivation(
                    room_id=room_id,
                    tier=3,
                    valve_pct=valve_pct,
                    activated_at=now,
                    reason=reason
                )
                self.context.active_rooms[room_id] = activation
                
                self.ad.log(
                    f"Load sharing Tier 3: Added next room '{room_id}' at {valve_pct}% (priority room after maxing existing)",
                    level="WARNING"
                )
                return True
            else:
                # No more rooms available
                self.ad.log(
                    "Load sharing Tier 3: All Tier 3 rooms maxed out and no more rooms available",
                    level="INFO"
                )
                return False
        else:
            # Escalate existing rooms by 10%
            escalated_count = 0
            for room in tier3_rooms:
                if room.valve_pct < 100:
                    old_pct = room.valve_pct
                    room.valve_pct = min(100, room.valve_pct + 10)
                    escalated_count += 1
                    self.ad.log(
                        f"Load sharing Tier 3: Escalating '{room.room_id}' from {old_pct}% to {room.valve_pct}%",
                        level="INFO"
                    )
            
            if escalated_count > 0:
                self.ad.log(
                    f"Load sharing Tier 3: Escalated {escalated_count} room(s) by 10%",
                    level="INFO"
                )
                return True
            
            return False
