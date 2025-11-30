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
import constants as C


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
    
    
    def update_room_temperature(self, room_id: str, temp: float, is_stale: bool) -> None:
        """Update just the temperature sensor entity (lightweight operation).
        
        This is a lightweight method that only updates the temperature sensor entity
        without touching other room entities. Called on every source sensor change
        to provide real-time temperature updates independent of recompute logic.
        
        NOTE: Expects temperature to already be smoothed if smoothing is enabled.
        Smoothing is applied in app.py sensor_changed() before calling this method.
        
        Args:
            room_id: Room identifier
            temp: Temperature in °C (already smoothed if applicable, or None if unavailable)
            is_stale: Whether all sensors are stale/unavailable
        """
        room_config = self.config.rooms.get(room_id, {})
        precision = room_config.get('precision', 1)
        temp_entity = f"sensor.pyheat_{room_id}_temperature"
        
        if temp is not None:
            # Temperature is already smoothed (if enabled), just display it
            self.ad.set_state(temp_entity, 
                             state=round(temp, precision),
                             attributes={
                                 'unit_of_measurement': '°C',
                                 'device_class': 'temperature',
                                 'state_class': 'measurement',
                                 'is_stale': is_stale
                             })
        else:
            self.ad.set_state(temp_entity, state="unavailable")
    
    def _check_if_forever(self, room_id: str) -> bool:
        """Check if schedule is set to run forever (no blocks on any day).
        
        Args:
            room_id: Room identifier
            
        Returns:
            True if all days have no schedule blocks
        """
        if not hasattr(self, 'scheduler_ref') or not self.scheduler_ref:
            return False
        
        schedule = self.scheduler_ref.config.schedules.get(room_id)
        if not schedule:
            return True  # No schedule = forever
        
        week_schedule = schedule.get('week', {})
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        
        # Check if all days have empty blocks
        for day in day_names:
            blocks = week_schedule.get(day, [])
            if blocks:  # If any day has blocks, not forever
                return False
        
        return True
    
    def _format_status_text(self, room_id: str, data: Dict, scheduled_temp: float = None) -> str:
        """Format human-readable status text for a room.
        
        Implements STATUS_FORMAT_SPEC.md formats:
        - Load Sharing (priority): "Pre-warming for HH:MM" or "Fallback heating P{N}"
        - Auto: "Auto: T° until HH:MM on $DAY (S°)" or "Auto: T° forever"
        - Override: "Override: T° (ΔD°) until HH:MM" - delta calculated on-the-fly
        - Manual: "Manual: T°"
        - Off: "Heating Off"
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            scheduled_temp: Currently scheduled temperature from schedule (if available)
            
        Returns:
            Formatted status string
        """
        mode = data.get('mode', 'off')
        
        # Check load sharing FIRST (takes priority over override)
        if hasattr(self.ad, 'load_sharing') and self.ad.load_sharing:
            load_sharing_context = self.ad.load_sharing.context
            if load_sharing_context and load_sharing_context.active_rooms:
                for room_id_key, activation in load_sharing_context.active_rooms.items():
                    if activation.room_id == room_id:
                        # Room is in load sharing
                        if activation.tier in [1, 2]:
                            # Tier 1/2: Schedule-aware pre-warming
                            # Get next schedule block time
                            if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
                                try:
                                    from datetime import datetime
                                    now = datetime.now()
                                    # Look ahead up to 2 hours for Tier 2
                                    next_block = self.scheduler_ref.get_next_schedule_block(
                                        room_id, now, within_minutes=120
                                    )
                                    if next_block:
                                        block_start_dt, _, _ = next_block
                                        return f"Pre-warming for {block_start_dt.strftime('%H:%M')}"
                                except Exception as e:
                                    self.ad.log(f"Error getting schedule for load sharing status: {e}", level="WARNING")
                            # Fallback if can't get schedule time
                            return "Pre-warming for schedule"
                        else:
                            # Tier 3: Fallback priority
                            room_config = self.config.rooms.get(room_id, {})
                            load_sharing_config = room_config.get('load_sharing', {})
                            priority = load_sharing_config.get('fallback_priority', '?')
                            return f"Fallback heating P{priority}"
                        break
        
        # Check if override is active
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        override_active = False
        override_target = None
        end_time_str = ""
        
        if self.ad.entity_exists(timer_entity):
            timer_state = self.ad.get_state(timer_entity)
            if timer_state in ["active", "paused"]:
                override_active = True
                
                # Get override target
                target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                if self.ad.entity_exists(target_entity):
                    try:
                        override_target = float(self.ad.get_state(target_entity))
                    except (ValueError, TypeError):
                        pass
                
                # Get end time from timer
                finishes_at = self.ad.get_state(timer_entity, attribute="finishes_at")
                if finishes_at:
                    try:
                        from datetime import datetime
                        end_dt = datetime.fromisoformat(finishes_at.replace('Z', '+00:00'))
                        end_time_str = f" until {end_dt.strftime('%H:%M')}"
                    except Exception as e:
                        self.ad.log(f"Error formatting override end time for {room_id}: {e}", level="WARNING")
                        end_time_str = " until ??:??"
        
        # Handle override (in auto mode)
        if override_active and override_target is not None and mode == 'auto':
            # Calculate delta if we have scheduled temp
            delta_str = ""
            if scheduled_temp is not None:
                delta = override_target - scheduled_temp
                delta_str = f" ({delta:+.1f}°)"
            
            return f"Override: {override_target:.1f}°{delta_str}{end_time_str}"
        
        # Handle manual mode
        if mode == 'manual':
            manual_setpoint = data.get('manual_setpoint', data.get('target', 0))
            return f'Manual: {manual_setpoint:.1f}°'
        
        # Handle off mode
        if mode == 'off':
            return 'Heating Off'
        
        # Handle auto mode (no override)
        if mode == 'auto':
            target = data.get('target')
            
            if target is None:
                return 'Auto: ??°'
            
            # Check if schedule is forever
            if self._check_if_forever(room_id):
                return f"Auto: {target:.1f}° forever"
            
            # Get next schedule change
            if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
                from datetime import datetime
                now = datetime.now()
                
                # Get holiday mode
                holiday_mode = False
                if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                    holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                
                next_change = self.scheduler_ref.get_next_schedule_change(room_id, now, holiday_mode)
                
                if next_change:
                    next_time, next_temp, day_offset = next_change
                    
                    # Determine day name based on day_offset
                    if day_offset == 0:
                        # Today - no day name needed
                        return f"Auto: {target:.1f}° until {next_time} ({next_temp:.1f}°)"
                    else:
                        # Future day - include day name
                        day_names_display = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                        future_day_idx = (now.weekday() + day_offset) % 7
                        day_name = day_names_display[future_day_idx]
                        return f"Auto: {target:.1f}° until {next_time} on {day_name} ({next_temp:.1f}°)"
                else:
                    # No next change found - treat as forever
                    return f"Auto: {target:.1f}° forever"
            
            # Fallback: no scheduler available
            return f"Auto: {target:.1f}°"
        
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
        
        # Add cycling protection state if available
        if hasattr(self.ad, 'cycling'):
            cycling_state_dict = self.ad.cycling.get_state_dict()
            cooldowns_last_hour = len([
                entry for entry in self.ad.cycling.cooldown_history
                if (now - entry[0]).total_seconds() < 3600
            ])
            
            attrs['cycling_protection'] = {
                'state': self.ad.cycling.state,
                'cooldown_start': self.ad.cycling.cooldown_entry_time.isoformat() if self.ad.cycling.cooldown_entry_time else None,
                'saved_setpoint': self.ad.cycling.saved_setpoint,
                'recovery_threshold': self.ad.cycling._get_recovery_threshold() if self.ad.cycling.state == 'COOLDOWN' else None,
                'cooldowns_last_hour': cooldowns_last_hour
            }
        
        # Add load sharing state if available
        if hasattr(self.ad, 'load_sharing'):
            load_sharing_status = self.ad.load_sharing.get_status()
            attrs['load_sharing'] = load_sharing_status
        
        # Add per-room data
        total_valve = 0
        for room_id, data in room_data.items():
            room_attrs = {
                'mode': data.get('mode', 'off'),
                'temperature': round(data['temp'], 1) if data['temp'] is not None else None,
                'target': round(data['target'], 1) if data['target'] is not None else None,
                'calling_for_heat': data.get('calling', False),
                'valve_percent': data.get('valve_percent', 0),
                'is_stale': data.get('is_stale', True),
            }
            
            # Add estimated capacity if load monitoring enabled
            if hasattr(self, 'load_calculator_ref') and self.load_calculator_ref:
                if self.load_calculator_ref.enabled:
                    estimated_capacity = self.load_calculator_ref.estimated_capacities.get(room_id, 0.0)
                    room_attrs['estimated_dump_capacity'] = round(estimated_capacity, 0)
            
            attrs['rooms'][room_id] = room_attrs
            total_valve += data.get('valve_percent', 0)
        
        attrs['total_valve_percent'] = total_valve
        
        # Add total estimated capacity if available
        if hasattr(self, 'load_calculator_ref') and self.load_calculator_ref:
            if self.load_calculator_ref.enabled:
                attrs['total_estimated_dump_capacity'] = round(self.load_calculator_ref.get_total_estimated_capacity(), 0)
        
        # Set state
        self.ad.set_state(C.STATUS_ENTITY, state=state, attributes=attrs)
        
    def publish_room_entities(self, room_id: str, data: Dict, now: datetime) -> None:
        """Publish per-room entities.
        
        NOTE: Temperature sensor is NOT updated here - it's updated in real-time
        by sensor_changed() with smoothing applied. This prevents recompute from
        overwriting smoothed values with raw fused temperatures.
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            now: Current datetime
        """
        room_config = self.config.rooms.get(room_id, {})
        room_name = room_config.get('name', room_id)
        precision = room_config.get('precision', 1)
        
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
        
        # Check if override is active
        override_active = False
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.ad.entity_exists(timer_entity):
            timer_state = self.ad.get_state(timer_entity)
            if timer_state in ["active", "paused"]:
                override_active = True
        
        # Get scheduled temperature if available from scheduler
        scheduled_temp = None
        if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
            try:
                # Get holiday mode
                holiday_mode = False
                if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                    holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                scheduled_info = self.scheduler_ref.get_scheduled_target(room_id, now, holiday_mode)
                if scheduled_info is not None:
                    scheduled_temp = scheduled_info['target']
            except Exception as e:
                self.ad.log(f"Error getting scheduled temp for {room_id}: {e}", level="WARNING")
        
        # Format state string based on mode (legacy for backward compatibility)
        if data['mode'] == 'manual':
            manual_setpoint = data.get('manual_setpoint')
            if manual_setpoint is not None:
                state_str = f"manual({manual_setpoint})"
            else:
                state_str = f"manual({data.get('target', 0)})"
        else:
            state_str = data['mode']
        
        # Append override indicator to legacy state
        if override_active:
            state_str = f"{state_str} (override)"
        elif data['mode'] == 'auto' and data.get('calling', False):
            state_str = f"heating ({data.get('valve_percent', 0)}%)"
        
        # Generate human-readable formatted status
        formatted_status = self._format_status_text(room_id, data, scheduled_temp)
        
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
            'formatted_status': formatted_status,  # Human-readable status
            'scheduled_temp': round(scheduled_temp, precision) if scheduled_temp is not None else None,
        }
        
        # Add override details if active
        if override_active:
            # Get override target
            target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
            if self.ad.entity_exists(target_entity):
                try:
                    override_target_value = float(self.ad.get_state(target_entity))
                    attributes['override_target'] = round(override_target_value, precision)
                except (ValueError, TypeError):
                    pass
            
            # Get timer end time and remaining minutes
            if self.ad.entity_exists(timer_entity):
                finishes_at = self.ad.get_state(timer_entity, attribute="finishes_at")
                if finishes_at:
                    attributes['override_end_time'] = finishes_at
                    try:
                        from datetime import datetime
                        end_dt = datetime.fromisoformat(finishes_at.replace('Z', '+00:00'))
                        now_dt = datetime.now(end_dt.tzinfo)
                        remaining = (end_dt - now_dt).total_seconds() / 60
                        attributes['override_remaining_minutes'] = max(0, int(remaining))
                    except Exception as e:
                        self.ad.log(f"Error calculating remaining time for {room_id}: {e}", level="WARNING")
            
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
        
        # Capacity data now in sensor.pyheat_status room attributes - no separate sensors needed
        
        # Calling binary sensor
        calling_entity = f"binary_sensor.pyheat_{room_id}_calling_for_heat"
        self.ad.set_state(calling_entity, 
                     state="on" if data.get('calling', False) else "off",
                     attributes={'friendly_name': f"{room_name} Calling for Heat"}, replace=True)
