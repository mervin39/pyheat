# -*- coding: utf-8 -*-
"""
trv_controller.py - TRV valve control with feedback confirmation

Responsibilities:
- Control TRV valve positions
- Monitor valve feedback and detect unexpected positions
- Handle TRV setpoint locking (force to 35°C)
- Rate limiting and retry logic
- Non-blocking command execution
"""

from datetime import datetime
from typing import Dict, Optional
import constants as C


class TRVController:
    """Manages TRV valve control with feedback confirmation."""
    
    def __init__(self, ad, config, alert_manager=None):
        """Initialize the TRV controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: Optional AlertManager instance for notifications
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        self.trv_last_commanded = {}  # {room_id: percent}
        self.trv_last_update = {}  # {room_id: timestamp}
        self.unexpected_valve_positions = {}  # {room_id: {actual, expected, detected_at}}
        self._valve_command_state = {}  # {state_key: command_state_dict}
        self._valve_feedback_cache = {}  # {room_id: {'value': int, 'timestamp': datetime}}
        self._cache_ttl_seconds = 5.0  # Cache feedback values for 5 seconds
        
    def get_valve_feedback(self, room_id: str) -> Optional[int]:
        """Get current TRV valve position feedback (0-100%).
        
        Returns the current valve position from the TRV feedback sensor.
        Results are cached for 5 seconds to avoid excessive HA queries.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Current valve position percentage (0-100), or None if unavailable/stale
        """
        now = datetime.now()
        
        # Check cache first
        cached = self._valve_feedback_cache.get(room_id)
        if cached:
            age = (now - cached['timestamp']).total_seconds()
            if age < self._cache_ttl_seconds:
                return cached['value']
        
        # Cache miss or expired - fetch from HA
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return None
            
        fb_entity = room_config['trv']['fb_valve']
        fb_state = self.ad.get_state(fb_entity)
        
        if fb_state in [None, "unknown", "unavailable"]:
            return None
            
        try:
            feedback = int(float(fb_state))
            # Update cache
            self._valve_feedback_cache[room_id] = {
                'value': feedback,
                'timestamp': now
            }
            return feedback
        except (ValueError, TypeError):
            return None
    
    def get_valve_command(self, room_id: str) -> Optional[int]:
        """Get last commanded valve position (0-100%).
        
        Returns the value we last commanded to the TRV, not what the TRV reports.
        This is used for comparing expected vs actual positions.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Last commanded valve position percentage (0-100), or None if never commanded
        """
        return self.trv_last_commanded.get(room_id)
    
    def is_valve_feedback_consistent(self, room_id: str, tolerance: float = 5.0) -> bool:
        """Check if TRV feedback matches last commanded value within tolerance.
        
        Used for TRV health validation during boiler cycling. Compares the current
        feedback against what was LAST COMMANDED, not what we're about to command next.
        
        Args:
            room_id: Room identifier
            tolerance: Maximum allowed difference in percentage points (default: 5%)
            
        Returns:
            True if feedback matches commanded value within tolerance, False otherwise
        """
        feedback = self.get_valve_feedback(room_id)
        commanded = self.get_valve_command(room_id)
        
        if feedback is None or commanded is None:
            return False
            
        return abs(feedback - commanded) <= tolerance
        
    def initialize_from_ha(self) -> None:
        """Initialize TRV state from current Home Assistant valve positions."""
        for room_id, room_cfg in self.config.rooms.items():
            if room_cfg.get('disabled'):
                continue
                
            # Read current valve position from feedback sensor
            fb_entity = room_cfg['trv']['fb_valve']
            try:
                state_str = self.ad.get_state(fb_entity)
                if state_str and state_str not in ['unknown', 'unavailable']:
                    current_percent = int(float(state_str))
                    self.trv_last_commanded[room_id] = current_percent
                    self.ad.log(f"Initialized TRV {room_id} valve = {current_percent}%", level="DEBUG")
            except (ValueError, TypeError) as e:
                self.ad.log(f"Could not initialize TRV {room_id}: {e}", level="WARNING")
                
    def set_valve(self, room_id: str, percent: int, now: datetime, is_correction: bool = False, persistence_active: bool = False) -> None:
        """Set TRV valve position with non-blocking feedback confirmation.
        
        Args:
            room_id: Room identifier
            percent: Desired valve percentage (0-100)
            now: Current datetime
            is_correction: If True, bypass rate limiting and change checks
            persistence_active: If True, valve persistence is active (skip feedback checks)
        """
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return
        
        if not is_correction:
            # Normal flow: check rate limiting
            min_interval = room_config['valve_update']['min_interval_s']
            last_update = self.trv_last_update.get(room_id)
            
            if last_update:
                elapsed = (now - last_update).total_seconds()
                if elapsed < min_interval:
                    self.ad.log(f"TRV {room_id}: Rate limited (elapsed={elapsed:.1f}s < min={min_interval}s)", level="DEBUG")
                    return
            
            # Check if value actually changed
            last_commanded = self.trv_last_commanded.get(room_id)
            if last_commanded == percent:
                return
        else:
            # Correction flow: log the correction
            unexpected = self.unexpected_valve_positions.get(room_id, {})
            self.ad.log(
                f"Correcting unexpected valve position for room '{room_id}': "
                f"actual={unexpected.get('actual')}%, expected={unexpected.get('expected')}%, "
                f"commanding to {percent}%",
                level="INFO"
            )
            if room_id in self.unexpected_valve_positions:
                del self.unexpected_valve_positions[room_id]
        
        last_commanded = self.trv_last_commanded.get(room_id)
        self.ad.log(f"Setting TRV for room '{room_id}': {percent}% open (was {last_commanded}%)")
        
        # Start non-blocking valve command sequence
        self._start_valve_command(room_id, percent, now)
    
    def _start_valve_command(self, room_id: str, percent: int, now: datetime) -> None:
        """Initiate a non-blocking valve command with feedback confirmation."""
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return
        
        state_key = f"valve_cmd_{room_id}"
        
        # Cancel any existing command for this room
        if state_key in self._valve_command_state:
            old_state = self._valve_command_state[state_key]
            if 'handle' in old_state and old_state['handle']:
                self.ad.cancel_timer(old_state['handle'])
        
        # Initialize command state
        self._valve_command_state[state_key] = {
            'room_id': room_id,
            'target_percent': percent,
            'attempt': 0,
            'start_time': now,
            'handle': None,
        }
        
        # Send the command immediately
        self._execute_valve_command(state_key)
    
    def _execute_valve_command(self, state_key: str) -> None:
        """Execute a valve command and schedule feedback check."""
        if state_key not in self._valve_command_state:
            return
        
        state = self._valve_command_state[state_key]
        room_id = state['room_id']
        target_percent = state['target_percent']
        attempt = state['attempt']
        
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            del self._valve_command_state[state_key]
            return
        
        trv = room_config['trv']
        max_retries = C.TRV_COMMAND_MAX_RETRIES
        
        self.ad.log(f"TRV {room_id}: Setting valve to {target_percent}%, attempt {attempt+1}/{max_retries}", level="DEBUG")
        
        try:
            # Send command (only opening_degree, since TRV is locked in "open" mode)
            self.ad.call_service("number/set_value",
                            entity_id=trv['cmd_valve'],
                            value=target_percent)
            
            # Schedule feedback check
            handle = self.ad.run_in(self._check_valve_feedback, 
                               C.TRV_COMMAND_RETRY_INTERVAL_S, 
                               state_key=state_key)
            state['handle'] = handle
            
        except Exception as e:
            self.ad.log(f"TRV {room_id}: Failed to send valve command: {e}", level="ERROR")
            del self._valve_command_state[state_key]
    
    def _check_valve_feedback(self, kwargs) -> None:
        """Callback to check valve feedback after a command."""
        state_key = kwargs.get('state_key')
        if not state_key or state_key not in self._valve_command_state:
            return
        
        state = self._valve_command_state[state_key]
        room_id = state['room_id']
        target_percent = state['target_percent']
        attempt = state['attempt']
        
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            del self._valve_command_state[state_key]
            return
        
        trv = room_config['trv']
        max_retries = C.TRV_COMMAND_MAX_RETRIES
        tolerance = C.TRV_COMMAND_FEEDBACK_TOLERANCE
        
        # Check feedback sensor
        try:
            fb_state = self.ad.get_state(trv['fb_valve'])
            if fb_state and fb_state not in ['unknown', 'unavailable']:
                actual_percent = int(float(fb_state))
                
                if abs(actual_percent - target_percent) <= tolerance:
                    # Success
                    self.ad.log(f"TRV {room_id}: Valve confirmed at {actual_percent}%", level="DEBUG")
                    self.trv_last_commanded[room_id] = target_percent
                    self.trv_last_update[room_id] = datetime.now()
                    del self._valve_command_state[state_key]
                    # Clear any previous TRV alerts for this room
                    if self.alert_manager:
                        from alert_manager import AlertManager
                        self.alert_manager.clear_error(f"{AlertManager.ALERT_TRV_FEEDBACK_TIMEOUT}_{room_id}")
                        self.alert_manager.clear_error(f"{AlertManager.ALERT_TRV_UNAVAILABLE}_{room_id}")
                    return
                else:
                    # Mismatch
                    if attempt + 1 < max_retries:
                        # Retry
                        self.ad.log(f"TRV {room_id}: Feedback mismatch (actual={actual_percent}%, target={target_percent}%), retrying", level="WARNING")
                        state['attempt'] = attempt + 1
                        self._execute_valve_command(state_key)
                    else:
                        # Max retries reached
                        self.ad.log(f"TRV {room_id}: Max retries reached, actual={actual_percent}%, target={target_percent}%", level="ERROR")
                        # Still update our tracking to actual value
                        self.trv_last_commanded[room_id] = actual_percent
                        self.trv_last_update[room_id] = datetime.now()
                        del self._valve_command_state[state_key]
                        # Report critical alert for TRV feedback timeout
                        if self.alert_manager:
                            from alert_manager import AlertManager
                            self.alert_manager.report_error(
                                f"{AlertManager.ALERT_TRV_FEEDBACK_TIMEOUT}_{room_id}",
                                AlertManager.SEVERITY_CRITICAL,
                                f"TRV valve feedback mismatch after multiple retries.\n\n"
                                f"**Commanded:** {target_percent}%\n"
                                f"**Actual:** {actual_percent}%\n\n"
                                f"Check TRV batteries, connection, or mechanical issues.",
                                room_id=room_id,
                                auto_clear=True
                            )
            else:
                # Feedback unavailable, retry if attempts remain
                if attempt + 1 < max_retries:
                    self.ad.log(f"TRV {room_id}: Feedback unavailable, retrying", level="WARNING")
                    state['attempt'] = attempt + 1
                    self._execute_valve_command(state_key)
                else:
                    self.ad.log(f"TRV {room_id}: Max retries reached, feedback unavailable", level="ERROR")
                    del self._valve_command_state[state_key]
                    # Report critical alert for TRV unavailable
                    if self.alert_manager:
                        from alert_manager import AlertManager
                        self.alert_manager.report_error(
                            f"{AlertManager.ALERT_TRV_UNAVAILABLE}_{room_id}",
                            AlertManager.SEVERITY_CRITICAL,
                            f"TRV feedback sensor unavailable after multiple retries.\n\n"
                            f"Lost communication with TRV. Check TRV connectivity and batteries.",
                            room_id=room_id,
                            auto_clear=True
                        )
                    
        except Exception as e:
            self.ad.log(f"TRV {room_id}: Error checking feedback: {e}", level="ERROR")
            del self._valve_command_state[state_key]
    
    def check_feedback_for_unexpected_position(self, room_id: str, feedback_percent: int, now: datetime, persistence_active: bool = False) -> None:
        """Check if TRV feedback matches expected position and trigger correction if needed.
        
        Args:
            room_id: Room identifier
            feedback_percent: Current valve position from feedback sensor
            now: Current datetime
            persistence_active: If True, valve persistence is active (skip feedback checks)
        """
        # CRITICAL: During valve persistence, feedback changes are expected as valves
        # are forcibly held open. Don't trigger corrections during persistence to avoid
        # fighting with the persistence logic.
        if persistence_active:
            self.ad.log(f"TRV feedback ignored for '{room_id}' (valve persistence active)", level="DEBUG")
            return
        
        # Check if there's an active valve command in progress
        state_key = f"valve_cmd_{room_id}"
        if state_key in self._valve_command_state:
            self.ad.log(f"TRV feedback for '{room_id}' ignored - valve command in progress", level="DEBUG")
            return
        
        # Compare feedback to expected
        expected_percent = self.trv_last_commanded.get(room_id)
        if expected_percent is None:
            return
        
        tolerance = C.TRV_COMMAND_FEEDBACK_TOLERANCE
        if abs(feedback_percent - expected_percent) > tolerance:
            # Unexpected position detected!
            self.ad.log(
                f"WARNING: Unexpected valve position for room '{room_id}': "
                f"feedback={feedback_percent}%, expected={expected_percent}%. Triggering correction.",
                level="WARNING"
            )
            self.unexpected_valve_positions[room_id] = {
                'actual': feedback_percent,
                'expected': expected_percent,
                'detected_at': now,
            }
    
    def lock_all_setpoints(self) -> None:
        """Lock all TRV setpoints to maximum (35C) to force valves into open mode."""
        self.ad.log("Locking all TRV setpoints to 35C (open mode)")
        for room_id in self.config.rooms.keys():
            self.lock_setpoint(room_id)
    
    def lock_setpoint(self, room_id: str) -> None:
        """Lock a single TRV's setpoint to maximum (35C).
        
        Args:
            room_id: Room identifier
        """
        room_config = self.config.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return
        
        trv = room_config['trv']
        climate_entity = trv['climate']
        
        try:
            # Get current setpoint
            current_state = self.ad.get_state(climate_entity, attribute='all')
            if current_state and 'attributes' in current_state:
                current_temp = current_state['attributes'].get('temperature')
                
                if current_temp != C.TRV_LOCKED_SETPOINT_C:
                    self.ad.log(f"Locking TRV setpoint for '{room_id}': {current_temp}C -> {C.TRV_LOCKED_SETPOINT_C}C")
                    self.ad.call_service('climate/set_temperature',
                                    entity_id=climate_entity,
                                    temperature=C.TRV_LOCKED_SETPOINT_C)
                else:
                    self.ad.log(f"TRV setpoint for '{room_id}' already locked at {C.TRV_LOCKED_SETPOINT_C}C", level="DEBUG")
        except Exception as e:
            self.ad.log(f"Failed to lock TRV setpoint for '{room_id}': {e}", level="ERROR")
    
    def check_all_setpoints(self) -> None:
        """Check all TRV setpoints and relock if needed."""
        for room_id in self.config.rooms.keys():
            self.lock_setpoint(room_id)
