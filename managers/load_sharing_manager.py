# -*- coding: utf-8 -*-
"""
load_sharing_manager.py - Load sharing manager for PyHeat

Responsibilities:
- Evaluate load sharing needs based on capacity and cycling risk
- Select rooms using two-tier cascading strategy (schedule + fallback)
- Manage state transitions and exit conditions
- Provide valve commands for load sharing rooms
- Track activation context and timing
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from load_sharing_state import LoadSharingState, RoomActivation, LoadSharingContext
import constants as C

# Tier identifiers for load sharing room selection
TIER_SCHEDULE = 1   # Schedule-aware pre-warming
TIER_FALLBACK = 2   # Fallback priority list


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
        
        # Configuration (loaded from boiler.yaml in initialize_from_ha)
        self.min_calling_capacity_w = None  # Activation threshold
        self.target_capacity_w = None       # Target capacity to reach
        self.min_activation_duration_s = None  # Minimum activation duration
        self.fallback_timeout_s = None      # Fallback tier timeout
        self.fallback_cooldown_s = None     # Fallback tier cooldown period
        self.high_return_delta_c = None     # Return temp delta for cycling risk detection
        
        # Control entities (HA helpers)
        self.mode_select_entity = C.HELPER_LOAD_SHARING_MODE
        
    def initialize_from_ha(self) -> None:
        """Load configuration and initial state from Home Assistant.
        
        Load sharing is controlled by:
        - input_select.pyheat_load_sharing_mode (Off/Conservative/Balanced/Aggressive)
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
        self.fallback_timeout_s = ls_config.get('fallback_timeout_s', 900)
        self.fallback_cooldown_s = ls_config.get('fallback_cooldown_s', 1800)
        self.high_return_delta_c = ls_config['high_return_delta_c']
        
        # Validate all required config loaded
        if None in [self.min_calling_capacity_w, self.target_capacity_w, 
                    self.min_activation_duration_s, self.fallback_timeout_s,
                    self.fallback_cooldown_s, self.high_return_delta_c]:
            raise ValueError(
                "LoadSharingManager: Configuration not properly initialized. "
                "Ensure initialize_from_ha() is called before evaluate()."
            )
        
        # Check mode
        mode = self._get_mode()
        
        if mode == C.LOAD_SHARING_MODE_OFF:
            self.context.state = LoadSharingState.DISABLED
            self.ad.log(
                f"LoadSharingManager: DISABLED (mode={mode})",
                level="INFO"
            )
        else:
            self.context.state = LoadSharingState.INACTIVE
            self.ad.log(
                f"LoadSharingManager: Initialized (inactive, mode={mode}) - "
                f"capacity threshold={self.min_calling_capacity_w}W, "
                f"target={self.target_capacity_w}W, "
                f"fallback_timeout={self.fallback_timeout_s}s, "
                f"fallback_cooldown={self.fallback_cooldown_s}s",
                level="INFO"
            )
    
    def _get_mode(self) -> str:
        """Get current load sharing mode.
        
        Returns:
            Mode string: 'Off', 'Conservative', 'Balanced', or 'Aggressive'
            Falls back to 'Aggressive' if entity missing or error
        """
        try:
            state = self.ad.get_state(self.mode_select_entity)
            if state in [C.LOAD_SHARING_MODE_OFF, C.LOAD_SHARING_MODE_CONSERVATIVE,
                        C.LOAD_SHARING_MODE_BALANCED, C.LOAD_SHARING_MODE_AGGRESSIVE]:
                return state
            elif state is None:
                self.ad.log(
                    f"LoadSharingManager: Mode entity does not exist yet, defaulting to Aggressive",
                    level="INFO"
                )
                return C.LOAD_SHARING_MODE_AGGRESSIVE
            else:
                self.ad.log(
                    f"LoadSharingManager: Invalid mode '{state}', defaulting to Aggressive",
                    level="WARNING"
                )
                return C.LOAD_SHARING_MODE_AGGRESSIVE
        except Exception as e:
            self.ad.log(
                f"LoadSharingManager: Failed to read mode: {e}. "
                f"Defaulting to Aggressive for backward compatibility.",
                level="INFO"
            )
            return C.LOAD_SHARING_MODE_AGGRESSIVE
    
    def evaluate(self, room_states: Dict[str, Dict], boiler_state: str, cycling_protection_state: str) -> Dict[str, int]:
        """Evaluate load sharing needs and return valve commands.
        
        Implements two-tier cascade with one-room-at-a-time escalation (up to 100%)
        before adding the next room. Schedule-aware rooms (closest first) are 
        preferred over fallback rooms.
        
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
        
        # Check if disabled
        if self.context.state == LoadSharingState.DISABLED:
            return {}
        
        # Check mode (in case it was toggled to Off)
        mode = self._get_mode()
        if mode == C.LOAD_SHARING_MODE_OFF:
            if self.context.is_active():
                self._deactivate("mode changed to Off")
            return {}
        
        # If currently inactive, check entry conditions
        if not self.context.is_active():
            if self._evaluate_entry_conditions(room_states, cycling_protection_state):
                self._activate_and_escalate(room_states, now)
                
                if self.context.active_rooms:
                    return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
            
            return {}
        
        # If currently active, check exit conditions
        if self._evaluate_exit_conditions(room_states, now):
            self._deactivate("exit conditions met")
            return {}
        
        # Load sharing is active - return current valve commands
        return {room_id: room.valve_pct for room_id, room in self.context.active_rooms.items()}
    
    def _activate_and_escalate(self, room_states: Dict, now: datetime) -> None:
        """Activate load sharing with two-tier cascade and one-room-at-a-time escalation.
        
        Strategy: Add rooms one at a time, escalating each to 100% before adding
        the next room. Schedule-aware rooms (closest first) are preferred over
        fallback rooms (priority-based).
        
        Mode controls which tiers are available:
        - Conservative: Tier 1 only (schedule pre-warming)
        - Balanced: Tier 1 + Tier 2 Phase A (passive rooms)
        - Aggressive: All tiers (includes Phase B fallback priority)
        
        Tier 1 (Schedule-aware): Rooms with upcoming schedule within 2x lookahead
        - Sorted by closest schedule first
        - Add one room at 50%, escalate to 100%, then add next if needed
        
        Tier 2 (Fallback): Passive rooms + priority list  
        - Same one-at-a-time approach
        - Warning-level logging indicates schedule gap
        
        Args:
            room_states: Room state dict
            now: Current datetime
        """
        # Get current mode
        mode = self._get_mode()
        
        # Initialize trigger context first
        self._initialize_trigger_context(room_states, now)
        
        # Get all schedule-aware candidates (sorted by closest first)
        schedule_candidates = self._select_schedule_rooms(room_states, now)
        
        # Process schedule rooms one at a time
        for room_id, valve_pct, reason, target_temp, minutes_until in schedule_candidates:
            # Add room at initial valve percentage
            self._activate_schedule_room(room_id, valve_pct, reason, target_temp, now, minutes_until)
            
            # Check if sufficient
            total_capacity = self._calculate_total_system_capacity(room_states)
            if total_capacity >= self.target_capacity_w:
                self.ad.log(
                    f"Load sharing: Schedule room '{room_id}' sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                    level="INFO"
                )
                return
            
            # Escalate this room to 100% before adding another
            activation = self.context.active_rooms[room_id]
            while activation.valve_pct < 100:
                old_pct = activation.valve_pct
                activation.valve_pct = min(100, activation.valve_pct + 10)
                self.ad.log(
                    f"Load sharing: Escalating schedule room '{room_id}' from {old_pct}% to {activation.valve_pct}%",
                    level="DEBUG"
                )
                
                total_capacity = self._calculate_total_system_capacity(room_states)
                if total_capacity >= self.target_capacity_w:
                    self.context.state = LoadSharingState.SCHEDULE_ESCALATED
                    self.ad.log(
                        f"Load sharing: Schedule room '{room_id}' at {activation.valve_pct}% sufficient "
                        f"({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                        level="INFO"
                    )
                    return
            
            # Room at 100%, still need more capacity - continue to next schedule room
            self.context.state = LoadSharingState.SCHEDULE_ESCALATED
        
        # Conservative mode: Stop after Tier 1 (schedule tier only)
        if mode == C.LOAD_SHARING_MODE_CONSERVATIVE:
            if schedule_candidates:
                self.ad.log(
                    f"Load sharing: Conservative mode - schedule tier exhausted, no fallback allowed",
                    level="INFO"
                )
            else:
                self.ad.log(
                    f"Load sharing: Conservative mode - no schedule tier candidates available",
                    level="INFO"
                )
            return
        
        # Schedule rooms exhausted - try fallback tier (if mode allows)
        fallback_candidates = self._select_fallback_rooms(room_states, mode)
        
        if fallback_candidates:
            # Process fallback rooms one at a time
            for room_id, valve_pct, reason, target_temp in fallback_candidates:
                # Skip if already active (from schedule tier)
                if room_id in self.context.active_rooms:
                    continue
                    
                # Add room at initial valve percentage
                self._activate_fallback_room(room_id, valve_pct, reason, target_temp, now)
                
                # Check if sufficient
                total_capacity = self._calculate_total_system_capacity(room_states)
                if total_capacity >= self.target_capacity_w:
                    self.ad.log(
                        f"Load sharing: Fallback room '{room_id}' sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                        level="WARNING"
                    )
                    return
                
                # Escalate this room to 100% before adding another
                activation = self.context.active_rooms[room_id]
                while activation.valve_pct < 100:
                    old_pct = activation.valve_pct
                    activation.valve_pct = min(100, activation.valve_pct + 10)
                    self.ad.log(
                        f"Load sharing: Escalating fallback room '{room_id}' from {old_pct}% to {activation.valve_pct}%",
                        level="DEBUG"
                    )
                    
                    total_capacity = self._calculate_total_system_capacity(room_states)
                    if total_capacity >= self.target_capacity_w:
                        self.context.state = LoadSharingState.FALLBACK_ESCALATED
                        self.ad.log(
                            f"Load sharing: Fallback room '{room_id}' at {activation.valve_pct}% sufficient "
                            f"({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                            level="WARNING"
                        )
                        return
                
                # Room at 100%, still need more capacity - continue to next fallback room
                self.context.state = LoadSharingState.FALLBACK_ESCALATED
            
            # All fallback rooms exhausted
            total_capacity = self._calculate_total_system_capacity(room_states)
            if total_capacity >= self.target_capacity_w:
                self.ad.log(
                    f"Load sharing: All fallback rooms exhausted but sufficient ({total_capacity:.0f}W >= {self.target_capacity_w}W)",
                    level="WARNING"
                )
            else:
                self.ad.log(
                    f"Load sharing: All tiers exhausted ({total_capacity:.0f}W < {self.target_capacity_w}W) - "
                    f"accepting cycling as lesser evil",
                    level="INFO"
                )
            return
        
        # No fallback rooms available
        total_capacity = self._calculate_total_system_capacity(room_states)
        if self.context.active_rooms:
            self.ad.log(
                f"Load sharing: Schedule tier only ({total_capacity:.0f}W < {self.target_capacity_w}W), "
                f"no fallback rooms available",
                level="INFO"
            )
        else:
            self.ad.log(
                f"Load sharing: No rooms available in any tier - accepting cycling as lesser evil",
                level="INFO"
            )
    
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
            'mode': self._get_mode(),
            'decision_explanation': self._build_decision_explanation(),
            'decision_details': self._build_decision_details()
        }
    
    def _build_decision_explanation(self) -> str:
        """Build concise human-readable explanation of load sharing decision.
        
        Returns single-line summary suitable for general display (80-120 chars).
        For inactive/disabled states, returns minimal text since state is self-explanatory.
        For active states, provides detailed explanation of trigger and room selections.
        
        Returns:
            Human-readable explanation string
        """
        if self.context.state == LoadSharingState.DISABLED:
            return "disabled"
        
        if not self.context.is_active():
            return ""
        
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
            tier_name = {1: "schedule", 2: "fallback"}[tier]
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
                'reason': 'disabled'
            }
        
        if not self.context.is_active():
            return {
                'status': 'inactive',
                'reason': ''
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
                2: 'Fallback (passive/priority)'
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
                'schedule_count': len(self.context.schedule_rooms),
                'fallback_count': len(self.context.fallback_rooms)
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
    # Entry/Exit Condition Evaluation
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
        
        # Calculate total system capacity: calling rooms + passive rooms with open valves
        all_capacities = self.load_calculator.get_all_estimated_capacities()
        total_capacity = 0.0
        
        # Add calling room capacity (full capacity)
        for room_id in calling_rooms:
            capacity = all_capacities.get(room_id)
            if capacity is not None:
                total_capacity += capacity
        
        # Add passive room capacity (valve-adjusted)
        # Passive rooms with open valves contribute to heat dissipation
        passive_capacity = 0.0
        for room_id, state in room_states.items():
            if state.get('operating_mode') == 'passive' and not state.get('calling', False):
                valve_pct = state.get('valve_percent', 0)
                if valve_pct > 0:
                    capacity = all_capacities.get(room_id)
                    if capacity is not None:
                        effective_capacity = capacity * (valve_pct / 100.0)
                        passive_capacity += effective_capacity
        
        total_capacity += passive_capacity
        
        if passive_capacity > 0:
            self.ad.log(
                f"Load sharing entry check: Including {passive_capacity:.0f}W from passive rooms "
                f"(total capacity: {total_capacity:.0f}W)",
                level="DEBUG"
            )
        
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
    
    def _select_schedule_rooms(self, room_states: Dict, now: datetime) -> List[Tuple[str, int, str, float, float]]:
        """Select schedule-aware rooms for load sharing.
        
        Selection criteria:
        - Room in "auto" mode
        - Not currently calling for heat
        - Has schedule block within lookahead window (config × multiplier)
        - Schedule target > current temperature
        
        Sorted by: minutes_until ASC (closest schedule first)
        
        Args:
            room_states: Room state dict from room_controller
            now: Current datetime
            
        Returns:
            List of (room_id, valve_pct, reason, target_temp, minutes_until) tuples
            sorted by closest schedule first
        """
        candidates = []
        
        for room_id, state in room_states.items():
            # Skip if not in auto mode (only include auto mode rooms)
            if state.get('mode') != 'auto':
                continue
            
            # Skip if already calling
            if state.get('calling', False):
                continue
            
            # Get room config for lookahead window (with multiplier)
            room_cfg = self.config.rooms.get(room_id, {})
            load_sharing_cfg = room_cfg.get('load_sharing', {})
            base_lookahead_m = load_sharing_cfg.get('schedule_lookahead_m', C.LOAD_SHARING_SCHEDULE_LOOKAHEAD_M_DEFAULT)
            effective_lookahead_m = base_lookahead_m * C.LOAD_SHARING_LOOKAHEAD_MULTIPLIER
            
            # Check for schedule block within effective lookahead window
            next_block = self.scheduler.get_next_schedule_block(room_id, now, within_minutes=effective_lookahead_m)
            
            if next_block is None:
                # No schedule block within window
                continue
            
            start_time, end_time, target_temp, block_mode = next_block
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
            
            # Determine reason string (include block mode for visibility)
            reason = f"schedule_{int(minutes_until)}m_{block_mode}"
            
            candidates.append((room_id, need, target_temp, minutes_until, reason))
        
        # Sort by minutes_until (ascending) - closest schedule first
        candidates.sort(key=lambda x: x[3])
        
        # Return as list of (room_id, valve_pct, reason, target_temp, minutes_until)
        # Initial valve opening uses LOAD_SHARING_INITIAL_PCT (default 50%)
        selections = []
        for room_id, need, target, minutes, reason in candidates:
            selections.append((room_id, C.LOAD_SHARING_INITIAL_PCT, reason, target, minutes))
            self.ad.log(
                f"Load sharing schedule candidate: {room_id} - need={need:.1f}C, target={target:.1f}C, "
                f"minutes_until={minutes:.0f}, valve={C.LOAD_SHARING_INITIAL_PCT}%",
                level="DEBUG"
            )
        
        return selections
    
    def _select_fallback_rooms(self, room_states: Dict, mode: str) -> List[Tuple[str, int, str, float]]:
        """Select fallback rooms: Phase A (passive rooms), then Phase B (fallback priority).
        
        This is the fallback tier when schedule-aware rooms are insufficient.
        Warning-level logging indicates a schedule gap that should be addressed.
        
        Mode controls which phases are available:
        - Balanced: Phase A only (passive rooms at max_temp)
        - Aggressive: Phase A + Phase B (includes fallback priority list)
        
        PHASE A: Passive room opportunistic heating
        - Current operating_mode == 'passive' (room is passive RIGHT NOW)
        - Not currently calling for heat
        - Current temperature < max_temp (room can still accept heat)
        - Uses 50% initial valve (overrides user's passive_valve_percent)
        
        PHASE B: Priority list fallback (only if Phase A insufficient)
        - Deterministic selection when schedules don't help
        - Only available in Aggressive mode
        
        STRATEGY: Maximize existing rooms before adding new ones.
        - Add ONE room at a time in priority order
        - Room will be escalated (50% -> 60% -> 70% -> 80% -> 90% -> 100%) before next room is added
        - This minimizes the number of rooms heated (energy efficiency)
        
        Selection criteria for Phase B:
        - Room in "auto" mode (respects user intent)
        - Not currently calling for heat
        - Not already in schedule-aware tier
        - Has fallback_priority configured (rooms without this are excluded)
        - NOT in timeout cooldown (rooms that recently timed out are excluded)
        - NO temperature check - ultimate fallback accepts any auto mode room
        
        Sorted by: fallback_priority ASC (lower number = higher priority)
        
        Args:
            room_states: Room state dict from room_controller
            
        Returns:
            List of (room_id, valve_pct, reason, target_temp) tuples (returns ONE room, will be escalated later)
        """
        # ===== PHASE A: Passive rooms =====
        passive_candidates = []
        
        for room_id, state in room_states.items():
            # Must be in passive operating mode RIGHT NOW
            if state.get('operating_mode') != 'passive':
                continue
            
            # Skip if calling (comfort/frost protection)
            if state.get('calling', False):
                continue
            
            # Get current temp and max_temp
            temp = state.get('temp')
            max_temp = state.get('target')  # For passive, target is max_temp
            
            if temp is None or max_temp is None:
                continue  # Skip rooms with stale sensors
            
            if temp >= max_temp:
                continue  # Already at or above max_temp
            
            # Calculate capacity contribution
            need = max_temp - temp
            all_capacities = self.load_calculator.get_all_estimated_capacities()
            room_capacity = all_capacities.get(room_id)
            
            if room_capacity is None:
                continue  # No capacity estimate
            
            passive_candidates.append((room_id, need, room_capacity, max_temp))
        
        # Sort by need (neediest first)
        passive_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Return passive rooms with standard initial valve percentages
        if passive_candidates:
            selections = []
            for room_id, need, capacity, max_temp in passive_candidates:
                selections.append((
                    room_id, 
                    C.LOAD_SHARING_INITIAL_PCT,  # 50%
                    "passive_room",
                    max_temp
                ))
                self.ad.log(
                    f"Load sharing fallback Phase A: {room_id} - need={need:.1f}C, "
                    f"max_temp={max_temp:.1f}C, valve={C.LOAD_SHARING_INITIAL_PCT}%",
                    level="DEBUG"
                )
            
            return selections
        
        # Balanced mode: Stop after Phase A (passive rooms only)
        if mode == C.LOAD_SHARING_MODE_BALANCED:
            self.ad.log(
                f"Load sharing: Balanced mode - Phase A exhausted, Phase B not allowed",
                level="INFO"
            )
            return []
        
        # ===== PHASE B: Fallback priority (Aggressive mode only) =====
        candidates = []
        active_room_ids = set(self.context.active_rooms.keys())
        now = datetime.now()
        
        # Clean up expired cooldown entries
        expired_cooldowns = []
        for room_id, timeout_time in list(self.context.fallback_timeout_history.items()):
            cooldown_elapsed = (now - timeout_time).total_seconds()
            if cooldown_elapsed >= self.fallback_cooldown_s:
                expired_cooldowns.append(room_id)
        
        for room_id in expired_cooldowns:
            del self.context.fallback_timeout_history[room_id]
            self.ad.log(
                f"Load sharing: Fallback cooldown expired for '{room_id}' - now eligible",
                level="DEBUG"
            )
        
        for room_id, state in room_states.items():
            # Skip if not in auto mode
            if state.get('mode') != 'auto':
                continue
            
            # Skip if already calling
            if state.get('calling', False):
                continue
            
            # Skip if already in schedule tier
            if room_id in active_room_ids:
                continue
            
            # Check if room recently timed out (cooldown enforcement)
            last_timeout = self.context.fallback_timeout_history.get(room_id)
            if last_timeout is not None:
                cooldown_elapsed = (now - last_timeout).total_seconds()
                if cooldown_elapsed < self.fallback_cooldown_s:
                    remaining_s = self.fallback_cooldown_s - cooldown_elapsed
                    self.ad.log(
                        f"Load sharing fallback: Skipping '{room_id}' - in cooldown "
                        f"(remaining: {remaining_s:.0f}s / {self.fallback_cooldown_s}s)",
                        level="DEBUG"
                    )
                    continue  # Skip - still in cooldown period
            
            # Get room config for fallback priority
            room_cfg = self.config.rooms.get(room_id, {})
            load_sharing_cfg = room_cfg.get('load_sharing', {})
            fallback_priority = load_sharing_cfg.get('fallback_priority')
            
            # Skip if no fallback_priority configured (explicit exclusion)
            if fallback_priority is None:
                continue
            
            # Passive rooms are now reconsidered in Phase B with fallback_priority
            # They will use fallback_comfort_target_c instead of their max_temp
            is_passive = state.get('operating_mode') == 'passive'
            reason = f"fallback_p{fallback_priority}{'_passive' if is_passive else ''}"
            candidates.append((room_id, fallback_priority, reason, is_passive))
            
            self.ad.log(
                f"Load sharing fallback Phase B candidate: {room_id} - priority={fallback_priority}"
                f"{' (passive - will use comfort target)' if is_passive else ''}",
                level="DEBUG"
            )
        
        # Sort: passive rooms first (by priority), then non-passive rooms (by priority)
        # This prioritizes rooms configured for opportunistic heating in emergency fallback
        candidates.sort(key=lambda x: (not x[3], x[1]))  # (not is_passive, priority)
        
        # Return ONLY the highest priority room (will be escalated before adding more)
        # Initial valve opening: 50% (compromise between flow and energy)
        if candidates:
            room_id, priority, reason, is_passive = candidates[0]
            all_capacities = self.load_calculator.get_all_estimated_capacities()
            room_capacity = all_capacities.get(room_id)
            
            if room_capacity is None:
                self.ad.log(
                    f"Load sharing fallback Phase B: Skipping {room_id} - no capacity estimate",
                    level="DEBUG"
                )
                return []
            
            # Get comfort target for fallback pre-warming
            # Uses global comfort target (default 20C) to bypass low parking temperatures
            # This applies to BOTH passive rooms (reconsidered here) AND normal fallback rooms
            ls_config = self.config.boiler_config.get('load_sharing', {})
            fallback_target = ls_config.get('fallback_comfort_target_c', 20.0)
            
            valve_pct = C.LOAD_SHARING_INITIAL_PCT
            effective_room_capacity = room_capacity * (valve_pct / 100.0)
            current_capacity = self._calculate_total_system_capacity(room_states)
            new_total_capacity = current_capacity + effective_room_capacity
            
            self.ad.log(
                f"Load sharing fallback Phase B selection: {room_id} - priority={priority}, "
                f"valve={valve_pct}%, target={fallback_target:.1f}C{' (passive room)' if is_passive else ''}, "
                f"adds {effective_room_capacity:.0f}W (total: {new_total_capacity:.0f}W)",
                level="DEBUG"
            )
            
            return [(room_id, valve_pct, reason, fallback_target)]
        
        return []
    
    def _initialize_trigger_context(self, room_states: Dict, now: datetime) -> None:
        """Initialize the trigger context for load sharing activation.
        
        This must be called before room activation methods.
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
    
    def _activate_schedule_room(self, room_id: str, valve_pct: int, reason: str, 
                                 target_temp: float, now: datetime, minutes_until: float) -> None:
        """Activate a single schedule-aware room for load sharing.
        
        Args:
            room_id: Room to activate
            valve_pct: Initial valve percentage
            reason: Reason string for logging
            target_temp: Target temperature for this room
            now: Current datetime
            minutes_until: Minutes until scheduled heat
        """
        activation = RoomActivation(
            room_id=room_id,
            tier=TIER_SCHEDULE,
            valve_pct=valve_pct,
            activated_at=now,
            reason=reason,
            target_temp=target_temp
        )
        self.context.active_rooms[room_id] = activation
        
        # Set state if first room
        if len(self.context.active_rooms) == 1:
            self.context.state = LoadSharingState.SCHEDULE_ACTIVE
            self.ad.log(
                f"Load sharing ACTIVATED (schedule): '{room_id}' at {valve_pct}% | "
                f"Schedule in {minutes_until:.0f}m, target={target_temp:.1f}C | "
                f"Trigger: {len(self.context.trigger_calling_rooms)} room(s) at {self.context.trigger_capacity:.0f}W",
                level="INFO"
            )
        else:
            self.ad.log(
                f"Load sharing: Added schedule room '{room_id}' at {valve_pct}% (schedule in {minutes_until:.0f}m)",
                level="INFO"
            )
    
    def _activate_fallback_room(self, room_id: str, valve_pct: int, reason: str,
                                 target_temp: float, now: datetime) -> None:
        """Activate a single fallback room for load sharing.
        
        Args:
            room_id: Room to activate
            valve_pct: Initial valve percentage
            reason: Reason string for logging
            target_temp: Target temperature for this room
            now: Current datetime
        """
        activation = RoomActivation(
            room_id=room_id,
            tier=TIER_FALLBACK,
            valve_pct=valve_pct,
            activated_at=now,
            reason=reason,
            target_temp=target_temp
        )
        self.context.active_rooms[room_id] = activation
        
        # Set state
        self.context.state = LoadSharingState.FALLBACK_ACTIVE
        
        # Log with WARNING (indicates schedule gap)
        self.ad.log(
            f"Load sharing: Added FALLBACK room '{room_id}' at {valve_pct}% ({reason}) - "
            f"WARNING: Schedule gap detected, consider improving schedules",
            level="WARNING"
        )
    
    def _evaluate_exit_conditions(self, room_states: Dict, now: datetime) -> bool:
        """Check if load sharing should deactivate.
        
        Exit conditions (any triggers exit):
        A. Original calling room(s) stopped (none still calling)
        B. Additional room(s) started calling (recalculate capacity) - BYPASSES minimum duration
        C. Load sharing room now naturally calling (remove from load sharing)
        D. Fallback rooms exceeded timeout (15 minutes max for tier 2 fallback)
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
                self.context.trigger_capacity = new_total_capacity  # Fix: update capacity when trigger set changes
        
        # For ALL OTHER exit triggers, enforce minimum activation duration
        if not self.context.can_exit(now, self.min_activation_duration_s):
            return False
        
        # Exit Trigger D: Check fallback timeouts FIRST (before other conditions)
        # Remove fallback rooms that have exceeded their timeout
        fallback_rooms_to_remove = []
        for room_id, activation in list(self.context.active_rooms.items()):
            if activation.tier == TIER_FALLBACK:
                duration = (now - activation.activated_at).total_seconds()
                if duration >= self.fallback_timeout_s:
                    # Record timeout event for cooldown enforcement
                    self.context.fallback_timeout_history[room_id] = now
                    
                    cooldown_until = now + timedelta(seconds=self.fallback_cooldown_s)
                    self.ad.log(
                        f"Load sharing: Fallback room '{room_id}' exceeded timeout "
                        f"({duration:.0f}s >= {self.fallback_timeout_s}s) - removing "
                        f"(cooldown until {cooldown_until.strftime('%H:%M')})",
                        level="INFO"
                    )
                    fallback_rooms_to_remove.append(room_id)
        
        # Remove timed-out fallback rooms
        for room_id in fallback_rooms_to_remove:
            del self.context.active_rooms[room_id]
        
        # If only fallback rooms were active and all timed out, deactivate
        if not self.context.active_rooms:
            self.ad.log("Load sharing exit: All fallback rooms timed out", level="INFO")
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
        """Calculate total system capacity including calling rooms, passive rooms, and load sharing rooms.
        
        Includes:
        - All naturally calling rooms at their current capacity
        - All passive mode rooms with open valves at their effective capacity (valve adjusted)
        - All load sharing rooms at their effective capacity (valve adjusted)
        
        Args:
            room_states: Room state dict
            
        Returns:
            Total system capacity in watts
        """
        total = 0.0
        all_capacities = self.load_calculator.get_all_estimated_capacities()
        
        # Add calling rooms (full capacity)
        for room_id, state in room_states.items():
            if state.get('calling', False):
                capacity = all_capacities.get(room_id)
                if capacity is not None:
                    total += capacity
        
        # Add passive rooms with open valves (valve-adjusted capacity)
        # These rooms contribute to heat dissipation even though they're not calling
        for room_id, state in room_states.items():
            if state.get('operating_mode') == 'passive' and not state.get('calling', False):
                valve_pct = state.get('valve_percent', 0)
                if valve_pct > 0:
                    capacity = all_capacities.get(room_id)
                    if capacity is not None:
                        effective_capacity = capacity * (valve_pct / 100.0)
                        total += effective_capacity
        
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
