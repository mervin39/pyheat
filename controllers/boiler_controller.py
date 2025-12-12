# -*- coding: utf-8 -*-
"""
boiler_controller.py - Boiler state machine and control

Responsibilities:
- Manage 6-state boiler FSM (OFF, PENDING_ON, ON, PENDING_OFF, PUMP_OVERRUN, INTERLOCK_BLOCKED)
- Anti-cycling protection (min on/off times, off-delay)
- TRV interlock checking (minimum valve opening)
- Pump overrun management (keep valves open after boiler off)
- TRV feedback confirmation (wait for valves to open)
- Safety room failsafe (emergency flow path)
"""

from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
import json
import constants as C


class BoilerController:
    """Manages comprehensive boiler state machine with safety interlocks.
    
    State Machine:
    - STATE_OFF: Boiler off, no demand
    - STATE_PENDING_ON: Demand exists, waiting for TRV confirmation
    - STATE_ON: Boiler actively heating
    - STATE_PENDING_OFF: Demand ceased, in off-delay period
    - STATE_PUMP_OVERRUN: Boiler commanded off, valves staying open
    - STATE_INTERLOCK_BLOCKED: Insufficient valve opening, cannot turn on
    """
    
    def __init__(self, ad, config, alert_manager=None, valve_coordinator=None, trvs=None, app_ref=None):
        """Initialize the boiler controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: Optional AlertManager instance for notifications
            valve_coordinator: Optional ValveCoordinator instance for managing valve persistence
            trvs: Optional TRVController instance for valve feedback validation
            app_ref: Optional reference to main PyHeat app for triggering recomputes
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        self.valve_coordinator = valve_coordinator
        self.trvs = trvs
        self.app_ref = app_ref
        
        # State machine state
        self.boiler_state = C.STATE_OFF
        self.boiler_state_entry_time = None
        
        # Timing tracking
        self.boiler_last_on = None
        self.boiler_last_off = None
        
    def update_state(self, any_calling: bool, active_rooms: List[str], 
                    room_data: Dict, now: datetime) -> Tuple[str, str, Dict[str, int], bool]:
        """Update boiler state machine based on demand and conditions.
        
        Full 6-state FSM with comprehensive safety features:
        - Anti-cycling protection (min on/off times)
        - Valve interlock system (prevents no-flow condition)
        - Pump overrun handling (keeps valves open after off)
        - TRV feedback confirmation (waits for valves to open)
        - Safety room failsafe (emergency flow path)
        
        Args:
            any_calling: Whether any room is calling for heat
            active_rooms: List of room IDs that are calling
            room_data: Dict of room states {room_id: room_dict}
            now: Current datetime
            
        Returns:
            Tuple of (boiler_state, reason, persisted_valve_percents, valves_must_stay_open)
        """
        # Initialize state_entry_time on first call
        if self.boiler_state_entry_time is None:
            self.boiler_state_entry_time = now
        
        # Build room_valve_percents dict from room_data
        room_valve_percents = {
            room_id: data.get('valve_percent', 0)
            for room_id, data in room_data.items()
        }
        
        # Calculate valve persistence for interlock safety
        persisted_valves, interlock_ok, interlock_reason = self._calculate_valve_persistence(
            active_rooms,
            room_valve_percents
        )
        
        # Calculate total valve opening from ALL rooms with open valves
        # This ensures we see the actual system-wide valve opening
        total_valve = sum(
            persisted_valves.get(room_id, room_valve_percents.get(room_id, 0))
            for room_id in room_valve_percents.keys()
            if persisted_valves.get(room_id, room_valve_percents.get(room_id, 0)) > 0
        )
        
        # Merge persisted valves with all room valve percents for pump overrun tracking
        all_valve_positions = room_valve_percents.copy()
        all_valve_positions.update(persisted_valves)
        
        # Check TRV feedback confirmation
        trv_feedback_ok = self._check_trv_feedback_confirmed(active_rooms, persisted_valves)
        
        # Read current boiler entity state (for safety check)
        boiler_entity_state = self._get_boiler_entity_state()
        
        # SAFETY CHECK: Detect and correct state desynchronization
        # This can happen if master enable is toggled, AppDaemon restarts, or the climate
        # entity goes unavailable and returns with an unexpected state.
        # Compare expected entity state vs actual entity state and correct if needed.
        expected_entity_state = "heat" if self.boiler_state == C.STATE_ON else "off"
        
        # Check if this is during startup (first recompute after initialization)
        # During AppDaemon restart, the state machine resets to OFF but the entity
        # may still be in 'heat' mode if heating was active before restart.
        # This is expected and normal - not a critical error.
        is_startup = getattr(self.ad, 'first_boot', False)
        
        if boiler_entity_state not in [expected_entity_state, "unknown", "unavailable"]:
            if self.boiler_state == C.STATE_ON and boiler_entity_state == "off":
                # State machine thinks ON but entity is OFF - reset state machine to OFF
                self.ad.log(
                    f"⚠️ Boiler state desync detected: state machine={self.boiler_state} "
                    f"(expects entity={expected_entity_state}) but climate entity={boiler_entity_state}. "
                    f"Resetting state machine to OFF.",
                    level="WARNING"
                )
                self._transition_to(C.STATE_OFF, now, "state desync correction - entity is off")
                # Cancel timers that may be stale
                self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                self._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
                
                # Report alert (only if not during startup)
                if self.alert_manager and not is_startup:
                    from alert_manager import AlertManager
                    self.alert_manager.report_error(
                        AlertManager.ALERT_BOILER_STATE_DESYNC,
                        AlertManager.SEVERITY_WARNING,
                        f"Boiler state desynchronization detected and corrected.\n\n"
                        f"**State Machine:** {self.boiler_state} (expected entity: {expected_entity_state})\n"
                        f"**Climate Entity:** {boiler_entity_state}\n\n"
                        f"**Action:** Reset state machine to OFF.\n\n"
                        f"This can occur after master enable toggle, system restart, or entity unavailability.",
                        auto_clear=True
                    )
            elif self.boiler_state != C.STATE_ON and boiler_entity_state == "heat":
                # State machine thinks OFF/PENDING/OVERRUN but entity is heating
                # During startup, this is expected and normal (entity hasn't been turned off yet)
                # After startup, this is unusual and warrants attention
                if is_startup:
                    # Normal startup behavior - DO NOT turn off the entity
                    # The state machine will re-evaluate demand immediately and decide
                    # whether to keep it on or turn it off. Turning it off here would
                    # create an unnecessary short cycle (off -> on within milliseconds)
                    self.ad.log(
                        f"Startup sync: Climate entity is heating while state machine is initializing. "
                        f"Skipping desync correction - state machine will re-evaluate demand immediately.",
                        level="DEBUG"
                    )
                    # Do NOT call self._set_boiler_off() - let the state machine handle it
                else:
                    # Unexpected desync during normal operation - turn off and re-evaluate
                    self.ad.log(
                        f"⚠️ Unexpected desync: Climate entity is heating when state machine is {self.boiler_state}. "
                        f"Turning off climate entity and re-evaluating demand.",
                        level="WARNING"
                    )
                    self._set_boiler_off()
                    # If we were in a state with timers running, preserve them
                    # (e.g., PUMP_OVERRUN timer should continue)
                    
                    # Report alert only during normal operation
                    if self.alert_manager:
                        from alert_manager import AlertManager
                        self.alert_manager.report_error(
                            AlertManager.ALERT_BOILER_STATE_DESYNC,
                            AlertManager.SEVERITY_WARNING,
                            f"⚠️ Climate entity was heating without state machine control.\n\n"
                            f"**State Machine:** {self.boiler_state} (expected entity: {expected_entity_state})\n"
                            f"**Climate Entity:** {boiler_entity_state}\n\n"
                            f"**Action:** Turned off climate entity and re-evaluating demand.\n\n"
                            f"This may occur after entity unavailability or manual control. "
                            f"System will resume normal operation based on current demand.",
                            auto_clear=True
                        )
        else:
            # No desync - clear any previous alerts
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_BOILER_STATE_DESYNC)
        
        # Determine if we have demand
        has_demand = len(active_rooms) > 0
        
        # Time in current state
        time_in_state = (now - self.boiler_state_entry_time).total_seconds() if self.boiler_state_entry_time else 0
        
        # State machine logic
        reason = ""
        valves_must_stay_open = False
        
        if self.boiler_state == C.STATE_OFF:
            if has_demand and interlock_ok:
                # Demand exists, check anti-cycling and TRV feedback
                if not self._check_min_off_time_elapsed():
                    self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "min_off_time not elapsed")
                    reason = f"Blocked: min_off_time not elapsed"
                elif not trv_feedback_ok:
                    self._transition_to(C.STATE_PENDING_ON, now, "waiting for TRV confirmation")
                    reason = "Waiting for TRV feedback confirmation"
                else:
                    # All conditions met, turn on
                    self._transition_to(C.STATE_ON, now, "demand and conditions met")
                    self._set_boiler_on()
                    self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self._get_min_on_time())
                    reason = f"Turned ON: {len(active_rooms)} room(s) calling"
            elif has_demand and not interlock_ok:
                self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "insufficient valve opening")
                reason = f"Interlock blocked: {interlock_reason}"
            else:
                reason = "Off: no demand"
        
        elif self.boiler_state == C.STATE_PENDING_ON:
            if not has_demand:
                self._transition_to(C.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased while pending"
            elif not interlock_ok:
                self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "interlock failed")
                reason = f"Interlock blocked: {interlock_reason}"
            elif trv_feedback_ok:
                # TRVs confirmed, turn on
                self._transition_to(C.STATE_ON, now, "TRV feedback confirmed")
                self._set_boiler_on()
                self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self._get_min_on_time())
                reason = f"Turned ON: TRVs confirmed at {total_valve}%"
            else:
                reason = f"Pending ON: waiting for TRV confirmation ({time_in_state:.0f}s)"
                # Log warning if stuck for >5 minutes
                if time_in_state > 300:
                    self.ad.log(
                        f"Boiler has been waiting for TRV feedback for {int(time_in_state/60)} minutes. "
                        f"Rooms: {', '.join(active_rooms)}",
                        level="WARNING"
                    )
        
        elif self.boiler_state == C.STATE_ON:
            # Valve coordinator tracks actual commanded positions for pump overrun
            # No need to save them here
            
            if not has_demand:
                # Demand stopped, enter off-delay period
                self._transition_to(C.STATE_PENDING_OFF, now, "demand ceased, entering off-delay")
                self._start_timer(C.HELPER_BOILER_OFF_DELAY_TIMER, self._get_off_delay())
                
                # Enable pump overrun persistence in valve coordinator
                # This snapshots current commanded positions for pump overrun period
                self.valve_coordinator.enable_pump_overrun_persistence()
                
                reason = f"Pending OFF: off-delay started"
                # Valves will be held by valve coordinator during PENDING_OFF and PUMP_OVERRUN
                valves_must_stay_open = True
                persisted_valves = self.valve_coordinator.get_persisted_valves()
            elif not interlock_ok:
                # Interlock failed while running - turn off immediately
                self.ad.log("Boiler: interlock failed while ON, turning off immediately", level="WARNING")
                self._transition_to(C.STATE_PUMP_OVERRUN, now, "interlock failed")
                self._set_boiler_off()
                
                # Enable pump overrun persistence
                self.valve_coordinator.enable_pump_overrun_persistence()
                
                self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self._get_min_off_time())
                # NOTE: Pump overrun timer will start when flame actually goes off
                # (via on_flame_off callback) to match boiler's physical pump overrun
                reason = "Turned OFF: interlock failed, waiting for flame off"
                valves_must_stay_open = True
                self.ad.log(
                    f"🔴 CRITICAL: Boiler interlock failed while running! Boiler turned off. "
                    f"Total valve opening dropped below minimum.",
                    level="ERROR"
                )
                # Report critical alert
                if self.alert_manager:
                    from alert_manager import AlertManager
                    self.alert_manager.report_error(
                        AlertManager.ALERT_BOILER_INTERLOCK_FAILURE,
                        AlertManager.SEVERITY_CRITICAL,
                        f"Boiler was running but valve interlock failed!\n\n"
                        f"**Reason:** {interlock_reason}\n\n"
                        f"The boiler has been turned off for safety. Check TRV operation and valve positions.",
                        auto_clear=True
                    )
            else:
                reason = f"ON: heating {len(active_rooms)} room(s), total valve {total_valve}%"
        
        elif self.boiler_state == C.STATE_PENDING_OFF:
            # CRITICAL: Valves must stay open during pending_off because boiler is still ON
            valves_must_stay_open = True
            persisted_valves = self.valve_coordinator.get_persisted_valves()
            self.ad.log(f"Boiler: STATE_PENDING_OFF - valve coordinator holding positions: {persisted_valves}", level="DEBUG")
            
            if has_demand and interlock_ok:
                # Demand returned during off-delay, return to ON
                self._transition_to(C.STATE_ON, now, "demand returned")
                self._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
                
                # Disable pump overrun persistence (returning to normal heating)
                self.valve_coordinator.disable_pump_overrun_persistence()
                
                reason = f"Returned to ON: demand resumed ({len(active_rooms)} room(s))"
            elif not self._is_timer_active(C.HELPER_BOILER_OFF_DELAY_TIMER):
                # Off-delay timer completed - check if we can transition to pump overrun
                boiler_entity_state = self._get_boiler_entity_state()
                boiler_is_off = boiler_entity_state == "off"
                
                # If boiler is already physically off (e.g., from desync handler), enter pump overrun immediately
                # Otherwise, check min_on_time before turning it off
                if boiler_is_off or self._check_min_on_time_elapsed():
                    # Enter pump overrun state
                    self._transition_to(C.STATE_PUMP_OVERRUN, now, "off-delay elapsed, entering pump overrun")
                    
                    # Turn off boiler if it's still on
                    if not boiler_is_off:
                        self._set_boiler_off()
                    
                    self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                    self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self._get_min_off_time())
                    # NOTE: Pump overrun timer will start when flame actually goes off
                    # (via on_flame_off callback) to match boiler's physical pump overrun
                    # If flame is already off, start immediately
                    if self._is_flame_off():
                        self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self._get_pump_overrun())
                        reason = "Pump overrun: boiler off, flame already off"
                    else:
                        reason = "Pump overrun: boiler commanded off, waiting for flame off"
                    
                    # Pump overrun persistence already enabled by valve coordinator
                    # (when we entered PENDING_OFF)
                    valves_must_stay_open = True
                else:
                    # Boiler is still on but min_on_time hasn't elapsed yet
                    reason = f"Pending OFF: waiting for min_on_time to turn off boiler"
            else:
                reason = f"Pending OFF: off-delay timer active"
        
        elif self.boiler_state == C.STATE_PUMP_OVERRUN:
            valves_must_stay_open = True
            persisted_valves = self.valve_coordinator.get_persisted_valves()
            
            if has_demand and interlock_ok and trv_feedback_ok:
                # New demand during pump overrun - check if min_off_time has elapsed
                if not self._check_min_off_time_elapsed():
                    # Cannot turn on yet - min_off_time anti-cycling protection
                    reason = f"Pump overrun: demand resumed but min_off_time not elapsed"
                    self.ad.log(
                        f"Boiler: Demand during pump overrun, but min_off_time timer still active. "
                        f"Waiting for anti-cycling protection.",
                        level="INFO"
                    )
                else:
                    # Min_off_time elapsed, can return to ON
                    self._transition_to(C.STATE_ON, now, "demand resumed during pump overrun, min_off_time elapsed")
                    self._set_boiler_on()
                    self._cancel_timer(C.HELPER_PUMP_OVERRUN_TIMER)
                    self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self._get_min_on_time())
                    reason = f"Returned to ON: demand during pump overrun"
                    valves_must_stay_open = False
                    
                    # Disable pump overrun persistence (returning to normal heating)
                    self.valve_coordinator.disable_pump_overrun_persistence()
            elif not self._is_timer_active(C.HELPER_PUMP_OVERRUN_TIMER):
                # Pump overrun timer completed
                self._transition_to(C.STATE_OFF, now, "pump overrun complete")
                
                # Disable pump overrun persistence in valve coordinator
                self.valve_coordinator.disable_pump_overrun_persistence()
                
                reason = "Pump overrun complete, now OFF"
                valves_must_stay_open = False
                persisted_valves = {}  # Clear local persistence flag
            else:
                reason = f"Pump overrun: timer active (valves must stay open)"
        
        elif self.boiler_state == C.STATE_INTERLOCK_BLOCKED:
            if has_demand and interlock_ok and trv_feedback_ok:
                # Interlock now satisfied
                if not self._check_min_off_time_elapsed():
                    reason = f"Interlock OK but min_off_time not elapsed"
                else:
                    self._transition_to(C.STATE_ON, now, "interlock satisfied")
                    self._set_boiler_on()
                    self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self._get_min_on_time())
                    reason = f"Turned ON: interlock now satisfied"
            elif not has_demand:
                self._transition_to(C.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased"
            else:
                reason = f"Blocked: {interlock_reason}"
                # Log warning if blocked for >5 minutes
                if time_in_state > 300:
                    self.ad.log(
                        f"Boiler interlock has been blocked for {int(time_in_state/60)} minutes. "
                        f"Total valve opening insufficient. "
                        f"Rooms calling: {', '.join(active_rooms) if active_rooms else 'none'}",
                        level="WARNING"
                    )
        
        # CRITICAL SAFETY: Emergency valve override
        # If state machine is OFF but climate entity could heat (not "off") and no rooms calling,
        # force safety room valve open to ensure there's a path for hot water.
        # NOTE: Only triggers when state machine is STATE_OFF - during PENDING_OFF and PUMP_OVERRUN,
        # valve persistence is already active and provides adequate flow path.
        safety_room = self.config.boiler_config.get('safety_room')
        if safety_room and self.boiler_state == C.STATE_OFF and boiler_entity_state != "off" and len(active_rooms) == 0:
            # Climate entity could heat but state machine is OFF with no demand - force valve open for safety!
            persisted_valves[safety_room] = 100
            self.ad.log(
                f"🔴 SAFETY: Climate entity is {boiler_entity_state} with no demand! Forcing {safety_room} valve to 100% for safety",
                level="WARNING"
            )
            
            # Report critical safety alert
            if self.alert_manager:
                from alert_manager import AlertManager
                room_name = self.config.rooms.get(safety_room, {}).get('name', safety_room)
                self.alert_manager.report_error(
                    AlertManager.ALERT_SAFETY_ROOM_ACTIVE,
                    AlertManager.SEVERITY_CRITICAL,
                    f"🔴 SAFETY: Emergency safety valve activated!\n\n"
                    f"**Climate Entity State:** {boiler_entity_state}\n"
                    f"**Rooms Calling for Heat:** None\n"
                    f"**Safety Room:** {room_name}\n\n"
                    f"The climate entity is not OFF but no rooms are calling for heat. "
                    f"This is an abnormal condition that could indicate:\n\n"
                    f"• State desynchronization (fixed automatically)\n"
                    f"• Climate entity unavailability/recovery\n"
                    f"• Manual control of the boiler\n\n"
                    f"The {room_name} valve has been forced to 100% to prevent a dangerous "
                    f"no-flow condition while the boiler could heat.",
                    room_id=safety_room,
                    auto_clear=True
                )
        else:
            # Safety room not needed - clear any previous alerts
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_SAFETY_ROOM_ACTIVE)
        
        # Update valve coordinator with legacy persistence overrides (if coordinator is available)
        # NOTE: Only for safety room and interlock-based persistence.
        # PENDING_OFF and PUMP_OVERRUN are handled by valve coordinator's pump overrun system.
        if self.valve_coordinator:
            # Only use legacy persistence for safety room (STATE_OFF with safety valve forced)
            if self.boiler_state == C.STATE_OFF and persisted_valves and valves_must_stay_open:
                # Safety room forced open - use legacy persistence
                persistence_reason = f"{self.boiler_state}: {reason}"
                self.valve_coordinator.set_persistence_overrides(persisted_valves, persistence_reason)
            elif self.boiler_state != C.STATE_PENDING_OFF and self.boiler_state != C.STATE_PUMP_OVERRUN:
                # Clear legacy persistence when not in pump overrun states
                # (pump overrun states are handled by valve coordinator's own system)
                self.valve_coordinator.clear_persistence_overrides()
        
        return self.boiler_state, reason, persisted_valves, valves_must_stay_open
    
    # ========================================================================
    # Helper Methods - Valve Interlock & TRV Feedback
    # ========================================================================
    
    def _calculate_valve_persistence(
        self,
        rooms_calling: List[str],
        room_valve_percents: Dict[str, int]
    ) -> Tuple[Dict[str, int], bool, str]:
        """Calculate valve persistence if needed to meet minimum total opening.
        
        NOTE: This method now considers ALL rooms with open valves, not just calling rooms.
        This allows for future features where rooms can maintain flow/circulation even when
        not actively calling for heat (e.g., for system balancing or frost protection).
        
        Args:
            rooms_calling: List of room IDs calling for heat
            room_valve_percents: Dict mapping room_id -> calculated valve percent from bands
            
        Returns:
            Tuple of:
            - persisted_valve_percents: Dict[room_id, valve_percent] with persistence applied
            - interlock_ok: True if total >= min_valve_open_percent
            - reason: Explanation string
        """
        if not rooms_calling:
            return {}, False, "No rooms calling for heat"
        
        # Get minimum valve opening requirement
        min_valve_open = self.config.boiler_config.get('interlock', {}).get(
            'min_valve_open_percent',
            C.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        # Calculate total from ALL rooms with open valves (not just calling rooms)
        # This ensures the interlock sees the actual total valve opening in the system
        total_from_bands = sum(
            valve_pct for valve_pct in room_valve_percents.values() if valve_pct > 0
        )
        
        # Check if we need to apply persistence
        if total_from_bands >= min_valve_open:
            # Valve bands are sufficient
            self.ad.log(
                f"Boiler: total valve opening {total_from_bands}% >= min {min_valve_open}% "
                f"(from all rooms with open valves), using valve bands",
                level="DEBUG"
            )
            return room_valve_percents.copy(), True, f"Total {total_from_bands}% >= min {min_valve_open}%"
        
        # Need to persist valves - distribute evenly across calling rooms
        n_rooms = len(rooms_calling)
        persist_percent = int((min_valve_open + n_rooms - 1) / n_rooms)  # Round up
        
        # Safety clamp: never command valve >100% even if config is misconfigured
        persist_percent = min(100, persist_percent)
        
        persisted = {
            room_id: persist_percent
            for room_id in rooms_calling
        }
        
        new_total = persist_percent * n_rooms
        
        self.ad.log(
            f"Boiler: INTERLOCK PERSISTENCE: total from bands {total_from_bands}% < "
            f"min {min_valve_open}% -> setting {n_rooms} room(s) to {persist_percent}% "
            f"each (new total: {new_total}%)",
            level="INFO"
        )
        
        return persisted, True, f"Persistence: {n_rooms} rooms @ {persist_percent}% = {new_total}%"
    
    def _check_trv_feedback_confirmed(
        self,
        rooms_calling: List[str],
        valve_persistence: Dict[str, int]
    ) -> bool:
        """Check if TRV feedback confirms valves are at commanded positions.
        
        CRITICAL: This checks if TRV feedback matches the LAST COMMANDED positions
        (stored in trv_last_commanded), NOT the new desired positions from valve_persistence.
        valve_persistence here represents what was already sent to TRVs in a previous cycle.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            valve_persistence: Dict of commanded valve percentages (already sent)
            
        Returns:
            True if all calling rooms have TRV feedback matching commanded position
        """
        if not rooms_calling:
            return True  # No rooms calling, trivially satisfied
        
        for room_id in rooms_calling:
            room_config = self.config.rooms.get(room_id)
            if not room_config:
                self.ad.log(f"Boiler: room {room_id} not found, skipping feedback check", level="WARNING")
                return False
            
            # Check if TRV feedback is consistent with last commanded position
            if not self.trvs.is_valve_feedback_consistent(room_id, tolerance=C.TRV_COMMAND_FEEDBACK_TOLERANCE):
                commanded = self.trvs.get_valve_command(room_id)
                feedback = self.trvs.get_valve_feedback(room_id)
                
                # Provide more context in the log message
                if feedback is None:
                    if self.trvs.is_in_startup_grace_period():
                        status = "unknown (startup grace period - allowing)"
                    else:
                        status = "unknown (degraded mode - allowing if recently commanded)"
                else:
                    status = f"{feedback}% (mismatch)"
                
                self.ad.log(
                    f"Boiler: room {room_id} TRV feedback {status} != commanded {commanded}%",
                    level="DEBUG"
                )
                return False
        
        return True
    
    # ========================================================================
    # Helper Methods - Boiler Control
    # ========================================================================
    
    def _get_boiler_entity_state(self) -> str:
        """Get the current state of the boiler climate entity.
        
        Returns:
            Entity state: 'off', 'heat', etc., or 'unknown' if unavailable
        """
        boiler_entity = self.config.boiler_config.get('entity_id')
        if not boiler_entity:
            return "unknown"
            
        try:
            state = self.ad.get_state(boiler_entity)
            return state if state else "unknown"
        except Exception:
            return "unknown"
    
    def _set_boiler_on(self) -> None:
        """Turn boiler on using climate.turn_on service."""
        boiler_entity = self.config.boiler_config.get('entity_id')
        if not boiler_entity:
            self.ad.log("No boiler entity configured", level="ERROR")
            return
        
        try:
            # Turn on the climate entity (setpoint already configured on the entity)
            self.ad.call_service('climate/turn_on',
                            entity_id=boiler_entity)
            self.ad.log(f"Boiler ON")
            # Clear any previous control failure alert
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_BOILER_CONTROL_FAILURE)
        except Exception as e:
            self.ad.log(f"Failed to turn boiler on: {e}", level="ERROR")
            # Report critical alert for boiler control failure
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.report_error(
                    AlertManager.ALERT_BOILER_CONTROL_FAILURE,
                    AlertManager.SEVERITY_CRITICAL,
                    f"Failed to turn boiler ON: {e}\n\n"
                    f"Check boiler entity ({boiler_entity}) availability and network connection.",
                    auto_clear=True
                )
    
    def _set_boiler_off(self) -> None:
        """Turn boiler off using climate.turn_off service."""
        boiler_entity = self.config.boiler_config.get('entity_id')
        if not boiler_entity:
            self.ad.log("No boiler entity configured", level="ERROR")
            return
        
        try:
            # Turn off the climate entity
            self.ad.call_service('climate/turn_off',
                            entity_id=boiler_entity)
            self.ad.log(f"Boiler OFF")
            # Clear any previous control failure alert
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_BOILER_CONTROL_FAILURE)
        except Exception as e:
            self.ad.log(f"Failed to turn boiler off: {e}", level="ERROR")
            # Report critical alert for boiler control failure
            if self.alert_manager:
                from alert_manager import AlertManager
                self.alert_manager.report_error(
                    AlertManager.ALERT_BOILER_CONTROL_FAILURE,
                    AlertManager.SEVERITY_CRITICAL,
                    f"Failed to turn boiler OFF: {e}\n\n"
                    f"Check boiler entity ({boiler_entity}) availability and network connection.",
                    auto_clear=True
                )
    
    # ========================================================================
    # Helper Methods - Timer Management
    # ========================================================================
    
    def _start_timer(self, timer_entity: str, duration_seconds: int) -> None:
        """Start a timer helper with the specified duration.
        
        Args:
            timer_entity: Entity ID of timer
            duration_seconds: Duration in seconds
        """
        if not self.ad.entity_exists(timer_entity):
            self.ad.log(f"Boiler: timer entity {timer_entity} does not exist, cannot start", level="DEBUG")
            return
            
        try:
            # Convert seconds to HH:MM:SS format
            hours = duration_seconds // 3600
            minutes = (duration_seconds % 3600) // 60
            seconds = duration_seconds % 60
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            self.ad.call_service("timer/start",
                            entity_id=timer_entity,
                            duration=duration_str)
            self.ad.log(f"Boiler: started {timer_entity} for {duration_str}", level="DEBUG")
        except Exception as e:
            self.ad.log(f"Boiler: failed to start timer {timer_entity}: {e}", level="WARNING")
    
    def _cancel_timer(self, timer_entity: str) -> None:
        """Cancel a running timer.
        
        Args:
            timer_entity: Entity ID of timer
        """
        if not self.ad.entity_exists(timer_entity):
            return
            
        try:
            self.ad.call_service("timer/cancel", entity_id=timer_entity)
            self.ad.log(f"Boiler: cancelled {timer_entity}", level="DEBUG")
        except Exception as e:
            self.ad.log(f"Boiler: failed to cancel timer {timer_entity}: {e}", level="DEBUG")
    
    def _is_timer_active(self, timer_entity: str) -> bool:
        """Check if a timer is currently active (running).
        
        Args:
            timer_entity: Entity ID of timer
            
        Returns:
            True if timer is active, False otherwise
        """
        if not self.ad.entity_exists(timer_entity):
            return False
            
        try:
            timer_state = self.ad.get_state(timer_entity)
            return timer_state == "active"
        except Exception as e:
            self.ad.log(f"Boiler: failed to check timer {timer_entity}: {e}", level="DEBUG")
            return False
    
    def _check_min_on_time_elapsed(self) -> bool:
        """Check if minimum on time constraint is satisfied.
        
        Returns:
            True if min_on_time timer is not active (constraint satisfied)
        """
        return not self._is_timer_active(C.HELPER_BOILER_MIN_ON_TIMER)
    
    def _check_min_off_time_elapsed(self) -> bool:
        """Check if minimum off time constraint is satisfied.
        
        Returns:
            True if min_off_time timer is not active (constraint satisfied)
        """
        return not self._is_timer_active(C.HELPER_BOILER_MIN_OFF_TIMER)
    
    # ========================================================================
    # Helper Methods - State Transitions & Config Access
    # ========================================================================
    
    def _transition_to(self, new_state: str, now: datetime, reason: str) -> None:
        """Transition to new boiler state with logging.
        
        Args:
            new_state: Target state
            now: Current datetime
            reason: Reason for transition
        """
        if new_state != self.boiler_state:
            self.ad.log(f"Boiler: {self.boiler_state} -> {new_state} ({reason})")
            self.boiler_state = new_state
            self.boiler_state_entry_time = now

            # Queue CSV log event for boiler state change
            if self.app_ref and hasattr(self.app_ref, 'queue_csv_event'):
                self.app_ref.queue_csv_event(f'boiler_state_{new_state.lower()}')
    
    def _get_min_on_time(self) -> int:
        """Get minimum on time from config."""
        return self.config.boiler_config.get('anti_cycling', {}).get(
            'min_on_time_s',
            C.BOILER_MIN_ON_TIME_DEFAULT
        )
    
    def _get_min_off_time(self) -> int:
        """Get minimum off time from config."""
        return self.config.boiler_config.get('anti_cycling', {}).get(
            'min_off_time_s',
            C.BOILER_MIN_OFF_TIME_DEFAULT
        )
    
    def _get_off_delay(self) -> int:
        """Get off delay time from config."""
        return self.config.boiler_config.get('anti_cycling', {}).get(
            'off_delay_s',
            C.BOILER_OFF_DELAY_DEFAULT
        )
    
    def _get_pump_overrun(self) -> int:
        """Get pump overrun time from config."""
        return self.config.boiler_config.get('pump_overrun_s', C.BOILER_PUMP_OVERRUN_DEFAULT)
    
    def _is_flame_off(self) -> bool:
        """Check if the boiler flame is currently off.
        
        Returns:
            True if flame is off or unavailable, False if flame is on
        """
        if not self.ad.entity_exists(C.OPENTHERM_FLAME):
            return True  # Assume off if sensor doesn't exist
        
        flame_state = self.ad.get_state(C.OPENTHERM_FLAME)
        return flame_state != 'on'
    
    def on_flame_off(self, entity, attribute, old, new, kwargs):
        """Handle flame-off event to start pump overrun timer.
        
        Called when binary_sensor.opentherm_flame changes to 'off'.
        Only starts the pump overrun timer if we're in STATE_PUMP_OVERRUN
        and the timer hasn't already been started.
        
        This ensures pump overrun timing matches the boiler's physical pump
        overrun, which starts when the flame actually extinguishes (not when
        we command the boiler off).
        
        Args:
            entity: Entity that changed
            attribute: Attribute that changed
            old: Previous state
            new: New state
            kwargs: Additional arguments
        """
        if new != 'off' or old != 'on':
            return
        
        # Only relevant if we're in PUMP_OVERRUN state
        if self.boiler_state != C.STATE_PUMP_OVERRUN:
            return
        
        # Only start timer if not already running
        if self._is_timer_active(C.HELPER_PUMP_OVERRUN_TIMER):
            self.ad.log(
                "Boiler: Flame off detected but pump overrun timer already running",
                level="DEBUG"
            )
            return
        
        # Start the pump overrun timer now that flame is actually off
        self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self._get_pump_overrun())
        self.ad.log(
            f"Boiler: Flame off detected in PUMP_OVERRUN state - starting "
            f"{self._get_pump_overrun()}s pump overrun timer",
            level="INFO"
        )

