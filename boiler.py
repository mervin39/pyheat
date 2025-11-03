"""
boiler.py - Boiler control module with comprehensive state machine

Responsibilities:
- Control boiler on/off with anti-cycling protection
- Enforce TRV-open interlock safety
- Manage state transitions with proper delays and confirmations
- Use timer helpers for event-driven anti-cycling logic
- Handle pump overrun to protect boiler and ensure proper heat dissipation

State Machine:
- off: Boiler off, no demand
- pending_on: Demand exists, waiting for TRV confirmation and anti-cycling delays
- on: Boiler actively heating
- pending_off: No demand, in off-delay period (prevents rapid cycling)
- pump_overrun: Boiler off but valves must stay open for pump overrun
- interlock_blocked: Demand exists but insufficient valve opening

Control Modes:
- Binary (opentherm: false): Control via setpoint (30°C on, 5°C off) for Nest
- OpenTherm (opentherm: true): Future implementation with modulation

Safety Features:
- TRV feedback confirmation before turning on boiler
- Minimum on/off times to prevent short-cycling (using timer helpers)
- Off-delay to handle brief demand interruptions (using timer helpers)
- Pump overrun support to dissipate residual heat (using timer helpers)
- Interlock: sum(valve_open_percent) >= min_valve_open_percent

Timer Helpers (Event-Driven):
- timer.pyheat_boiler_min_on_timer: Ensures boiler stays on for min_on_time_s
- timer.pyheat_boiler_min_off_timer: Prevents restart before min_off_time_s elapses
- timer.pyheat_boiler_off_delay_timer: Delays turn-off to prevent rapid cycling
- timer.pyheat_boiler_pump_overrun_timer: Keeps valves open during pump overrun
"""

from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from . import constants


class BoilerManager:
    """Manages boiler control with state machine and safety interlocks."""
    
    # State constants
    STATE_OFF = "off"
    STATE_PENDING_ON = "pending_on"
    STATE_ON = "on"
    STATE_PENDING_OFF = "pending_off"
    STATE_PUMP_OVERRUN = "pump_overrun"
    STATE_INTERLOCK_BLOCKED = "interlock_blocked"
    
    def __init__(self, boiler_config: Dict):
        """Initialize boiler manager with configuration.
        
        Args:
            boiler_config: Parsed boiler.yaml configuration
        """
        boiler_cfg = boiler_config.get("boiler", {})
        
        # Check if we have a real configuration or just placeholder
        # (orchestrator may be created before config is loaded)
        self.boiler_entity = boiler_cfg.get("entity_id")
        if not self.boiler_entity:
            # No entity configured - use stub mode until config reloaded
            log.warning("BoilerManager: no entity_id configured, using stub mode (will reload when config available)")
            self.stub_mode = True
            self.boiler_entity = "input_boolean.pyheat_boiler_actor"  # fallback
        else:
            self.stub_mode = False
        
        # OpenTherm mode flag
        self.opentherm_mode = boiler_cfg.get("opentherm", False)
        
        # Binary control settings
        binary_cfg = boiler_cfg.get("binary_control", {})
        self.on_setpoint = binary_cfg.get("on_setpoint_c", constants.BOILER_BINARY_ON_SETPOINT_DEFAULT)
        self.off_setpoint = binary_cfg.get("off_setpoint_c", constants.BOILER_BINARY_OFF_SETPOINT_DEFAULT)
        
        # Pump overrun
        self.pump_overrun_s = boiler_cfg.get("pump_overrun_s", constants.BOILER_PUMP_OVERRUN_DEFAULT)
        
        # Anti-cycling settings
        anti_cycling = boiler_cfg.get("anti_cycling", {})
        self.min_on_time_s = anti_cycling.get("min_on_time_s", constants.BOILER_MIN_ON_TIME_DEFAULT)
        self.min_off_time_s = anti_cycling.get("min_off_time_s", constants.BOILER_MIN_OFF_TIME_DEFAULT)
        self.off_delay_s = anti_cycling.get("off_delay_s", constants.BOILER_OFF_DELAY_DEFAULT)
        
        # Interlock configuration
        interlock_cfg = boiler_cfg.get("interlock", {})
        self.min_valve_open_percent = interlock_cfg.get(
            "min_valve_open_percent",
            constants.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        # State tracking
        self.current_state = self.STATE_OFF
        self.state_entry_time: Optional[datetime] = None
        
        # Track last valve positions when boiler was ON (for pending_off and pump_overrun)
        self.last_valve_positions: Dict[str, int] = {}
        
        # Timer entities for anti-cycling and pump overrun (event-driven)
        self.min_on_timer = "timer.pyheat_boiler_min_on_timer"
        self.min_off_timer = "timer.pyheat_boiler_min_off_timer"
        self.off_delay_timer = "timer.pyheat_boiler_off_delay_timer"
        self.pump_overrun_timer = "timer.pyheat_boiler_pump_overrun_timer"
        
        # Read current boiler state
        if not self.stub_mode:
            try:
                hvac_action = state.getattr(self.boiler_entity).get("hvac_action", "off")
                if hvac_action in ("heating", "idle"):
                    self.current_state = self.STATE_ON
                else:
                    self.current_state = self.STATE_OFF
            except (NameError, AttributeError):
                log.warning(f"BoilerManager: entity {self.boiler_entity} unavailable, assuming OFF")
                self.current_state = self.STATE_OFF
        
        log.info(f"BoilerManager: initialized")
        if self.stub_mode:
            log.warning(f"  STUB MODE: waiting for configuration reload")
        else:
            log.info(f"  Entity: {self.boiler_entity}")
            log.info(f"  OpenTherm mode: {self.opentherm_mode}")
            if not self.opentherm_mode:
                log.info(f"  Binary control: ON={self.on_setpoint}°C, OFF={self.off_setpoint}°C")
            log.info(f"  Min valve open: {self.min_valve_open_percent}%")
            log.info(f"  Anti-cycling: min_on={self.min_on_time_s}s, min_off={self.min_off_time_s}s, off_delay={self.off_delay_s}s")
            log.info(f"  Pump overrun: {self.pump_overrun_s}s")
            log.info(f"  Initial state: {self.current_state}")
    
    def _set_boiler_setpoint(self, setpoint: float) -> None:
        """Set boiler setpoint (binary control mode).
        
        Args:
            setpoint: Target temperature in °C
        """
        try:
            # Determine if we want boiler ON or OFF based on setpoint
            want_on = (setpoint >= self.on_setpoint)
            
            if want_on:
                # Turn ON: set mode to heat and high setpoint
                service.call(
                    "climate",
                    "set_hvac_mode",
                    entity_id=self.boiler_entity,
                    hvac_mode="heat"
                )
                service.call(
                    "climate",
                    "set_temperature",
                    entity_id=self.boiler_entity,
                    temperature=setpoint
                )
                log.debug(f"BoilerManager: set hvac_mode=heat, temperature={setpoint}°C")
            else:
                # Turn OFF: set mode to off (setpoint doesn't matter when off)
                service.call(
                    "climate",
                    "set_hvac_mode",
                    entity_id=self.boiler_entity,
                    hvac_mode="off"
                )
                log.debug(f"BoilerManager: set hvac_mode=off")
        except Exception as e:
            log.error(f"BoilerManager: failed to set boiler state: {e}")
    
    def _get_hvac_action(self) -> str:
        """Get current HVAC action from boiler.
        
```
        
        Returns:
            'heating', 'idle', 'off', or 'unknown'
        """
        try:
            action = state.getattr(self.boiler_entity).get("hvac_action", "unknown")
            return action
        except (NameError, AttributeError):
            return "unknown"
    
    def _start_timer(self, timer_entity: str, duration_seconds: int) -> None:
        """Start a timer helper with the specified duration.
        
        Args:
            timer_entity: Entity ID of timer
            duration_seconds: Duration in seconds
        """
        try:
            # Convert seconds to HH:MM:SS format
            hours = duration_seconds // 3600
            minutes = (duration_seconds % 3600) // 60
            seconds = duration_seconds % 60
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            service.call(
                "timer",
                "start",
                entity_id=timer_entity,
                duration=duration_str
            )
            log.debug(f"BoilerManager: started {timer_entity} for {duration_str}")
        except Exception as e:
            log.warning(f"BoilerManager: failed to start timer {timer_entity}: {e}")
    
    def _cancel_timer(self, timer_entity: str) -> None:
        """Cancel a running timer.
        
        Args:
            timer_entity: Entity ID of timer
        """
        try:
            service.call("timer", "cancel", entity_id=timer_entity)
            log.debug(f"BoilerManager: cancelled {timer_entity}")
        except Exception as e:
            log.debug(f"BoilerManager: failed to cancel timer {timer_entity}: {e}")
    
    def _is_timer_active(self, timer_entity: str) -> bool:
        """Check if a timer is currently active (running).
        
        Args:
            timer_entity: Entity ID of timer
            
        Returns:
            True if timer is active, False otherwise
        """
        try:
            timer_state = state.get(timer_entity)
            return timer_state == "active"
        except Exception as e:
            log.debug(f"BoilerManager: failed to check timer {timer_entity}: {e}")
            return False
    
    def _check_min_on_time_elapsed(self) -> bool:
        """Check if minimum on time constraint is satisfied.
        
        Returns:
            True if min_on_time timer is not active (constraint satisfied)
        """
        # If timer is active, constraint is NOT satisfied
        # If timer is idle/finished, constraint IS satisfied
        return not self._is_timer_active(self.min_on_timer)
    
    def _check_min_off_time_elapsed(self) -> bool:
        """Check if minimum off time constraint is satisfied.
        
        Returns:
            True if min_off_time timer is not active (constraint satisfied)
        """
        # If timer is active, constraint is NOT satisfied
        # If timer is idle/finished, constraint IS satisfied
        return not self._is_timer_active(self.min_off_timer)
    
    def _transition_to(self, new_state: str, now: datetime, reason: str) -> None:
        """Transition to new state with logging.
        
        Args:
            new_state: Target state
            now: Current datetime
            reason: Reason for transition
        """
        if new_state != self.current_state:
            log.info(f"BoilerManager: {self.current_state} → {new_state} ({reason})")
            self.current_state = new_state
            self.state_entry_time = now
    
    def calculate_valve_overrides(
        self,
        rooms_calling: List[str],
        room_valve_percents: Dict[str, int]
    ) -> Tuple[Dict[str, int], bool, str]:
        """Calculate valve overrides if needed to meet minimum total opening.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            room_valve_percents: Dict mapping room_id -> calculated valve percent from bands
            
        Returns:
            Tuple of:
            - overridden_valve_percents: Dict[room_id, valve_percent] with overrides applied
            - interlock_ok: True if total >= min_valve_open_percent
            - reason: Explanation string
        """
        if not rooms_calling:
            return {}, False, "No rooms calling for heat"
        
        # Calculate total from band-calculated percentages
        # Note: Pyscript doesn't support generator expressions, use explicit loop
        total_from_bands = 0
        for room_id in rooms_calling:
            total_from_bands += room_valve_percents.get(room_id, 0)
        
        # Check if we need to override
        if total_from_bands >= self.min_valve_open_percent:
            # Valve bands are sufficient
            log.debug(
                f"BoilerManager: total valve opening {total_from_bands}% >= "
                f"min {self.min_valve_open_percent}%, using valve bands"
            )
            return room_valve_percents.copy(), True, f"Total {total_from_bands}% >= min {self.min_valve_open_percent}%"
        
        # Need to override - distribute evenly across calling rooms
        n_rooms = len(rooms_calling)
        override_percent = int((self.min_valve_open_percent + n_rooms - 1) / n_rooms)  # Round up
        
        # Safety clamp: never command valve >100% even if config is misconfigured
        override_percent = min(100, override_percent)
        
        overridden = {
            room_id: override_percent
            for room_id in rooms_calling
        }
        
        new_total = override_percent * n_rooms
        
        log.info(
            f"BoilerManager: INTERLOCK OVERRIDE: total from bands {total_from_bands}% < "
            f"min {self.min_valve_open_percent}% -> setting {n_rooms} room(s) to {override_percent}% "
            f"each (new total: {new_total}%)"
        )
        
        return overridden, True, f"Override: {n_rooms} rooms @ {override_percent}% = {new_total}%"
    
    def _check_trv_feedback_confirmed(
        self,
        rooms_calling: List[str],
        valve_overrides: Dict[str, int],
        trv_manager: Any
    ) -> bool:
        """Check if TRV feedback confirms valves are at commanded positions.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            valve_overrides: Dict of commanded valve percentages
            trv_manager: TRVManager instance
            
        Returns:
            True if all calling rooms have TRV feedback matching commanded position
        """
        if not rooms_calling:
            return True  # No rooms calling, trivially satisfied
        
        if not trv_manager:
            log.warning("BoilerManager: no TRV manager available, skipping feedback check")
            return False
        
        for room_id in rooms_calling:
            commanded = valve_overrides.get(room_id, 0)
            trv = trv_manager.get_trv(room_id)
            
            if not trv:
                log.warning(f"BoilerManager: no TRV for room {room_id}, cannot confirm")
                return False
            
            feedback = trv.get_feedback_percent()
            if feedback is None:
                log.debug(f"BoilerManager: room {room_id} TRV feedback unavailable")
                return False
            
            if feedback != commanded:
                log.debug(f"BoilerManager: room {room_id} TRV feedback {feedback}% != commanded {commanded}%")
                return False
        
        return True
    
    def update(
        self,
        rooms_calling_for_heat: List[str],
        room_valve_percents: Dict[str, int],
        trv_manager: Any,
        now: datetime
    ) -> Dict[str, Any]:
        """Update boiler state machine based on demand and conditions.
        
        Args:
            rooms_calling_for_heat: List of room IDs calling for heat
            room_valve_percents: Dict of room_id -> valve percent (from bands, before override)
            trv_manager: TRVManager instance for feedback confirmation
            now: Current datetime
            
        Returns:
            Dict with boiler status:
            {
                "state": str (current state machine state),
                "boiler_on": bool (is boiler commanded on),
                "hvac_action": str (actual boiler status),
                "rooms_calling": List[str],
                "reason": str,
                "interlock_ok": bool,
                "overridden_valve_percents": Dict[str, int],
                "total_valve_percent": int,
                "valves_must_stay_open": bool (true during pump overrun)
            }
        """
        # Initialize state_entry_time on first call
        if self.state_entry_time is None:
            self.state_entry_time = now
        
        # Calculate valve overrides if needed
        overridden_valves, interlock_ok, interlock_reason = self.calculate_valve_overrides(
            rooms_calling_for_heat,
            room_valve_percents
        )
        
        # Calculate total valve opening (no generator expressions in pyscript)
        total_valve = 0
        for room_id in rooms_calling_for_heat:
            total_valve += overridden_valves.get(room_id, 0)
        
        # Merge overridden valves with all room valve percents for pump overrun tracking
        # This ensures we save ALL room valve positions, not just calling rooms
        all_valve_positions = room_valve_percents.copy()
        all_valve_positions.update(overridden_valves)
        
        # Check TRV feedback confirmation
        trv_feedback_ok = self._check_trv_feedback_confirmed(
            rooms_calling_for_heat,
            overridden_valves,
            trv_manager
        )
        
        # Read current HVAC action
        hvac_action = self._get_hvac_action()
        
        # Determine if we have demand
        has_demand = len(rooms_calling_for_heat) > 0
        
        # Time in current state
        time_in_state = (now - self.state_entry_time).total_seconds() if self.state_entry_time else 0
        
        # State machine logic
        reason = ""
        valves_must_stay_open = False
        
        if self.current_state == self.STATE_OFF:
            if has_demand and interlock_ok:
                # Demand exists, check anti-cycling and TRV feedback
                if not self._check_min_off_time_elapsed():
                    self._transition_to(self.STATE_INTERLOCK_BLOCKED, now, "min_off_time not elapsed")
                    reason = f"Blocked: min_off_time ({self.min_off_time_s}s) not elapsed"
                elif not trv_feedback_ok:
                    self._transition_to(self.STATE_PENDING_ON, now, "waiting for TRV confirmation")
                    reason = "Waiting for TRV feedback confirmation"
                else:
                    # All conditions met, turn on
                    self._transition_to(self.STATE_ON, now, "demand and conditions met")
                    self._set_boiler_setpoint(self.on_setpoint)
                    # Start min_on_time timer to enforce minimum on duration
                    self._start_timer(self.min_on_timer, self.min_on_time_s)
                    reason = f"Turned ON: {len(rooms_calling_for_heat)} room(s) calling"
            elif has_demand and not interlock_ok:
                self._transition_to(self.STATE_INTERLOCK_BLOCKED, now, "insufficient valve opening")
                reason = f"Interlock blocked: {interlock_reason}"
            else:
                reason = "Off: no demand"
        
        elif self.current_state == self.STATE_PENDING_ON:
            if not has_demand:
                self._transition_to(self.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased while pending"
            elif not interlock_ok:
                self._transition_to(self.STATE_INTERLOCK_BLOCKED, now, "interlock failed")
                reason = f"Interlock blocked: {interlock_reason}"
            elif trv_feedback_ok:
                # TRVs confirmed, turn on
                self._transition_to(self.STATE_ON, now, "TRV feedback confirmed")
                self._set_boiler_setpoint(self.on_setpoint)
                # Start min_on_time timer to enforce minimum on duration
                self._start_timer(self.min_on_timer, self.min_on_time_s)
                reason = f"Turned ON: TRVs confirmed at {total_valve}%"
            else:
                reason = f"Pending ON: waiting for TRV confirmation ({time_in_state:.0f}s)"
        
        elif self.current_state == self.STATE_ON:
            # Save ALL valve positions (not just calling rooms) for pump overrun safety
            if has_demand and all_valve_positions:
                self.last_valve_positions = all_valve_positions.copy()
                log.debug(f"BoilerManager: STATE_ON saved valve positions: {self.last_valve_positions}")
            
            if not has_demand:
                # Demand stopped, enter off-delay period (last_valve_positions already saved above when demand existed)
                log.debug(f"BoilerManager: STATE_ON → PENDING_OFF, preserved valve positions: {self.last_valve_positions}")
                self._transition_to(self.STATE_PENDING_OFF, now, "demand ceased, entering off-delay")
                # Start off-delay timer
                self._start_timer(self.off_delay_timer, self.off_delay_s)
                reason = f"Pending OFF: off-delay ({self.off_delay_s}s) started"
            elif not interlock_ok:
                # Interlock failed while running - turn off immediately
                log.warning("BoilerManager: interlock failed while ON, turning off immediately")
                self._transition_to(self.STATE_PUMP_OVERRUN, now, "interlock failed")
                self._set_boiler_setpoint(self.off_setpoint)
                # Cancel min_on_time timer and start min_off_time + pump_overrun timers
                self._cancel_timer(self.min_on_timer)
                self._start_timer(self.min_off_timer, self.min_off_time_s)
                self._start_timer(self.pump_overrun_timer, self.pump_overrun_s)
                reason = "Turned OFF: interlock failed"
                valves_must_stay_open = True
            else:
                reason = f"ON: heating {len(rooms_calling_for_heat)} room(s), total valve {total_valve}%"
        
        elif self.current_state == self.STATE_PENDING_OFF:
            # CRITICAL: Valves must stay open during pending_off because boiler is still ON
            valves_must_stay_open = True
            # Use last known valve positions instead of current (which would be 0%)
            overridden_valves = self.last_valve_positions.copy()
            log.debug(f"BoilerManager: STATE_PENDING_OFF using saved positions: {overridden_valves}")
            
            if has_demand and interlock_ok:
                # Demand returned during off-delay, return to ON
                self._transition_to(self.STATE_ON, now, "demand returned")
                # Cancel off-delay timer since we're returning to ON
                self._cancel_timer(self.off_delay_timer)
                reason = f"Returned to ON: demand resumed ({len(rooms_calling_for_heat)} room(s))"
            elif not self._is_timer_active(self.off_delay_timer):
                # Off-delay timer completed, check min_on_time
                if not self._check_min_on_time_elapsed():
                    reason = f"Pending OFF: waiting for min_on_time ({self.min_on_time_s}s)"
                else:
                    # Turn off and enter pump overrun
                    self._transition_to(self.STATE_PUMP_OVERRUN, now, "off-delay elapsed, turning off")
                    self._set_boiler_setpoint(self.off_setpoint)  # Command boiler to turn off
                    # Cancel min_on_time timer and start min_off_time + pump_overrun timers
                    self._cancel_timer(self.min_on_timer)
                    self._start_timer(self.min_off_timer, self.min_off_time_s)
                    self._start_timer(self.pump_overrun_timer, self.pump_overrun_s)
                    reason = "Pump overrun: boiler commanded off"
                    valves_must_stay_open = True
            else:
                # Still waiting for off-delay timer
                reason = f"Pending OFF: off-delay timer active"
        
        elif self.current_state == self.STATE_PUMP_OVERRUN:
            valves_must_stay_open = True
            # Use last known valve positions to keep valves open during pump overrun
            overridden_valves = self.last_valve_positions.copy()
            
            if has_demand and interlock_ok and trv_feedback_ok:
                # New demand during pump overrun, can return to ON
                self._transition_to(self.STATE_ON, now, "demand resumed during pump overrun")
                self._set_boiler_setpoint(self.on_setpoint)
                # Cancel pump_overrun timer, restart min_on_time timer
                self._cancel_timer(self.pump_overrun_timer)
                self._start_timer(self.min_on_timer, self.min_on_time_s)
                reason = f"Returned to ON: demand during pump overrun"
                valves_must_stay_open = False
            elif not self._is_timer_active(self.pump_overrun_timer):
                # Pump overrun timer completed
                self._transition_to(self.STATE_OFF, now, "pump overrun complete")
                reason = "Pump overrun complete, now OFF"
                valves_must_stay_open = False
            else:
                # Still in pump overrun
                reason = f"Pump overrun: timer active (valves must stay open)"
        
        elif self.current_state == self.STATE_INTERLOCK_BLOCKED:
            if has_demand and interlock_ok and trv_feedback_ok:
                # Interlock now satisfied
                if not self._check_min_off_time_elapsed():
                    reason = f"Interlock OK but min_off_time not elapsed"
                else:
                    self._transition_to(self.STATE_ON, now, "interlock satisfied")
                    self._set_boiler_setpoint(self.on_setpoint)
                    # Start min_on_time timer
                    self._start_timer(self.min_on_timer, self.min_on_time_s)
                    reason = f"Turned ON: interlock now satisfied"
            elif not has_demand:
                self._transition_to(self.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased"
            else:
                reason = f"Blocked: {interlock_reason}"
        
        # Determine boiler_on flag
        boiler_on = self.current_state in (self.STATE_ON, self.STATE_PENDING_OFF)
        
        return {
            "state": self.current_state,
            "boiler_on": boiler_on,
            "hvac_action": hvac_action,
            "rooms_calling": rooms_calling_for_heat,
            "reason": reason,
            "interlock_ok": interlock_ok,
            "overridden_valve_percents": overridden_valves,
            "total_valve_percent": total_valve,
            "valves_must_stay_open": valves_must_stay_open,
            "time_in_state_s": time_in_state,
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get current boiler status.
        
        Returns:
            Dict with current status
        """
        return {
            "entity": self.boiler_entity,
            "state": self.current_state,
            "opentherm_mode": self.opentherm_mode,
            "min_valve_open_percent": self.min_valve_open_percent,
            "min_on_timer_active": self._is_timer_active(self.min_on_timer),
            "min_off_timer_active": self._is_timer_active(self.min_off_timer),
            "off_delay_timer_active": self._is_timer_active(self.off_delay_timer),
            "pump_overrun_timer_active": self._is_timer_active(self.pump_overrun_timer),
        }
    
    def reload_config(self, boiler_config: Dict) -> None:
        """Reload boiler configuration.
        
        Args:
            boiler_config: New parsed boiler.yaml configuration
        """
        boiler_cfg = boiler_config.get("boiler", {})
        
        # Update entity if changed
        new_entity = boiler_cfg.get("entity_id")
        if new_entity and new_entity != self.boiler_entity:
            log.info(f"BoilerManager: entity changed {self.boiler_entity} -> {new_entity}")
            self.boiler_entity = new_entity
            
            # Exit stub mode if we now have a real entity
            if self.stub_mode:
                self.stub_mode = False
                log.info("BoilerManager: exiting stub mode, now active with real boiler entity")
        
        # Update settings (don't reset state machine)
        self.opentherm_mode = boiler_cfg.get("opentherm", False)
        
        binary_cfg = boiler_cfg.get("binary_control", {})
        self.on_setpoint = binary_cfg.get("on_setpoint_c", constants.BOILER_BINARY_ON_SETPOINT_DEFAULT)
        self.off_setpoint = binary_cfg.get("off_setpoint_c", constants.BOILER_BINARY_OFF_SETPOINT_DEFAULT)
        
        self.pump_overrun_s = boiler_cfg.get("pump_overrun_s", constants.BOILER_PUMP_OVERRUN_DEFAULT)
        
        anti_cycling = boiler_cfg.get("anti_cycling", {})
        self.min_on_time_s = anti_cycling.get("min_on_time_s", constants.BOILER_MIN_ON_TIME_DEFAULT)
        self.min_off_time_s = anti_cycling.get("min_off_time_s", constants.BOILER_MIN_OFF_TIME_DEFAULT)
        self.off_delay_s = anti_cycling.get("off_delay_s", constants.BOILER_OFF_DELAY_DEFAULT)
        
        interlock_cfg = boiler_cfg.get("interlock", {})
        new_min = interlock_cfg.get(
            "min_valve_open_percent",
            constants.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        if new_min != self.min_valve_open_percent:
            log.info(f"BoilerManager: min_valve_open_percent changed {self.min_valve_open_percent}% -> {new_min}%")
            self.min_valve_open_percent = new_min
        
        log.info("BoilerManager: configuration reloaded")


# Module-level instance (initialized by orchestrator)
_boiler_mgr: Optional[BoilerManager] = None


def init(boiler_config: Dict) -> BoilerManager:
    """Initialize the boiler manager module.
    
    Args:
        boiler_config: Parsed boiler.yaml configuration
    
    Returns:
        BoilerManager: Initialized boiler manager instance
    """
    global _boiler_mgr
    
    log.info("BoilerManager: initializing...")
    _boiler_mgr = BoilerManager(boiler_config)
    log.info("BoilerManager: initialization complete")
    
    return _boiler_mgr



def get_status() -> Dict[str, Any]:
    """Get current boiler status.
    
    Returns:
        Dict with current status
    """
    if not _boiler_mgr:
        return {"entity": None, "state": "off"}
    
    return _boiler_mgr.get_status()
