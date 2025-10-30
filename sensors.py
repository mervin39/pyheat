"""
sensors.py - Temperature sensor fusion and staleness tracking

Responsibilities:
- Maintain per-room temperature readings from one or more sensors
- Compute fused room temperature (primary average, fallback to fallback average)
- Track staleness per sensor and per room using timeout_m
- Apply room precision when publishing sensor.pyheat_<room>_temperature
- Accept last-known HA state on startup (or treat as stale)
- No file I/O or HA wiring - just fusion logic and status

Room temperature rules:
- Average all available PRIMARY sensors
- If no primary sensors available, average FALLBACK sensors
- If no sensors available, mark room STALE
- Unavailable or stale sensors are excluded from average
- Sensor is stale if no update in timeout_m minutes
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, List, Any
from . import constants


class SensorManager:
    """Manages temperature sensors and computes fused room temperatures."""
    
    def __init__(self):
        """Initialize the sensor manager."""
        self.sensors: Dict[str, Dict[str, Any]] = {}  # entity_id -> sensor data
        self.rooms: Dict[str, Dict[str, Any]] = {}    # room_id -> room config
        self.room_sensors: Dict[str, List[str]] = {}  # room_id -> [entity_ids]
        
        log.debug("SensorManager: initialized")
    
    def reload_rooms(self, rooms_cfg: Dict) -> None:
        """Refresh sensor mappings/roles/precision after rooms.yaml changes.
        
        Args:
            rooms_cfg: Parsed rooms.yaml dict with structure:
                {
                    "rooms": [
                        {
                            "id": "living",
                            "precision": 1,
                            "sensors": [
                                {"entity_id": "sensor.living_temp", "role": "primary", "timeout_m": 180},
                                ...
                            ],
                            ...
                        },
                        ...
                    ]
                }
        """
        log.info("SensorManager: reloading room configurations")
        
        # Clear existing mappings
        self.sensors.clear()
        self.rooms.clear()
        self.room_sensors.clear()
        
        # Process each room
        for room_data in rooms_cfg.get("rooms", []):
            room_id = room_data.get("id")
            if not room_id:
                log.warning("SensorManager: skipping room with no ID")
                continue
            
            # Store room config
            self.rooms[room_id] = {
                "precision": room_data.get("precision", 1),
                "timeout_m": room_data.get("timeout_m", constants.TIMEOUT_MIN_M),
            }
            
            # Track sensors for this room
            room_sensor_ids = []
            
            for sensor_cfg in room_data.get("sensors", []):
                entity_id = sensor_cfg.get("entity_id")
                if not entity_id:
                    log.warning(f"SensorManager: room {room_id} has sensor with no entity_id")
                    continue
                
                role = sensor_cfg.get("role", "primary")
                timeout_m = sensor_cfg.get("timeout_m", room_data.get("timeout_m", constants.TIMEOUT_MIN_M))
                
                # Validate timeout
                if timeout_m < constants.TIMEOUT_MIN_M:
                    log.warning(f"SensorManager: sensor {entity_id} timeout {timeout_m}m too low, using {constants.TIMEOUT_MIN_M}m")
                    timeout_m = constants.TIMEOUT_MIN_M
                
                # Store sensor data
                self.sensors[entity_id] = {
                    "room_id": room_id,
                    "role": role,  # "primary" or "fallback"
                    "timeout_m": timeout_m,
                    "value": None,
                    "last_update": None,
                }
                
                room_sensor_ids.append(entity_id)
                
                log.debug(f"SensorManager: registered {entity_id} for room {room_id} (role={role}, timeout={timeout_m}m)")
            
            # Store room's sensor list
            self.room_sensors[room_id] = room_sensor_ids
            
            log.info(f"SensorManager: room {room_id} has {len(room_sensor_ids)} sensor(s), precision={self.rooms[room_id]['precision']}")
        
        log.info(f"SensorManager: loaded {len(self.rooms)} room(s) with {len(self.sensors)} total sensor(s)")
    
    def update_sensor(self, entity_id: str, value: float, ts: datetime) -> None:
        """Record a new reading for a specific sensor.
        
        Args:
            entity_id: Sensor entity ID (e.g., "sensor.living_temp")
            value: Temperature reading in °C
            ts: Timestamp of the reading (datetime with timezone)
        """
        if entity_id not in self.sensors:
            log.debug(f"SensorManager: ignoring update for unknown sensor {entity_id}")
            return
        
        sensor = self.sensors[entity_id]
        room_id = sensor["room_id"]
        
        # Update sensor data
        sensor["value"] = value
        sensor["last_update"] = ts
        
        log.debug(f"SensorManager: {entity_id} updated: {value:.1f}°C at {ts.strftime('%H:%M:%S')}")
    
    def is_sensor_stale(self, entity_id: str, now: datetime) -> bool:
        """Check if a sensor is stale based on its timeout.
        
        Args:
            entity_id: Sensor entity ID
            now: Current datetime (with timezone)
            
        Returns:
            True if sensor is stale, False otherwise
        """
        if entity_id not in self.sensors:
            return True  # Unknown sensor is considered stale
        
        sensor = self.sensors[entity_id]
        
        # No reading yet
        if sensor["last_update"] is None:
            return True
        
        # Check timeout
        timeout_delta = timedelta(minutes=sensor["timeout_m"])
        age = now - sensor["last_update"]
        
        return age > timeout_delta
    
    def get_room_temp(self, room_id: str, now: datetime) -> Tuple[Optional[float], bool]:
        """Compute fused room temperature from available sensors.
        
        Logic:
        1. Get all PRIMARY sensors that are available and not stale
        2. If any primaries available, average them
        3. Otherwise, get all FALLBACK sensors that are available and not stale
        4. If any fallbacks available, average them
        5. Otherwise, room is STALE (return None, True)
        
        Args:
            room_id: Room ID
            now: Current datetime (with timezone)
            
        Returns:
            Tuple of (fused_temp, is_stale)
            - fused_temp: Average temperature in °C, or None if room is stale
            - is_stale: True if room has no available sensors
        """
        if room_id not in self.room_sensors:
            log.warning(f"SensorManager: get_room_temp called for unknown room {room_id}")
            return None, True
        
        sensor_ids = self.room_sensors[room_id]
        
        # Collect available sensors by role
        primary_temps = []
        fallback_temps = []
        
        for entity_id in sensor_ids:
            sensor = self.sensors[entity_id]
            
            # Read current value from Home Assistant state
            try:
                state_val = state.get(entity_id)
            except NameError:
                log.warning(f"SensorManager: entity {entity_id} does not exist in Home Assistant")
                continue
            
            # Skip if no state or state is unavailable/unknown
            if state_val is None or state_val in ["unavailable", "unknown"]:
                continue
            
            # Try to parse as float
            try:
                temp_value = float(state_val)
            except (ValueError, TypeError):
                log.warning(f"SensorManager: invalid temperature value for {entity_id}: {state_val}")
                continue
            
            # Update sensor cache (for staleness tracking)
            sensor["value"] = temp_value
            sensor["last_update"] = now
            
            # Check staleness
            if self.is_sensor_stale(entity_id, now):
                continue
            
            # Add to appropriate list
            if sensor["role"] == "primary":
                primary_temps.append(temp_value)
            else:  # fallback
                fallback_temps.append(temp_value)
        
        # Apply fusion logic
        precision = self.rooms[room_id].get("precision", 1)
        
        if primary_temps:
            # Use primary sensors
            fused = sum(primary_temps) / len(primary_temps)
            fused_rounded = round(fused, precision)
            log.debug(f"SensorManager: room {room_id} temp from {len(primary_temps)} primary sensor(s): {fused_rounded:.{precision}f}°C")
            return fused_rounded, False
        elif fallback_temps:
            # Fall back to fallback sensors
            fused = sum(fallback_temps) / len(fallback_temps)
            fused_rounded = round(fused, precision)
            log.debug(f"SensorManager: room {room_id} temp from {len(fallback_temps)} fallback sensor(s): {fused_rounded:.{precision}f}°C")
            return fused_rounded, False
        else:
            # No sensors available - room is stale
            log.warning(f"SensorManager: room {room_id} has no available sensors (STALE)")
            return None, True
    
    def get_room_sensor_status(self, room_id: str, now: datetime) -> List[Dict[str, Any]]:
        """Get per-sensor availability/stale info for status/debug.
        
        Args:
            room_id: Room ID
            now: Current datetime (with timezone)
            
        Returns:
            List of dicts with sensor status:
            [
                {
                    "entity_id": "sensor.living_temp",
                    "role": "primary",
                    "value": 20.5,
                    "last_update": "15:30:45",
                    "is_stale": False,
                    "age_minutes": 2.3
                },
                ...
            ]
        """
        if room_id not in self.room_sensors:
            return []
        
        status_list = []
        
        for entity_id in self.room_sensors[room_id]:
            sensor = self.sensors[entity_id]
            is_stale = self.is_sensor_stale(entity_id, now)
            
            # Calculate age
            age_minutes = None
            last_update_str = None
            if sensor["last_update"]:
                age = now - sensor["last_update"]
                age_minutes = age.total_seconds() / 60
                last_update_str = sensor["last_update"].strftime("%H:%M:%S")
            
            status_list.append({
                "entity_id": entity_id,
                "role": sensor["role"],
                "value": sensor["value"],
                "last_update": last_update_str,
                "is_stale": is_stale,
                "age_minutes": age_minutes,
                "timeout_m": sensor["timeout_m"],
            })
        
        return status_list
    
    def get_all_room_temps(self, now: datetime) -> Dict[str, Tuple[Optional[float], bool]]:
        """Get fused temperatures for all rooms.
        
        Args:
            now: Current datetime (with timezone)
            
        Returns:
            Dict of room_id -> (temp, is_stale)
        """
        result = {}
        for room_id in self.room_sensors.keys():
            result[room_id] = self.get_room_temp(room_id, now)
        return result
    
    def get_room_precision(self, room_id: str) -> int:
        """Get the configured precision for a room.
        
        Args:
            room_id: Room ID
            
        Returns:
            Precision (decimal places), default 1
        """
        if room_id not in self.rooms:
            return 1
        return self.rooms[room_id].get("precision", 1)


# Module-level singleton instance (will be initialized by orchestrator)
_manager: Optional[SensorManager] = None


def init() -> SensorManager:
    """Initialize the sensor manager singleton.
    
    Returns:
        SensorManager instance
    """
    global _manager
    if _manager is None:
        _manager = SensorManager()
    return _manager


def get_manager() -> Optional[SensorManager]:
    """Get the sensor manager singleton.
    
    Returns:
        SensorManager instance or None if not initialized
    """
    return _manager
