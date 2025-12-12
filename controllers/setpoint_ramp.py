# -*- coding: utf-8 -*-
"""
setpoint_ramp.py - Dynamic setpoint ramping for short-cycling prevention

Responsibilities:
- Monitor flow temperature vs setpoint
- Ramp setpoint upward when flow temp approaches current setpoint
- Coordinate with cycling protection for cooldown handling
- Infer ramp state from physical boiler setpoint (no persistence needed)
- Validate configuration on startup
"""

from datetime import datetime
from typing import Optional, Dict, Tuple, Any
import constants as C


class SetpointRamp:
    """Manages dynamic setpoint ramping to prevent short-cycling.
    
    When flow temperature approaches the current setpoint, incrementally
    raises the setpoint to keep the boiler running longer and reduce
    cooldown cycles.
    
    State Machine:
    - INACTIVE: Feature disabled or conditions not met
    - RAMPING: Actively ramped above baseline setpoint
    """
    
    # State constants
    STATE_INACTIVE = "INACTIVE"
    STATE_RAMPING = "RAMPING"
    
    def __init__(self, ad, config, cycling_protection_ref=None, app_ref=None):
        """Initialize setpoint ramp controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            cycling_protection_ref: Optional CyclingProtection instance for coordination
            app_ref: Optional reference to main PyHeat app for triggering recomputes
        """
        self.ad = ad
        self.config = config
        self.cycling = cycling_protection_ref
        self.app_ref = app_ref
        
        # Configuration (loaded from boiler.yaml)
        self.delta_trigger_c: Optional[float] = None  # Flow temp threshold above setpoint to trigger ramp
        self.delta_increase_c: Optional[float] = None  # Amount to increase setpoint each step
        
        # State
        self.state = self.STATE_INACTIVE
        self.baseline_setpoint: Optional[float] = None  # User's desired baseline setpoint
        self.current_ramped_setpoint: Optional[float] = None  # Current ramped value
        self.ramp_steps_applied: int = 0  # Number of ramp steps applied
        
        # Feature control entities
        self.enable_entity = C.HELPER_SETPOINT_RAMP_ENABLE
        self.max_entity = C.HELPER_SETPOINT_RAMP_MAX
    
    def set_cycling_protection_ref(self, cycling_protection_ref) -> None:
        """Set cycling protection reference after initialization.
        
        This allows cycling protection to be created after setpoint ramp,
        avoiding circular dependency issues during initialization.
        
        Args:
            cycling_protection_ref: CyclingProtection instance
        """
        self.cycling = cycling_protection_ref
        
    def initialize_from_ha(self) -> None:
        """Load configuration and initialize state from physical boiler state.

        NEW APPROACH: Infer ramping state from actual boiler setpoint instead of
        persisted state. This eliminates complex restoration logic and prevents
        desync issues.

        Physical state detection:
        - If boiler > helper and flame ON: we're actively ramping
        - If boiler > helper and flame OFF: reset to helper (stale ramp)
        - Otherwise: inactive at baseline

        This is self-correcting and requires no persistence.
        """
        # Load and validate configuration from boiler.yaml
        self._load_and_validate_config()

        # Check if feature is enabled
        if not self._is_feature_enabled():
            self.ad.log(
                "SetpointRamp: Feature disabled via input_boolean - staying INACTIVE",
                level="INFO"
            )
            self.state = self.STATE_INACTIVE
            return

        # Get current baseline from HA helper
        helper_setpoint = self._get_baseline_setpoint()
        if helper_setpoint is None:
            self.ad.log(
                "SetpointRamp: Cannot initialize - baseline setpoint unavailable",
                level="WARNING"
            )
            return

        # Get current physical boiler setpoint (source of truth)
        boiler_setpoint = self._get_current_ha_setpoint()
        if boiler_setpoint is None:
            self.ad.log(
                "SetpointRamp: Cannot initialize - boiler setpoint unavailable",
                level="WARNING"
            )
            return

        # Check flame state
        flame_is_on = self._is_flame_on()

        # CRITICAL: Check if cycling protection is in COOLDOWN
        # If so, DO NOT interfere - cooldown owns setpoint control
        if self.cycling:
            cycling_state = getattr(self.cycling, 'state', None)
            if cycling_state == C.CYCLING_STATE_COOLDOWN:
                self.ad.log(
                    f"SetpointRamp: Cycling protection in COOLDOWN (boiler at {boiler_setpoint:.1f}C) - "
                    f"skipping initialization (cooldown owns setpoint)",
                    level="INFO"
                )
                # Set baseline but stay INACTIVE
                self.baseline_setpoint = helper_setpoint
                self.state = self.STATE_INACTIVE
                return

        # Detect ramping state from physical boiler setpoint
        if boiler_setpoint > helper_setpoint + 0.1:  # Allow 0.1C tolerance for rounding
            if flame_is_on:
                # Actively ramping - continue from current position
                self.baseline_setpoint = helper_setpoint
                self.current_ramped_setpoint = boiler_setpoint
                self.state = self.STATE_RAMPING
                # Estimate steps applied (for logging)
                self.ramp_steps_applied = int((boiler_setpoint - helper_setpoint) / self.delta_increase_c)

                self.ad.log(
                    f"SetpointRamp: Detected active ramping - boiler at {boiler_setpoint:.1f}C, "
                    f"helper at {helper_setpoint:.1f}C, flame ON - continuing ramp",
                    level="INFO"
                )
            else:
                # Flame OFF but boiler still high - stale ramp state, reset to baseline
                self.baseline_setpoint = helper_setpoint
                self.current_ramped_setpoint = helper_setpoint
                self.ramp_steps_applied = 0
                self.state = self.STATE_INACTIVE

                self.ad.log(
                    f"SetpointRamp: Detected stale ramp - boiler at {boiler_setpoint:.1f}C, "
                    f"helper at {helper_setpoint:.1f}C, flame OFF - resetting to baseline",
                    level="INFO"
                )

                # Reset boiler to baseline
                self.ad.call_service(
                    'climate/set_temperature',
                    entity_id=C.OPENTHERM_CLIMATE,
                    temperature=helper_setpoint
                )
        else:
            # Normal operation - boiler at or near baseline
            self.baseline_setpoint = helper_setpoint
            self.current_ramped_setpoint = helper_setpoint
            self.ramp_steps_applied = 0
            self.state = self.STATE_INACTIVE

            self.ad.log(
                f"SetpointRamp: Normal operation - boiler at {boiler_setpoint:.1f}C, "
                f"helper at {helper_setpoint:.1f}C - starting INACTIVE",
                level="DEBUG"
            )
    
    def _load_and_validate_config(self) -> None:
        """Load and validate setpoint_ramp configuration from boiler.yaml.
        
        Raises:
            ValueError: If configuration is invalid or missing required values
        """
        ramp_config = self.config.boiler_config.get('setpoint_ramp', {})
        
        # Check for required configuration
        if 'delta_trigger_c' not in ramp_config:
            raise ValueError(
                "Missing required config: boiler.setpoint_ramp.delta_trigger_c must be defined. "
                "This sets the flow temperature threshold above setpoint to trigger ramping. "
                "Example: 3.0 means ramp when flow temp >= setpoint + 3.0C"
            )
        
        if 'delta_increase_c' not in ramp_config:
            raise ValueError(
                "Missing required config: boiler.setpoint_ramp.delta_increase_c must be defined. "
                "This sets how much to increase setpoint each ramp step. "
                "Example: 1.0 means increase setpoint by 1.0C per step"
            )
        
        # Parse and validate delta_trigger_c
        try:
            self.delta_trigger_c = float(ramp_config['delta_trigger_c'])
            if not (0.1 <= self.delta_trigger_c <= 10.0):
                raise ValueError(
                    f"boiler.setpoint_ramp.delta_trigger_c must be between 0.1 and 10.0C "
                    f"(got {self.delta_trigger_c:.1f}C)"
                )
            
            # Warning if too high (may trigger cooldown before ramping)
            if self.delta_trigger_c > 4.0:
                self.ad.log(
                    f"WARNING: boiler.setpoint_ramp.delta_trigger_c is {self.delta_trigger_c:.1f}C "
                    f"which is quite high. Cycling protection may trigger before ramping occurs. "
                    f"Consider reducing to 3.0-4.0C for optimal results.",
                    level="WARNING"
                )
                
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid boiler.setpoint_ramp.delta_trigger_c: {ramp_config['delta_trigger_c']}. "
                f"Must be a number between 0.1 and 10.0. Error: {e}"
            )
        
        # Parse and validate delta_increase_c
        try:
            self.delta_increase_c = float(ramp_config['delta_increase_c'])
            if not (0.1 <= self.delta_increase_c <= 5.0):
                raise ValueError(
                    f"boiler.setpoint_ramp.delta_increase_c must be between 0.1 and 5.0C "
                    f"(got {self.delta_increase_c:.1f}C)"
                )
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid boiler.setpoint_ramp.delta_increase_c: {ramp_config['delta_increase_c']}. "
                f"Must be a number between 0.1 and 5.0. Error: {e}"
            )
        
        self.ad.log(
            f"SetpointRamp: Configuration loaded - "
            f"delta_trigger={self.delta_trigger_c:.1f}C, "
            f"delta_increase={self.delta_increase_c:.1f}C",
            level="INFO"
        )
    
    def evaluate_and_apply(self, flow_temp: float, current_setpoint: float,
                          baseline_setpoint: float, boiler_state: str,
                          cycling_state: str) -> Optional[float]:
        """Evaluate if ramping needed and return new setpoint.

        Only evaluates when:
        - Feature is enabled
        - Boiler state == STATE_ON (actively heating)
        - Cycling state == NORMAL (not in cooldown)
        - Flame is ON (actively burning)

        Args:
            flow_temp: Current flow temperature from sensor
            current_setpoint: Current climate entity setpoint
            baseline_setpoint: User's desired baseline from helper
            boiler_state: Current boiler FSM state
            cycling_state: Current cycling protection state

        Returns:
            New setpoint to apply, or None if no change needed
        """
        # Check if feature enabled
        if not self._is_feature_enabled():
            # Feature disabled - reset if currently ramping
            if self.state == self.STATE_RAMPING:
                self.ad.log(
                    "SetpointRamp: Feature disabled - resetting to baseline",
                    level="INFO"
                )
                self._reset_to_baseline(baseline_setpoint)
                return baseline_setpoint
            return None

        # Initialize baseline if not set (e.g., feature was just enabled)
        if self.baseline_setpoint is None:
            self._reset_to_baseline(baseline_setpoint)
            return baseline_setpoint

        # Update baseline if user changed it
        if self.baseline_setpoint != baseline_setpoint:
            self.ad.log(
                f"SetpointRamp: Baseline changed from {self.baseline_setpoint:.1f}C "
                f"to {baseline_setpoint:.1f}C - resetting ramp",
                level="INFO"
            )
            self._reset_to_baseline(baseline_setpoint)
            return baseline_setpoint

        # Only ramp when boiler is actively heating (STATE_ON) and not in cooldown
        if boiler_state != C.STATE_ON or cycling_state != C.CYCLING_STATE_NORMAL:
            # Not heating - don't evaluate ramp, but preserve state
            return None

        # Check if flame is actually ON
        # Don't ramp if flame is off (even if boiler state is ON)
        try:
            flame_state = self.ad.get_state(C.OPENTHERM_FLAME)
            if flame_state != 'on':
                # Flame not on - don't ramp
                return None
        except Exception as e:
            self.ad.log(
                f"SetpointRamp: Failed to read flame state: {e}",
                level="WARNING"
            )
            return None
        
        # Get max setpoint
        max_setpoint = self._get_max_setpoint()
        if max_setpoint is None:
            self.ad.log(
                "SetpointRamp: Cannot evaluate - max setpoint unavailable",
                level="WARNING"
            )
            return None
        
        # Check if we should ramp
        should_ramp = flow_temp >= (current_setpoint + self.delta_trigger_c)
        
        if should_ramp:
            # Calculate new setpoint
            new_setpoint = current_setpoint + self.delta_increase_c
            
            # Cap at max
            if new_setpoint > max_setpoint:
                new_setpoint = max_setpoint
            
            # Only apply if actually increased
            if new_setpoint > current_setpoint:
                old_state = self.state
                self.state = self.STATE_RAMPING
                self.current_ramped_setpoint = new_setpoint
                self.ramp_steps_applied += 1
                
                self.ad.log(
                    f"SetpointRamp: Flow temp {flow_temp:.1f}C >= threshold "
                    f"{current_setpoint + self.delta_trigger_c:.1f}C - "
                    f"ramping {current_setpoint:.1f}C -> {new_setpoint:.1f}C "
                    f"(step {self.ramp_steps_applied})",
                    level="INFO"
                )

                # Queue CSV log event if state transitioned from INACTIVE to RAMPING
                if old_state == self.STATE_INACTIVE and self.app_ref and hasattr(self.app_ref, 'queue_csv_event'):
                    self.app_ref.queue_csv_event('setpoint_ramp_started')

                # No persistence needed - state inferred from physical boiler on next restart

                return new_setpoint
            elif new_setpoint >= max_setpoint:
                # At max - log but don't spam
                if self.ramp_steps_applied % 5 == 0:  # Log every 5th evaluation at max
                    self.ad.log(
                        f"SetpointRamp: At maximum setpoint {max_setpoint:.1f}C "
                        f"(flow temp {flow_temp:.1f}C)",
                        level="DEBUG"
                    )
        
        return None
    
    def on_baseline_setpoint_changed(self, new_baseline: float) -> None:
        """Handle user changing input_number.pyheat_opentherm_setpoint.
        
        Args:
            new_baseline: New baseline setpoint value
        """
        if self.baseline_setpoint is not None and abs(self.baseline_setpoint - new_baseline) > 0.1:
            self.ad.log(
                f"SetpointRamp: User changed baseline setpoint "
                f"{self.baseline_setpoint:.1f}C -> {new_baseline:.1f}C - resetting ramp",
                level="INFO"
            )
            self._reset_to_baseline(new_baseline)
    
    def on_cooldown_entered(self) -> None:
        """Handle cycling protection entering cooldown.
        
        Cooldown logic takes over setpoint control (drops to 30C).
        Save ramp state to restore on cooldown exit.
        """
        if self.state == self.STATE_RAMPING:
            ramp_temp = self.current_ramped_setpoint if self.current_ramped_setpoint is not None else 0.0
            self.ad.log(
                f"SetpointRamp: Cooldown entered while ramping at {ramp_temp:.1f}C - "
                f"saving state (will restore to baseline on exit)",
                level="INFO"
            )
            # State is already saved - just need to preserve it through cooldown
            # Don't reset to baseline yet - that happens on cooldown exit
    
    def on_cooldown_exited(self) -> None:
        """Handle cycling protection exiting cooldown.
        
        Note: Cooldown causes flame to go OFF, which triggers flame-OFF
        reset naturally. This method kept for coordination but may not
        need to do anything (flame-OFF already handled reset).
        """
        # Flame-OFF reset already handled the setpoint reset
        # This method kept for future coordination needs
        pass
    
    def on_flame_off(self, entity, attribute, old, new, kwargs):
        """Handle flame OFF event - reset ramped setpoint to baseline.

        When flame goes OFF for any reason (DHW, cooldown, loss of demand),
        reset to user's desired baseline setpoint. This ensures when flame
        comes back on, heating starts from user's intent.

        Aligns with feature goal: prevent short-cycling within a heating
        cycle, not across heating cycles.

        Args:
            entity: Entity ID (binary_sensor.opentherm_flame)
            attribute: Attribute that changed (usually None for state)
            old: Previous state value
            new: New state value
            kwargs: Additional callback parameters
        """
        if new == 'off' and old == 'on':
            # Only reset if we have a baseline and we're not in cooldown
            # (cooldown has its own exit logic that restores baseline)
            if self.baseline_setpoint is None:
                return

            # Check if cycling protection is in cooldown
            # Don't reset during cooldown - it has its own setpoint (30C)
            # and will restore baseline on exit
            if self.cycling:
                cycling_state = getattr(self.cycling, 'state', None)
                if cycling_state == C.CYCLING_STATE_COOLDOWN:
                    self.ad.log(
                        "SetpointRamp: Flame OFF during cooldown - skipping reset "
                        "(cooldown exit will restore baseline)",
                        level="DEBUG"
                    )
                    return

            # Get current climate entity setpoint
            try:
                current_setpoint_str = self.ad.get_state(
                    C.OPENTHERM_CLIMATE,
                    attribute='temperature'
                )
                if current_setpoint_str not in ['unknown', 'unavailable', None]:
                    current_setpoint = float(current_setpoint_str)
                else:
                    current_setpoint = None
            except (ValueError, TypeError):
                current_setpoint = None

            # Reset if climate setpoint doesn't match baseline
            # (regardless of whether state is RAMPING - we want consistency)
            if current_setpoint is not None and abs(current_setpoint - self.baseline_setpoint) > 0.1:
                self.ad.log(
                    f"SetpointRamp: Flame OFF detected - resetting from "
                    f"{current_setpoint:.1f}C to baseline {self.baseline_setpoint:.1f}C",
                    level="INFO"
                )

                # Reset internal state to INACTIVE
                # This prevents evaluate_and_apply() from immediately ramping again
                old_state = self.state
                self._reset_to_baseline(self.baseline_setpoint)

                # Apply baseline setpoint to climate entity
                # This ensures setpoint returns to user's desired value
                self.ad.call_service(
                    'climate/set_temperature',
                    entity_id=C.OPENTHERM_CLIMATE,
                    temperature=self.baseline_setpoint
                )

                # Queue CSV log event if state transitioned from RAMPING to INACTIVE
                if old_state == self.STATE_RAMPING and self.app_ref and hasattr(self.app_ref, 'queue_csv_event'):
                    self.app_ref.queue_csv_event('setpoint_ramp_reset')
            elif self.state == self.STATE_RAMPING:
                # Setpoint already at baseline, but internal state is RAMPING
                # Reset internal state to INACTIVE for consistency
                self.ad.log(
                    f"SetpointRamp: Flame OFF - setpoint already at baseline "
                    f"{self.baseline_setpoint:.1f}C, resetting internal state to INACTIVE",
                    level="DEBUG"
                )
                self._reset_to_baseline(self.baseline_setpoint)
    
    def _reset_to_baseline(self, baseline: float) -> None:
        """Reset ramp state to baseline setpoint.

        Args:
            baseline: Baseline setpoint to reset to
        """
        self.baseline_setpoint = baseline
        self.current_ramped_setpoint = baseline
        self.ramp_steps_applied = 0
        self.state = self.STATE_INACTIVE
        # No persistence needed - state inferred from physical boiler on next restart
    
    def _is_feature_enabled(self) -> bool:
        """Check if setpoint ramp feature is enabled.
        
        Returns:
            True if enabled, False otherwise
        """
        try:
            state = self.ad.get_state(self.enable_entity)
            return state == "on"
        except Exception as e:
            self.ad.log(
                f"SetpointRamp: Failed to read enable state: {e}",
                level="WARNING"
            )
            return False
    
    def _get_baseline_setpoint(self) -> Optional[float]:
        """Get current baseline setpoint from helper.
        
        Returns:
            Baseline setpoint in C, or None if unavailable
        """
        try:
            state = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
            if state in ['unknown', 'unavailable', None]:
                return None
            return float(state)
        except (ValueError, TypeError):
            return None
    
    def _get_max_setpoint(self) -> Optional[float]:
        """Get maximum ramp setpoint from helper.
        
        Returns:
            Maximum setpoint in C, or None if unavailable
        """
        try:
            state = self.ad.get_state(self.max_entity)
            if state in ['unknown', 'unavailable', None]:
                return None
            return float(state)
        except (ValueError, TypeError):
            return None
    
    def is_ramping_active(self) -> bool:
        """Check if ramping is currently active.

        Used by cycling protection to skip setpoint validation.

        Returns:
            True if actively ramping above baseline
        """
        return self.state == self.STATE_RAMPING


    def _get_current_ha_setpoint(self) -> Optional[float]:
        """Get current setpoint from HA climate entity.

        Returns:
            Current setpoint in C, or None if unavailable
        """
        try:
            state = self.ad.get_state(C.OPENTHERM_CLIMATE, attribute='temperature')
            if state in ['unknown', 'unavailable', None]:
                return None
            return float(state)
        except (ValueError, TypeError):
            return None

    def _is_flame_on(self) -> bool:
        """Check if boiler flame is currently ON.

        Returns:
            True if flame is ON, False otherwise (including errors)
        """
        try:
            state = self.ad.get_state(C.OPENTHERM_FLAME)
            return state == 'on'
        except Exception:
            return False

    def get_state_dict(self) -> Dict:
        """Get current state as dict for logging and status publishing.
        
        Returns:
            Dict with enabled, state, baseline, ramped, max, steps
        """
        return {
            'enabled': self._is_feature_enabled(),
            'state': self.state,
            'baseline_setpoint': self.baseline_setpoint if self.baseline_setpoint else '',
            'current_ramped_setpoint': self.current_ramped_setpoint if self.current_ramped_setpoint else '',
            'ramp_steps_applied': self.ramp_steps_applied,
            'max_setpoint': self._get_max_setpoint() if self._is_feature_enabled() else ''
        }
