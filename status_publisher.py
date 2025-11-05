# -*- coding: utf-8 -*-
"""
status_publisher.py - Status entity publishing

Responsibilities:
- Publish system status to sensor.pyheat_status
- Publish per-room entities (temperature, target, state, valve, calling)
- Format status attributes for Home Assistant
"""

from datetime import datetime
from typing import Dict, List, Any
import pyheat.constants as C


class StatusPublisher:
    """Publishes PyHeat status to Home Assistant entities."""
    
    def __init__(self, ad, config):
        """Initialize the status publisher.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        
    def publish_system_status(self, any_calling: bool, active_rooms: List[str],
                             room_data: Dict, boiler_state: str, boiler_reason: str,
                             now: datetime) -> None:
        """Publish main system status entity.
        
        Args:
            any_calling: Whether any room is calling for heat
            active_rooms: List of calling room IDs
            room_data: Dict of room states
            boiler_state: Current boiler state
            boiler_reason: Reason for boiler state
            now: Current datetime
        """
        # Build state string
        if any_calling:
            state = f"heating ({len(active_rooms)} room{'s' if len(active_rooms) != 1 else ''})"
        else:
            state = "idle"
        
        # Build attributes
        attrs = {
            'rooms': {},
            'boiler_state': boiler_state,
            'boiler_reason': boiler_reason,
            'total_valve_percent': 0,
            'last_recompute': now.isoformat(),
        }
        
        # Add per-room data
        total_valve = 0
        for room_id, data in room_data.items():
            attrs['rooms'][room_id] = {
                'mode': data.get('mode', 'off'),
                'temperature': round(data['temp'], 1) if data['temp'] is not None else None,
                'target': round(data['target'], 1) if data['target'] is not None else None,
                'calling_for_heat': data.get('calling', False),
                'valve_percent': data.get('valve_percent', 0),
                'is_stale': data.get('is_stale', True),
            }
            total_valve += data.get('valve_percent', 0)
        
        attrs['total_valve_percent'] = total_valve
        
        # Set state
        self.ad.set_state(C.STATUS_ENTITY, state=state, attributes=attrs)
        
    def publish_room_entities(self, room_id: str, data: Dict, now: datetime) -> None:
        """Publish per-room entities.
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            now: Current datetime
        """
        precision = self.config.rooms[room_id].get('precision', 1)
        
        # Temperature sensor
        temp_entity = f"sensor.pyheat_{room_id}_temperature"
        if data['temp'] is not None:
            self.ad.set_state(temp_entity, 
                         state=round(data['temp'], precision),
                         attributes={'unit_of_measurement': '°C', 'is_stale': data['is_stale']})
        else:
            self.ad.set_state(temp_entity, state="unavailable")
        
        # Target sensor
        target_entity = f"sensor.pyheat_{room_id}_target"
        if data['target'] is not None:
            self.ad.set_state(target_entity, 
                         state=round(data['target'], precision),
                         attributes={'unit_of_measurement': '°C'})
        else:
            self.ad.set_state(target_entity, state="unavailable")
        
        # State sensor
        state_entity = f"sensor.pyheat_{room_id}_state"
        state_str = data['mode']
        if data['mode'] == 'auto' and data.get('calling', False):
            state_str = f"heating ({data.get('valve_percent', 0)}%)"
        self.ad.set_state(state_entity, state=state_str)
        
        # Valve percent number - use service call instead of set_state for number entities
        valve_entity = f"number.pyheat_{room_id}_valve_percent"
        try:
            self.ad.call_service('number/set_value',
                            entity_id=valve_entity,
                            value=data.get('valve_percent', 0))
        except Exception as e:
            # If entity doesn't exist, create it via set_state
            self.ad.set_state(valve_entity, 
                         state=data.get('valve_percent', 0),
                         attributes={'unit_of_measurement': '%'})
        
        # Calling binary sensor
        calling_entity = f"binary_sensor.pyheat_{room_id}_calling_for_heat"
        self.ad.set_state(calling_entity, 
                     state="on" if data.get('calling', False) else "off")
