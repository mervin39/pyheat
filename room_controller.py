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
import json
import pyheat.constants as C


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
        
        self.room_call_for_heat = {}  # {room_id: bool}
        self.room_current_band = {}  # {room_id: band_index}
        self.room_last_valve = {}  # {room_id: percent}
        self.room_last_target = {}  # {room_id: float} - tracks previous target to detect changes
        
    def initialize_from_ha(self) -> None:
        """Initialize room state from current Home Assistant TRV positions.
        
        CRITICAL: Initialize room_call_for_heat based on current valve position.
        If valve is open (>0%), assume room was calling for heat before restart.
        This prevents sudden valve closures when in hysteresis deadband on startup
        (prevents boiler running with all valves closed after AppDaemon restart).
        
        Also initialize room_last_target to current targets to prevent false
        "target changed" detection on first recompute after restart.
        """
        for room_id, room_cfg in self.config.rooms.items():
            if room_cfg.get('disabled'):
                continue
                
            # Get current valve position from TRV controller
            fb_valve_entity = room_cfg['trv']['fb_valve']
            try:
                fb_valve_str = self.ad.get_state(fb_valve_entity)
                if fb_valve_str and fb_valve_str not in ['unknown', 'unavailable']:
                    fb_valve = int(float(fb_valve_str))
                    
                    # If valve is open, assume room was calling for heat
                    if fb_valve > 0:
                        self.room_call_for_heat[room_id] = True
                        self.ad.log(f"Room {room_id}: Initialized, valve at {fb_valve}%, assumed calling for heat", level="DEBUG")
                    else:
                        self.ad.log(f"Room {room_id}: Initialized, valve at {fb_valve}%", level="DEBUG")
            except (ValueError, TypeError) as e:
                self.ad.log(f"Failed to initialize room {room_id} state: {e}", level="WARNING")
            
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
                current_target = self.scheduler.resolve_room_target(room_id, now, room_mode, holiday_mode, False)
                if current_target is not None:
                    self.room_last_target[room_id] = current_target
                    self.ad.log(f"Room {room_id}: Initialized target tracking at {current_target}C", level="DEBUG")
            except Exception as e:
                self.ad.log(f"Failed to initialize target tracking for room {room_id}: {e}", level="WARNING")
        
        # Load persisted calling state from HA
        self._load_persisted_state()
        
    def _load_persisted_state(self) -> None:
        """Load last_calling state from HA persistence entity on init.
        
        Migrates data from old pyheat_pump_overrun_valves format if needed.
        Format: {"pete": [valve_percent, last_calling], ...}
        Array indices: [0]=valve_percent (0-100), [1]=last_calling (0=False, 1=True)
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            self.ad.log("Room persistence entity does not exist, skipping state load", level="DEBUG")
            return
            
        try:
            data_str = self.ad.get_state(C.HELPER_ROOM_PERSISTENCE)
            if not data_str or data_str in ['unknown', 'unavailable']:
                # Try migrating from old format
                self._migrate_from_old_format()
                return
                
            data = json.loads(data_str)
            for room_id, arr in data.items():
                if len(arr) >= 2 and room_id in self.config.rooms:
                    # Override valve-based heuristic with persisted state
                    persisted_calling = bool(arr[1])
                    self.room_call_for_heat[room_id] = persisted_calling
                    self.ad.log(f"Room {room_id}: Loaded persisted calling state = {persisted_calling}", level="DEBUG")
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.ad.log(f"Failed to parse room persistence: {e}, using valve-based heuristic", level="WARNING")
    
    def _migrate_from_old_format(self) -> None:
        """One-time migration from old pyheat_pump_overrun_valves format.
        
        Old format: {"pete": 70, "lounge": 100, ...}
        New format: {"pete": [70, 0], "lounge": [100, 0], ...}
        """
        if not self.ad.entity_exists(C.HELPER_PUMP_OVERRUN_VALVES):
            return
            
        try:
            old_data_str = self.ad.get_state(C.HELPER_PUMP_OVERRUN_VALVES)
            if not old_data_str or old_data_str in ['unknown', 'unavailable', '']:
                return
                
            old_data = json.loads(old_data_str)
            if not old_data:
                return
                
            # Convert old format to new format
            new_data = {}
            for room_id, valve_percent in old_data.items():
                if room_id in self.config.rooms:
                    # Preserve valve position, initialize calling based on valve > 0
                    calling = 1 if valve_percent > 0 else 0
                    new_data[room_id] = [int(valve_percent), calling]
            
            # Save to new entity
            if new_data:
                self.ad.call_service("input_text/set_value",
                    entity_id=C.HELPER_ROOM_PERSISTENCE,
                    value=json.dumps(new_data, separators=(',', ':'))
                )
                self.ad.log(f"Migrated {len(new_data)} rooms from old persistence format", level="INFO")
                
                # Clear old entity
                self.ad.call_service("input_text/set_value",
                    entity_id=C.HELPER_PUMP_OVERRUN_VALVES,
                    value=""
                )
        except (json.JSONDecodeError, ValueError, TypeError, Exception) as e:
            self.ad.log(f"Failed to migrate from old persistence format: {e}", level="WARNING")
    
    def _persist_calling_state(self, room_id: str, calling: bool) -> None:
        """Update last_calling in persistence entity.
        
        Preserves existing valve_percent while updating calling state.
        """
        if not self.ad.entity_exists(C.HELPER_ROOM_PERSISTENCE):
            return
            
        try:
            data_str = self.ad.get_state(C.HELPER_ROOM_PERSISTENCE)
            data = json.loads(data_str) if data_str and data_str not in ['unknown', 'unavailable', ''] else {}
            
            # Initialize room entry if missing
            if room_id not in data:
                data[room_id] = [0, 0]
            
            # Update calling state (preserve valve_percent at index 0)
            data[room_id][1] = 1 if calling else 0
            
            self.ad.call_service("input_text/set_value",
                entity_id=C.HELPER_ROOM_PERSISTENCE,
                value=json.dumps(data, separators=(',', ':'))
            )
        except (json.JSONDecodeError, ValueError, TypeError, Exception) as e:
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
        
        # Get target
        target = self.scheduler.resolve_room_target(room_id, now, room_mode, holiday_mode, is_stale)
        
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
