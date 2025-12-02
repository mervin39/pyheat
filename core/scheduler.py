# -*- coding: utf-8 -*-
"""
scheduler.py - Schedule resolution and target temperature calculation

Responsibilities:
- Resolve room target temperatures based on mode and schedule
- Handle override timers
- Apply holiday mode
- Manage manual setpoints
"""

from datetime import datetime
from typing import Optional, Dict
import constants as C


class Scheduler:
    """Handles schedule resolution and target temperature determination."""
    
    def __init__(self, ad, config, override_manager=None):
        """Initialize the scheduler.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            override_manager: OverrideManager instance (optional, for override checking)
        """
        self.ad = ad
        self.config = config
        self.override_manager = override_manager
        
    def resolve_room_target(self, room_id: str, now: datetime, room_mode: str, 
                           holiday_mode: bool, is_stale: bool) -> Optional[Dict]:
        """Resolve the target temperature and operating mode for a room.
        
        Precedence (highest wins): off → manual → passive → override → schedule/default
        
        Args:
            room_id: Room identifier
            now: Current datetime
            room_mode: Room mode (auto/manual/passive/off)
            holiday_mode: Whether holiday mode is active
            is_stale: Whether temperature sensors are stale
            
        Returns:
            Dict with keys:
                'target': float - Temperature (setpoint or max temp)
                'mode': str - 'active' or 'passive'
                'valve_percent': Optional[int] - For passive mode only
            Or None if room is off
            Note: Manual mode returns target even if sensors are stale
        """
        # Room off → no target
        if room_mode == "off":
            return None
        
        # Manual mode → active heating with manual setpoint
        if room_mode == "manual":
            setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.ad.entity_exists(setpoint_entity):
                try:
                    setpoint = float(self.ad.get_state(setpoint_entity))
                    # Round to room precision
                    precision = self.config.rooms[room_id].get('precision', 1)
                    return {
                        'target': round(setpoint, precision),
                        'mode': 'active',
                        'valve_percent': None,
                        'min_target': None,
                        'is_default_mode': False  # Manual mode (not schedule-based)
                    }
                except (ValueError, TypeError):
                    self.ad.log(f"Invalid manual setpoint for room '{room_id}'", level="WARNING")
                    return None
            return None
        
        # Passive mode → passive heating with helper entity values
        if room_mode == "passive":
            max_temp = self._get_passive_max_temp(room_id)
            valve_pct = self._get_passive_valve_percent(room_id)
            min_temp = self._get_passive_min_temp(room_id)
            precision = self.config.rooms[room_id].get('precision', 1)
            return {
                'target': round(max_temp, precision),
                'mode': 'passive',
                'valve_percent': valve_pct,
                'min_target': round(min_temp, precision),
                'is_default_mode': False  # Passive mode (user-selected, not schedule-based)
            }
        
        # Auto mode → check for override, then schedule
        
        # Check for active override via override manager
        # Override ALWAYS forces active heating (not passive)
        if self.override_manager:
            override_target = self.override_manager.get_override_target(room_id)
            if override_target is not None:
                precision = self.config.rooms[room_id].get('precision', 1)
                return {
                    'target': round(override_target, precision),
                    'mode': 'active',
                    'valve_percent': None,
                    'min_target': None,
                    'is_default_mode': False  # Override (temporary)
                }
        
        # No override → get scheduled target (may be active or passive)
        scheduled_info = self.get_scheduled_target(room_id, now, holiday_mode)
        if scheduled_info is not None:
            precision = self.config.rooms[room_id].get('precision', 1)
            # Round target but preserve mode and valve_percent
            scheduled_info['target'] = round(scheduled_info['target'], precision)
            return scheduled_info
        
        return None
        
    def _get_passive_max_temp(self, room_id: str) -> float:
        """Get the passive max temperature for a room from helper entity.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Passive max temperature (defaults to PASSIVE_MAX_TEMP_DEFAULT if not found)
        """
        entity = C.HELPER_ROOM_PASSIVE_MAX_TEMP.format(room=room_id)
        if self.ad.entity_exists(entity):
            try:
                return float(self.ad.get_state(entity))
            except (ValueError, TypeError):
                self.ad.log(f"Invalid passive_max_temp for room '{room_id}', using default", level="WARNING")
        return C.PASSIVE_MAX_TEMP_DEFAULT
    
    def _get_passive_valve_percent(self, room_id: str) -> int:
        """Get the passive valve opening percentage for a room from helper entity.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Passive valve opening percentage (defaults to PASSIVE_VALVE_PERCENT_DEFAULT if not found)
        """
        entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room_id)
        if self.ad.entity_exists(entity):
            try:
                return int(float(self.ad.get_state(entity)))
            except (ValueError, TypeError):
                self.ad.log(f"Invalid passive_valve_percent for room '{room_id}', using default", level="WARNING")
        return C.PASSIVE_VALVE_PERCENT_DEFAULT
    
    def _get_passive_min_temp(self, room_id: str) -> float:
        """Get the passive minimum temperature (comfort floor) for a room from helper entity.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Passive minimum temperature (defaults to frost_protection_temp_c if not found)
        """
        entity = C.HELPER_ROOM_PASSIVE_MIN_TEMP.format(room=room_id)
        if self.ad.entity_exists(entity):
            try:
                min_temp = float(self.ad.get_state(entity))
                # Validate that min_temp >= frost_protection_temp_c
                frost_temp = self.config.system_config.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
                if min_temp < frost_temp:
                    self.ad.log(f"Room '{room_id}' passive_min_temp ({min_temp}C) is below frost_protection_temp_c ({frost_temp}C), using frost protection temp", level="WARNING")
                    return frost_temp
                return min_temp
            except (ValueError, TypeError):
                self.ad.log(f"Invalid passive_min_temp for room '{room_id}', using frost protection temp", level="WARNING")
        # Default to frost protection temperature
        return self.config.system_config.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
    
    def get_scheduled_target(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[Dict]:
        """Get the scheduled target temperature and mode for a room.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active
            
        Returns:
            Dict with keys:
                'target': float - Temperature (setpoint or max temp)
                'mode': str - 'active' or 'passive'
                'valve_percent': Optional[int] - For passive mode only
            Or None if no schedule
        """
        schedule = self.config.schedules.get(room_id)
        if not schedule:
            return None
        
        # If holiday mode, return active heating at holiday target
        if holiday_mode:
            return {
                'target': C.HOLIDAY_TARGET_C,
                'mode': 'active',
                'valve_percent': None,
                'min_target': None,
                'is_default_mode': False  # Holiday mode
            }
        
        # Get day of week (0=Monday, 6=Sunday)
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        day_name = day_names[now.weekday()]
        
        # Get blocks for today
        week_schedule = schedule.get('week', {})
        blocks = week_schedule.get(day_name, [])
        
        # Find active block
        current_time = now.strftime("%H:%M")
        
        for block in blocks:
            start_time = block['start']
            end_time = block.get('end', '23:59')
            
            # Convert end='23:59' to '24:00' for comparison
            if end_time == '23:59':
                end_time = '24:00'
            
            # Check if current time is within block (start inclusive, end exclusive)
            if start_time <= current_time < end_time:
                # Get mode from block (default to active if not specified)
                block_mode = block.get('mode', 'active')
                valve_percent = block.get('valve_percent') if block_mode == 'passive' else None
                
                # For passive mode, check for min_target in schedule
                min_target = None
                if block_mode == 'passive':
                    # Scheduled min_target takes precedence, otherwise fall back to entity
                    if 'min_target' in block:
                        min_target = block['min_target']
                        # Validate against frost protection temp
                        frost_temp = self.config.system_config.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
                        if min_target < frost_temp:
                            self.ad.log(f"Room '{room_id}' scheduled min_target ({min_target}C) is below frost_protection_temp_c ({frost_temp}C), using frost protection temp", level="ERROR")
                            min_target = frost_temp
                    else:
                        # Use entity value
                        min_target = self._get_passive_min_temp(room_id)
                
                return {
                    'target': block['target'],
                    'mode': block_mode,
                    'valve_percent': valve_percent,
                    'min_target': min_target,
                    'is_default_mode': False,  # This is from a scheduled block
                    'block_end_time': block.get('end', '23:59')  # When this block ends
                }
        
        # No active block → return default target
        # Check if default should use passive mode
        default_mode = schedule.get('default_mode', 'active')
        
        if default_mode == 'passive':
            default_target = schedule.get('default_target')
            # Use schedule values if provided, otherwise fall back to entity values
            valve_percent = schedule.get('default_valve_percent')
            if valve_percent is None:
                valve_percent = self._get_passive_valve_percent(room_id)
            
            min_temp = schedule.get('default_min_temp')
            if min_temp is None:
                min_temp = self._get_passive_min_temp(room_id)
            else:
                # Validate against frost protection temp
                frost_temp = self.config.system_config.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
                if min_temp < frost_temp:
                    self.ad.log(f"Room '{room_id}' default_min_temp ({min_temp}C) is below frost_protection_temp_c ({frost_temp}C), using frost protection temp", level="ERROR")
                    min_temp = frost_temp
            
            return {
                'target': default_target,
                'mode': 'passive',
                'valve_percent': valve_percent,
                'min_target': min_temp,
                'is_default_mode': True  # This is from default_mode
            }
        else:
            return {
                'target': schedule.get('default_target'),
                'mode': 'active',
                'valve_percent': None,
                'min_target': None,
                'is_default_mode': True  # This is from default_target
            }
    
    def get_next_schedule_change(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[tuple[str, float, int]]:
        """Get the next schedule change time and target temperature.
        
        Enhanced to detect gaps between blocks and return default_target for gap starts.
        Now skips blocks with the same temperature to find the next actual temperature change.
        Searches through the entire week to find next change.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active
            
        Returns:
            Tuple of (time_string, target_temp, day_offset) for next change, or None if no schedule
            time_string format: "HH:MM" (24-hour)
            day_offset: 0 for today, 1 for tomorrow, etc.
        """
        schedule = self.config.schedules.get(room_id)
        if not schedule or holiday_mode:
            return None
        
        # Get current target temperature to compare against
        current_target_info = self.resolve_room_target(room_id, now, "auto", holiday_mode, False)
        if current_target_info is None:
            return None
        current_target = current_target_info['target']
        
        # Get day of week (0=Monday, 6=Sunday)
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        current_day_idx = now.weekday()
        
        # Get schedule details
        week_schedule = schedule.get('week', {})
        current_time = now.strftime("%H:%M")
        default_target = schedule.get('default_target')
        
        # Track what temperature we're at as we scan forward
        scanning_target = current_target
        
        # Search through the week starting from today
        for day_offset in range(8):  # Check up to 8 days (full week + 1 to handle wraparound)
            day_idx = (current_day_idx + day_offset) % 7
            day_name = day_names[day_idx]
            blocks = week_schedule.get(day_name, [])
            
            if day_offset == 0:
                # TODAY - start from current time
                
                # Check if we're currently in a block
                in_block = False
                current_block_end = None
                
                for block in blocks:
                    block_start = block['start']
                    block_end = block.get('end', '23:59')
                    if block_end == '23:59':
                        block_end = '24:00'
                    
                    if block_start <= current_time < block_end:
                        in_block = True
                        current_block_end = block_end
                        scanning_target = block['target']
                        break
                
                if in_block:
                    # Check remaining blocks today after current block
                    for block in blocks:
                        if block['start'] >= current_block_end:
                            if block['target'] != scanning_target:
                                return (block['start'], block['target'], 0)
                            scanning_target = block['target']
                    
                    # Check if there's a gap at end of current block
                    has_next_block = any(b['start'] == current_block_end for b in blocks)
                    if not has_next_block and current_block_end != '24:00':
                        if default_target != scanning_target:
                            return (current_block_end, default_target, 0)
                        scanning_target = default_target
                    # If block goes to end of day, maintain its target for tomorrow
                else:
                    # Currently in gap - check remaining blocks today
                    scanning_target = default_target
                    for block in blocks:
                        if block['start'] > current_time:
                            if block['target'] != scanning_target:
                                return (block['start'], block['target'], 0)
                            # Update scanning target for rest of day
                            block_end = block.get('end', '23:59')
                            if block_end == '23:59':
                                block_end = '24:00'
                            scanning_target = block['target']
                            
                            # Check if there's a gap after this block
                            has_next_block = any(b['start'] == block_end for b in blocks if b['start'] > block['start'])
                            if not has_next_block and block_end != '24:00':
                                if default_target != scanning_target:
                                    return (block_end, default_target, 0)
                                scanning_target = default_target
                
                # At end of today, determine what temperature we'll be at midnight
                if blocks:
                    last_block = blocks[-1]
                    last_end = last_block.get('end', '23:59')
                    if last_end == '23:59':
                        last_end = '24:00'
                    
                    if last_end == '24:00':
                        scanning_target = last_block['target']
                    else:
                        scanning_target = default_target
                else:
                    scanning_target = default_target
                    
            else:
                # FUTURE DAYS - check from 00:00
                
                if not blocks:
                    # No blocks this day - stays at current scanning target (likely default)
                    # No change happens
                    continue
                
                # Check if first block starts at 00:00 or if there's a gap
                first_block = blocks[0]
                
                if first_block['start'] != '00:00':
                    # Gap at midnight - check if temp changes to default
                    if default_target != scanning_target:
                        return ('00:00', default_target, day_offset)
                    scanning_target = default_target
                    
                    # Check first block
                    if first_block['target'] != scanning_target:
                        return (first_block['start'], first_block['target'], day_offset)
                    scanning_target = first_block['target']
                else:
                    # Block at 00:00
                    if first_block['target'] != scanning_target:
                        return ('00:00', first_block['target'], day_offset)
                    scanning_target = first_block['target']
                
                # Check remaining blocks in this day
                for i, block in enumerate(blocks[1:], 1):
                    if block['target'] != scanning_target:
                        return (block['start'], block['target'], day_offset)
                    scanning_target = block['target']
                
                # Check what temperature we'll be at end of this day
                last_block = blocks[-1]
                last_end = last_block.get('end', '23:59')
                if last_end == '23:59':
                    last_end = '24:00'
                
                if last_end != '24:00':
                    # Gap at end of day
                    if default_target != scanning_target:
                        return (last_end, default_target, day_offset)
                    scanning_target = default_target
                # else: block goes to midnight, scanning_target stays as is
            
            # If we've checked a full week and nothing changed, it's forever
            if day_offset >= 7:
                break
        
        # No next change found (truly forever)
        return None
    
    def get_next_schedule_block(self, room_id: str, from_time: datetime, 
                               within_minutes: int) -> Optional[tuple[datetime, datetime, float, str]]:
        """Get the next schedule block within a time window.
        
        Used by load sharing to determine if a room has an upcoming schedule
        that would benefit from pre-warming.
        
        Args:
            room_id: Room identifier
            from_time: Start time to search from
            within_minutes: Maximum lookahead window in minutes
            
        Returns:
            Tuple of (block_start_datetime, block_end_datetime, target_temp, block_mode) or None
            block_mode is 'active' (default) or 'passive'
            
        Phase 0: Infrastructure only
        """
        schedule = self.config.schedules.get(room_id)
        if not schedule:
            return None
        
        default_target = schedule.get('default_target', 16.0)
        week_schedule = schedule.get('week', {})
        
        # Calculate end time of search window
        from datetime import timedelta
        search_end = from_time + timedelta(minutes=within_minutes)
        
        # Search through schedule blocks
        current_time = from_time
        while current_time <= search_end:
            day_name = current_time.strftime('%A').lower()
            day_schedule = week_schedule.get(day_name, {})
            blocks = day_schedule.get('blocks', [])
            
            current_time_str = current_time.strftime('%H:%M')
            
            # Find next block that starts after current_time on this day
            for block in blocks:
                block_start_str = block['start']
                block_end_str = block.get('end', '23:59')
                block_target = block['target']
                block_mode = block.get('mode', 'active')  # Read block mode
                
                # Parse times for comparison
                block_start_h, block_start_m = map(int, block_start_str.split(':'))
                block_end_h, block_end_m = map(int, block_end_str.split(':'))
                current_h, current_m = map(int, current_time_str.split(':'))
                
                # Convert to minutes since midnight for comparison
                block_start_mins = block_start_h * 60 + block_start_m
                block_end_mins = block_end_h * 60 + block_end_m
                current_mins = current_h * 60 + current_m
                
                # Check if block starts within our window
                if block_start_mins >= current_mins:
                    # Calculate absolute datetimes
                    block_start_dt = current_time.replace(
                        hour=block_start_h, 
                        minute=block_start_m, 
                        second=0, 
                        microsecond=0
                    )
                    block_end_dt = current_time.replace(
                        hour=block_end_h, 
                        minute=block_end_m, 
                        second=0, 
                        microsecond=0
                    )
                    
                    # Only return if block_target is higher than default (heating needed)
                    if block_target > default_target and block_start_dt <= search_end:
                        return (block_start_dt, block_end_dt, block_target, block_mode)
            
            # Move to next day
            current_time = (current_time + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        
        return None
