# -*- coding: utf-8 -*-
"""
scheduler.py - Schedule resolution and target temperature calculation

Responsibilities:
- Resolve room target temperatures based on mode and schedule
- Handle override/boost timers
- Apply holiday mode
- Manage manual setpoints
"""

from datetime import datetime
from typing import Optional
import pyheat.constants as C


class Scheduler:
    """Handles schedule resolution and target temperature determination."""
    
    def __init__(self, ad, config):
        """Initialize the scheduler.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        
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
        
        # Check for active override/boost
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            timer_state = self.ad.get_state(timer_entity)
            if timer_state in ["active", "paused"]:
                # Override is active, get the target
                target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                if self.ad.entity_exists(target_entity):
                    try:
                        override_target = float(self.ad.get_state(target_entity))
                        # Sentinel value 0 means cleared (entity min is 5)
                        if override_target >= C.TARGET_MIN_C:
                            precision = self.config.rooms[room_id].get('precision', 1)
                            return round(override_target, precision)
                    except (ValueError, TypeError):
                        self.ad.log(f"Invalid override target for room '{room_id}'", level="WARNING")
        
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
    
    def get_next_schedule_change(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[tuple[str, float]]:
        """Get the next schedule change time and target temperature.
        
        Enhanced to detect gaps between blocks and return default_target for gap starts.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active
            
        Returns:
            Tuple of (time_string, target_temp) for next change, or None if no schedule
            time_string format: "HH:MM" (24-hour)
        """
        schedule = self.config.schedules.get(room_id)
        if not schedule or holiday_mode:
            return None
        
        # Get day of week (0=Monday, 6=Sunday)
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        day_name = day_names[now.weekday()]
        
        # Get blocks for today
        week_schedule = schedule.get('week', {})
        blocks = week_schedule.get(day_name, [])
        
        # Current time
        current_time = now.strftime("%H:%M")
        default_target = schedule.get('default_target')
        
        # Check if we're currently in a block
        in_block = False
        current_block_end = None
        for block in blocks:
            start_time = block['start']
            end_time = block.get('end', '23:59')
            
            if end_time == '23:59':
                end_time = '24:00'
            
            if start_time <= current_time < end_time:
                in_block = True
                current_block_end = end_time
                break
        
        # Find next event
        if in_block:
            # Currently in block - next event is end of this block OR start of next block
            # Look for next block after current block end
            next_block_start = None
            next_block_target = None
            
            for block in blocks:
                start_time = block['start']
                if start_time >= current_block_end:
                    next_block_start = start_time
                    next_block_target = block['target']
                    break
            
            # Check if there's a gap
            if next_block_start is None or next_block_start > current_block_end:
                # Gap exists - next change is to default_target at current_block_end
                if current_block_end == '24:00':
                    # End of day - check tomorrow for next block
                    tomorrow_idx = (now.weekday() + 1) % 7
                    tomorrow_name = day_names[tomorrow_idx]
                    tomorrow_blocks = week_schedule.get(tomorrow_name, [])
                    
                    if tomorrow_blocks:
                        first_block = tomorrow_blocks[0]
                        # If tomorrow's first block doesn't start at 00:00, return default at 00:00
                        if first_block['start'] != '00:00':
                            return ('00:00', default_target)
                        else:
                            return (first_block['start'], first_block['target'])
                    else:
                        # Tomorrow has no blocks - stays at default
                        return None
                else:
                    # Gap today - return default_target at block end
                    return (current_block_end, default_target)
            else:
                # Next block starts immediately after current - return that
                return (next_block_start, next_block_target)
        else:
            # Currently NOT in block (in gap or no blocks) - find next block start
            for block in blocks:
                start_time = block['start']
                if start_time > current_time:
                    return (start_time, block['target'])
            
            # No more blocks today - check tomorrow
            tomorrow_idx = (now.weekday() + 1) % 7
            tomorrow_name = day_names[tomorrow_idx]
            tomorrow_blocks = week_schedule.get(tomorrow_name, [])
            
            if tomorrow_blocks:
                # Return first block of tomorrow
                first_block = tomorrow_blocks[0]
                return (first_block['start'], first_block['target'])
        
        # No next change found
        return None
