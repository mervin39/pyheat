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
from typing import Optional
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
                           holiday_mode: bool, is_stale: bool) -> Optional[float]:
        """Resolve the target temperature for a room.
        
        Precedence (highest wins): off → manual → override → schedule/default
        
        Args:
            room_id: Room identifier
            now: Current datetime
            room_mode: Room mode (auto/manual/off)
            holiday_mode: Whether holiday mode is active
            is_stale: Whether temperature sensors are stale
            
        Returns:
            Target temperature in C, or None if room is off
            Note: Manual mode returns target even if sensors are stale
        """
        # Room off → no target
        if room_mode == "off":
            return None
        
        # Manual mode → use manual setpoint
        if room_mode == "manual":
            setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.ad.entity_exists(setpoint_entity):
                try:
                    setpoint = float(self.ad.get_state(setpoint_entity))
                    # Round to room precision
                    precision = self.config.rooms[room_id].get('precision', 1)
                    return round(setpoint, precision)
                except (ValueError, TypeError):
                    self.ad.log(f"Invalid manual setpoint for room '{room_id}'", level="WARNING")
                    return None
            return None
        
        # Auto mode → check for override, then schedule
        
        # Check for active override via override manager
        if self.override_manager:
            override_target = self.override_manager.get_override_target(room_id)
            if override_target is not None:
                precision = self.config.rooms[room_id].get('precision', 1)
                return round(override_target, precision)
        
        # No override → get scheduled target
        scheduled_target = self.get_scheduled_target(room_id, now, holiday_mode)
        if scheduled_target is not None:
            precision = self.config.rooms[room_id].get('precision', 1)
            return round(scheduled_target, precision)
        
        return None
        
    def get_scheduled_target(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[float]:
        """Get the scheduled target temperature for a room.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active
            
        Returns:
            Target temperature from schedule or default, or None if no schedule
        """
        schedule = self.config.schedules.get(room_id)
        if not schedule:
            return None
        
        # If holiday mode, return holiday target
        if holiday_mode:
            return C.HOLIDAY_TARGET_C
        
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
                return block['target']
        
        # No active block → return default
        return schedule.get('default_target')
    
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
        current_target = self.resolve_room_target(room_id, now, "auto", holiday_mode, False)
        if current_target is None:
            return None
        
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
