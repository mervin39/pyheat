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
- Infer cooldown state from physical boiler setpoint (eliminates desync issues)
- Persist metadata (entry_time, saved_setpoint) for history
- Increment HA counter helper (counter.pyheat_cooldowns) on each cooldown event

CRITICAL LOGIC:
- Cooldown ENTRY: Triggers on EITHER flow overheat OR high return temp (OR logic)
- Cooldown EXIT: Requires BOTH flow AND return temps safe (AND logic)
- State detection: If boiler at 30°C, we're in COOLDOWN (physical state is truth)
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from collections import deque
import os
import constants as C


def _increment_cooldowns_counter(ad) -> None:
    """Increment the cooldowns counter by 1.

    Uses HA counter helper for automatic persistence.

    Args:
        ad: AppDaemon API reference
    """
    try:
        ad.call_service('counter/increment', entity_id=C.COOLDOWNS_ENTITY)
    except Exception as e:
        ad.log(f"Error incrementing cooldowns counter: {e}", level="WARNING")


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
    
    def __init__(self, ad, config, alert_manager=None, boiler_controller=None, app_ref=None, setpoint_ramp_ref=None):
        """Initialize cycling protection controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            alert_manager: AlertManager instance for notifications
            boiler_controller: BoilerController instance for state checking
            app_ref: Reference to main app for triggering logs
            setpoint_ramp_ref: Optional SetpointRamp instance for coordination
        """
        self.ad = ad
        self.config = config
        self.alert_manager = alert_manager
        self.boiler_controller = boiler_controller
        self.app_ref = app_ref
        self.setpoint_ramp = setpoint_ramp_ref
        # Construct absolute path from app root (same pattern as config_loader)
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        persistence_file = os.path.join(app_dir, C.PERSISTENCE_FILE)
        self.persistence = PersistenceManager(persistence_file)
        
        # State machine state
        self.state = self.STATE_NORMAL
        self.cooldown_entry_time: Optional[datetime] = None
        self.saved_setpoint: Optional[float] = None
        
        # Cooldown history for excessive cycling detection
        # List of tuples: (timestamp, return_temp, setpoint)
        self.cooldown_history: List[Tuple[datetime, float, float]] = []
        
        # DHW history tracking for improved detection
        # Circular buffers storing (timestamp, state) tuples
        self.dhw_history_binary = deque(maxlen=C.CYCLING_DHW_HISTORY_BUFFER_SIZE)
        self.dhw_history_flow = deque(maxlen=C.CYCLING_DHW_HISTORY_BUFFER_SIZE)

        # Flow temp history tracking for sensor lag compensation
        # Circular buffer storing (timestamp, flow_temp, setpoint) tuples
        # Critical: Store setpoint with flow temp to handle setpoint ramping correctly
        self.flow_temp_history = deque(maxlen=C.CYCLING_FLOW_TEMP_HISTORY_BUFFER_SIZE)

        # Recovery monitoring handle
        self.recovery_handle = None

        # Boiler availability tracking (for alerting if boiler entity unavailable)
        self.boiler_unavailable_since: Optional[datetime] = None
        self.boiler_unavailable_alerted: bool = False
        
    def initialize_from_ha(self) -> None:
        """Detect cooldown state from physical boiler setpoint.

        NEW APPROACH: Infer COOLDOWN state from actual boiler setpoint instead of
        persisted state. This eliminates desync issues where internal state says
        COOLDOWN but boiler is at normal setpoint (or vice versa).

        Physical state detection:
        - If boiler at CYCLING_COOLDOWN_SETPOINT (30C +/- 0.5C): we're in COOLDOWN
          - Load metadata (entry_time, saved_setpoint) from persistence
          - Use defaults if missing
          - Check exit conditions immediately
          - Resume recovery monitoring
        - Otherwise: we're in NORMAL

        Metadata (entry_time, saved_setpoint) still persisted for history tracking,
        but state itself is inferred from physical boiler. Cooldowns count is now
        managed by HA counter helper (counter.pyheat_cooldowns).
        """
        # Get physical boiler setpoint (source of truth for state)
        boiler_setpoint = self._get_current_setpoint()
        if boiler_setpoint is None:
            self.ad.log(
                "CyclingProtection: Cannot detect state - boiler setpoint unavailable "
                "(will retry when available)",
                level="WARNING"
            )
            # Default to NORMAL and continue - will detect correctly once available
            self.state = self.STATE_NORMAL
            return

        # Detect COOLDOWN from physical setpoint
        is_at_cooldown_setpoint = abs(boiler_setpoint - C.CYCLING_COOLDOWN_SETPOINT) < 0.5

        if is_at_cooldown_setpoint:
            # Boiler is at cooldown setpoint - we're in COOLDOWN
            # Load persisted metadata for context
            try:
                state_dict = self.persistence.get_cycling_protection_state()

                # Load entry_time from persistence
                cooldown_start_str = state_dict.get('cooldown_start')
                if cooldown_start_str:
                    self.cooldown_entry_time = datetime.fromisoformat(cooldown_start_str)
                else:
                    # No entry time - assume started now (conservative)
                    self.cooldown_entry_time = datetime.now()
                    self.ad.log(
                        "CyclingProtection: Detected cooldown but no entry_time persisted - "
                        "assuming started now",
                        level="WARNING"
                    )

                # Load saved_setpoint from persistence
                self.saved_setpoint = state_dict.get('saved_setpoint')
                if self.saved_setpoint is None:
                    # No saved setpoint - use helper as fallback
                    helper_setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
                    if helper_setpoint not in ['unknown', 'unavailable', None]:
                        self.saved_setpoint = float(helper_setpoint)
                    else:
                        # Last resort: use reasonable default
                        self.saved_setpoint = 50.0
                    self.ad.log(
                        f"CyclingProtection: Detected cooldown but no saved_setpoint persisted - "
                        f"using fallback {self.saved_setpoint:.1f}C",
                        level="WARNING"
                    )

            except Exception as e:
                self.ad.log(
                    f"CyclingProtection: Failed to load metadata: {e} - using defaults",
                    level="WARNING"
                )
                self.cooldown_entry_time = datetime.now()
                self.saved_setpoint = 50.0

            # Set state to COOLDOWN
            self.state = self.STATE_COOLDOWN

            # Calculate duration for logging
            duration_s = (datetime.now() - self.cooldown_entry_time).total_seconds()

            self.ad.log(
                f"CyclingProtection: Detected COOLDOWN from boiler setpoint "
                f"({boiler_setpoint:.1f}C == {C.CYCLING_COOLDOWN_SETPOINT}C) - "
                f"duration {int(duration_s)}s, will restore to {self.saved_setpoint:.1f}C",
                level="INFO"
            )

            # Immediately check if we can exit cooldown (temps may have cooled while we were down)
            self._check_recovery_immediate()

            # If still in cooldown after check, resume monitoring
            if self.state == self.STATE_COOLDOWN:
                self._resume_cooldown_monitoring()

        else:
            # Boiler not at cooldown setpoint - we're in NORMAL
            self.state = self.STATE_NORMAL
            self.saved_setpoint = None
            self.cooldown_entry_time = None

            self.ad.log(
                f"CyclingProtection: Normal operation detected "
                f"(boiler at {boiler_setpoint:.1f}C)",
                level="DEBUG"
            )

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

    def on_flow_or_setpoint_change(self, entity, attribute, old, new, kwargs):
        """Track flow temp and setpoint changes for sensor lag compensation.

        Called whenever flow temp sensor or setpoint changes.
        Stores (timestamp, flow_temp, setpoint) tuples in circular buffer.

        Critical: Stores BOTH values together at same timestamp to correctly
        handle setpoint ramping - each historical flow temp is compared to
        the setpoint that was active at that same moment.

        Args:
            entity: Entity ID that changed (flow temp sensor or climate/helper)
            attribute: Attribute that changed (may be 'temperature' for climate entity)
            old: Previous value
            new: New value
            kwargs: Additional callback parameters
        """
        timestamp = datetime.now()

        # Read current flow temp and setpoint
        flow_temp = self._get_flow_temp()
        setpoint = self._get_current_setpoint()

        # Only append if both values are valid
        if flow_temp is not None and setpoint is not None:
            self.flow_temp_history.append((timestamp, flow_temp, setpoint))

            # Debug logging for significant changes (flow temp increases)
            if old and new:
                try:
                    old_val = float(old)
                    new_val = float(new)
                    # Log if flow temp increased by more than 2°C
                    if entity == C.OPENTHERM_HEATING_TEMP and new_val > old_val + 2.0:
                        self.ad.log(
                            f"Flow temp increased: {old_val:.1f}C -> {new_val:.1f}C "
                            f"(setpoint: {setpoint:.1f}C, tracking in history)",
                            level="DEBUG"
                        )
                except (ValueError, TypeError):
                    pass

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
        """Sync climate entity to helper value on startup (unless in cooldown or ramping).
        
        Should be called after initialize_from_ha() during app initialization.
        Ensures climate entity matches user's desired setpoint from helper.
        
        Skips sync if setpoint ramping will restore ramped state (flame ON with persisted ramp).
        """
        if self.state == self.STATE_COOLDOWN:
            # Already at 30°C protecting - don't interfere
            self.ad.log(
                "Startup: In COOLDOWN state - skipping setpoint sync",
                level="INFO"
            )
            return
        
        # Don't interfere if setpoint ramping will restore ramped state
        # Check: flame ON + persisted ramp state exists
        if self.setpoint_ramp:
            # Check if flame is ON (boiler actively heating)
            try:
                flame_state = self.ad.get_state(C.OPENTHERM_FLAME)
                if flame_state == 'on':
                    # Check if persisted ramp state exists
                    from persistence import PersistenceManager
                    import os
                    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    persistence_file = os.path.join(app_dir, C.PERSISTENCE_FILE)
                    persistence = PersistenceManager(persistence_file)
                    persisted_state = persistence.get_setpoint_ramp_state()
                    
                    # If we have valid persisted ramped state, let setpoint_ramp handle it
                    if persisted_state.get('current_ramped_setpoint'):
                        self.ad.log(
                            "Startup: Flame ON with persisted ramp state - skipping sync "
                            "(setpoint_ramp will restore ramped setpoint)",
                            level="INFO"
                        )
                        return
            except Exception as e:
                self.ad.log(
                    f"Startup: Failed to check ramp state: {e} - proceeding with sync",
                    level="WARNING"
                )
            
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
        """Periodic validation: ensure climate setpoint matches helper (unless in cooldown or ramping).
        
        Called every 60 seconds from periodic recompute to detect and correct setpoint drift.
        Skips validation when in COOLDOWN state or when setpoint ramping is active.
        """
        # Don't interfere if we're actively protecting
        if self.state == self.STATE_COOLDOWN:
            return
        
        # Don't interfere if setpoint ramping is active
        if self.setpoint_ramp and self.setpoint_ramp.is_ramping_active():
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
    
    def _dhw_was_recently_active(self, lookback_seconds: int = None) -> bool:
        """Check if DHW was active in recent history (backward-looking check).

        This catches the race condition where tap closes just before flame OFF.
        By the time flame OFF event fires, DHW sensors may already show 'off',
        but the history buffer will still contain the 'on' states.

        Args:
            lookback_seconds: How far back to check history (default: from constants) constants)

        Returns:
            True if DHW was active within lookback window, False otherwise
        """
        if lookback_seconds is None:
            lookback_seconds = C.CYCLING_DHW_LOOKBACK_S
        
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

    def _flow_was_recently_overheating(self, lookback_seconds: int = None) -> Tuple[bool, float, float]:
        """Check if flow temp exceeded its setpoint+2C threshold in recent history.

        This compensates for flame sensor lag (4-6s). By the time the flame sensor
        reports OFF, the physical flame has been off for several seconds and flow
        temp has already dropped. This function looks back at recent history to find
        the peak flow temp and compares it to the setpoint that was active at that
        same moment.

        Critical: Each historical flow temp is compared to its corresponding setpoint
        at the same timestamp. This correctly handles:
        - Setpoint ramping (dynamic increases during heating)
        - User setpoint changes
        - Cooldown transitions (setpoint drops to 30C)

        Args:
            lookback_seconds: How far back to check history (default: from constants)

        Returns:
            Tuple of (was_overheating, peak_flow, peak_setpoint):
            - was_overheating: True if any point in history exceeded threshold
            - peak_flow: Highest flow temp that triggered overheat (0.0 if none)
            - peak_setpoint: Setpoint at time of peak_flow (0.0 if none)
        """
        if lookback_seconds is None:
            lookback_seconds = C.CYCLING_FLOW_TEMP_LOOKBACK_S

        cutoff = datetime.now() - timedelta(seconds=lookback_seconds)

        peak_flow = 0.0
        peak_setpoint = 0.0
        was_overheating = False

        # Check each historical point: flow_temp vs (setpoint_at_that_time + 2C)
        for timestamp, flow_temp, setpoint_at_time in self.flow_temp_history:
            if timestamp >= cutoff:
                threshold_at_time = setpoint_at_time + C.CYCLING_FLOW_OVERHEAT_MARGIN_C
                if flow_temp >= threshold_at_time:
                    was_overheating = True
                    # Track the highest flow temp that exceeded its threshold
                    if flow_temp > peak_flow:
                        peak_flow = flow_temp
                        peak_setpoint = setpoint_at_time

        return was_overheating, peak_flow, peak_setpoint

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
        dhw_recently_active = self._dhw_was_recently_active()
        
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

        # Check current flow temp
        flow_overheat_now = flow_temp >= flow_overheat_threshold

        # CRITICAL: Also check recent history to compensate for flame sensor lag (4-6s)
        # By the time flame sensor reports OFF, flow temp may have already dropped
        flow_overheat_history, peak_flow, peak_setpoint = self._flow_was_recently_overheating()

        # Combine current and historical checks (OR logic)
        flow_overheat = flow_overheat_now or flow_overheat_history

        # Check return temp (fallback detection)
        return_high = return_temp >= return_threshold

        # Build detailed log message
        log_parts = [
            f"Flame OFF: Confirmed CH shutdown | ",
            f"DHW at flame OFF: binary={dhw_binary_at_flame_off}, flow={dhw_flow_at_flame_off} | ",
            f"DHW now: binary={dhw_binary_now}, flow={dhw_flow_now} | ",
            f"Flow NOW: {flow_temp:.1f}C (overheat if >={flow_overheat_threshold:.1f}C) {'OVERHEAT' if flow_overheat_now else 'OK'} | "
        ]

        if flow_overheat_history:
            log_parts.append(
                f"Flow HISTORY: Peak {peak_flow:.1f}C (was >={peak_setpoint + C.CYCLING_FLOW_OVERHEAT_MARGIN_C:.1f}C) OVERHEAT | "
            )
        else:
            log_parts.append(f"Flow HISTORY: OK (no overheat in last {C.CYCLING_FLOW_TEMP_LOOKBACK_S}s) | ")

        log_parts.append(f"Return: {return_temp:.1f}C (high if >={return_threshold:.1f}C) {'HIGH' if return_high else 'OK'} | ")
        log_parts.append(f"Setpoint: {setpoint:.1f}C")

        self.ad.log("".join(log_parts), level="INFO")

        if flow_overheat or return_high:
            # Determine trigger reason for logging
            if flow_overheat_now and flow_overheat_history and return_high:
                reason = "flow overheat (current AND history) AND high return temp"
            elif flow_overheat_now and return_high:
                reason = "flow overheat (current) AND high return temp"
            elif flow_overheat_history and return_high:
                reason = "flow overheat (history) AND high return temp"
            elif flow_overheat_now:
                reason = "flow temperature exceeds setpoint (current)"
            elif flow_overheat_history:
                reason = f"flow temperature exceeded setpoint in history (peak {peak_flow:.1f}C at setpoint {peak_setpoint:.1f}C)"
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
        
        # Notify setpoint ramp about cooldown entry
        if self.setpoint_ramp:
            self.setpoint_ramp.on_cooldown_entered()
        
        # Save state
        self.state = self.STATE_COOLDOWN
        self.saved_setpoint = original_setpoint
        self.cooldown_entry_time = now
        
        # Trigger CSV log for state change
        if self.app_ref and hasattr(self.app_ref, 'recompute_and_publish'):
            self.app_ref.recompute_and_publish('cycling_cooldown_entered', now)
        
        # Add to history for excessive cycling detection
        self.cooldown_history.append((now, return_temp, original_setpoint))

        # Increment cooldowns counter in Home Assistant
        _increment_cooldowns_counter(self.ad)
        
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
        
        # Trigger CSV log for state change
        if self.app_ref and hasattr(self.app_ref, 'recompute_and_publish'):
            self.app_ref.recompute_and_publish('cycling_cooldown_entered', now)
        
        # Start recovery monitoring
        self._start_recovery_monitoring()
        
    def _start_recovery_monitoring(self):
        """Start periodic recovery temperature monitoring."""
        self.recovery_handle = self.ad.run_in(
            self._check_recovery,
            C.CYCLING_RECOVERY_MONITORING_INTERVAL_S
        )
        
    def _resume_cooldown_monitoring(self):
        """Resume cooldown monitoring after AppDaemon restart.
        
        CRITICAL: Must restore cooldown setpoint (30C) to prevent flame restart.
        Without this, the boiler can restart during cooldown and cause short-cycling.
        """
        if self.state == self.STATE_COOLDOWN:
            # Restore cooldown setpoint (critical for preventing flame restart)
            self._set_setpoint(C.CYCLING_COOLDOWN_SETPOINT)
            self.ad.log(
                f"Restored cooldown setpoint to {C.CYCLING_COOLDOWN_SETPOINT}C "
                f"(saved setpoint: {self.saved_setpoint}C)",
                level="INFO"
            )
            # Start recovery monitoring
            self._start_recovery_monitoring()
            
    def _check_recovery_immediate(self) -> None:
        """Immediately check if recovery conditions are met (synchronous version).

        Called during initialization to check if cooldown can exit immediately.
        Does not schedule follow-up checks - that's done by _resume_cooldown_monitoring().
        """
        if self.state != self.STATE_COOLDOWN:
            return

        now = datetime.now()
        flow_temp = self._get_flow_temp()
        return_temp = self._get_return_temp()
        recovery_threshold = self._get_recovery_threshold()

        if flow_temp is None or return_temp is None:
            self.ad.log(
                "CyclingProtection: Cannot check recovery immediately - missing temp data",
                level="WARNING"
            )
            return

        # Calculate time in cooldown
        time_in_cooldown = (now - self.cooldown_entry_time).total_seconds()

        # Check for timeout
        if time_in_cooldown > C.CYCLING_COOLDOWN_MAX_DURATION_S:
            self.ad.log(
                f"CyclingProtection: COOLDOWN TIMEOUT on initialization - "
                f"stuck for {int(time_in_cooldown/60)} minutes, forcing exit",
                level="ERROR"
            )
            self.state = self.STATE_TIMEOUT
            self._exit_cooldown()
            return

        # Check if temps are safe
        max_temp = max(flow_temp, return_temp)
        temps_safe = max_temp <= recovery_threshold

        if temps_safe:
            self.ad.log(
                f"CyclingProtection: Recovery conditions met on initialization - exiting cooldown "
                f"(Flow={flow_temp:.1f}C, Return={return_temp:.1f}C <= {recovery_threshold:.1f}C)",
                level="INFO"
            )
            self._exit_cooldown()
        else:
            self.ad.log(
                f"CyclingProtection: Still cooling on initialization - "
                f"max temp {max_temp:.1f}C > {recovery_threshold:.1f}C "
                f"(elapsed: {int(time_in_cooldown)}s)",
                level="DEBUG"
            )

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
        
        # Notify setpoint ramp about cooldown exit
        if self.setpoint_ramp:
            self.setpoint_ramp.on_cooldown_exited()
        
        # Log exit
        return_temp = self._get_return_temp()
        self.ad.log(
            f"COOLDOWN ENDED | "
            f"Duration: {int(duration)}s | Return: {return_temp:.1f}C | "
            f"Restored setpoint: {self.saved_setpoint:.1f}C",
            level="INFO"
        )
        
        # Trigger CSV log for state change
        if self.app_ref and hasattr(self.app_ref, 'recompute_and_publish'):
            from datetime import datetime as dt
            self.app_ref.recompute_and_publish('cycling_cooldown_ended', dt.now())
        
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

        Also tracks boiler availability for alerting if entity is unavailable
        for an extended period (5+ minutes).

        Returns:
            Current setpoint in °C, or None if unavailable
        """
        # Read temperature attribute from climate entity
        setpoint = self.ad.get_state(C.OPENTHERM_CLIMATE, attribute='temperature')
        if setpoint in ['unknown', 'unavailable', None]:
            # Fallback: try reading from helper entity
            setpoint = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)

        if setpoint in ['unknown', 'unavailable', None]:
            # Boiler entity unavailable - track for alerting
            now = datetime.now()

            if self.boiler_unavailable_since is None:
                # Just became unavailable
                self.boiler_unavailable_since = now
                self.ad.log(
                    "Boiler climate entity unavailable - tracking for alert if prolonged",
                    level="WARNING"
                )
            else:
                # Already unavailable - check if alert threshold reached
                unavailable_duration = (now - self.boiler_unavailable_since).total_seconds()

                if unavailable_duration > 300 and not self.boiler_unavailable_alerted:  # 5 minutes
                    # Send alert
                    if self.alert_manager:
                        self.alert_manager.report_error(
                            "boiler_entity_unavailable",
                            self.alert_manager.SEVERITY_CRITICAL,
                            f"Boiler climate entity ({C.OPENTHERM_CLIMATE}) has been unavailable "
                            f"for {int(unavailable_duration/60)} minutes.\n\n"
                            f"PyHeat cannot control heating without the boiler entity. "
                            f"Please check:\n"
                            f"- Home Assistant is running\n"
                            f"- OpenTherm integration is working\n"
                            f"- Boiler is powered on and connected",
                            auto_clear=True
                        )
                        self.boiler_unavailable_alerted = True
                        self.ad.log(
                            f"ALERT: Boiler entity unavailable for {int(unavailable_duration/60)} minutes",
                            level="ERROR"
                        )

            return None

        # Boiler entity available - clear tracking
        if self.boiler_unavailable_since is not None:
            unavailable_duration = (datetime.now() - self.boiler_unavailable_since).total_seconds()
            self.ad.log(
                f"Boiler climate entity restored after {int(unavailable_duration)} seconds",
                level="INFO"
            )
            self.boiler_unavailable_since = None
            self.boiler_unavailable_alerted = False

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
