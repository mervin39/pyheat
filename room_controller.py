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
        self.room_call_for_heat[room_id] = calling
        
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
        """Compute valve percentage using stepped bands with hysteresis.
        
        Args:
            room_id: Room identifier
            target: Target temperature (C)
            temp: Current temperature (C)
            calling: Whether room is calling for heat
            
        Returns:
            Valve percentage (0-100)
        """
        if not calling:
            self.room_current_band[room_id] = 0
            return 0
        
        # Get valve band config
        bands = self.config.rooms[room_id]['valve_bands']
        t_low = bands['t_low']
        t_mid = bands['t_mid']
        t_max = bands['t_max']
        low_pct = int(bands['low_percent'])
        mid_pct = int(bands['mid_percent'])
        max_pct = int(bands['max_percent'])
        step_hyst = bands['step_hysteresis_c']
        
        # Calculate error (positive = below target)
        error = target - temp
        
        # Get current band (default to 0 if not set)
        current_band = self.room_current_band.get(room_id, 0)
        
        # Determine new band with hysteresis
        new_band = current_band
        
        # Determine target band based on error (without hysteresis)
        if error < t_low:
            target_band = 0
        elif error < t_mid:
            target_band = 1
        elif error < t_max:
            target_band = 2
        else:
            target_band = 3
        
        # Apply hysteresis rules
        if target_band > current_band:
            # Increasing demand - check if we've crossed threshold + hysteresis
            if target_band == 1 and error >= t_low + step_hyst:
                new_band = 1
            elif target_band == 2 and error >= t_mid + step_hyst:
                new_band = 2
            elif target_band == 3 and error >= t_max + step_hyst:
                new_band = 3
        elif target_band < current_band:
            # Decreasing demand - only drop one band at a time
            if current_band == 3 and error < t_max - step_hyst:
                new_band = 2
            elif current_band == 2 and error < t_mid - step_hyst:
                new_band = 1
            elif current_band == 1 and error < t_low - step_hyst:
                new_band = 0
        
        # Store new band
        self.room_current_band[room_id] = new_band
        
        # Map band to percentage
        band_to_percent = {
            0: 0,
            1: low_pct,
            2: mid_pct,
            3: max_pct
        }
        
        valve_pct = band_to_percent[new_band]
        
        # Log band changes
        if new_band != current_band:
            self.ad.log(
                f"Room '{room_id}': valve band {current_band} -> {new_band} "
                f"(error={error:.2f}C, valve={valve_pct}%)",
                level="INFO"
            )
        
        return valve_pct
    
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
