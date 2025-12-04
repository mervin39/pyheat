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
    
    def _get_passive_valve_percent(self, room_id: str) -> int:
        """Get passive valve percent for a room.
        
        Uses schedule's default_valve_percent if set, otherwise falls back
        to the HA input_number entity value.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Passive valve percent (0-100)
        """
        # Check schedule's default_valve_percent first
        schedule = self.config.schedules.get(room_id, {})
        schedule_valve = schedule.get('default_valve_percent')
        if schedule_valve is not None:
            return int(schedule_valve)
        
        # Fall back to HA entity
        passive_valve_entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room_id)
        if self.ad.entity_exists(passive_valve_entity):
            try:
                valve_str = self.ad.get_state(passive_valve_entity)
                if valve_str not in [None, "unknown", "unavailable"]:
                    return int(float(valve_str))
            except (ValueError, TypeError):
                pass
        
        return C.PASSIVE_VALVE_PERCENT_DEFAULT
    
    
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
    
    def _format_status_text(self, room_id: str, data: Dict, scheduled_info: Dict = None) -> str:
        """Format human-readable status text for a room.
        
        Implements STATUS_FORMAT_SPEC.md formats:
        - Frost Protection (highest priority): "FROST PROTECTION: T° -> TT°"
        - Load Sharing (priority): "Pre-warming for HH:MM" or "Fallback heating P{N}"
        - Auto: "Auto: T° until HH:MM on $DAY (S°)" or "Auto: T° forever"
        - Auto + Scheduled Passive: "Auto (passive): X-Y°, Z% until HH:MM on $DAY (A°)"
        - Override: "Override: T° (ΔD°) until HH:MM" - delta calculated on-the-fly
        - Manual: "Manual: T°"
        - Passive: "Passive: X-Y°, Z%"
        - Off: "Heating Off"
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            scheduled_info: Currently scheduled info dict from scheduler (includes target, mode, is_default_mode, block_end_time)
            
        Returns:
            Formatted status string
        """
        mode = data.get('mode', 'off')
        operating_mode = data.get('operating_mode', mode)  # Get actual operating mode
        scheduled_temp = scheduled_info['target'] if scheduled_info else None
        
        # Check frost protection FIRST (highest priority)
        if data.get('frost_protection', False):
            temp = data.get('temp', 0)
            target = data.get('target', 0)
            return f"FROST PROTECTION: {temp:.1f}C -> {target:.1f}C (emergency heating)"
        
        # Check comfort mode SECOND (passive mode below minimum temperature)
        if data.get('comfort_mode', False):
            temp = data.get('temp', 0)
            min_temp = data.get('passive_min_temp', 0)
            return f"Comfort heating (below {min_temp:.1f}C)"
        
        # Check load sharing SECOND (takes priority over override)
        if hasattr(self.ad, 'load_sharing') and self.ad.load_sharing:
            load_sharing_context = self.ad.load_sharing.context
            if load_sharing_context and load_sharing_context.active_rooms:
                for room_id_key, activation in load_sharing_context.active_rooms.items():
                    if activation.room_id == room_id:
                        # Room is in load sharing
                        if activation.tier == 1:
                            # Tier 1: Schedule-aware pre-warming
                            # Get next schedule block time
                            if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
                                try:
                                    from datetime import datetime
                                    now = datetime.now()
                                    # Look ahead up to 2 hours
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
                        elif activation.tier == 2:
                            # Tier 2: Fallback (passive rooms + priority list)
                            room_config = self.config.rooms.get(room_id, {})
                            load_sharing_config = room_config.get('load_sharing', {})
                            priority = load_sharing_config.get('fallback_priority', '?')
                            valve_pct = activation.valve_pct
                            return f"Fallback heating P{priority} ({valve_pct}%)"
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
        
        # Handle passive mode (not in comfort mode)
        if mode == 'passive':
            max_temp = data.get('target')  # This is the max_temp in passive mode
            min_temp = data.get('passive_min_temp')
            
            if max_temp is None or min_temp is None:
                return 'Passive (opportunistic)'
            
            # Get configured passive valve percent (schedule takes precedence over entity)
            passive_valve_percent = self._get_passive_valve_percent(room_id)
            
            return f'Passive: {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}%'
        
        # Handle auto mode (no override)
        if mode == 'auto':
            target = data.get('target')
            
            if target is None:
                return 'Auto: ??°'
            
            # Check if we're in a scheduled passive block (auto mode but operating in passive)
            if operating_mode == 'passive':
                max_temp = target  # In passive mode, target is the max temp
                min_temp = data.get('passive_min_temp')
                
                # Get configured passive valve percent (schedule takes precedence over entity)
                passive_valve_percent = self._get_passive_valve_percent(room_id)
                
                # Check if we're in a scheduled block (not default mode)
                is_in_block = scheduled_info and not scheduled_info.get('is_default_mode', True)
                
                if is_in_block and 'block_end_time' in scheduled_info:
                    # In a scheduled passive block - show when THIS BLOCK ends
                    block_end = scheduled_info['block_end_time']
                    if block_end == '23:59':
                        block_end = '24:00'
                    
                    # Get what temperature will be active when this block ends
                    # This will be the default_target (not necessarily the next scheduled block)
                    if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
                        from datetime import datetime
                        now = datetime.now()
                        
                        holiday_mode = False
                        if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                        
                        # Parse block end time and get schedule at that moment
                        schedule = self.scheduler_ref.config.schedules.get(room_id)
                        default_target = schedule.get('default_target') if schedule else None
                        
                        # Simulate what temperature will be at block end time
                        end_hour, end_min = map(int, block_end.replace('24:00', '23:59').split(':'))
                        simulated_time = now.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)
                        
                        # Get what would be scheduled at block end time
                        future_scheduled = self.scheduler_ref.get_scheduled_target(room_id, simulated_time, holiday_mode)
                        
                        if future_scheduled and min_temp is not None:
                            # Check if next mode is also passive
                            if future_scheduled.get('mode') == 'passive':
                                next_max = future_scheduled['target']
                                next_min = future_scheduled.get('min_target', 8.0)
                                next_valve = future_scheduled.get('valve_percent', 30)
                                return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {block_end} (passive {next_min:.0f}-{next_max:.0f}°, {next_valve}%)"
                            else:
                                # Next mode is active
                                temp_after_block = future_scheduled['target']
                                return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {block_end} ({temp_after_block:.1f}°)"
                    
                    # Fallback if can't get next temperature
                    if min_temp is not None:
                        return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {block_end}"
                    else:
                        return f"Auto (passive): max {max_temp:.0f}°, {passive_valve_percent}% until {block_end}"
                else:
                    # In default passive mode - show when next block starts
                    if hasattr(self, 'scheduler_ref') and self.scheduler_ref:
                        from datetime import datetime
                        now = datetime.now()
                        
                        holiday_mode = False
                        if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                            holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"
                        
                        next_change = self.scheduler_ref.get_next_schedule_change(room_id, now, holiday_mode)
                        
                        if next_change and min_temp is not None:
                            next_time, next_temp, day_offset = next_change
                            
                            # Get what mode will be active at the next change
                            next_hour, next_min = map(int, next_time.split(':'))
                            future_time = now.replace(hour=next_hour, minute=next_min, second=0, microsecond=0)
                            if day_offset > 0:
                                from datetime import timedelta
                                future_time = future_time + timedelta(days=day_offset)
                            
                            future_scheduled = self.scheduler_ref.get_scheduled_target(room_id, future_time, holiday_mode)
                            
                            if day_offset == 0:
                                if future_scheduled and future_scheduled.get('mode') == 'passive':
                                    # Next change is to another passive mode
                                    next_max = future_scheduled['target']
                                    next_min_temp = future_scheduled.get('min_target', 8.0)
                                    next_valve = future_scheduled.get('valve_percent', 30)
                                    return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {next_time} (passive {next_min_temp:.0f}-{next_max:.0f}°, {next_valve}%)"
                                else:
                                    # Next change is to active mode
                                    return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {next_time} ({next_temp:.1f}°)"
                            else:
                                day_names_display = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                                future_day_idx = (now.weekday() + day_offset) % 7
                                day_name = day_names_display[future_day_idx]
                                
                                if future_scheduled and future_scheduled.get('mode') == 'passive':
                                    # Next change is to another passive mode
                                    next_max = future_scheduled['target']
                                    next_min_temp = future_scheduled.get('min_target', 8.0)
                                    next_valve = future_scheduled.get('valve_percent', 30)
                                    return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {next_time} on {day_name} (passive {next_min_temp:.0f}-{next_max:.0f}°, {next_valve}%)"
                                else:
                                    # Next change is to active mode
                                    return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}% until {next_time} on {day_name} ({next_temp:.1f}°)"
                    
                    # Fallback for auto passive without next change info - check if forever
                    forever_suffix = " forever" if self._check_if_forever(room_id) else ""
                    if min_temp is not None:
                        return f"Auto (passive): {min_temp:.0f}-{max_temp:.0f}°, {passive_valve_percent}%{forever_suffix}"
                    else:
                        return f"Auto (passive): max {max_temp:.0f}°, {passive_valve_percent}%{forever_suffix}"
            
            # Standard auto mode (active heating)
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
                    
                    # Get what mode will be active at the next change
                    # Simulate the future time to check if it's passive
                    next_hour, next_min = map(int, next_time.split(':'))
                    future_time = now.replace(hour=next_hour, minute=next_min, second=0, microsecond=0)
                    if day_offset > 0:
                        from datetime import timedelta
                        future_time = future_time + timedelta(days=day_offset)
                    
                    future_scheduled = self.scheduler_ref.get_scheduled_target(room_id, future_time, holiday_mode)
                    
                    # Determine day name based on day_offset
                    if day_offset == 0:
                        # Today - no day name needed
                        if future_scheduled and future_scheduled.get('mode') == 'passive':
                            # Next change is to passive mode
                            next_max = future_scheduled['target']
                            next_min = future_scheduled.get('min_target', 8.0)
                            next_valve = future_scheduled.get('valve_percent', 30)
                            return f"Auto: {target:.1f}° until {next_time} (passive {next_min:.0f}-{next_max:.0f}°, {next_valve}%)"
                        else:
                            # Next change is to active mode
                            return f"Auto: {target:.1f}° until {next_time} ({next_temp:.1f}°)"
                    else:
                        # Future day - include day name
                        day_names_display = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                        future_day_idx = (now.weekday() + day_offset) % 7
                        day_name = day_names_display[future_day_idx]
                        
                        if future_scheduled and future_scheduled.get('mode') == 'passive':
                            # Next change is to passive mode
                            next_max = future_scheduled['target']
                            next_min = future_scheduled.get('min_target', 8.0)
                            next_valve = future_scheduled.get('valve_percent', 30)
                            return f"Auto: {target:.1f}° until {next_time} on {day_name} (passive {next_min:.0f}-{next_max:.0f}°, {next_valve}%)"
                        else:
                            # Next change is to active mode
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
                'operating_mode': data.get('operating_mode', 'off'),
                'temperature': round(data['temp'], 1) if data['temp'] is not None else None,
                'target': round(data['target'], 1) if data['target'] is not None else None,
                'calling_for_heat': data.get('calling', False),
                'valve_percent': data.get('valve_percent', 0),
                'is_stale': data.get('is_stale', True),
                'frost_protection': data.get('frost_protection', False),
            }
            
            # Add estimated capacity if load monitoring enabled
            if hasattr(self, 'load_calculator_ref') and self.load_calculator_ref:
                if self.load_calculator_ref.enabled:
                    estimated_capacity = self.load_calculator_ref.estimated_capacities.get(room_id, 0.0)
                    room_attrs['estimated_dump_capacity'] = round(estimated_capacity, 0)
            
            # Add passive-specific fields when in passive mode
            if data.get('operating_mode') == 'passive':
                room_attrs['passive_max_temp'] = data.get('target')  # In passive mode, target is max_temp
                room_attrs['passive_min_temp'] = data.get('passive_min_temp')  # Comfort floor
                room_attrs['comfort_mode'] = data.get('comfort_mode', False)
            
            attrs['rooms'][room_id] = room_attrs
            total_valve += data.get('valve_percent', 0)
        
        attrs['total_valve_percent'] = total_valve
        
        # Add total estimated capacity if available
        if hasattr(self, 'load_calculator_ref') and self.load_calculator_ref:
            if self.load_calculator_ref.enabled:
                attrs['total_estimated_dump_capacity'] = round(self.load_calculator_ref.get_total_estimated_capacity(), 0)
        
        # Set state (replace=True ensures all attributes are set fresh, not merged)
        self.ad.set_state(C.STATUS_ENTITY, state=state, attributes=attrs, replace=True)
        
        # Publish system-wide calling for heat binary sensor
        self.ad.set_state(
            C.CALLING_FOR_HEAT_ENTITY,
            state="on" if any_calling else "off",
            attributes={
                'friendly_name': 'PyHeat Calling for Heat',
                'device_class': 'heat',
                'active_rooms': active_rooms,
                'room_count': len(active_rooms)
            },
            replace=True
        )
        
        # Publish cooldown active binary sensor
        if hasattr(self.ad, 'cycling'):
            cooldown_active = self.ad.cycling.state == 'COOLDOWN'
            cooldown_attrs = {
                'friendly_name': 'PyHeat Cooldown Active',
                'icon': 'mdi:snowflake-alert' if cooldown_active else 'mdi:snowflake'
            }
            if cooldown_active:
                cooldown_attrs['cooldown_start'] = self.ad.cycling.cooldown_entry_time.isoformat() if self.ad.cycling.cooldown_entry_time else None
                cooldown_attrs['saved_setpoint'] = self.ad.cycling.saved_setpoint
                cooldown_attrs['recovery_threshold'] = self.ad.cycling._get_recovery_threshold()
            self.ad.set_state(
                C.COOLDOWN_ACTIVE_ENTITY,
                state="on" if cooldown_active else "off",
                attributes=cooldown_attrs,
                replace=True
            )
        
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
        scheduled_info = None
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
        formatted_status = self._format_status_text(room_id, data, scheduled_info)
        
        # Build comprehensive attributes
        attributes = {
            'friendly_name': f"{room_name} State",
            'mode': data['mode'],
            'operating_mode': data.get('operating_mode', 'off'),
            'temperature': round(data['temp'], precision) if data['temp'] is not None else None,
            'target': round(data['target'], precision) if data['target'] is not None else None,
            'calling_for_heat': data.get('calling', False),
            'valve_percent': data.get('valve_percent', 0),
            'is_stale': data.get('is_stale', False),
            'frost_protection': data.get('frost_protection', False),
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
