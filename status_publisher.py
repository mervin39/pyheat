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
import json
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
    
    def _get_override_type(self, room_id: str) -> str:
        """Get override type for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            "none", "boost", or "override"
        """
        if not self.ad.entity_exists(C.HELPER_OVERRIDE_TYPES):
            return "none"
        
        try:
            value = self.ad.get_state(C.HELPER_OVERRIDE_TYPES)
            if value and value != "":
                override_types = json.loads(value)
                info = override_types.get(room_id)
                
                if info is None:
                    return "none"
                elif isinstance(info, str):
                    return info
                elif isinstance(info, dict):
                    return info.get("type", "none")
                else:
                    return "none"
            return "none"
        except (json.JSONDecodeError, TypeError) as e:
            self.ad.log(f"Failed to parse override types: {e}", level="WARNING")
            return "none"
    
    def _get_override_info(self, room_id: str) -> Dict[str, Any]:
        """Get full override/boost information for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Dict with keys: type ("none", "boost", "override"), delta (for boost), 
            target (for override), end_time (ISO format), remaining_minutes
        """
        result = {"type": "none"}
        
        if not self.ad.entity_exists(C.HELPER_OVERRIDE_TYPES):
            return result
        
        try:
            value = self.ad.get_state(C.HELPER_OVERRIDE_TYPES)
            if value and value != "":
                override_types = json.loads(value)
                info = override_types.get(room_id)
                
                if info is None or info == "none":
                    return result
                elif isinstance(info, str):
                    result["type"] = info
                elif isinstance(info, dict):
                    result["type"] = info.get("type", "none")
                    if "delta" in info:
                        result["delta"] = info["delta"]
                
                # Get timer information if override/boost is active
                if result["type"] != "none":
                    timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
                    if self.ad.entity_exists(timer_entity):
                        timer_state = self.ad.get_state(timer_entity)
                        if timer_state == "active":
                            # Get finishes_at attribute
                            finishes_at = self.ad.get_state(timer_entity, attribute="finishes_at")
                            if finishes_at:
                                result["end_time"] = finishes_at
                                
                                # Calculate remaining minutes
                                try:
                                    from datetime import datetime
                                    end_dt = datetime.fromisoformat(finishes_at.replace('Z', '+00:00'))
                                    now_dt = datetime.now(end_dt.tzinfo)
                                    remaining = (end_dt - now_dt).total_seconds() / 60
                                    result["remaining_minutes"] = max(0, int(remaining))
                                except Exception as e:
                                    self.ad.log(f"Error calculating remaining time for {room_id}: {e}", level="WARNING")
                    
                    # Get target temperature for override
                    if result["type"] == "override":
                        target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                        if self.ad.entity_exists(target_entity):
                            result["target"] = float(self.ad.get_state(target_entity))
                
            return result
        except (json.JSONDecodeError, TypeError) as e:
            self.ad.log(f"Failed to parse override info for {room_id}: {e}", level="WARNING")
            return result
    
    def _format_time_remaining(self, minutes: int) -> str:
        """Format remaining time as human-readable string.
        
        Args:
            minutes: Total minutes
            
        Returns:
            Formatted string like "45m", "2h", "4h 30m"
        """
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        mins = minutes % 60
        if mins == 0:
            return f"{hours}h"
        return f"{hours}h {mins}m"
    
    def _format_status_text(self, room_id: str, data: Dict, override_info: Dict, scheduled_temp: float = None) -> str:
        """Format human-readable status text for a room.
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            override_info: Override information from _get_override_info()
            scheduled_temp: Currently scheduled temperature from schedule (if available)
            
        Returns:
            Formatted status string
        """
        mode = data.get('mode', 'off')
        override_type = override_info.get('type', 'none')
        
        # Handle boost
        if override_type == 'boost':
            delta = override_info.get('delta', 0)
            remaining = override_info.get('remaining_minutes', 0)
            time_str = self._format_time_remaining(remaining)
            
            if scheduled_temp is not None:
                boosted_temp = scheduled_temp + delta
                sign = '+' if delta > 0 else ''
                return f"Boost {sign}{delta:.1f}°: {scheduled_temp:.1f}° → {boosted_temp:.1f}°. {time_str} left"
            else:
                sign = '+' if delta > 0 else ''
                return f"Boost {sign}{delta:.1f}°. {time_str} left"
        
        # Handle override
        if override_type == 'override':
            target = override_info.get('target', data.get('target', 20))
            remaining = override_info.get('remaining_minutes', 0)
            time_str = self._format_time_remaining(remaining)
            
            if scheduled_temp is not None:
                return f"Override: {scheduled_temp:.1f}° → {target:.1f}°. {time_str} left"
            else:
                return f"Override: {target:.1f}°. {time_str} left"
        
        # Handle manual mode
        if mode == 'manual':
            manual_setpoint = data.get('manual_setpoint', data.get('target', 0))
            temp = data.get('temp')
            if temp is not None and manual_setpoint is not None:
                if temp < manual_setpoint - 0.3:
                    return 'Heating up'
                elif temp > manual_setpoint + 0.3:
                    return 'Cooling down'
                else:
                    return 'At target temperature'
            return f'Manual: {manual_setpoint:.1f}°'
        
        # Handle off mode
        if mode == 'off':
            return 'Heating off'
        
        # Handle auto mode
        if mode == 'auto':
            temp = data.get('temp')
            target = data.get('target')
            
            if temp is None or target is None:
                return 'Sensor issue'
            
            if data.get('calling', False):
                valve = data.get('valve_percent', 0)
                return f'Heating ({valve}%)'
            elif temp < target - 0.1:
                return 'Heating up'
            elif temp > target + 0.1:
                return 'Cooling down'
            else:
                return 'At target temperature'
        
        return 'Unknown'
        
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
        # Build state string based on boiler state machine (like monolithic version)
        if boiler_state == C.STATE_ON:
            state = f"heating ({len(active_rooms)} room{'s' if len(active_rooms) != 1 else ''})"
        elif boiler_state == C.STATE_PUMP_OVERRUN:
            state = "pump overrun"
        elif boiler_state == C.STATE_PENDING_ON:
            state = "pending on (waiting for TRVs)"
        elif boiler_state == C.STATE_PENDING_OFF:
            state = "pending off (delay)"
        elif boiler_state == C.STATE_INTERLOCK_BLOCKED:
            state = "blocked (interlock)"
        else:
            state = "idle"
        
        # Build attributes
        attrs = {
            'any_call_for_heat': any_calling,
            'active_rooms': active_rooms,
            'room_calling_count': len(active_rooms),
            'total_rooms': len(self.config.rooms),
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
        room_config = self.config.rooms.get(room_id, {})
        room_name = room_config.get('name', room_id)
        precision = room_config.get('precision', 1)
        
        # Temperature sensor
        temp_entity = f"sensor.pyheat_{room_id}_temperature"
        if data['temp'] is not None:
            self.ad.set_state(temp_entity, 
                         state=round(data['temp'], precision),
                         attributes={
                             'unit_of_measurement': '°C',
                             'device_class': 'temperature',
                             'state_class': 'measurement',
                             'is_stale': data['is_stale']
                         })
        else:
            self.ad.set_state(temp_entity, state="unavailable")
        
        # Target sensor
        target_entity = f"sensor.pyheat_{room_id}_target"
        if data['target'] is not None:
            self.ad.set_state(target_entity, 
                         state=round(data['target'], precision),
                         attributes={
                             'unit_of_measurement': '°C',
                             'device_class': 'temperature',
                             'state_class': 'measurement'
                         })
        else:
            self.ad.set_state(target_entity, state="unavailable")
        
        # State sensor with comprehensive attributes
        state_entity = f"sensor.pyheat_{room_id}_state"
        
        # Get override/boost information
        override_info = self._get_override_info(room_id)
        override_type = override_info.get('type', 'none')
        
        # Get scheduled temperature if available from scheduler
        scheduled_temp = None
        if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
            try:
                scheduled_temp = self.scheduler_ref.get_current_target(room_id, now)
            except:
                pass
        
        # Format state string based on mode (legacy for backward compatibility)
        if data['mode'] == 'manual':
            manual_setpoint = data.get('manual_setpoint')
            if manual_setpoint is not None:
                state_str = f"manual({manual_setpoint})"
            else:
                state_str = f"manual({data.get('target', 0)})"
        else:
            state_str = data['mode']
        
        # Append override/boost indicator to legacy state
        if override_type != "none":
            state_str = f"{state_str} ({override_type})"
        elif data['mode'] == 'auto' and data.get('calling', False):
            state_str = f"heating ({data.get('valve_percent', 0)}%)"
        
        # Generate human-readable formatted status
        formatted_status = self._format_status_text(room_id, data, override_info, scheduled_temp)
        
        # Build comprehensive attributes
        attributes = {
            'friendly_name': f"{room_name} State",
            'mode': data['mode'],
            'temperature': round(data['temp'], precision) if data['temp'] is not None else None,
            'target': round(data['target'], precision) if data['target'] is not None else None,
            'calling_for_heat': data.get('calling', False),
            'valve_percent': data.get('valve_percent', 0),
            'is_stale': data.get('is_stale', False),
            'manual_setpoint': data.get('manual_setpoint'),
            'formatted_status': formatted_status,  # NEW: Human-readable status
            'scheduled_temp': round(scheduled_temp, precision) if scheduled_temp is not None else None,
        }
        
        # Add override/boost details if active
        if override_type != 'none':
            attributes['override_type'] = override_type
            attributes['override_end_time'] = override_info.get('end_time')
            attributes['override_remaining_minutes'] = override_info.get('remaining_minutes')
            
            if override_type == 'boost':
                attributes['boost_delta'] = override_info.get('delta')
                if scheduled_temp is not None:
                    attributes['boosted_target'] = round(scheduled_temp + override_info.get('delta', 0), precision)
            elif override_type == 'override':
                attributes['override_target'] = override_info.get('target')
            
        self.ad.set_state(state_entity, state=state_str, attributes=attributes, replace=True)
        
        # Valve percent sensor (read-only information)
        valve_entity = f"sensor.pyheat_{room_id}_valve_percent"
        valve_percent = data.get('valve_percent', 0)
        try:
            # Convert to string to avoid AppDaemon issues with numeric 0
            valve_state = str(int(valve_percent))
            self.ad.set_state(
                valve_entity,
                state=valve_state,
                attributes={
                    "unit_of_measurement": "%",
                    "friendly_name": f"{room_name} Valve Position"
                }
            )
        except Exception as e:
            self.ad.log(f"ERROR: Failed to set {valve_entity}: {type(e).__name__}: {e}", level="ERROR")
            import traceback
            self.ad.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
        
        # Calling binary sensor
        calling_entity = f"binary_sensor.pyheat_{room_id}_calling_for_heat"
        self.ad.set_state(calling_entity, 
                     state="on" if data.get('calling', False) else "off",
                     attributes={'friendly_name': f"{room_name} Calling for Heat"}, replace=True)
