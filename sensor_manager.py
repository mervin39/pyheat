# -*- coding: utf-8 -*-
"""
sensor_manager.py - Temperature sensor fusion and staleness tracking

Responsibilities:
- Track sensor values and timestamps
- Implement sensor fusion (primary/fallback, averaging)
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
        
    def initialize_from_ha(self) -> None:
        """Initialize sensor values from current Home Assistant state."""
        now = datetime.now()
        
        for room_id, room_cfg in self.config.rooms.items():
            for sensor_cfg in room_cfg['sensors']:
                entity_id = sensor_cfg['entity_id']
                
                try:
                    state_str = self.ad.get_state(entity_id)
                    if state_str and state_str not in ['unknown', 'unavailable']:
                        value = float(state_str)
                        self.sensor_last_values[entity_id] = (value, now)
                        self.ad.log(f"Initialized sensor {entity_id} = {value}°C", level="DEBUG")
                except (ValueError, TypeError) as e:
                    self.ad.log(f"Could not initialize sensor {entity_id}: {e}", level="WARNING")
    
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
