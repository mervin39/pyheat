"""
trv.py - TRV control adapter for Zigbee2MQTT TRVs

Responsibilities:
- Send valve position commands to number.trv_<room>_valve_opening/closing_degree
- Read feedback from sensor.<trv_base>_valve_opening/closing_degree_z2m
- Track commanded vs actual valve positions
- Report interlock status (feedback matches command)

TRV Control Model (Sonoff TRVZB via zigbee2mqtt):
- Command entities: number.trv_<room>_valve_opening_degree, number.trv_<room>_valve_closing_degree
- To set valve to P% open: opening_degree = P, closing_degree = (100 - P)
- Feedback entities: sensor.<trv_base>_valve_opening_degree_z2m, sensor.<trv_base>_valve_closing_degree_z2m
- Interlock: feedback must exactly match command for TRV to be considered "open"

Note: This module is stateless regarding policy. Banding, rate limits, and valve percentage
decisions are made in room_controller.py. This module just sends commands and reads feedback.
"""

from typing import Dict, Optional, Any
from . import constants


class TRVController:
    """Controls a single TRV with command/feedback tracking."""
    
    def __init__(self, room_id: str, trv_base: str):
        """Initialize TRV controller.
        
        Args:
            room_id: Room identifier
            trv_base: TRV base name (e.g., "trv_pete" from "climate.trv_pete")
        """
        self.room_id = room_id
        self.trv_base = trv_base
        
        # Derive entity IDs
        entities = constants.get_trv_entities(trv_base)
        self.cmd_open_entity = entities["cmd_open"]
        self.cmd_close_entity = entities["cmd_close"]
        self.fb_open_entity = entities["fb_open"]
        self.fb_close_entity = entities["fb_close"]
        
        # Track last commanded value
        self.last_commanded_percent: Optional[int] = None
        
        log.debug(f"TRVController {room_id}: initialized with base '{trv_base}'")
        log.debug(f"  Commands: {self.cmd_open_entity}, {self.cmd_close_entity}")
        log.debug(f"  Feedback: {self.fb_open_entity}, {self.fb_close_entity}")
    
    async def set_valve_percent(self, percent: int) -> None:
        """Set valve opening percentage.
        
        Args:
            percent: Desired valve opening (0-100%)
        """
        # Clamp to valid range
        percent = max(0, min(100, percent))
        
        # Calculate opening and closing degrees
        opening = percent
        closing = 100 - percent
        
        # Track command
        self.last_commanded_percent = percent
        
        log.info(f"TRVController {self.room_id}: setting valve to {percent}% (open={opening}, close={closing})")
        
        # Issue commands to HA entities using number.set_value service
        try:
            # Set opening degree
            service.call("number", "set_value", entity_id=self.cmd_open_entity, value=opening)
            
            # Set closing degree
            service.call("number", "set_value", entity_id=self.cmd_close_entity, value=closing)
            
            log.debug(f"TRVController {self.room_id}: commands sent successfully")
            
        except Exception as e:
            log.error(f"TRVController {self.room_id}: failed to set valve: {e}")
    
    def get_feedback_percent(self) -> Optional[int]:
        """Get current feedback valve opening percentage.
        
        Returns:
            Current valve opening % from feedback sensors, or None if unavailable
        """
        try:
            # Read feedback from sensors
            try:
                fb_open = state.get(self.fb_open_entity)
                fb_close = state.get(self.fb_close_entity)
            except NameError as e:
                log.warning(f"TRVController {self.room_id}: feedback entity does not exist: {e}")
                return None
            
            # Check validity
            if fb_open is None or fb_close is None:
                log.debug(f"TRVController {self.room_id}: feedback unavailable (open={fb_open}, close={fb_close})")
                return None
            
            # Convert to numbers
            try:
                fb_open = float(fb_open)
                fb_close = float(fb_close)
            except (ValueError, TypeError):
                log.warning(f"TRVController {self.room_id}: feedback not numeric (open={fb_open}, close={fb_close})")
                return None
            
            # Sanity check: opening + closing should be ~100
            total = fb_open + fb_close
            if abs(total - 100) > 5:  # Allow 5% tolerance
                log.warning(f"TRVController {self.room_id}: feedback inconsistent (open={fb_open}, close={fb_close}, total={total})")
                # Return None to indicate feedback is unreliable (TRV may be mid-transition)
                return None
            
            # Return opening percentage
            return int(round(fb_open))
            
        except Exception as e:
            log.error(f"TRVController {self.room_id}: error reading feedback: {e}")
            return None
    
    def matches_command(self) -> bool:
        """Check if feedback exactly matches last commanded value.
        
        This is used for the TRV-open interlock: boiler can only turn on if
        at least one TRV feedback confirms the valve is open as commanded.
        
        Returns:
            True if feedback exactly equals last commanded opening, False otherwise
        """
        if self.last_commanded_percent is None:
            # No command sent yet
            return False
        
        feedback = self.get_feedback_percent()
        if feedback is None:
            # No feedback available
            return False
        
        # Check for exact match
        matches = (feedback == self.last_commanded_percent)
        
        if not matches:
            log.debug(f"TRVController {self.room_id}: feedback mismatch (cmd={self.last_commanded_percent}%, fb={feedback}%)")
        
        return matches
    
    def get_status(self) -> Dict[str, Any]:
        """Get diagnostic status snapshot.
        
        Returns:
            Dict with commanded, feedback, and match status
        """
        feedback = self.get_feedback_percent()
        matches = self.matches_command()
        
        return {
            "room_id": self.room_id,
            "trv_base": self.trv_base,
            "commanded_percent": self.last_commanded_percent,
            "feedback_percent": feedback,
            "matches_command": matches,
            "entities": {
                "cmd_open": self.cmd_open_entity,
                "cmd_close": self.cmd_close_entity,
                "fb_open": self.fb_open_entity,
                "fb_close": self.fb_close_entity,
            }
        }


class TRVManager:
    """Manages all TRV controllers."""
    
    def __init__(self):
        """Initialize the TRV manager."""
        self.trvs: Dict[str, TRVController] = {}
        log.debug("TRVManager: initialized")
    
    def reload_rooms(self, rooms_cfg: Dict) -> None:
        """Create/update TRV controllers from rooms.yaml.
        
        Args:
            rooms_cfg: Parsed rooms.yaml dict
        """
        log.info("TRVManager: reloading TRV configurations")
        
        # Track which rooms we've seen
        seen_rooms = set()
        
        for room_data in rooms_cfg.get("rooms", []):
            room_id = room_data.get("id")
            if not room_id:
                log.warning("TRVManager: skipping room with no ID")
                continue
            
            # Extract TRV configuration
            trv_cfg = room_data.get("trv")
            if not trv_cfg:
                log.warning(f"TRVManager: room {room_id} has no TRV configuration, skipping")
                continue
            
            trv_entity = trv_cfg.get("entity_id")
            if not trv_entity:
                log.warning(f"TRVManager: room {room_id} TRV has no entity_id, skipping")
                continue
            
            # Extract base name from climate entity
            # Format: climate.trv_<room> → trv_<room>
            if not trv_entity.startswith("climate."):
                log.warning(f"TRVManager: room {room_id} TRV entity '{trv_entity}' not a climate entity, skipping")
                continue
            
            trv_base = trv_entity.replace("climate.", "")
            
            seen_rooms.add(room_id)
            
            # Create or update TRV controller
            if room_id in self.trvs:
                log.debug(f"TRVManager: room {room_id} TRV already exists, keeping instance")
            else:
                self.trvs[room_id] = TRVController(room_id, trv_base)
                log.info(f"TRVManager: created TRV controller for room {room_id} (base: {trv_base})")
        
        # Remove TRVs for rooms no longer in config
        removed = set(self.trvs.keys()) - seen_rooms
        for room_id in removed:
            log.info(f"TRVManager: removing TRV for room {room_id}")
            del self.trvs[room_id]
        
        log.info(f"TRVManager: managing {len(self.trvs)} TRV(s)")
    
    def get_trv(self, room_id: str) -> Optional[TRVController]:
        """Get TRV controller for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            TRVController instance or None
        """
        return self.trvs.get(room_id)
    
    async def set_valve_percent(self, room_id: str, percent: int) -> bool:
        """Set valve opening percentage for a room.
        
        Args:
            room_id: Room identifier
            percent: Desired valve opening (0-100%)
            
        Returns:
            True if command was sent, False if room TRV not found
        """
        trv = self.get_trv(room_id)
        if not trv:
            log.warning(f"TRVManager: no TRV found for room {room_id}")
            return False
        
        await trv.set_valve_percent(percent)
        return True
    
    def get_feedback_percent(self, room_id: str) -> Optional[int]:
        """Get feedback valve opening percentage for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Current valve opening % or None
        """
        trv = self.get_trv(room_id)
        if not trv:
            return None
        
        return trv.get_feedback_percent()
    
    def matches_command(self, room_id: str) -> bool:
        """Check if TRV feedback matches commanded position.
        
        Args:
            room_id: Room identifier
            
        Returns:
            True if feedback matches command exactly, False otherwise
        """
        trv = self.get_trv(room_id)
        if not trv:
            return False
        
        return trv.matches_command()
    
    def get_interlock_status(self) -> Dict[str, Any]:
        """Get overall TRV interlock status for boiler control.
        
        The boiler should only turn on if at least one TRV is confirmed open
        (feedback matches command and opening > threshold).
        
        Returns:
            Dict with:
            - any_open: bool - at least one TRV open and confirmed
            - open_count: int - number of TRVs with confirmed opening
            - total_count: int - total number of TRVs
            - rooms: list of room_ids with confirmed opening
        """
        open_rooms = []
        
        for room_id, trv in self.trvs.items():
            feedback = trv.get_feedback_percent()
            matches = trv.matches_command()
            
            # Consider "open" if feedback >= threshold and matches command
            if matches and feedback is not None and feedback >= constants.TRV_INTERLOCK_MIN_OPEN_PCT:
                open_rooms.append(room_id)
        
        return {
            "any_open": len(open_rooms) > 0,
            "open_count": len(open_rooms),
            "total_count": len(self.trvs),
            "rooms": open_rooms,
        }
    
    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status for all TRVs.
        
        Returns:
            Dict mapping room_id -> status dict
        """
        return {
            room_id: trv.get_status()
            for room_id, trv in self.trvs.items()
        }


# Module-level singleton
_manager: Optional[TRVManager] = None


def init() -> TRVManager:
    """Initialize the TRV manager singleton.
    
    Returns:
        TRVManager instance
    """
    global _manager
    if _manager is None:
        _manager = TRVManager()
    return _manager


def get_manager() -> Optional[TRVManager]:
    """Get the TRV manager singleton.
    
    Returns:
        TRVManager instance or None
    """
    return _manager
