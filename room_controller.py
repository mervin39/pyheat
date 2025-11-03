"""
room_controller.py - Per-room state machine and heating logic

Responsibilities:
- Model a Room as a first-class object with state
- Resolve effective target using precedence (off → manual → override/boost → schedule/default)
- Compute call-for-heat using per-room asymmetric hysteresis
- Compute valve percent using stepped bands + step hysteresis + rate limiting
- Track per-room override/boost state
- Expose concise status strings
- Publish room-derived entities

Room States:
- auto: follow schedule/default, apply overrides/boosts
- manual: target from input_number, overrides/boosts ignored
- off: never heat, valve 0%, no call-for-heat
- stale: all sensors unavailable → safe mode: valve 0%, no call-for-heat

Precedence (highest wins):
- room off → manual → override → schedule block → default_target
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Literal, Any, Tuple
from . import constants


class Room:
    """First-class room object that owns sensors, TRV, and heating logic."""
    
    def __init__(self, room_id: str, config: Dict[str, Any]):
        """Initialize a room.
        
        Args:
            room_id: Unique room identifier
            config: Room configuration from rooms.yaml with optional tuning
        """
        self.room_id = room_id
        self.room_name = config.get("name", room_id.replace("_", " ").title())
        
        # Configuration
        self.precision = config.get("precision", 1)
        
        # Hysteresis config (asymmetric deadband for call-for-heat)
        hysteresis = config.get("hysteresis", {})
        self.on_delta_c = hysteresis.get("on_delta_c", constants.HYSTERESIS_DEFAULT["on_delta_c"])
        self.off_delta_c = hysteresis.get("off_delta_c", constants.HYSTERESIS_DEFAULT["off_delta_c"])
        
        # Validate hysteresis
        if self.on_delta_c < self.off_delta_c:
            log.warning(f"Room {room_id}: on_delta_c ({self.on_delta_c}) < off_delta_c ({self.off_delta_c}), using defaults")
            self.on_delta_c = constants.HYSTERESIS_DEFAULT["on_delta_c"]
            self.off_delta_c = constants.HYSTERESIS_DEFAULT["off_delta_c"]
        
        # Valve bands config (stepped percentage control)
        bands = config.get("valve_bands", {})
        self.t_low = bands.get("t_low", constants.VALVE_BANDS_DEFAULT["t_low"])
        self.t_mid = bands.get("t_mid", constants.VALVE_BANDS_DEFAULT["t_mid"])
        self.t_max = bands.get("t_max", constants.VALVE_BANDS_DEFAULT["t_max"])
        self.low_percent = bands.get("low_percent", constants.VALVE_BANDS_DEFAULT["low_percent"])
        self.mid_percent = bands.get("mid_percent", constants.VALVE_BANDS_DEFAULT["mid_percent"])
        self.max_percent = bands.get("max_percent", constants.VALVE_BANDS_DEFAULT["max_percent"])
        self.step_hysteresis_c = bands.get("step_hysteresis_c", constants.VALVE_BANDS_DEFAULT["step_hysteresis_c"])
        
        # Valve update rate limiting
        update_cfg = config.get("valve_update", {})
        self.min_interval_s = update_cfg.get("min_interval_s", constants.VALVE_UPDATE_DEFAULT["min_interval_s"])
        
        # State variables (updated by update_inputs)
        self.temp: Optional[float] = None  # Fused room temperature
        self.is_stale = True  # Room has no available sensors
        self.mode: Literal["auto", "manual", "off"] = "auto"
        self.manual_setpoint: Optional[float] = None
        self.schedule_target: Optional[float] = None  # From scheduler (with holiday mode applied)
        
        # Override/boost state
        self.override_kind: Optional[Literal["override", "boost"]] = None
        self.override_target: Optional[float] = None  # Absolute target for override
        self.override_delta: Optional[float] = None  # Delta for boost
        self.override_expires: Optional[datetime] = None
        
        # Try to restore override state from persisted entities
        override_timer_name = f"timer.pyheat_{self.room_id}_override"
        timer_state = state.get(override_timer_name)
        
        if timer_state == "active":
            log.debug(f"Room {self.room_id}: restoring override state from persisted entities")
            
            # Get persisted target and timer expiry
            try:
                target_entity = f"input_number.pyheat_{self.room_id}_override_target"
                target = float(state.get(target_entity) or 0)
                
                # Get timer expiry time from finishes_at attribute
                timer_attrs = state.getattr(override_timer_name)
                finishes_at_str = timer_attrs.get("finishes_at") if timer_attrs else None
                
                if finishes_at_str:
                    # Parse ISO format datetime string
                    from datetime import datetime
                    # Remove timezone info and parse (pyscript handles timezone internally)
                    finishes_at_clean = finishes_at_str.replace("+00:00", "").replace("Z", "")
                    self.override_expires = datetime.fromisoformat(finishes_at_clean).replace(tzinfo=timezone.utc)
                
                # Only restore override if target > 5 (values ≤5 indicate cleared override that was clamped to min)
                if target > 5.0:
                    # We have a persisted target, this is an override
                    self.override_kind = "override"
                    self.override_target = target
                    log.info(f"Room {self.room_id}: restored override from persisted state (target={target}°C, expires={self.override_expires})")
                else:
                    # Timer active but no valid persisted target - stale timer, don't restore
                    log.info(f"Room {self.room_id}: override timer active but no valid target (value={target}), not restoring")
                    
            except Exception as e:
                log.warning(f"Room {self.room_id}: failed to restore override state: {e}")
        
        # Computed outputs (updated by compute)
        self.target: Optional[float] = None  # Resolved target
        self.call_for_heat = False
        self.valve_percent = 0
        self.state: Literal["auto", "manual", "off", "stale"] = "stale"
        
        # Internal state for hysteresis
        self._prev_call_for_heat = False
        self._prev_valve_band: Optional[int] = None  # 0=off, 1=low, 2=mid, 3=max
        self._last_valve_update: Optional[datetime] = None
        
        log.debug(f"Room {room_id} initialized: precision={self.precision}, "
                  f"hysteresis=({self.on_delta_c:.2f}/{self.off_delta_c:.2f}), "
                  f"bands=({self.t_low:.2f}/{self.t_mid:.2f}/{self.t_max:.2f})")
    
    def update_inputs(
        self,
        *,
        temp: Optional[float] = None,
        is_stale: bool = True,
        mode: str = "auto",
        schedule_target: Optional[float] = None,
        manual_setpoint: Optional[float] = None,
    ) -> None:
        """Update inputs from sensors, scheduler, and UI.
        
        Args:
            temp: Fused room temperature (from sensors module)
            is_stale: True if room has no available sensors
            mode: Room mode ("auto", "manual", "off")
            schedule_target: Scheduled target (from scheduler, with holiday mode)
            manual_setpoint: Manual setpoint (from input_number)
        """
        self.temp = temp
        self.is_stale = is_stale
        self.mode = mode
        self.schedule_target = schedule_target
        self.manual_setpoint = manual_setpoint
        
        log.debug(f"Room {self.room_id} inputs: temp={temp}, stale={is_stale}, "
                  f"mode={mode}, sched={schedule_target}, manual={manual_setpoint}")
    
    def apply_override(self, target: float, minutes: int, now: datetime) -> None:
        """Start/extend an absolute target override.
        
        Args:
            target: Absolute target temperature
            minutes: Duration in minutes
            now: Current datetime
        """
        self.override_kind = "override"
        self.override_target = target
        self.override_delta = None
        self.override_expires = now + timedelta(minutes=minutes)
        
        # Persist override target for recovery after pyscript reload
        try:
            state.set(f"input_number.pyheat_{self.room_id}_override_target", value=target)
        except Exception as e:
            log.warning(f"Room {self.room_id}: failed to persist override target: {e}")
        
        log.info(f"Room {self.room_id}: override to {target}°C for {minutes}m (expires {self.override_expires.strftime('%H:%M:%S')})")
    
    def apply_boost(self, delta: float, minutes: int, now: datetime) -> None:
        """Start/extend a delta-based boost.
        
        Args:
            delta: Temperature delta (can be negative)
            minutes: Duration in minutes
            now: Current datetime
        """
        self.override_kind = "boost"
        self.override_target = None
        self.override_delta = delta
        self.override_expires = now + timedelta(minutes=minutes)
        
        # Boost will calculate target during compute(), persist it then
        # For now, just log - target will be persisted on first compute
        
        log.info(f"Room {self.room_id}: boost {delta:+.1f}°C for {minutes}m (expires {self.override_expires.strftime('%H:%M:%S')})")
    
    def clear_override(self) -> None:
        """Cancel any active override/boost."""
        if self.override_kind:
            log.info(f"Room {self.room_id}: clearing {self.override_kind}")
        
        self.override_kind = None
        self.override_target = None
        self.override_delta = None
        self.override_expires = None
        
        # Clear persisted override target
        try:
            state.set(f"input_number.pyheat_{self.room_id}_override_target", value=0)
        except Exception as e:
            log.debug(f"Room {self.room_id}: failed to clear persisted override: {e}")
    
    def set_mode(self, mode: Literal["auto", "manual", "off"]) -> None:
        """Change room mode.
        
        Args:
            mode: New mode
        """
        if mode != self.mode:
            log.info(f"Room {self.room_id}: mode {self.mode} → {mode}")
            self.mode = mode
    
    def _check_override_expired(self, now: datetime) -> None:
        """Check if override/boost has expired and clear it.
        
        Args:
            now: Current datetime
        """
        if self.override_expires and now >= self.override_expires:
            log.info(f"Room {self.room_id}: {self.override_kind} expired")
            self.clear_override()
    
    def _resolve_target(self, now: datetime) -> Optional[float]:
        """Resolve effective target using precedence chain.
        
        Precedence (highest wins):
        1. Room off → target = None
        2. Manual mode → manual_setpoint
        3. Override/boost (only in auto mode)
        4. Schedule target (includes holiday mode from scheduler)
        
        Args:
            now: Current datetime
            
        Returns:
            Resolved target temperature, or None
        """
        # Check override expiry first
        self._check_override_expired(now)
        
        # Mode-based precedence
        if self.mode == "off":
            return None
        
        if self.mode == "manual":
            # Manual mode uses setpoint, ignores overrides/boosts
            return self.manual_setpoint
        
        # Auto mode: check for active override/boost
        if self.mode == "auto" and self.override_kind:
            if self.override_kind == "override":
                # Absolute target override
                return self.override_target
            elif self.override_kind == "boost" and self.schedule_target is not None:
                # Delta boost on top of schedule
                computed_target = self.schedule_target + self.override_delta
                
                # Persist computed target for reload recovery (only if not already persisted)
                if self.override_target is None:
                    self.override_target = computed_target
                    try:
                        state.set(f"input_number.pyheat_{self.room_id}_override_target", value=computed_target)
                    except Exception as e:
                        log.debug(f"Room {self.room_id}: failed to persist boost target: {e}")
                
                return computed_target
        
        # Default to schedule target (which already includes holiday mode)
        return self.schedule_target
    
    def _compute_call_for_heat(self, target: Optional[float]) -> bool:
        """Compute call-for-heat using asymmetric hysteresis.
        
        Logic:
        - If no target or temp is stale → False
        - e = target - temp (positive means below target)
        - Start calling when e ≥ on_delta_c
        - Stop calling when e ≤ off_delta_c
        - If off_delta_c < e < on_delta_c, keep previous state
        
        Args:
            target: Resolved target temperature
            
        Returns:
            True to call for heat, False otherwise
        """
        # Sanity checks
        if target is None or self.temp is None or self.is_stale:
            self._prev_call_for_heat = False
            return False
        
        # Error term (positive = below target)
        e = target - self.temp
        
        # Apply asymmetric hysteresis
        if e >= self.on_delta_c:
            # Clearly below target → start heating
            call = True
        elif e <= self.off_delta_c:
            # At or above target → stop heating
            call = False
        else:
            # In deadband → keep previous state
            call = self._prev_call_for_heat
        
        # Update state
        self._prev_call_for_heat = call
        
        if call != self._prev_call_for_heat:
            log.debug(f"Room {self.room_id}: call_for_heat changed to {call} (e={e:.2f}°C)")
        
        return call
    
    def _compute_valve_percent(self, target: Optional[float], now: datetime) -> int:
        """Compute valve opening percentage using stepped bands with hysteresis.
        
        Bands (based on error e = target - temp):
        - e < t_low → 0%
        - t_low ≤ e < t_mid → low_percent
        - t_mid ≤ e < t_max → mid_percent  
        - e ≥ t_max → max_percent
        
        Step hysteresis: Applied only for adjacent band transitions to prevent oscillation
        at band boundaries. Multi-band jumps (e.g., 0→3) skip hysteresis as the change
        is clearly significant.
        
        Rate limiting: only update if min_interval_s has passed.
        
        Args:
            target: Resolved target temperature
            now: Current datetime
            
        Returns:
            Valve opening percentage (0-100)
        """
        # Sanity checks
        if target is None or self.temp is None or self.is_stale or self.mode == "off":
            self._prev_valve_band = 0
            self._last_valve_update = now
            return 0
        
        # Error term
        e = target - self.temp
        
        # Determine target band (without hysteresis first)
        if e < self.t_low:
            target_band = 0
        elif e < self.t_mid:
            target_band = 1
        elif e < self.t_max:
            target_band = 2
        else:
            target_band = 3
        
        # Apply step hysteresis (only for adjacent band transitions)
        # Multi-band jumps indicate significant changes and should happen immediately
        current_band = self._prev_valve_band if self._prev_valve_band is not None else target_band
        band_delta = abs(target_band - current_band)
        
        if band_delta > 1:
            # Multi-band jump (e.g., 0→2, 0→3, 2→0) - skip hysteresis, change is significant
            current_band = target_band
        elif target_band > current_band:
            # Adjacent band increase - apply hysteresis to prevent oscillation at boundary
            if current_band == 0 and e >= self.t_low + self.step_hysteresis_c:
                current_band = 1
            elif current_band == 1 and e >= self.t_mid + self.step_hysteresis_c:
                current_band = 2
            elif current_band == 2 and e >= self.t_max + self.step_hysteresis_c:
                current_band = 3
        elif target_band < current_band:
            # Adjacent band decrease - apply hysteresis to prevent oscillation at boundary
            if current_band == 3 and e < self.t_max - self.step_hysteresis_c:
                current_band = 2
            elif current_band == 2 and e < self.t_mid - self.step_hysteresis_c:
                current_band = 1
            elif current_band == 1 and e < self.t_low - self.step_hysteresis_c:
                current_band = 0
        
        # Map band to percentage
        band_map = {
            0: 0,
            1: self.low_percent,
            2: self.mid_percent,
            3: self.max_percent,
        }
        valve_percent = band_map[current_band]
        
        # Always update the calculated valve percent (orchestrator needs current value)
        # Rate limiting only prevents sending frequent TRV commands, not updating calculations
        self.valve_percent = int(valve_percent)
        
        # Rate limiting for TRV command timing
        should_send_command = True
        if self._last_valve_update is not None:
            elapsed = (now - self._last_valve_update).total_seconds()
            if elapsed < self.min_interval_s and current_band == self._prev_valve_band:
                # Too soon to send TRV command and band hasn't changed
                log.debug(f"Room {self.room_id}: valve update rate limited ({elapsed:.1f}s < {self.min_interval_s}s), calc={valve_percent}%")
                should_send_command = False
        
        # Update state tracking only if we would send a command
        if should_send_command:
            if current_band != self._prev_valve_band:
                log.debug(f"Room {self.room_id}: valve band {self._prev_valve_band} → {current_band} ({valve_percent}%)")
            
            self._prev_valve_band = current_band
            self._last_valve_update = now
        
        return int(valve_percent)
    
    def compute(self, now: datetime) -> Dict[str, Any]:
        """Recompute target, call_for_heat, valve_percent, state, and status.
        
        This is the main computation function called on every recompute.
        
        Args:
            now: Current datetime
            
        Returns:
            Summary dict with:
            {
                "room_id": str,
                "state": str,
                "temp": float|None,
                "target": float|None,
                "call_for_heat": bool,
                "valve_percent": int,
                "status": str,
                "override_active": bool,
                "override_remaining_m": int|None,
            }
        """
        # Resolve target
        self.target = self._resolve_target(now)
        
        # Determine state
        if self.is_stale:
            self.state = "stale"
        elif self.mode == "off":
            self.state = "off"
        elif self.mode == "manual":
            self.state = "manual"
        else:
            self.state = "auto"
        
        # Compute call for heat
        self.call_for_heat = self._compute_call_for_heat(self.target)
        
        # Compute valve percent
        self.valve_percent = self._compute_valve_percent(self.target, now)
        
        # Build status string
        status = self._build_status_string(now)
        
        # Calculate override remaining time
        override_remaining_m = None
        if self.override_expires:
            remaining = self.override_expires - now
            override_remaining_m = max(0, int(remaining.total_seconds() / 60))
        
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "state": self.state,
            "temp": self.temp,
            "target": self.target,
            "call_for_heat": self.call_for_heat,
            "valve_percent": self.valve_percent,
            "status": status,
            "override_active": self.override_kind is not None,
            "override_remaining_m": override_remaining_m,
        }
    
    def _build_status_string(self, now: datetime) -> str:
        """Build concise status string for sensor.pyheat_<room>_status.
        
        Returns:
            Status string like:
            - "at_target"
            - "heating"
            - "boost(+2.0) 23m"
            - "override(21.0) 15m"
            - "manual(20.0)"
            - "manual(stale)"
            - "off"
            - "stale"
        """
        # Handle special states first
        if self.state == "off":
            return "off"
        
        if self.state == "stale":
            return "stale"
        
        if self.state == "manual":
            if self.is_stale:
                return "manual(stale)"
            elif self.manual_setpoint is not None:
                return f"manual({self.manual_setpoint:.1f})"
            else:
                return "manual(no setpoint)"
        
        # Auto mode - check for override/boost
        if self.override_kind == "boost" and self.override_delta is not None:
            remaining_m = 0
            if self.override_expires:
                remaining = self.override_expires - now
                remaining_m = max(0, int(remaining.total_seconds() / 60))
            return f"boost({self.override_delta:+.1f}) {remaining_m}m"
        
        if self.override_kind == "override" and self.override_target is not None:
            remaining_m = 0
            if self.override_expires:
                remaining = self.override_expires - now
                remaining_m = max(0, int(remaining.total_seconds() / 60))
            return f"override({self.override_target:.1f}) {remaining_m}m"
        
        # Normal auto mode
        if self.call_for_heat:
            return "heating"
        else:
            return "at_target"
    
    def get_status(self) -> Dict[str, Any]:
        """Get read-only snapshot for diagnostics.
        
        Returns:
            Dict with full room status
        """
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "state": self.state,
            "mode": self.mode,
            "temp": self.temp,
            "is_stale": self.is_stale,
            "target": self.target,
            "schedule_target": self.schedule_target,
            "manual_setpoint": self.manual_setpoint,
            "call_for_heat": self.call_for_heat,
            "valve_percent": self.valve_percent,
            "override_kind": self.override_kind,
            "override_target": self.override_target,
            "override_delta": self.override_delta,
            "override_expires": self.override_expires.isoformat() if self.override_expires else None,
        }


class RoomControllerManager:
    """Manages all room controllers."""
    
    def __init__(self):
        """Initialize the room controller manager."""
        self.rooms: Dict[str, Room] = {}
        log.debug("RoomControllerManager: initialized")
    
    def reload_rooms(self, rooms_cfg: Dict) -> None:
        """Create/update room controllers from rooms.yaml.
        
        Args:
            rooms_cfg: Parsed rooms.yaml dict
        """
        log.info("RoomControllerManager: reloading rooms")
        
        # Track which rooms we've seen
        seen_rooms = set()
        
        for room_data in rooms_cfg.get("rooms", []):
            room_id = room_data.get("id")
            if not room_id:
                log.warning("RoomControllerManager: skipping room with no ID")
                continue
            
            seen_rooms.add(room_id)
            
            # Create or update room
            if room_id in self.rooms:
                log.debug(f"RoomControllerManager: room {room_id} already exists, keeping instance")
                # Could update config here if needed
            else:
                self.rooms[room_id] = Room(room_id, room_data)
                log.info(f"RoomControllerManager: created room {room_id}")
        
        # Remove rooms that are no longer in config
        removed = set(self.rooms.keys()) - seen_rooms
        for room_id in removed:
            log.info(f"RoomControllerManager: removing room {room_id}")
            del self.rooms[room_id]
        
        log.info(f"RoomControllerManager: managing {len(self.rooms)} room(s)")
    
    def get_room(self, room_id: str) -> Optional[Room]:
        """Get a room controller by ID.
        
        Args:
            room_id: Room ID
            
        Returns:
            Room instance or None
        """
        return self.rooms.get(room_id)
    
    def get_all_rooms(self) -> Dict[str, Room]:
        """Get all room controllers.
        
        Returns:
            Dict of room_id -> Room
        """
        return self.rooms


# Module-level singleton
_manager: Optional[RoomControllerManager] = None


def init() -> RoomControllerManager:
    """Initialize the room controller manager singleton.
    
    Returns:
        RoomControllerManager instance
    """
    global _manager
    if _manager is None:
        _manager = RoomControllerManager()
    return _manager


def get_manager() -> Optional[RoomControllerManager]:
    """Get the room controller manager singleton.
    
    Returns:
        RoomControllerManager instance or None
    """
    return _manager
