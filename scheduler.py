"""
scheduler.py - Schedule resolution and override/boost handling

Responsibilities:
- Resolve scheduled target for a room at a given local time
- Apply holiday mode substitution of base/default target
- Combine schedule with optional override/boost (precedence handled in room_controller)
- Expose block metadata for status strings
- React to in-memory schedule reloads (no disk I/O)

Schedule structure:
- Default target applies outside any blocks
- Blocks define time ranges with specific targets
- Times are local wall clock in HA's timezone
- Blocks are non-overlapping (validated by config_loader)
"""

from datetime import datetime, time as dt_time
from typing import Dict, Optional, List, Literal, Any
from . import constants


class ScheduleManager:
    """Manages heating schedules and resolves targets."""
    
    def __init__(self):
        """Initialize the schedule manager."""
        self.schedules: Dict[str, Dict[str, Any]] = {}  # room_id -> schedule data
        self.holiday_mode = False
        
        log.debug("ScheduleManager: initialized")
    
    def reload(self, schedules: Dict) -> None:
        """Replace in-memory schedules after validation by config_loader.
        
        Args:
            schedules: Parsed schedules.yaml dict with structure:
                {
                    "rooms": [
                        {
                            "id": "living",
                            "default_target": 19.5,
                            "week": {
                                "mon": [
                                    {"start": "07:00", "end": "09:00", "target": 21.0},
                                    {"start": "17:00", "end": "22:00", "target": 21.0}
                                ],
                                "tue": [...],
                                ...
                            }
                        },
                        ...
                    ]
                }
        """
        log.info("ScheduleManager: reloading schedules")
        
        self.schedules.clear()
        
        for room_sched in schedules.get("rooms", []):
            room_id = room_sched.get("id")
            if not room_id:
                log.warning("ScheduleManager: skipping schedule with no ID")
                continue
            
            default_target = room_sched.get("default_target")
            week = room_sched.get("week", {})
            
            # Parse and sort blocks for each day
            parsed_week = {}
            for day_name in constants.WEEKDAY_KEYS:
                blocks = week.get(day_name, [])
                parsed_blocks = []
                
                for block in blocks:
                    start_str = block.get("start")
                    end_str = block.get("end")
                    target = block.get("target")
                    
                    if not start_str or target is None:
                        log.warning(f"ScheduleManager: room {room_id} {day_name} has incomplete block, skipping")
                        continue
                    
                    # Parse times
                    try:
                        start_time = datetime.strptime(start_str, constants.TIME_FORMAT).time()
                        
                        # End time is optional (defaults to midnight)
                        if end_str:
                            # Handle 23:59 as midnight
                            if end_str == "23:59":
                                end_time = dt_time(23, 59, 59)  # End of day
                            else:
                                end_time = datetime.strptime(end_str, constants.TIME_FORMAT).time()
                        else:
                            end_time = dt_time(23, 59, 59)  # Until midnight
                        
                        parsed_blocks.append({
                            "start": start_time,
                            "end": end_time,
                            "target": target,
                        })
                    except ValueError as e:
                        log.error(f"ScheduleManager: room {room_id} {day_name} has invalid time format: {e}")
                        continue
                
                # Sort blocks by start time
                parsed_blocks.sort(key=lambda b: b["start"])
                parsed_week[day_name] = parsed_blocks
            
            # Store schedule
            self.schedules[room_id] = {
                "default_target": default_target,
                "week": parsed_week,
            }
            
            # Log summary (avoid generator expression for pyscript compatibility)
            total_blocks = 0
            for blocks in parsed_week.values():
                total_blocks += len(blocks)
            log.info(f"ScheduleManager: room {room_id} loaded with default={default_target}°C, {total_blocks} block(s)")
        
        log.info(f"ScheduleManager: loaded {len(self.schedules)} room schedule(s)")
    
    def set_holiday_mode(self, enabled: bool) -> None:
        """Enable or disable holiday mode.
        
        Args:
            enabled: True to enable holiday mode, False to disable
        """
        if self.holiday_mode != enabled:
            self.holiday_mode = enabled
            log.info(f"ScheduleManager: holiday mode {'enabled' if enabled else 'disabled'}")
    
    def get_scheduled_target(self, room_id: str, ts: datetime) -> Optional[float]:
        """Resolve scheduled target for a room at a given time.
        
        This returns the default target or block target, with holiday mode applied.
        Does NOT include overrides/boosts (those are handled in room_controller).
        
        Args:
            room_id: Room ID
            ts: Local datetime (in HA's timezone)
            
        Returns:
            Target temperature in °C, or None if room has no schedule
        """
        if room_id not in self.schedules:
            log.debug(f"ScheduleManager: no schedule for room {room_id}")
            return None
        
        schedule = self.schedules[room_id]
        default_target = schedule["default_target"]
        
        # Get the base target (from schedule block or default)
        base_target = self._get_base_target(room_id, ts)
        if base_target is None:
            base_target = default_target
        
        # Apply holiday mode if enabled
        if self.holiday_mode:
            final_target = constants.HOLIDAY_TARGET_C
            log.debug(f"ScheduleManager: room {room_id} using holiday target {final_target}°C (base was {base_target}°C)")
        else:
            final_target = base_target
        
        return final_target
    
    def _get_base_target(self, room_id: str, ts: datetime) -> Optional[float]:
        """Get the base scheduled target (block or default) without holiday mode.
        
        Args:
            room_id: Room ID
            ts: Local datetime
            
        Returns:
            Target from active block, or None to use default
        """
        if room_id not in self.schedules:
            return None
        
        schedule = self.schedules[room_id]
        
        # Get day name (0=Monday, 6=Sunday)
        weekday_idx = ts.weekday()
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day_name = day_names[weekday_idx]
        
        blocks = schedule["week"].get(day_name, [])
        current_time = ts.time()
        
        # Find active block
        for block in blocks:
            start = block["start"]
            end = block["end"]
            
            # Check if current time is in this block
            # Block is inclusive of start, exclusive of end
            if start <= current_time < end:
                log.debug(f"ScheduleManager: room {room_id} in {day_name} block {start}-{end}: {block['target']}°C")
                return block["target"]
        
        # No active block, use default (caller will handle)
        return None
    
    def current_block(self, room_id: str, ts: datetime) -> Optional[Dict[str, Any]]:
        """Get the active schedule block info for status/debug.
        
        Args:
            room_id: Room ID
            ts: Local datetime
            
        Returns:
            Dict with block info, or None if no active block:
            {
                "day": "mon",
                "start": "07:00",
                "end": "09:00",
                "target": 21.0
            }
        """
        if room_id not in self.schedules:
            return None
        
        schedule = self.schedules[room_id]
        
        # Get day name
        weekday_idx = ts.weekday()
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day_name = day_names[weekday_idx]
        
        blocks = schedule["week"].get(day_name, [])
        current_time = ts.time()
        
        # Find active block
        for block in blocks:
            start = block["start"]
            end = block["end"]
            
            if start <= current_time < end:
                return {
                    "day": day_name,
                    "start": start.strftime(constants.TIME_FORMAT),
                    "end": end.strftime(constants.TIME_FORMAT),
                    "target": block["target"],
                }
        
        return None
    
    def with_override(
        self,
        base_target: Optional[float],
        *,
        kind: Optional[Literal["override", "boost"]] = None,
        target: Optional[float] = None,
        delta: Optional[float] = None,
    ) -> Optional[float]:
        """Apply override/boost to a base target.
        
        This is a pure function that combines a base target with an override/boost.
        Precedence is handled by the caller (room_controller).
        
        Args:
            base_target: Base scheduled target (or None)
            kind: Type of override ("override" for absolute, "boost" for delta, None for none)
            target: Absolute target for "override" kind
            delta: Temperature delta for "boost" kind
            
        Returns:
            Final target after applying override/boost, or base_target if no override
        """
        if kind is None:
            return base_target
        
        if kind == "override":
            if target is not None:
                log.debug(f"ScheduleManager: applying override target {target}°C (base was {base_target}°C)")
                return target
            else:
                log.warning("ScheduleManager: override kind specified but no target provided")
                return base_target
        
        elif kind == "boost":
            if delta is not None and base_target is not None:
                boosted = base_target + delta
                log.debug(f"ScheduleManager: applying boost {delta:+.1f}°C: {base_target}°C → {boosted}°C")
                return boosted
            elif delta is not None and base_target is None:
                log.warning("ScheduleManager: boost specified but no base_target to boost from")
                return None
            else:
                log.warning("ScheduleManager: boost kind specified but no delta provided")
                return base_target
        
        else:
            log.warning(f"ScheduleManager: unknown override kind '{kind}'")
            return base_target
    
    def get_default_target(self, room_id: str) -> Optional[float]:
        """Get the default target for a room (for service calls).
        
        Args:
            room_id: Room ID
            
        Returns:
            Default target temperature, or None if room has no schedule
        """
        if room_id not in self.schedules:
            return None
        return self.schedules[room_id]["default_target"]
    
    def has_schedule(self, room_id: str) -> bool:
        """Check if a room has a schedule.
        
        Args:
            room_id: Room ID
            
        Returns:
            True if room has a schedule, False otherwise
        """
        return room_id in self.schedules


# Module-level singleton instance
_manager: Optional[ScheduleManager] = None


def init() -> ScheduleManager:
    """Initialize the schedule manager singleton.
    
    Returns:
        ScheduleManager instance
    """
    global _manager
    if _manager is None:
        _manager = ScheduleManager()
    return _manager


def get_manager() -> Optional[ScheduleManager]:
    """Get the schedule manager singleton.
    
    Returns:
        ScheduleManager instance or None if not initialized
    """
    return _manager
