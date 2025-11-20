# -*- coding: utf-8 -*-
"""sensor_manager.py - Temperature sensor fusion and staleness tracking

Responsibilities:
- Track sensor values and timestamps
- Implement sensor fusion (primary/fallback, averaging)
- Apply exponential moving average smoothing to fused temperatures
- Detect stale sensors
- Initialize sensor state from Home Assistant
"""

from datetime import datetime
from typing import Dict, Tuple, Optional


class SensorManager:
    """Manages temperature sensor fusion and staleness detection."""
    
    def __init__(self, ad, config):
        """Initialize the sensor manager.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        self.sensor_last_values = {}  # {entity_id: (value, timestamp)}
        self.sensor_attributes = {}  # {entity_id: temperature_attribute or None}
        
        # EMA smoothing state: {room_id: smoothed_temperature}
        # Stores the previous smoothed value for each room to compute moving average
        self.smoothed_temps = {}
        
        # Build attribute mapping from config
        self._build_attribute_map()
        
    def _build_attribute_map(self) -> None:
        """Build mapping of entity_id to temperature_attribute from config."""
        for room_id, room_cfg in self.config.rooms.items():
            for sensor_cfg in room_cfg['sensors']:
                entity_id = sensor_cfg['entity_id']
                temp_attribute = sensor_cfg.get('temperature_attribute')
                self.sensor_attributes[entity_id] = temp_attribute
        
    def initialize_from_ha(self) -> None:
        """Initialize sensor values from current Home Assistant state."""
        now = datetime.now()
        
        for room_id, room_cfg in self.config.rooms.items():
            for sensor_cfg in room_cfg['sensors']:
                entity_id = sensor_cfg['entity_id']
                temp_attribute = sensor_cfg.get('temperature_attribute')
                
                try:
                    # If temperature_attribute is specified, read from attribute
                    if temp_attribute:
                        state_str = self.ad.get_state(entity_id, attribute=temp_attribute)
                    else:
                        state_str = self.ad.get_state(entity_id)
                    
                    if state_str and state_str not in ['unknown', 'unavailable']:
                        value = float(state_str)
                        self.sensor_last_values[entity_id] = (value, now)
                        source = f"attribute '{temp_attribute}'" if temp_attribute else "state"
                        self.ad.log(f"Initialized sensor {entity_id} = {value}C (from {source})", level="DEBUG")
                except (ValueError, TypeError) as e:
                    self.ad.log(f"Could not initialize sensor {entity_id}: {e}", level="WARNING")
    
    def get_sensor_value(self, entity_id: str) -> Optional[float]:
        """Get current temperature value from a sensor entity.
        
        Respects the temperature_attribute configuration. If specified, reads from
        the attribute; otherwise reads from the entity state.
        
        Args:
            entity_id: Sensor entity ID
            
        Returns:
            Temperature value in °C, or None if unavailable/invalid
        """
        temp_attribute = self.sensor_attributes.get(entity_id)
        
        try:
            if temp_attribute:
                state_str = self.ad.get_state(entity_id, attribute=temp_attribute)
            else:
                state_str = self.ad.get_state(entity_id)
            
            if state_str and state_str not in ['unknown', 'unavailable']:
                return float(state_str)
        except (ValueError, TypeError) as e:
            self.ad.log(f"Invalid sensor value for {entity_id}: {e}", level="WARNING")
        
        return None
    
    def update_sensor(self, entity_id: str, value: float, timestamp: datetime) -> None:
        """Update a sensor's value and timestamp.
        
        Args:
            entity_id: Sensor entity ID
            value: Temperature value in °C
            timestamp: Timestamp of the reading
        """
        self.sensor_last_values[entity_id] = (value, timestamp)
        
    def get_room_temperature(self, room_id: str, now: datetime) -> Tuple[Optional[float], bool]:
        """Get fused temperature for a room.
        
        Uses sensor fusion with primary/fallback roles and staleness detection.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            
        Returns:
            Tuple of (temperature, is_stale)
            - temperature: Average of available sensors, or None if all stale/unavailable
            - is_stale: True if all sensors are stale or unavailable
        """
        room_config = self.config.rooms.get(room_id)
        if not room_config:
            return None, True
        
        # Categorize sensors by role
        primary_sensors = [s for s in room_config['sensors'] if s.get('role') == 'primary']
        fallback_sensors = [s for s in room_config['sensors'] if s.get('role') == 'fallback']
        
        # Try primary sensors first
        temps = []
        for sensor_cfg in primary_sensors:
            entity_id = sensor_cfg['entity_id']
            timeout_m = sensor_cfg.get('timeout_m', 180)
            
            if entity_id in self.sensor_last_values:
                value, timestamp = self.sensor_last_values[entity_id]
                age_minutes = (now - timestamp).total_seconds() / 60
                
                if age_minutes <= timeout_m:
                    temps.append(value)
        
        # If no primary sensors available, try fallback
        if not temps and fallback_sensors:
            for sensor_cfg in fallback_sensors:
                entity_id = sensor_cfg['entity_id']
                timeout_m = sensor_cfg.get('timeout_m', 180)
                
                if entity_id in self.sensor_last_values:
                    value, timestamp = self.sensor_last_values[entity_id]
                    age_minutes = (now - timestamp).total_seconds() / 60
                    
                    if age_minutes <= timeout_m:
                        temps.append(value)
        
        # Return average or None
        if temps:
            avg_temp = sum(temps) / len(temps)
            return avg_temp, False
        else:
            return None, True
    
    def _apply_smoothing(self, room_id: str, raw_temp: float) -> float:
        """Apply exponential moving average smoothing to temperature.
        
        Smooths the fused temperature to reduce noise and prevent control
        instability when sensors report slightly different values that cause
        the averaged result to flip across decision boundaries.
        
        CRITICAL: Smoothing is applied to the temperature used for BOTH display
        AND control decisions (hysteresis, valve bands). This ensures consistent
        behavior - what you see is what affects heating control.
        
        Args:
            room_id: Room identifier
            raw_temp: Raw fused temperature in °C
            
        Returns:
            Smoothed temperature in °C
        """
        room_config = self.config.rooms.get(room_id, {})
        smoothing_config = room_config.get('smoothing', {})
        
        # Check if smoothing is enabled for this room
        if not smoothing_config.get('enabled', False):
            return raw_temp
        
        # Get smoothing factor (alpha) with fallback to default
        import constants as C
        alpha = smoothing_config.get('alpha', C.TEMPERATURE_SMOOTHING_ALPHA_DEFAULT)
        
        # Clamp alpha to valid range [0.0, 1.0]
        alpha = max(0.0, min(1.0, alpha))
        
        # First reading for this room - no history to smooth with
        if room_id not in self.smoothed_temps:
            self.smoothed_temps[room_id] = raw_temp
            return raw_temp
        
        # Apply EMA: smoothed = alpha * new + (1 - alpha) * previous
        previous = self.smoothed_temps[room_id]
        smoothed = alpha * raw_temp + (1.0 - alpha) * previous
        
        # Store for next iteration
        self.smoothed_temps[room_id] = smoothed
        
        return smoothed
    
    def get_room_temperature_smoothed(self, room_id: str, now: datetime) -> Tuple[Optional[float], bool]:
        """Get smoothed fused temperature for a room.
        
        This is the main method used by control logic. It applies smoothing to
        the fused temperature to ensure consistent control behavior.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            
        Returns:
            Tuple of (smoothed_temperature, is_stale)
            - smoothed_temperature: Smoothed average of available sensors, or None if all stale
            - is_stale: True if all sensors are stale or unavailable
        """
        raw_temp, is_stale = self.get_room_temperature(room_id, now)
        
        if raw_temp is None:
            return None, is_stale
        
        smoothed_temp = self._apply_smoothing(room_id, raw_temp)
        return smoothed_temp, is_stale
