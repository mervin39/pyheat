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
import pyheat.constants as C


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
    
    def __init__(self, ad, config, alert_manager=None, valve_coordinator=None):
        """Initialize the boiler controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: Optional AlertManager instance for notifications
            valve_coordinator: Optional ValveCoordinator instance for managing valve persistence
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        self.valve_coordinator = valve_coordinator
        
        # State machine state
        self.boiler_state = C.STATE_OFF
        self.boiler_state_entry_time = None
        
        # Timing tracking
        self.boiler_last_on = None
        self.boiler_last_off = None
        
        # Valve positions tracking for pump overrun and safety
        self.boiler_last_valve_positions = {}  # {room_id: valve_percent}
        
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
        
        # Calculate total valve opening
        total_valve = sum(persisted_valves.get(room_id, 0) for room_id in active_rooms)
        
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
        
        if boiler_entity_state not in [expected_entity_state, "unknown", "unavailable"]:
            self.ad.log(
                f"⚠️ Boiler state desync detected: state machine={self.boiler_state} "
                f"(expects entity={expected_entity_state}) but climate entity={boiler_entity_state}. "
                f"This can occur after master enable toggle, system restart, or entity unavailability. "
                f"Correcting desynchronization...",
                level="WARNING"
            )
            
            if self.boiler_state == C.STATE_ON and boiler_entity_state == "off":
                # State machine thinks ON but entity is OFF - reset state machine to OFF
                self._transition_to(C.STATE_OFF, now, "state desync correction - entity is off")
                # Cancel timers that may be stale
                self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                self._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
                
                # Report alert
                if self.alert_manager:
                    from pyheat.alert_manager import AlertManager
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
                # State machine thinks OFF/PENDING/OVERRUN but entity is heating - turn entity off
                self.ad.log(
                    f"🔴 CRITICAL: Climate entity is heating when state machine is {self.boiler_state}. "
                    f"Turning off climate entity immediately.",
                    level="ERROR"
                )
                self._set_boiler_off()
                # If we were in a state with timers running, preserve them
                # (e.g., PUMP_OVERRUN timer should continue)
                
                # Report critical alert
                if self.alert_manager:
                    from pyheat.alert_manager import AlertManager
                    self.alert_manager.report_error(
                        AlertManager.ALERT_BOILER_STATE_DESYNC,
                        AlertManager.SEVERITY_CRITICAL,
                        f"🔴 CRITICAL: Climate entity heating without state machine control!\n\n"
                        f"**State Machine:** {self.boiler_state} (expected entity: {expected_entity_state})\n"
                        f"**Climate Entity:** {boiler_entity_state}\n\n"
                        f"**Action:** Turned off climate entity immediately.\n\n"
                        f"This prevented uncontrolled heating. The climate entity may have returned from "
                        f"unavailability with an unexpected state, or was manually controlled.",
                        auto_clear=True
                    )
        else:
            # No desync - clear any previous alerts
            if self.alert_manager:
                from pyheat.alert_manager import AlertManager
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
            # Save ALL valve positions (not just calling rooms) for pump overrun safety
            if has_demand and all_valve_positions:
                self.boiler_last_valve_positions = all_valve_positions.copy()
                self.ad.log(f"Boiler: saved valve positions: {self.boiler_last_valve_positions}", level="DEBUG")
            
            if not has_demand:
                # Demand stopped, enter off-delay period
                self.ad.log(f"Boiler: STATE_ON -> PENDING_OFF, preserved valve positions: {self.boiler_last_valve_positions}", level="DEBUG")
                self._transition_to(C.STATE_PENDING_OFF, now, "demand ceased, entering off-delay")
                self._start_timer(C.HELPER_BOILER_OFF_DELAY_TIMER, self._get_off_delay())
                reason = f"Pending OFF: off-delay started"
                # CRITICAL: Valves must stay open immediately upon entering PENDING_OFF
                valves_must_stay_open = True
                persisted_valves = self.boiler_last_valve_positions.copy()
            elif not interlock_ok:
                # Interlock failed while running - turn off immediately
                self.ad.log("Boiler: interlock failed while ON, turning off immediately", level="WARNING")
                self._transition_to(C.STATE_PUMP_OVERRUN, now, "interlock failed")
                self._set_boiler_off()
                self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self._get_min_off_time())
                self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self._get_pump_overrun())
                reason = "Turned OFF: interlock failed"
                valves_must_stay_open = True
                self.ad.log(
                    f"🔴 CRITICAL: Boiler interlock failed while running! Boiler turned off. "
                    f"Total valve opening dropped below minimum.",
                    level="ERROR"
                )
                # Report critical alert
                if self.alert_manager:
                    from pyheat.alert_manager import AlertManager
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
            persisted_valves = self.boiler_last_valve_positions.copy()
            self.ad.log(f"Boiler: STATE_PENDING_OFF using saved positions: {persisted_valves}", level="DEBUG")
            
            if has_demand and interlock_ok:
                # Demand returned during off-delay, return to ON
                self._transition_to(C.STATE_ON, now, "demand returned")
                self._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
                reason = f"Returned to ON: demand resumed ({len(active_rooms)} room(s))"
            elif not self._is_timer_active(C.HELPER_BOILER_OFF_DELAY_TIMER):
                # Off-delay timer completed, check min_on_time
                if not self._check_min_on_time_elapsed():
                    reason = f"Pending OFF: waiting for min_on_time"
                else:
                    # Turn off and enter pump overrun
                    self._transition_to(C.STATE_PUMP_OVERRUN, now, "off-delay elapsed, turning off")
                    self._set_boiler_off()
                    self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                    self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self._get_min_off_time())
                    self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self._get_pump_overrun())
                    self._save_pump_overrun_valves()
                    reason = "Pump overrun: boiler commanded off"
                    valves_must_stay_open = True
            else:
                reason = f"Pending OFF: off-delay timer active"
        
        elif self.boiler_state == C.STATE_PUMP_OVERRUN:
            valves_must_stay_open = True
            persisted_valves = self.boiler_last_valve_positions.copy()
            
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
            elif not self._is_timer_active(C.HELPER_PUMP_OVERRUN_TIMER):
                # Pump overrun timer completed
                self._transition_to(C.STATE_OFF, now, "pump overrun complete")
                self._clear_pump_overrun_valves()
                reason = "Pump overrun complete, now OFF"
                valves_must_stay_open = False
                persisted_valves = {}  # Clear persistence so valves can close
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
        # If climate entity is not OFF (could heat at any time) but no rooms calling for heat,
        # force safety room valve open to ensure there's a path for hot water
        safety_room = self.config.boiler_config.get('safety_room')
        if safety_room and boiler_entity_state != "off" and len(active_rooms) == 0:
            # Climate entity is not off but no demand - force valve open for safety!
            persisted_valves[safety_room] = 100
            self.ad.log(
                f"🔴 SAFETY: Climate entity is {boiler_entity_state} with no demand! Forcing {safety_room} valve to 100% for safety",
                level="WARNING"
            )
            
            # Report critical safety alert
            if self.alert_manager:
                from pyheat.alert_manager import AlertManager
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
                from pyheat.alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_SAFETY_ROOM_ACTIVE)
        
        # Update valve coordinator with persistence overrides (if coordinator is available)
        if self.valve_coordinator:
            if persisted_valves and valves_must_stay_open:
                # Set persistence overrides in coordinator
                persistence_reason = f"{self.boiler_state}: {reason}"
                self.valve_coordinator.set_persistence_overrides(persisted_valves, persistence_reason)
            elif not valves_must_stay_open:
                # Clear persistence when no longer needed
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
        
        # Calculate total from band-calculated percentages
        total_from_bands = sum(room_valve_percents.get(room_id, 0) for room_id in rooms_calling)
        
        # Check if we need to apply persistence
        if total_from_bands >= min_valve_open:
            # Valve bands are sufficient
            self.ad.log(
                f"Boiler: total valve opening {total_from_bands}% >= min {min_valve_open}%, using valve bands",
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
        
        Args:
            rooms_calling: List of room IDs calling for heat
            valve_persistence: Dict of commanded valve percentages
            
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
            
            commanded = valve_persistence.get(room_id, 0)
            trv = room_config['trv']
            
            # Get TRV feedback
            fb_valve_entity = trv['fb_valve']
            if not self.ad.entity_exists(fb_valve_entity):
                self.ad.log(f"Boiler: room {room_id} TRV feedback entity {fb_valve_entity} does not exist", level="DEBUG")
                return False
                
            fb_valve_str = self.ad.get_state(fb_valve_entity)
            if fb_valve_str in [None, "unknown", "unavailable"]:
                self.ad.log(f"Boiler: room {room_id} TRV feedback unavailable", level="DEBUG")
                return False
            
            try:
                feedback = int(float(fb_valve_str))
            except (ValueError, TypeError):
                self.ad.log(f"Boiler: room {room_id} TRV feedback invalid: {fb_valve_str}", level="DEBUG")
                return False
            
            # Check if feedback matches commanded (with tolerance)
            tolerance = C.TRV_COMMAND_FEEDBACK_TOLERANCE
            if abs(feedback - commanded) > tolerance:
                self.ad.log(f"Boiler: room {room_id} TRV feedback {feedback}% != commanded {commanded}%", level="DEBUG")
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
                from pyheat.alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_BOILER_CONTROL_FAILURE)
        except Exception as e:
            self.ad.log(f"Failed to turn boiler on: {e}", level="ERROR")
            # Report critical alert for boiler control failure
            if self.alert_manager:
                from pyheat.alert_manager import AlertManager
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
                from pyheat.alert_manager import AlertManager
                self.alert_manager.clear_error(AlertManager.ALERT_BOILER_CONTROL_FAILURE)
        except Exception as e:
            self.ad.log(f"Failed to turn boiler off: {e}", level="ERROR")
            # Report critical alert for boiler control failure
            if self.alert_manager:
                from pyheat.alert_manager import AlertManager
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
    # Helper Methods - Pump Overrun Valve Persistence
    # ========================================================================
    
    def _save_pump_overrun_valves(self) -> None:
        """Persist valve positions to unified room persistence entity.
        
        Updates valve_percent (index 0) while preserving last_calling (index 1).
        Format: {"pete": [valve_percent, last_calling], ...}
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            self.ad.log(f"Boiler: room persistence entity {C.HELPER_ROOM_PERSISTENCE} does not exist", level="DEBUG")
            return
            
        try:
            # Load existing data to preserve calling state
            data_str = self.ad.get_state(C.HELPER_ROOM_PERSISTENCE)
            data = json.loads(data_str) if data_str and data_str not in ['unknown', 'unavailable', ''] else {}
            
            # Update valve positions (index 0), preserve calling state (index 1)
            for room_id, valve_percent in self.boiler_last_valve_positions.items():
                if room_id not in data:
                    data[room_id] = [0, 0]
                data[room_id][0] = int(valve_percent)
                # Keep index 1 (calling state) unchanged
            
            self.ad.call_service("input_text/set_value",
                            entity_id=C.HELPER_ROOM_PERSISTENCE,
                            value=json.dumps(data, separators=(',', ':')))
            self.ad.log(f"Boiler: saved pump overrun valves: {self.boiler_last_valve_positions}", level="DEBUG")
        except Exception as e:
            self.ad.log(f"Boiler: failed to save pump overrun valves: {e}", level="WARNING")
    
    def _clear_pump_overrun_valves(self) -> None:
        """Clear persisted valve positions in room persistence entity.
        
        Sets valve_percent (index 0) to 0, preserves last_calling (index 1).
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            return
            
        try:
            # Load existing data to preserve calling state
            data_str = self.ad.get_state(C.HELPER_ROOM_PERSISTENCE)
            data = json.loads(data_str) if data_str and data_str not in ['unknown', 'unavailable', ''] else {}
            
            # Clear valve positions (index 0), preserve calling state (index 1)
            for room_id in data.keys():
                if isinstance(data[room_id], list) and len(data[room_id]) >= 2:
                    data[room_id][0] = 0
                    # Keep index 1 (calling state) unchanged
            
            self.ad.call_service("input_text/set_value",
                            entity_id=C.HELPER_ROOM_PERSISTENCE,
                            value=json.dumps(data, separators=(',', ':')))
            self.ad.log("Boiler: cleared pump overrun valves", level="DEBUG")
        except Exception as e:
            self.ad.log(f"Boiler: failed to clear pump overrun valves: {e}", level="DEBUG")
    
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

