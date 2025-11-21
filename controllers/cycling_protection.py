# -*- coding: utf-8 -*-
"""
cycling_protection.py - Short-cycling protection via return temperature monitoring

Responsibilities:
- Monitor flame OFF events via binary_sensor.opentherm_flame
- Distinguish DHW interruptions from CH shutdowns (100% accurate flag check)
- Detect high return temperature risk conditions
- Drop boiler setpoint to 30°C during cooldown period
- Monitor return temp and restore setpoint when safe
- Track cooldown history for excessive cycling alerts
- Persist state across AppDaemon restarts
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import json
import constants as C


class CyclingProtection:
    """Manages short-cycling protection via proactive cooldown detection.
    
    State Machine:
    - NORMAL: No cooldown active, monitoring flame status
    - COOLDOWN: Setpoint dropped to 30°C, monitoring return temp for recovery
    - TIMEOUT: Forced recovery after 30 minutes (alerts user)
    """
    
    # State constants
    STATE_NORMAL = "NORMAL"
    STATE_COOLDOWN = "COOLDOWN"
    STATE_TIMEOUT = "TIMEOUT"
    
    def __init__(self, ad, config, alert_manager=None):
        """Initialize cycling protection.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: Optional AlertManager instance for notifications
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        
        # State machine state
        self.state = self.STATE_NORMAL
        self.cooldown_entry_time: Optional[datetime] = None
        self.saved_setpoint: Optional[float] = None
        
        # Cooldown history for excessive cycling detection
        # List of tuples: (timestamp, return_temp, setpoint)
        self.cooldown_history: List[Tuple[datetime, float, float]] = []
        
        # Recovery monitoring handle
        self.recovery_handle = None
        
    def initialize_from_ha(self) -> None:
        """Restore state from Home Assistant persistence entity."""
        try:
            state_str = self.ad.get_state(C.HELPER_CYCLING_STATE)
            if state_str and state_str not in ['unknown', 'unavailable', '']:
                state_dict = json.loads(state_str)
                self.state = state_dict.get('mode', self.STATE_NORMAL)
                self.saved_setpoint = state_dict.get('saved_setpoint')
                
                cooldown_start_str = state_dict.get('cooldown_start')
                if cooldown_start_str:
                    self.cooldown_entry_time = datetime.fromisoformat(cooldown_start_str)
                    
                # If in cooldown, resume monitoring
                if self.state == self.STATE_COOLDOWN:
                    self.ad.log("Restored COOLDOWN state from persistence - resuming monitoring", level="INFO")
                    self._resume_cooldown_monitoring()
                    
        except (json.JSONDecodeError, ValueError) as e:
            self.ad.log(f"Failed to restore cycling protection state: {e}", level="WARNING")
            # Default to NORMAL on parse error
            self._reset_to_normal()
            
    def on_flame_off(self, entity, attribute, old, new, kwargs):
        """Flame went OFF - schedule delayed check.
        
        Called when binary_sensor.opentherm_flame changes to 'off'.
        Schedules evaluation after 2-second delay to allow sensors to stabilize.
        """
        if new == 'off' and old == 'on':
            self.ad.log("🔥 Flame OFF detected - scheduling cooldown evaluation", level="DEBUG")
            # Schedule check after sensor stabilization delay
            self.ad.run_in(self._evaluate_cooldown_need, C.CYCLING_SENSOR_DELAY_S)
            
    def _evaluate_cooldown_need(self, kwargs):
        """Delayed check after flame OFF - sensors have had time to update.
        
        Evaluates whether cooldown is needed based on:
        1. DHW status (ignore if DHW active)
        2. Return temperature vs setpoint
        """
        # FIRST: Check if this is a DHW interruption
        dhw_state = self.ad.get_state(C.OPENTHERM_DHW)
        if dhw_state == 'on':
            self.ad.log("Flame OFF: DHW active - ignoring (not a CH shutdown)", level="DEBUG")
            return
        
        # DHW is off - this is a genuine CH shutdown
        # Check if return temp is dangerously high
        return_temp = self._get_return_temp()
        setpoint = self._get_current_setpoint()
        
        if return_temp is None or setpoint is None:
            self.ad.log("Cannot evaluate cooldown: missing return temp or setpoint", level="WARNING")
            return
        
        delta = setpoint - return_temp
        threshold = setpoint - C.CYCLING_HIGH_RETURN_DELTA_C
        
        self.ad.log(
            f"🔥 Flame OFF | DHW: {dhw_state} | "
            f"Return: {return_temp:.1f}°C | Setpoint: {setpoint:.1f}°C | "
            f"Delta: {delta:.1f}°C",
            level="INFO"
        )
        
        if return_temp >= threshold:
            # High return temp detected - enter cooldown
            self._enter_cooldown(setpoint)
        else:
            # Normal conditions - no cooldown needed
            self.ad.log(
                f"Flame OFF: Normal conditions (return {return_temp:.1f}°C, "
                f"threshold {threshold:.1f}°C) - no cooldown needed",
                level="DEBUG"
            )
            
    def _enter_cooldown(self, original_setpoint: float):
        """Enter cooldown - drop setpoint to minimum.
        
        Args:
            original_setpoint: Current setpoint to save and restore later
        """
        now = datetime.now()
        return_temp = self._get_return_temp()
        threshold = original_setpoint - C.CYCLING_HIGH_RETURN_DELTA_C
        
        # Save state
        self.state = self.STATE_COOLDOWN
        self.saved_setpoint = original_setpoint
        self.cooldown_entry_time = now
        
        # Add to history for excessive cycling detection
        self.cooldown_history.append((now, return_temp, original_setpoint))
        
        # Check for excessive cycling
        recent_cooldowns = [
            entry for entry in self.cooldown_history
            if (now - entry[0]).total_seconds() < C.CYCLING_EXCESSIVE_WINDOW_S
        ]
        
        if len(recent_cooldowns) >= C.CYCLING_EXCESSIVE_COUNT:
            self.ad.log(
                f"⚠️ EXCESSIVE CYCLING: {len(recent_cooldowns)} cooldowns in "
                f"{C.CYCLING_EXCESSIVE_WINDOW_S/60:.0f} minutes!",
                level="WARNING"
            )
            
            # Alert user
            if self.alert_manager:
                cooldown_details = "\n".join([
                    f"- {entry[0].strftime('%H:%M:%S')}: Return {entry[1]:.1f}°C, Setpoint {entry[2]:.1f}°C"
                    for entry in recent_cooldowns
                ])
                
                self.alert_manager.report_error(
                    self.alert_manager.ALERT_CYCLING_PROTECTION_EXCESSIVE,
                    self.alert_manager.SEVERITY_WARNING,
                    f"Excessive short-cycling detected!\n\n"
                    f"**{len(recent_cooldowns)} cooldowns in {C.CYCLING_EXCESSIVE_WINDOW_S/60:.0f} minutes:**\n"
                    f"{cooldown_details}\n\n"
                    f"This suggests cooldown isn't solving the root cause. "
                    f"Consider:\n"
                    f"- Increasing recovery delta (currently {C.CYCLING_RECOVERY_DELTA_C}°C)\n"
                    f"- Lowering flow temperature setpoint\n"
                    f"- Checking if only 1 room is calling for heat\n\n"
                    f"System will continue trying to protect the boiler.",
                    auto_clear=True
                )
        
        # Drop setpoint to cooldown temperature
        self._set_setpoint(C.CYCLING_COOLDOWN_SETPOINT)
        
        # Save state to persistence
        self._save_state()
        
        # Log cooldown entry
        self.ad.log(
            f"❄️ COOLDOWN STARTED | "
            f"Return: {return_temp:.1f}°C >= Threshold: {threshold:.1f}°C | "
            f"Saved setpoint: {original_setpoint:.1f}°C → New: {C.CYCLING_COOLDOWN_SETPOINT}°C",
            level="WARNING"
        )
        
        # Start recovery monitoring
        self._start_recovery_monitoring()
        
    def _start_recovery_monitoring(self):
        """Start periodic recovery temperature monitoring."""
        self.recovery_handle = self.ad.run_in(
            self._check_recovery,
            C.CYCLING_RECOVERY_MONITORING_INTERVAL_S
        )
        
    def _resume_cooldown_monitoring(self):
        """Resume cooldown monitoring after AppDaemon restart."""
        if self.state == self.STATE_COOLDOWN:
            self._start_recovery_monitoring()
            
    def _check_recovery(self, kwargs):
        """Monitor return temp and restore setpoint when cool enough.
        
        Called periodically (every 10s) during cooldown to check if
        recovery threshold has been reached.
        """
        if self.state != self.STATE_COOLDOWN:
            return
        
        now = datetime.now()
        return_temp = self._get_return_temp()
        recovery_threshold = self._get_recovery_threshold()
        
        if return_temp is None:
            self.ad.log("Cannot check recovery: missing return temp", level="WARNING")
            # Try again in 10 seconds
            self.recovery_handle = self.ad.run_in(
                self._check_recovery,
                C.CYCLING_RECOVERY_MONITORING_INTERVAL_S
            )
            return
        
        # Calculate time in cooldown
        time_in_cooldown = (now - self.cooldown_entry_time).total_seconds()
        
        # Check for timeout
        if time_in_cooldown > C.CYCLING_COOLDOWN_MAX_DURATION_S:
            self.ad.log(
                f"🚨 COOLDOWN TIMEOUT: Stuck in cooldown for {int(time_in_cooldown/60)} minutes! "
                f"Return temp: {return_temp:.1f}°C, "
                f"Target: {recovery_threshold:.1f}°C",
                level="ERROR"
            )
            
            # Alert user via notification
            if self.alert_manager:
                self.alert_manager.report_error(
                    self.alert_manager.ALERT_CYCLING_PROTECTION_TIMEOUT,
                    self.alert_manager.SEVERITY_WARNING,
                    f"Cycling protection stuck in cooldown for {int(time_in_cooldown/60)} minutes.\n\n"
                    f"**Current:** {return_temp:.1f}°C return temp\n"
                    f"**Target:** {recovery_threshold:.1f}°C\n"
                    f"**Action:** Forcing recovery and restoring setpoint.\n\n"
                    f"This may indicate recovery threshold needs adjustment.",
                    auto_clear=True
                )
            
            # Force exit with timeout state
            self.state = self.STATE_TIMEOUT
            self._exit_cooldown()
            return
        
        # Log progress
        self.ad.log(
            f"Cooldown check: {return_temp:.1f}°C (target: {recovery_threshold:.1f}°C) "
            f"[{int(time_in_cooldown)}s elapsed]",
            level="DEBUG"
        )
        
        # Check if recovery threshold reached
        if return_temp <= recovery_threshold:
            self.ad.log(
                f"✅ Recovery threshold reached: {return_temp:.1f}°C <= {recovery_threshold:.1f}°C "
                f"(cooldown duration: {int(time_in_cooldown/60)}m {int(time_in_cooldown%60)}s)",
                level="INFO"
            )
            self._exit_cooldown()
        else:
            # Still cooling - check again in 10 seconds
            self.recovery_handle = self.ad.run_in(
                self._check_recovery,
                C.CYCLING_RECOVERY_MONITORING_INTERVAL_S
            )
            
    def _exit_cooldown(self):
        """Exit cooldown - restore saved setpoint."""
        if self.saved_setpoint is None:
            self.ad.log("Cannot exit cooldown: no saved setpoint!", level="ERROR")
            self._reset_to_normal()
            return
        
        # Calculate duration
        duration = 0
        if self.cooldown_entry_time:
            duration = (datetime.now() - self.cooldown_entry_time).total_seconds()
        
        # Restore setpoint
        self._set_setpoint(self.saved_setpoint)
        
        # Log exit
        return_temp = self._get_return_temp()
        self.ad.log(
            f"✅ COOLDOWN ENDED | "
            f"Duration: {int(duration)}s | Return: {return_temp:.1f}°C | "
            f"Restored setpoint: {self.saved_setpoint:.1f}°C",
            level="INFO"
        )
        
        # Clear state
        self._reset_to_normal()
        
    def _reset_to_normal(self):
        """Reset to normal state and clear all cooldown tracking."""
        self.state = self.STATE_NORMAL
        self.saved_setpoint = None
        self.cooldown_entry_time = None
        
        # Cancel recovery monitoring if active
        if self.recovery_handle:
            try:
                self.ad.cancel_timer(self.recovery_handle)
            except:
                pass
            self.recovery_handle = None
        
        # Save cleared state
        self._save_state()
        
    def _get_return_temp(self) -> Optional[float]:
        """Get current return temperature from OpenTherm sensor.
        
        Returns:
            Return temperature in °C, or None if unavailable
        """
        return_temp_str = self.ad.get_state(C.OPENTHERM_HEATING_RETURN_TEMP)
        if return_temp_str in ['unknown', 'unavailable', None]:
            return None
        try:
            return float(return_temp_str)
        except (ValueError, TypeError):
            return None
            
    def _get_current_setpoint(self) -> Optional[float]:
        """Get current boiler setpoint from climate entity.
        
        Returns:
            Current setpoint in °C, or None if unavailable
        """
        # Read temperature attribute from climate entity
        setpoint = self.ad.get_state(C.OPENTHERM_CLIMATE, attribute='temperature')
        if setpoint in ['unknown', 'unavailable', None]:
            # Fallback: try reading from helper entity
            setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
            
        if setpoint in ['unknown', 'unavailable', None]:
            return None
        try:
            return float(setpoint)
        except (ValueError, TypeError):
            return None
            
    def _set_setpoint(self, temperature: float):
        """Set boiler flow temperature setpoint via climate service.
        
        Args:
            temperature: Target setpoint in °C (30-80°C)
        """
        self.ad.call_service(
            'climate/set_temperature',
            entity_id=C.OPENTHERM_CLIMATE,
            temperature=temperature
        )
        
    def _get_recovery_threshold(self) -> float:
        """Calculate recovery temperature threshold.
        
        Formula: recovery_temp = max(saved_setpoint - DELTA, MIN)
        
        Returns:
            Recovery threshold in °C
        """
        if self.saved_setpoint is None:
            return C.CYCLING_RECOVERY_MIN_C
        
        recovery_temp = self.saved_setpoint - C.CYCLING_RECOVERY_DELTA_C
        recovery_temp = max(recovery_temp, C.CYCLING_RECOVERY_MIN_C)
        return recovery_temp
        
    def _save_state(self):
        """Persist state to Home Assistant helper entity."""
        state_dict = {
            'mode': self.state,
            'saved_setpoint': self.saved_setpoint,
            'cooldown_start': self.cooldown_entry_time.isoformat() if self.cooldown_entry_time else None
        }
        
        try:
            self.ad.set_state(
                C.HELPER_CYCLING_STATE,
                state=json.dumps(state_dict)
            )
        except Exception as e:
            self.ad.log(f"Failed to save cycling protection state: {e}", level="ERROR")
            
    def get_state_dict(self) -> Dict:
        """Get current state as dict for logging and status publishing.
        
        Returns:
            Dict with state, cooldown_count, saved_setpoint, recovery_threshold
        """
        # Count recent cooldowns (last hour)
        now = datetime.now()
        cooldown_count = len([
            entry for entry in self.cooldown_history
            if (now - entry[0]).total_seconds() < 3600
        ])
        
        return {
            'state': self.state,
            'cooldown_count': cooldown_count,
            'saved_setpoint': self.saved_setpoint if self.saved_setpoint else '',
            'recovery_threshold': self._get_recovery_threshold() if self.state == self.STATE_COOLDOWN else ''
        }
