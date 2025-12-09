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
from collections import deque
import constants as C
from persistence import PersistenceManager


def _ensure_cooldowns_sensor(ad, persistence: 'PersistenceManager') -> None:
    """Ensure the cooldowns counter sensor exists in Home Assistant.
    
    Creates sensor.pyheat_cooldowns if it doesn't exist or is unavailable.
    Uses maximum value between HA sensor and persisted count to ensure
    strictly increasing behavior (survives HA restarts/unavailability).
    
    Args:
        ad: AppDaemon API reference
        persistence: PersistenceManager instance for reading/writing persisted count
    """
    # Get persisted count
    persisted_count = persistence.get_cooldowns_count()
    
    # Check if sensor exists in HA
    current_state = ad.get_state(C.COOLDOWNS_ENTITY)
    ad.log(f"Checking cooldowns sensor: current_state={current_state}, persisted={persisted_count}", level="DEBUG")
    
    # Parse HA sensor value if valid
    ha_count = None
    if current_state not in [None, 'unknown', 'unavailable']:
        try:
            ha_count = int(float(current_state))
        except (ValueError, TypeError):
            ad.log(f"Invalid cooldowns sensor state: {current_state}", level="WARNING")
    
    # Determine authoritative count: max(HA, persisted)
    # This ensures strictly increasing behavior even if HA resets
    if ha_count is not None:
        authoritative_count = max(ha_count, persisted_count)
        if ha_count > persisted_count:
            # HA has higher value - update persistence
            ad.log(f"HA sensor has higher count ({ha_count} > {persisted_count}), updating persistence", level="INFO")
            persistence.update_cooldowns_count(ha_count)
        elif persisted_count > ha_count:
            # Persisted has higher value - update HA
            ad.log(f"Persistence has higher count ({persisted_count} > {ha_count}), updating HA sensor", level="INFO")
            ad.set_state(
                C.COOLDOWNS_ENTITY,
                state=str(persisted_count),
                attributes={
                    'friendly_name': 'PyHeat Cooldowns',
                    'state_class': 'total_increasing',
                    'icon': 'mdi:snowflake-thermometer'
                }
            )
        else:
            # Values match - all good
            ad.log(f"Cooldowns sensor matches persistence ({ha_count})", level="DEBUG")
    else:
        # HA sensor doesn't exist or unavailable - create with persisted count
        ad.log(f"Creating {C.COOLDOWNS_ENTITY} sensor with persisted count {persisted_count}...", level="INFO")
        ad.set_state(
            C.COOLDOWNS_ENTITY,
            state=str(persisted_count),
            attributes={
                'friendly_name': 'PyHeat Cooldowns',
                'state_class': 'total_increasing',
                'icon': 'mdi:snowflake-thermometer'
            }
        )
        ad.log(f"Created {C.COOLDOWNS_ENTITY} sensor", level="INFO")


def _increment_cooldowns_sensor(ad, persistence: 'PersistenceManager') -> None:
    """Increment the cooldowns counter sensor by 1.
    
    Updates both HA sensor and persisted count to ensure durability.
    Uses max(HA, persisted) + 1 to handle any inconsistencies.
    
    Args:
        ad: AppDaemon API reference
        persistence: PersistenceManager instance for reading/writing persisted count
    """
    try:
        # Get both values
        persisted_count = persistence.get_cooldowns_count()
        current_state = ad.get_state(C.COOLDOWNS_ENTITY)
        
        # Parse HA sensor value if valid
        ha_count = None
        if current_state not in [None, 'unknown', 'unavailable']:
            try:
                ha_count = int(float(current_state))
            except (ValueError, TypeError):
                ad.log(f"Invalid cooldowns sensor state during increment: {current_state}", level="WARNING")
        
        # Calculate new count: max(HA, persisted) + 1
        if ha_count is not None:
            new_count = max(ha_count, persisted_count) + 1
        else:
            new_count = persisted_count + 1
        
        # Update both HA and persistence
        ad.set_state(
            C.COOLDOWNS_ENTITY,
            state=str(new_count),
            attributes={
                'friendly_name': 'PyHeat Cooldowns',
                'state_class': 'total_increasing',
                'icon': 'mdi:snowflake-thermometer'
            }
        )
        persistence.update_cooldowns_count(new_count)
        
        ad.log(f"Incremented cooldowns: {persisted_count} -> {new_count} (HA was {ha_count})", level="DEBUG")
        
    except Exception as e:
        ad.log(f"Error incrementing cooldowns sensor: {e}", level="WARNING")


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
    
    def __init__(self, ad, config, alert_manager=None, boiler_controller=None):
        """Initialize cycling protection.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: Optional AlertManager instance for notifications
            boiler_controller: Optional BoilerController instance for state checks
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        self.boiler_controller = boiler_controller
        self.persistence = PersistenceManager(C.PERSISTENCE_FILE)
        
        # State machine state
        self.state = self.STATE_NORMAL
        self.cooldown_entry_time: Optional[datetime] = None
        self.saved_setpoint: Optional[float] = None
        
        # Cooldown history for excessive cycling detection
        # List of tuples: (timestamp, return_temp, setpoint)
        self.cooldown_history: List[Tuple[datetime, float, float]] = []
        
        # DHW history tracking for improved detection
        # Circular buffers storing (timestamp, state) tuples for last 5 seconds
        self.dhw_history_binary = deque(maxlen=100)
        self.dhw_history_flow = deque(maxlen=100)
        
        # Recovery monitoring handle
        self.recovery_handle = None
        
        # Track if cooldowns sensor has been initialized
        self._cooldowns_sensor_initialized = False
        
    def initialize_from_ha(self) -> None:
        """Restore state from persistence file."""
        # NOTE: Cooldowns sensor is initialized later via ensure_cooldowns_sensor()
        # to avoid HA API errors during app startup
        
        try:
            state_dict = self.persistence.get_cycling_protection_state()
            
            self.state = state_dict.get('mode', self.STATE_NORMAL)
            self.saved_setpoint = state_dict.get('saved_setpoint')
            
            cooldown_start_str = state_dict.get('cooldown_start')
            if cooldown_start_str:
                self.cooldown_entry_time = datetime.fromisoformat(cooldown_start_str)
                
            # If in cooldown, resume monitoring
            if self.state == self.STATE_COOLDOWN:
                self.ad.log("Restored COOLDOWN state from persistence - resuming monitoring", level="INFO")
                self._resume_cooldown_monitoring()
                
        except Exception as e:
            self.ad.log(f"Failed to restore cycling protection state: {e}", level="WARNING")
            # Default to NORMAL on error
            self._reset_to_normal()
    
    def ensure_cooldowns_sensor(self) -> None:
        """Ensure the cooldowns counter sensor exists in Home Assistant.
        
        Should be called after app initialization is complete (e.g., from first recompute)
        to avoid HA API errors during startup.
        """
        if self._cooldowns_sensor_initialized:
            return
        
        try:
            _ensure_cooldowns_sensor(self.ad, self.persistence)
            self._cooldowns_sensor_initialized = True
        except Exception as e:
            self.ad.log(f"Failed to ensure cooldowns sensor: {e}", level="ERROR")
    
    def on_dhw_state_change(self, entity, attribute, old, new, kwargs):
        """Track DHW sensor state changes for history-based detection.
        
        Called whenever DHW binary sensor or flow rate sensor changes.
        Maintains circular buffer of recent states for backward-looking checks.
        
        Args:
            entity: Entity ID that changed (binary sensor or flow rate)
            attribute: Attribute that changed (usually None for state changes)
            old: Previous state value
            new: New state value
            kwargs: Additional callback parameters
        """
        timestamp = datetime.now()
        
        # Append to appropriate history buffer
        if entity == C.OPENTHERM_DHW:
            self.dhw_history_binary.append((timestamp, new))
        elif entity == C.OPENTHERM_DHW_FLOW_RATE:
            self.dhw_history_flow.append((timestamp, new))
        
        # Debug logging for significant changes
        if new == 'on' or (old in ['off', '0', '0.0'] and new not in ['off', '0', '0.0']):
            self.ad.log(
                f"DHW state change: {entity.split('.')[-1]}={new} (tracking in history buffer)",
                level="DEBUG"
            )
            
    def on_flame_off(self, entity, attribute, old, new, kwargs):
        """Flame went OFF - capture DHW state and schedule delayed check.
        
        Called when binary_sensor.opentherm_flame changes to 'off'.
        Schedules evaluation after 2-second delay to allow sensors to stabilize.
        
        Critical: Captures BOTH DHW sensors NOW (at flame OFF time) to avoid
        missing fast DHW events that turn off before the delayed evaluation runs.
        Triple-check strategy uses both binary sensor and flow rate for redundancy.
        """
        if new == 'off' and old == 'on':
            # GUARD: Don't re-evaluate cooldown if already in cooldown
            # This prevents double-triggering when flame briefly turns on during cooldown
            # (e.g., due to pump overrun) and then turns off again
            if self.state == self.STATE_COOLDOWN:
                self.ad.log(
                    f"Flame OFF detected during cooldown - ignoring "
                    f"(already in cooldown state)",
                    level="DEBUG"
                )
                return
            
            # GUARD: Skip evaluation if this is an intentional shutdown by state machine
            # Cycling protection is designed to detect automatic boiler safety shutdowns
            # (overheat), not intentional shutdowns when no rooms are calling for heat
            if self.boiler_controller:
                boiler_state = self.boiler_controller.boiler_state
                if boiler_state in [C.STATE_PENDING_OFF, C.STATE_PUMP_OVERRUN]:
                    self.ad.log(
                        f"Flame OFF: Intentional shutdown by state machine "
                        f"(state={boiler_state}) - skipping cooldown evaluation",
                        level="DEBUG"
                    )
                    return
            
            # Capture BOTH DHW sensors at flame OFF time (before delay)
            dhw_binary_at_flame_off = self.ad.get_state(C.OPENTHERM_DHW)
            dhw_flow_at_flame_off = self.ad.get_state(C.OPENTHERM_DHW_FLOW_RATE)
            
            self.ad.log(
                f"Flame OFF detected | DHW binary: {dhw_binary_at_flame_off}, "
                f"flow: {dhw_flow_at_flame_off} - scheduling cooldown evaluation",
                level="DEBUG"
            )
            
            # Schedule check after sensor stabilization delay
            # Pass both captured DHW states to evaluation
            self.ad.run_in(
                self._evaluate_cooldown_need,
                C.CYCLING_SENSOR_DELAY_S,
                dhw_binary_at_flame_off=dhw_binary_at_flame_off,
                dhw_flow_at_flame_off=dhw_flow_at_flame_off
            )
            
    def on_setpoint_changed(self, entity, attribute, old, new, kwargs):
        """User changed desired setpoint via input_number helper.
        
        Behavior:
        - NORMAL/TIMEOUT: Apply immediately to climate entity
        - COOLDOWN: Store in saved_setpoint (defer until recovery)
        """
        try:
            new_setpoint = float(new)
        except (ValueError, TypeError):
            self.ad.log(f"Invalid setpoint value: {new}", level="WARNING")
            return
            
        if self.state == self.STATE_COOLDOWN:
            # Defer application until cooldown ends
            self.ad.log(
                f"User changed setpoint to {new_setpoint:.1f}C during cooldown - "
                f"will apply after recovery (currently at {C.CYCLING_COOLDOWN_SETPOINT}C)",
                level="INFO"
            )
            self.saved_setpoint = new_setpoint
            self._save_state()
        else:
            # Apply immediately
            self.ad.log(
                f"Applying user setpoint change: {old}C -> {new_setpoint:.1f}C",
                level="INFO"
            )
            self._set_setpoint(new_setpoint)
            
    def sync_setpoint_on_startup(self):
        """Sync climate entity to helper value on startup (unless in cooldown).
        
        Should be called after initialize_from_ha() during app initialization.
        Ensures climate entity matches user's desired setpoint from helper.
        """
        if self.state == self.STATE_COOLDOWN:
            # Already at 30°C protecting - don't interfere
            self.ad.log(
                "Startup: In COOLDOWN state - skipping setpoint sync",
                level="INFO"
            )
            return
            
        # Read desired setpoint from helper
        helper_setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
        if helper_setpoint in ['unknown', 'unavailable', None]:
            self.ad.log(
                "Startup: Cannot sync setpoint - helper unavailable",
                level="WARNING"
            )
            return
            
        try:
            desired_setpoint = float(helper_setpoint)
        except (ValueError, TypeError):
            self.ad.log(
                f"Startup: Invalid helper setpoint value: {helper_setpoint}",
                level="WARNING"
            )
            return
            
        # Apply to climate entity
        self.ad.log(
            f"Startup: Syncing climate entity to helper setpoint: {desired_setpoint:.1f}C",
            level="INFO"
        )
        self._set_setpoint(desired_setpoint)
        
    def validate_setpoint_vs_helper(self):
        """Periodic validation: ensure climate setpoint matches helper (unless in cooldown).
        
        Called every 60 seconds from periodic recompute to detect and correct setpoint drift.
        Skips validation when in COOLDOWN state to avoid interfering with protection logic.
        """
        # Don't interfere if we're actively protecting
        if self.state == self.STATE_COOLDOWN:
            return
            
        # Read helper setpoint (user's desired value)
        helper_setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
        if helper_setpoint in ['unknown', 'unavailable', None]:
            return
            
        try:
            desired_setpoint = float(helper_setpoint)
        except (ValueError, TypeError):
            return
            
        # Read actual climate entity setpoint
        actual_setpoint = self._get_current_setpoint()
        if actual_setpoint is None:
            return
            
        # Check for mismatch (allow 0.5°C tolerance for rounding)
        if abs(actual_setpoint - desired_setpoint) > 0.5:
            self.ad.log(
                f"WARNING: Setpoint drift detected: helper={desired_setpoint:.1f}C, "
                f"actual={actual_setpoint:.1f}C - correcting to match helper",
                level="WARNING"
            )
            self._set_setpoint(desired_setpoint)
    
    def _dhw_was_recently_active(self, lookback_seconds: int = 12) -> bool:
        """Check if DHW was active in recent history (backward-looking check).

        This catches the race condition where tap closes just before flame OFF.
        By the time flame OFF event fires, DHW sensors may already show 'off',
        but the history buffer will still contain the 'on' states.

        Args:
            lookback_seconds: How far back to check history (default: 12s)

        Returns:
            True if DHW was active within lookback window, False otherwise
        """
        cutoff = datetime.now() - timedelta(seconds=lookback_seconds)
        
        # Check binary sensor history
        binary_active = any(
            state == 'on' and timestamp >= cutoff
            for timestamp, state in self.dhw_history_binary
        )
        
        # Check flow sensor history
        flow_active = any(
            state not in ['off', '0', '0.0', None, 'unknown', 'unavailable'] 
            and timestamp >= cutoff
            for timestamp, state in self.dhw_history_flow
            if state  # Skip None/empty values
        )
        
        return binary_active or flow_active
            
    def _evaluate_cooldown_need(self, kwargs):
        """Delayed check after flame OFF - sensors have had time to update.
        
        Evaluates whether cooldown is needed based on:
        1. DHW status (ignore if DHW active) - TRIPLE-CHECK strategy:
           - Check both binary and flow rate sensors captured at flame OFF time
           - Also check both sensors again after 2s delay
           - DHW is active if EITHER sensor shows activity at EITHER time
        2. Return temperature vs setpoint
        """
        # FIRST: Check if this is a DHW interruption using triple-check strategy
        # Retrieve captured states from flame OFF time
        dhw_binary_at_flame_off = kwargs.get('dhw_binary_at_flame_off', 'unknown')
        dhw_flow_at_flame_off = kwargs.get('dhw_flow_at_flame_off', 'unknown')
        
        # Get current states (after 2s delay)
        dhw_binary_now = self.ad.get_state(C.OPENTHERM_DHW)
        dhw_flow_now = self.ad.get_state(C.OPENTHERM_DHW_FLOW_RATE)
        
        # Helper function to check if DHW is active
        def is_dhw_active(binary_state, flow_state):
            """DHW is active if binary='on' OR flow rate is non-zero."""
            if binary_state == 'on':
                return True
            try:
                flow_rate = float(flow_state)
                return flow_rate > 0.0
            except (ValueError, TypeError):
                # If flow state invalid, rely on binary only
                return False
        
        # QUAD-CHECK: DHW at flame OFF OR DHW now OR DHW in recent history
        dhw_was_active = is_dhw_active(dhw_binary_at_flame_off, dhw_flow_at_flame_off)
        dhw_is_active = is_dhw_active(dhw_binary_now, dhw_flow_now)
        dhw_recently_active = self._dhw_was_recently_active(lookback_seconds=12)
        
        if dhw_was_active or dhw_is_active or dhw_recently_active:
            self.ad.log(
                f"Flame OFF: DHW event detected | "
                f"At flame OFF: binary={dhw_binary_at_flame_off}, flow={dhw_flow_at_flame_off} | "
                f"After 2s: binary={dhw_binary_now}, flow={dhw_flow_now} | "
                f"Recent history: {dhw_recently_active} | "
                f"Ignoring (not a CH shutdown)",
                level="DEBUG"
            )
            return
        
        # Conservative fallback for uncertain states
        if dhw_binary_at_flame_off == 'unknown' or dhw_flow_at_flame_off == 'unknown':
            self.ad.log(
                f"Flame OFF: DHW state uncertain at flame OFF time - "
                f"skipping cooldown evaluation for safety",
                level="WARNING"
            )
            return
        
        # All checks confirm no DHW - this is a genuine CH shutdown
        # Check if flow or return temp indicates overheat condition
        flow_temp = self._get_flow_temp()
        return_temp = self._get_return_temp()
        setpoint = self._get_current_setpoint()
        
        if flow_temp is None or return_temp is None or setpoint is None:
            self.ad.log("Cannot evaluate cooldown: missing temperature data", level="WARNING")
            return
        
        # Calculate thresholds for dual-temperature detection
        flow_overheat_threshold = setpoint + C.CYCLING_FLOW_OVERHEAT_MARGIN_C  # Flow ABOVE setpoint
        return_threshold = setpoint - C.CYCLING_HIGH_RETURN_DELTA_C            # Return below setpoint
        
        # Check if EITHER temperature indicates overheat
        flow_overheat = flow_temp >= flow_overheat_threshold
        return_high = return_temp >= return_threshold
        
        self.ad.log(
            f"Flame OFF: Confirmed CH shutdown | "
            f"DHW at flame OFF: binary={dhw_binary_at_flame_off}, flow={dhw_flow_at_flame_off} | "
            f"DHW now: binary={dhw_binary_now}, flow={dhw_flow_now} | "
            f"Flow: {flow_temp:.1f}C (overheat if >={flow_overheat_threshold:.1f}C) {'OVERHEAT' if flow_overheat else 'OK'} | "
            f"Return: {return_temp:.1f}C (high if >={return_threshold:.1f}C) {'HIGH' if return_high else 'OK'} | "
            f"Setpoint: {setpoint:.1f}C",
            level="INFO"
        )
        
        if flow_overheat or return_high:
            # Determine trigger reason for logging
            if flow_overheat and return_high:
                reason = "flow overheat AND high return temp"
            elif flow_overheat:
                reason = "flow temperature exceeds setpoint"
            else:
                reason = "return temperature high (fallback)"
            
            self.ad.log(f"Entering cooldown: {reason}", level="WARNING")
            self._enter_cooldown(setpoint)
        else:
            # Normal conditions - no cooldown needed
            self.ad.log(
                f"Flame OFF: Normal conditions - no cooldown needed",
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
        
        # Increment cooldowns counter in Home Assistant and persistence
        _increment_cooldowns_sensor(self.ad, self.persistence)
        
        # Check for excessive cycling
        recent_cooldowns = [
            entry for entry in self.cooldown_history
            if (now - entry[0]).total_seconds() < C.CYCLING_EXCESSIVE_WINDOW_S
        ]
        
        if len(recent_cooldowns) >= C.CYCLING_EXCESSIVE_COUNT:
            self.ad.log(
                f"WARNING: EXCESSIVE CYCLING: {len(recent_cooldowns)} cooldowns in "
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
                    f"System will continue trying to protect the boiler.",
                    auto_clear=True
                )
        
        # Drop setpoint to cooldown temperature
        self._set_setpoint(C.CYCLING_COOLDOWN_SETPOINT)
        
        # Save state to persistence
        self._save_state()
        
        # Log cooldown entry
        self.ad.log(
            f"COOLDOWN STARTED | "
            f"Return: {return_temp:.1f}C >= Threshold: {threshold:.1f}C | "
            f"Saved setpoint: {original_setpoint:.1f}C -> New: {C.CYCLING_COOLDOWN_SETPOINT}C",
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
        flow_temp = self._get_flow_temp()
        return_temp = self._get_return_temp()
        recovery_threshold = self._get_recovery_threshold()
        
        if flow_temp is None or return_temp is None:
            self.ad.log("Cannot check recovery: missing temperature data", level="WARNING")
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
                f"ERROR: COOLDOWN TIMEOUT: Stuck in cooldown for {int(time_in_cooldown/60)} minutes! "
                f"Flow: {flow_temp:.1f}C, Return: {return_temp:.1f}C, "
                f"Target: {recovery_threshold:.1f}C",
                level="ERROR"
            )
            
            # Alert user via notification
            if self.alert_manager:
                self.alert_manager.report_error(
                    self.alert_manager.ALERT_CYCLING_PROTECTION_TIMEOUT,
                    self.alert_manager.SEVERITY_WARNING,
                    f"Cycling protection stuck in cooldown for {int(time_in_cooldown/60)} minutes.\n\n"
                    f"**Current:** Flow {flow_temp:.1f}C, Return {return_temp:.1f}C\n"
                    f"**Target:** {recovery_threshold:.1f}C\n"
                    f"**Action:** Forcing recovery and restoring setpoint.\n\n"
                    f"This may indicate recovery threshold needs adjustment.",
                    auto_clear=True
                )
            
            # Force exit with timeout state
            self.state = self.STATE_TIMEOUT
            self._exit_cooldown()
            return
        
        # Check max of both temps against threshold (ensures BOTH are safe)
        max_temp = max(flow_temp, return_temp)
        temps_safe = max_temp <= recovery_threshold
        
        # Log progress
        self.ad.log(
            f"Cooldown check: Flow={flow_temp:.1f}C Return={return_temp:.1f}C "
            f"max={max_temp:.1f}C (target<={recovery_threshold:.1f}C) "
            f"{'SAFE' if temps_safe else 'COOLING'} [{int(time_in_cooldown)}s elapsed]",
            level="DEBUG"
        )
        
        # Check if recovery threshold reached
        if temps_safe:
            self.ad.log(
                f"Recovery complete: Both temps safe "
                f"(Flow={flow_temp:.1f}C, Return={return_temp:.1f}C, max={max_temp:.1f}C <= {recovery_threshold:.1f}C) "
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
            f"COOLDOWN ENDED | "
            f"Duration: {int(duration)}s | Return: {return_temp:.1f}C | "
            f"Restored setpoint: {self.saved_setpoint:.1f}C",
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
    
    def _get_flow_temp(self) -> Optional[float]:
        """Get current flow/supply temperature from OpenTherm sensor.
        
        Returns:
            Flow temperature in °C, or None if unavailable
        """
        flow_temp_str = self.ad.get_state(C.OPENTHERM_HEATING_TEMP)
        if flow_temp_str in ['unknown', 'unavailable', None]:
            return None
        try:
            return float(flow_temp_str)
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
        """Persist state to persistence file."""
        state_dict = {
            'mode': self.state,
            'saved_setpoint': self.saved_setpoint,
            'cooldown_start': self.cooldown_entry_time.isoformat() if self.cooldown_entry_time else None
        }
        
        try:
            self.persistence.update_cycling_protection_state(state_dict)
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
