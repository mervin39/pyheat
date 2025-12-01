# -*- coding: utf-8 -*-
"""
room_controller.py - Per-room heating control logic

Responsibilities:
- Compute call-for-heat status with hysteresis
- Calculate valve percentages using stepped bands
- Coordinate sensor, schedule, and TRV components
- Track room state (calling, band, valve position)
"""

from datetime import datetime
from typing import Dict, Tuple, Optional
import constants as C
from persistence import PersistenceManager


class RoomController:
    """Manages per-room heating logic and state."""
    
    def __init__(self, ad, config, sensors, scheduler, trvs):
        """Initialize the room controller.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            sensors: SensorManager instance
            scheduler: Scheduler instance
            trvs: TRVController instance
        """
        self.ad = ad
        self.config = config
        self.sensors = sensors
        self.scheduler = scheduler
        self.trvs = trvs
        self.persistence = PersistenceManager(C.PERSISTENCE_FILE)
        
        self.room_call_for_heat = {}  # {room_id: bool}
        self.room_current_band = {}  # {room_id: band_index}
        self.room_last_valve = {}  # {room_id: percent}
        self.room_last_target = {}  # {room_id: float} - tracks previous target to detect changes
        self.room_frost_protection_active = {}  # {room_id: bool} - frost protection state
        self.room_frost_protection_alerted = {}  # {room_id: bool} - alert sent (rate limiting)
        
    def initialize_from_ha(self) -> None:
        """Initialize room state from Home Assistant.
        
        CRITICAL: room_call_for_heat is initialized from input_text.pyheat_room_persistence
        entity which is the single source of truth. This ensures hysteresis state
        survives restarts correctly, preventing spurious heating cycles or delays.
        
        Also initialize room_last_target to current targets to prevent false
        "target changed" detection on first recompute after restart.
        """
        for room_id, room_cfg in self.config.rooms.items():
            if room_cfg.get('disabled'):
                continue
            
            # Initialize target tracking - get current target from scheduler
            try:
                now = datetime.now()
                mode_entity = C.HELPER_ROOM_MODE.format(room=room_id)
                room_mode = self.ad.get_state(mode_entity) if self.ad.entity_exists(mode_entity) else "auto"
                room_mode = room_mode.lower() if room_mode else "auto"
                
                holiday_mode = False
                if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                    holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                
                # Get current target (pass is_stale=False as placeholder, it won't affect target resolution)
                current_target_info = self.scheduler.resolve_room_target(room_id, now, room_mode, holiday_mode, False)
                if current_target_info is not None:
                    current_target = current_target_info['target']
                    self.room_last_target[room_id] = current_target
                    self.ad.log(f"Room {room_id}: Initialized target tracking at {current_target}C", level="DEBUG")
            except Exception as e:
                self.ad.log(f"Failed to initialize target tracking for room {room_id}: {e}", level="WARNING")
        
        # Load persisted calling state from HA (single source of truth)
        self._load_persisted_state()
        
    def _load_persisted_state(self) -> None:
        """Load last_calling and passive_valve state from persistence file.
        
        This is the SINGLE SOURCE OF TRUTH for room_call_for_heat state.
        
        If persistence data is missing or invalid, defaults to False (not calling).
        The first recompute (within seconds) will establish correct state.
        """
        try:
            data = self.persistence.load()
            room_state = data.get('room_state', {})
            
            # Load state for each configured room
            for room_id in self.config.rooms.keys():
                if self.config.rooms[room_id].get('disabled'):
                    continue
                
                if room_id in room_state:
                    # Load persisted calling state
                    persisted_calling = room_state[room_id].get('last_calling', False)
                    self.room_call_for_heat[room_id] = persisted_calling
                    
                    # Load persisted passive valve state
                    persisted_passive_valve = room_state[room_id].get('passive_valve', 0)
                    self.room_last_valve[room_id] = persisted_passive_valve
                    
                    self.ad.log(
                        f"Room {room_id}: Loaded persisted state - "
                        f"calling={persisted_calling}, passive_valve={persisted_passive_valve}%",
                        level="DEBUG"
                    )
                else:
                    # Room not in persistence data (new room?) - default to False
                    self.room_call_for_heat[room_id] = False
                    self.room_last_valve[room_id] = 0
                    self.ad.log(f"Room {room_id}: Not in persistence data, defaulting to not calling", level="WARNING")
                    
        except Exception as e:
            self.ad.log(f"ERROR: Failed to load room persistence: {e}. All rooms defaulting to not calling.", level="ERROR")
            # Default all rooms to False on error
            for room_id in self.config.rooms.keys():
                if not self.config.rooms[room_id].get('disabled'):
                    self.room_call_for_heat[room_id] = False
                    self.room_last_valve[room_id] = 0
    
    def _persist_calling_state(self, room_id: str, calling: bool) -> None:
        """Update last_calling in persistence file.
        
        Preserves existing valve_percent and passive_valve while updating calling state.
        """
        try:
            self.persistence.update_room_state(room_id, last_calling=calling)
        except Exception as e:
            self.ad.log(f"Failed to persist calling state for {room_id}: {e}", level="WARNING")
        
    def compute_room(self, room_id: str, now: datetime) -> Dict:
        """Compute heating requirements for a room.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            
        Returns:
            Dictionary with room state:
            {
                'temp': float or None,
                'target': float or None,
                'is_stale': bool,
                'mode': str,
                'calling': bool,
                'valve_percent': int,
                'error': float or None
            }
        """
        # Get room mode
        mode_entity = C.HELPER_ROOM_MODE.format(room=room_id)
        room_mode = self.ad.get_state(mode_entity) if self.ad.entity_exists(mode_entity) else "off"
        room_mode = room_mode.lower() if room_mode else "auto"
        
        # Get holiday mode
        holiday_mode = False
        if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
        
        # Get temperature (smoothed for consistent control and display)
        temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
        
        # Check if master enable is on (required for frost protection)
        master_enabled = True
        if self.ad.entity_exists(C.HELPER_MASTER_ENABLE):
            master_enabled = self.ad.get_state(C.HELPER_MASTER_ENABLE) == "on"
        
        # FROST PROTECTION CHECK (HIGHEST PRIORITY - checked before mode logic)
        # Activates when room drops below safety threshold
        # Only for modes other than "off" and only when master_enable is on
        if room_mode != C.MODE_OFF and master_enabled and temp is not None and not is_stale:
            frost_temp = self.config.system_config.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
            hysteresis = self.config.rooms[room_id]['hysteresis']
            on_delta = hysteresis['on_delta_c']
            off_delta = hysteresis['off_delta_c']
            
            # Check if frost protection should activate/continue
            in_frost_protection = self.room_frost_protection_active.get(room_id, False)
            
            if not in_frost_protection and temp < (frost_temp - on_delta):
                # Activate frost protection
                self.room_frost_protection_active[room_id] = True
                self.ad.log(
                    f"FROST PROTECTION ACTIVATED: {room_id} at {temp:.1f}C "
                    f"(threshold: {frost_temp:.1f}C)",
                    level="WARNING"
                )
                
                # Send alert notification (rate limited - only once per activation)
                if not self.room_frost_protection_alerted.get(room_id, False):
                    room_name = self.config.rooms[room_id].get('name', room_id.capitalize())
                    if hasattr(self.ad, 'alerts'):
                        self.ad.alerts.report_error(
                            alert_id=f"frost_protection_{room_id}",
                            severity=self.ad.alerts.SEVERITY_WARNING,
                            message=f"Frost protection activated in {room_name}: {temp:.1f}°C (threshold: {frost_temp:.1f}°C). Emergency heating active.",
                            room_id=room_id,
                            auto_clear=True
                        )
                    self.room_frost_protection_alerted[room_id] = True
                
                return self._frost_protection_heating(room_id, temp, frost_temp, room_mode)
            
            elif in_frost_protection and temp > (frost_temp + off_delta):
                # Deactivate frost protection (recovered)
                self.room_frost_protection_active[room_id] = False
                self.room_frost_protection_alerted[room_id] = False  # Reset alert flag
                if hasattr(self.ad, 'alerts'):
                    self.ad.alerts.clear_error(f"frost_protection_{room_id}")
                self.ad.log(
                    f"FROST PROTECTION DEACTIVATED: {room_id} recovered to {temp:.1f}C",
                    level="INFO"
                )
                # Continue to normal mode logic below
            
            elif in_frost_protection:
                # Continue frost protection heating
                return self._frost_protection_heating(room_id, temp, frost_temp, room_mode)
        
        # Get target info (dict with target, mode, valve_percent)
        target_info = self.scheduler.resolve_room_target(room_id, now, room_mode, holiday_mode, is_stale)
        
        # Extract target temperature (may be setpoint for active, max_temp for passive)
        target = None
        operating_mode = 'off'  # 'active', 'passive', or 'off'
        passive_valve_percent = None
        
        if target_info is not None:
            target = target_info['target']
            operating_mode = target_info['mode']
            passive_valve_percent = target_info.get('valve_percent')
        
        # Get manual setpoint for status display
        manual_setpoint = None
        if room_mode == 'manual':
            manual_setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.ad.entity_exists(manual_setpoint_entity):
                try:
                    manual_setpoint = float(self.ad.get_state(manual_setpoint_entity))
                except (ValueError, TypeError):
                    pass
        
        # Initialize result
        result = {
            'temp': temp,
            'target': target,
            'is_stale': is_stale,
            'mode': room_mode,
            'operating_mode': operating_mode,
            'calling': False,
            'valve_percent': 0,
            'error': None,
            'manual_setpoint': manual_setpoint,
        }
        
        # If no target or no temp (and not manual), can't heat
        if target is None:
            self.room_call_for_heat[room_id] = False
            self.room_current_band[room_id] = 0
            result['valve_percent'] = 0
            # NOTE: Don't send valve command here - let app.py persistence logic handle it
            # (During pump overrun, app.py will use persisted valve positions instead of this 0%)
            return result
        
        if temp is None and room_mode != "manual":
            # Sensors stale and not manual → can't heat safely
            self.room_call_for_heat[room_id] = False
            self.room_current_band[room_id] = 0
            result['valve_percent'] = 0
            # NOTE: Don't send valve command here - let app.py persistence logic handle it
            return result
        
        # Manual mode with stale sensors → use last known or default to target
        if temp is None and room_mode == "manual":
            # Could use a default, but safer to not heat with no sensor
            self.room_call_for_heat[room_id] = False
            self.room_current_band[room_id] = 0
            result['valve_percent'] = 0
            # NOTE: Don't send valve command here - let app.py persistence logic handle it
            return result
        
        # PASSIVE MODE: Threshold control with hysteresis
        if operating_mode == 'passive':
            # Passive never calls for heat
            self.room_call_for_heat[room_id] = False
            self.room_current_band[room_id] = 0
            result['calling'] = False
            
            # Get hysteresis config (same as active mode to maintain consistency)
            hysteresis = self.config.rooms[room_id]['hysteresis']
            on_delta = hysteresis['on_delta_c']
            off_delta = hysteresis['off_delta_c']
            
            # Calculate error (positive = below target, negative = above target)
            error = target - temp
            result['error'] = error
            
            # Get previous valve state for hysteresis
            prev_valve = self.room_last_valve.get(room_id, 0)
            
            # Valve control with hysteresis to prevent cycling:
            # - Open when temp < max_temp - on_delta (e.g., < 17.7C for max_temp=18C)
            # - Close when temp > max_temp + off_delta (e.g., > 18.1C for max_temp=18C)
            # - Dead band: maintain previous state between thresholds
            if error > on_delta:
                # Too cold: open valve
                valve_percent = passive_valve_percent if passive_valve_percent is not None else 0
            elif error < -off_delta:
                # Too warm: close valve
                valve_percent = 0
            else:
                # Dead band: maintain previous state
                valve_percent = prev_valve
            
            result['valve_percent'] = valve_percent
            self.room_last_valve[room_id] = valve_percent
            
            # Persist passive valve state if changed
            if valve_percent != prev_valve:
                try:
                    self.persistence.update_room_state(room_id, passive_valve=valve_percent)
                except Exception as e:
                    self.ad.log(f"Failed to persist passive valve state for {room_id}: {e}", level="WARNING")
            
            return result
        
        # ACTIVE MODE: PID control with call for heat
        # Calculate error
        error = target - temp
        result['error'] = error
        
        # Compute call for heat
        calling = self.compute_call_for_heat(room_id, target, temp)
        result['calling'] = calling
        
        # Update and persist calling state if changed
        prev_calling = self.room_call_for_heat.get(room_id, False)
        self.room_call_for_heat[room_id] = calling
        if calling != prev_calling:
            self._persist_calling_state(room_id, calling)
        
        # Compute valve percentage
        valve_percent = self.compute_valve_percent(room_id, target, temp, calling)
        result['valve_percent'] = valve_percent
        self.room_last_valve[room_id] = valve_percent
        
        return result
    
    def compute_call_for_heat(self, room_id: str, target: float, temp: float) -> bool:
        """Determine if a room should call for heat using asymmetric hysteresis.
        
        Asymmetric hysteresis creates three temperature zones:
        - Zone 1 (too cold): t < S - on_delta → START/Continue heating
        - Zone 2 (deadband): S - on_delta ≤ t ≤ S + off_delta → MAINTAIN state
        - Zone 3 (too warm): t > S + off_delta → STOP heating
        
        When target changes, deadband is bypassed - heat until reaching S + off_delta.
        
        Args:
            room_id: Room identifier
            target: Target temperature (C)
            temp: Current temperature (C)
            
        Returns:
            True if room should call for heat, False otherwise
        """
        # Get hysteresis config
        hysteresis = self.config.rooms[room_id]['hysteresis']
        on_delta = hysteresis['on_delta_c']
        off_delta = hysteresis['off_delta_c']
        
        # Calculate error (positive = below target, negative = above target)
        error = target - temp
        
        # Get previous state and target
        prev_calling = self.room_call_for_heat.get(room_id, False)
        prev_target = self.room_last_target.get(room_id)
        
        # Update target tracking
        self.room_last_target[room_id] = target
        
        # Check if target has changed (with epsilon tolerance for floating-point comparison)
        target_changed = (prev_target is None or 
                         abs(target - prev_target) > C.TARGET_CHANGE_EPSILON)
        
        if target_changed:
            # Target changed → bypass deadband, use only upper threshold
            # Heat until temperature exceeds S + off_delta
            if prev_target is not None:
                self.ad.log(f"Room {room_id}: Target changed {prev_target:.1f}->{target:.1f}C, "
                           f"making fresh heating decision (error={error:.2f}C, t={temp:.1f}C)", level="DEBUG")
            return error >= -off_delta  # t ≤ S + off_delta → heat
        
        # Target unchanged → use normal hysteresis with three zones
        if error > on_delta:
            # Zone 1: t < S - on_delta (too cold)
            return True
        elif error < -off_delta:
            # Zone 3: t > S + off_delta (too warm, overshot)
            return False
        else:
            # Zone 2: S - on_delta ≤ t ≤ S + off_delta (deadband)
            # Maintain previous state
            return prev_calling

    def compute_valve_percent(self, room_id: str, target: float, temp: float, 
                             calling: bool) -> int:
        """Compute valve percentage with 3-band proportional control.
        
        Band Logic (supports 0/1/2 thresholds):
            error < band_1_error:              Band 1 (gentle, close to target)
            band_1_error ≤ error < band_2_error: Band 2 (moderate distance)
            error ≥ band_2_error:              Band Max (far from target)
        
        Band transitions use step_hysteresis_c to prevent oscillation.
        
        INVARIANT: If calling=True, valve MUST be > 0% (enforced at end)
        
        Args:
            room_id: Room identifier
            target: Target temperature (°C)
            temp: Current temperature (°C)
            calling: Whether room is calling for heat
            
        Returns:
            Valve opening percentage (0-100)
        """
        bands = self.config.rooms[room_id]['valve_bands']
        percentages = bands['percentages']
        
        # Not calling = valve closed
        if not calling:
            self.room_current_band[room_id] = 0
            return int(percentages[0])
        
        # Calculate temperature error (positive = need heat)
        error = target - temp
        
        # Get band configuration
        thresholds = bands['thresholds']
        num_bands = bands['num_bands']
        step_hyst = bands['step_hysteresis_c']
        
        # Determine target band based on number of thresholds
        if num_bands == 0:
            # No bands: just 0 or max
            target_band = 'max'
            
        elif num_bands == 1:
            # One threshold: band_1 vs max
            if error < thresholds['band_1']:
                target_band = 1
            else:
                target_band = 'max'
                
        elif num_bands == 2:
            # Two thresholds: band_1, band_2, or max
            if error < thresholds['band_1']:
                target_band = 1
            elif error < thresholds['band_2']:
                target_band = 2
            else:
                target_band = 'max'
        else:
            # Fallback: max
            target_band = 'max'
        
        # Apply band hysteresis (if num_bands > 0)
        current_band = self.room_current_band.get(room_id, 0)
        
        if num_bands == 0:
            # No hysteresis needed
            new_band = target_band
        else:
            new_band = self._apply_band_hysteresis(
                room_id, current_band, target_band, error, 
                thresholds, step_hyst, num_bands
            )
        
        # Get valve percentage
        valve_pct = percentages[new_band]
        
        # ENFORCE INVARIANT: calling rooms must have open valves
        # This handles the "calling with 0% valve" bug regardless of configuration
        if calling and valve_pct == 0:
            # Force to first available band
            if num_bands >= 1:
                valve_pct = percentages[1]
                new_band = 1
            else:
                valve_pct = percentages['max']
                new_band = 'max'
            
            self.ad.log(
                f"Room '{room_id}': calling for heat with error {error:.2f}°C but calculated 0% valve. "
                f"Forcing Band {new_band} ({valve_pct}%) to maintain heat demand.",
                level="INFO"
            )
        
        # Log band changes
        if new_band != current_band:
            self.ad.log(
                f"Room '{room_id}': valve band {current_band} -> {new_band} "
                f"(error={error:.2f}°C, valve={int(valve_pct)}%)",
                level="INFO"
            )
        
        self.room_current_band[room_id] = new_band
        return int(valve_pct)
    
    def _apply_band_hysteresis(self, room_id: str, current_band, target_band, 
                               error: float, thresholds: dict, step_hyst: float,
                               num_bands: int):
        """Apply hysteresis to band transitions.
        
        Args:
            room_id: Room identifier
            current_band: Current band (0, 1, 2, or 'max')
            target_band: Target band based on current error
            error: Temperature error (target - temp)
            thresholds: Dict of threshold values
            step_hyst: Hysteresis step (°C)
            num_bands: Number of defined bands (1 or 2)
            
        Returns:
            New band after applying hysteresis
        """
        # Convert 'max' to numeric for comparison
        max_band_num = num_bands + 1
        curr_num = current_band if isinstance(current_band, int) else max_band_num
        targ_num = target_band if isinstance(target_band, int) else max_band_num
        
        new_num = curr_num  # Default: stay in current band
        
        if targ_num > curr_num:
            # Moving up (more heat) - need to exceed threshold
            if num_bands == 1:
                # Only band_1 threshold
                if targ_num == 1 and error >= thresholds['band_1']:
                    new_num = 1
                elif targ_num == max_band_num and error >= thresholds['band_1']:
                    new_num = max_band_num
            elif num_bands == 2:
                # Both band_1 and band_2 thresholds
                if targ_num == 1 and error >= thresholds['band_1']:
                    new_num = 1
                elif targ_num == 2 and error >= thresholds['band_2']:
                    new_num = 2
                elif targ_num == max_band_num and error >= thresholds['band_2']:
                    new_num = max_band_num
                    
        elif targ_num < curr_num:
            # Moving down (less heat) - need to drop below threshold - hysteresis
            if num_bands == 1:
                # Only band_1 threshold
                if curr_num == max_band_num and error < thresholds['band_1'] - step_hyst:
                    new_num = 1
                elif curr_num == 1 and error < thresholds['band_1'] - step_hyst:
                    new_num = 0
            elif num_bands == 2:
                # Both band_1 and band_2 thresholds
                if curr_num == max_band_num and error < thresholds['band_2'] - step_hyst:
                    new_num = 2
                elif curr_num == 2 and error < thresholds['band_1'] - step_hyst:
                    new_num = 1
                elif curr_num == 1 and error < thresholds['band_1'] - step_hyst:
                    new_num = 0
        
        # Convert back to 'max' if needed
        return new_num if new_num < max_band_num else 'max'
    
    def _frost_protection_heating(self, room_id: str, temp: float, frost_temp: float, room_mode: str) -> Dict:
        """Generate heating command for frost protection mode.
        
        Frost protection uses emergency heating to rapidly recover room temperature
        when it drops below the safety threshold. This overrides all normal heating
        logic and forces maximum heating regardless of room mode or configuration.
        
        Args:
            room_id: Room identifier
            temp: Current temperature (C)
            frost_temp: Frost protection threshold temperature (C)
            room_mode: Room's configured mode (for display only)
            
        Returns:
            Dictionary with frost protection heating state:
            {
                'temp': float,
                'target': float,  # Frost protection temperature
                'is_stale': bool,  # False (temp is valid)
                'mode': str,  # Room's actual mode (auto/manual/passive)
                'operating_mode': str,  # 'frost_protection'
                'calling': bool,  # True (emergency heating)
                'valve_percent': int,  # 100 (maximum heating)
                'error': float,  # frost_temp - temp
                'frost_protection': bool,  # True (flag for status display)
            }
        """
        # Update internal state for frost protection
        self.room_call_for_heat[room_id] = True  # Calling for heat
        self.room_current_band[room_id] = 'max'  # Maximum band
        self.room_last_valve[room_id] = 100  # 100% valve
        
        return {
            'temp': temp,
            'target': frost_temp,
            'is_stale': False,  # Temp is valid (checked before calling)
            'mode': room_mode,  # Actual room mode (for display)
            'operating_mode': 'frost_protection',  # Special operating mode
            'calling': True,  # CALL FOR HEAT (emergency)
            'valve_percent': 100,  # MAXIMUM HEATING (override user settings)
            'error': frost_temp - temp,
            'frost_protection': True,  # Flag for status display and logging
        }
    
    def get_room_state(self, room_id: str) -> Dict:
        """Get current state for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Dict containing room state information
        """
        return {
            'calling': self.room_call_for_heat.get(room_id, False),
            'current_band': self.room_current_band.get(room_id),
            'last_valve': self.room_last_valve.get(room_id)
        }
