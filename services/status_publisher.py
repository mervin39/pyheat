# -*- coding: utf-8 -*-
"""
status_publisher.py - Status entity publishing

Responsibilities:
- Publish system status to sensor.pyheat_status
- Publish per-room entities (temperature, target, state, valve, calling)
- Format status attributes for Home Assistant
"""

from datetime import datetime
from typing import Dict, List, Any, Optional
import json
import constants as C


class StatusPublisher:
    """Publishes PyHeat status to Home Assistant entities."""
    
    def __init__(self, ad, config, overrides=None):
        """Initialize the status publisher.

        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            overrides: Optional OverrideManager instance for checking override state
        """
        self.ad = ad
        self.config = config
        self.overrides = overrides
    
    def _get_passive_valve_percent(self, room_id: str) -> int:
        """Get passive valve percent for a room from HA entity.

        Always reads from the HA input_number entity (runtime value).
        Note: schedule's default_valve_percent is only for initialization.

        Args:
            room_id: Room identifier

        Returns:
            Passive valve percent (0-100)
        """
        # Read from HA entity (runtime value)
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

    def _get_next_schedule_info(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[Dict]:
        """Get next schedule change information for display purposes.

        Implements the complete schedule finding logic:
        1. Determine if in a schedule block or default mode
        2. If in block: Find end time and what comes next (block or default)
        3. If in default: Find next scheduled block (may be days ahead)
        4. Loop through entire week if necessary
        5. Return None if no changes found (forever)

        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active

        Returns:
            Dict with keys:
                'time': str (HH:MM)
                'day_offset': int (0=today, 1=tomorrow, etc)
                'mode': str ('active' or 'passive')
                'target': float (setpoint for active, min_temp for passive)
                'passive_max_temp': Optional[float] (for passive only)
                'valve_percent': Optional[int] (for passive only)
            Or None if forever/no schedule/holiday mode
        """
        if not hasattr(self, 'scheduler_ref') or not self.scheduler_ref:
            return None

        schedule = self.scheduler_ref.config.schedules.get(room_id)
        if not schedule or holiday_mode:
            return None

        week_schedule = schedule.get('week', {})
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        current_day_idx = now.weekday()
        current_time = now.strftime("%H:%M")

        # Get default mode settings
        default_mode = schedule.get('default_mode', 'active')
        default_target = schedule.get('default_target', 16.0)

        # Step 1: Determine current state - are we in a schedule block?
        current_day_name = day_names[current_day_idx]
        blocks_today = week_schedule.get(current_day_name, [])

        # IMPORTANT: Sort blocks by start time (blocks may not be in order in YAML)
        blocks_today = sorted(blocks_today, key=lambda b: b['start'])

        in_block = False
        current_block = None
        current_block_end = None

        for block in blocks_today:
            block_start = block['start']
            block_end = block.get('end', '23:59')
            # Convert 23:59 to 24:00 for comparison
            if block_end == '23:59':
                block_end = '24:00'

            if block_start <= current_time < block_end:
                in_block = True
                current_block = block
                current_block_end = block_end
                break

        if in_block:
            # Step 2a: We're in a scheduled block - find what comes next at block end

            # Check if block ends at 24:00 (end of day)
            if current_block_end == '24:00':
                # Block goes to end of day - check next day at 00:00
                next_day_offset = 1
                next_day_idx = (current_day_idx + next_day_offset) % 7
                next_day_name = day_names[next_day_idx]
                next_day_blocks = week_schedule.get(next_day_name, [])

                # Sort blocks by start time (blocks may not be in order in YAML)
                next_day_blocks = sorted(next_day_blocks, key=lambda b: b['start']) if next_day_blocks else []

                # Check if there's a block at 00:00 on next day
                if next_day_blocks and next_day_blocks[0]['start'] == '00:00':
                    # Block defined at 00:00 tomorrow
                    next_block = next_day_blocks[0]
                    return self._build_schedule_info_dict('00:00', next_day_offset, next_block, schedule)
                else:
                    # Gap at midnight - revert to default
                    return self._build_schedule_info_dict_default('00:00', next_day_offset, schedule)
            else:
                # Block ends before end of day - check if there's a block starting at end time
                block_at_end = None
                for block in blocks_today:
                    if block['start'] == current_block_end:
                        block_at_end = block
                        break

                if block_at_end:
                    # New block defined at end time
                    return self._build_schedule_info_dict(current_block_end, 0, block_at_end, schedule)
                else:
                    # No block at end time - revert to default
                    return self._build_schedule_info_dict_default(current_block_end, 0, schedule)

        else:
            # Step 2b: We're in default mode - find next scheduled block

            # First, check remaining blocks today (after current time)
            for block in blocks_today:
                if block['start'] > current_time:
                    # Found next block today
                    return self._build_schedule_info_dict(block['start'], 0, block, schedule)

            # No more blocks today - search future days
            for day_offset in range(1, 8):  # Check next 7 days
                day_idx = (current_day_idx + day_offset) % 7
                day_name = day_names[day_idx]
                day_blocks = week_schedule.get(day_name, [])

                if day_blocks:
                    # Sort blocks by start time (blocks may not be in order in YAML)
                    day_blocks = sorted(day_blocks, key=lambda b: b['start'])
                    # Found a block on this day - use the first one
                    first_block = day_blocks[0]
                    return self._build_schedule_info_dict(first_block['start'], day_offset, first_block, schedule)

            # After checking entire week, no blocks found - this is "forever"
            return None

    def _build_schedule_info_dict(self, time_str: str, day_offset: int, block: Dict, schedule: Dict) -> Dict:
        """Build schedule info dict from a schedule block.

        Args:
            time_str: HH:MM time string
            day_offset: Days from now (0=today, 1=tomorrow, etc)
            block: Schedule block dict
            schedule: Full schedule dict (for defaults)

        Returns:
            Dict with schedule info
        """
        block_mode = block.get('mode', 'active')

        if block_mode == 'passive':
            # For passive: target is max_temp, need to get min_temp
            max_temp = block['target']
            min_temp = block.get('min_target')
            if min_temp is None:
                # Fall back to default or entity value (use 'or' to handle None in config)
                min_temp = schedule.get('default_min_temp') or C.FROST_PROTECTION_TEMP_C_DEFAULT
            valve_percent = block.get('valve_percent') or schedule.get('default_valve_percent') or C.PASSIVE_VALVE_PERCENT_DEFAULT

            return {
                'time': time_str,
                'day_offset': day_offset,
                'mode': 'passive',
                'target': min_temp,  # Display min as target
                'passive_max_temp': max_temp,
                'valve_percent': valve_percent
            }
        else:
            # Active mode
            return {
                'time': time_str,
                'day_offset': day_offset,
                'mode': 'active',
                'target': block['target'],
                'passive_max_temp': None,
                'valve_percent': None
            }

    def _build_schedule_info_dict_default(self, time_str: str, day_offset: int, schedule: Dict) -> Dict:
        """Build schedule info dict from default schedule settings.

        Args:
            time_str: HH:MM time string
            day_offset: Days from now (0=today, 1=tomorrow, etc)
            schedule: Full schedule dict

        Returns:
            Dict with schedule info
        """
        default_mode = schedule.get('default_mode', 'active')
        default_target = schedule.get('default_target', 16.0)

        if default_mode == 'passive':
            # Default passive mode
            max_temp = default_target
            # Use 'or' to handle None in config (explicit null values)
            min_temp = schedule.get('default_min_temp') or C.FROST_PROTECTION_TEMP_C_DEFAULT
            valve_percent = schedule.get('default_valve_percent') or C.PASSIVE_VALVE_PERCENT_DEFAULT

            return {
                'time': time_str,
                'day_offset': day_offset,
                'mode': 'passive',
                'target': min_temp,
                'passive_max_temp': max_temp,
                'valve_percent': valve_percent
            }
        else:
            # Default active mode
            return {
                'time': time_str,
                'day_offset': day_offset,
                'mode': 'active',
                'target': default_target,
                'passive_max_temp': None,
                'valve_percent': None
            }
    
    def _format_next_schedule_text(self, room_id: str, data: Dict, now: datetime) -> str:
        """Format next schedule information for display.

        NEW SIMPLIFIED LOGIC:
        - Off: "Heating Off"
        - Manual: "Manual"
        - Passive: "Passive"
        - Auto with override: Keep current override display (with countdown)
        - Auto without override: Show next schedule change details

        Args:
            room_id: Room identifier
            data: Room state dictionary
            now: Current datetime

        Returns:
            Formatted next schedule string
        """
        mode = data.get('mode', 'off')

        # Simple modes - no schedule info needed
        if mode == 'off':
            return 'Heating Off'

        if mode == 'manual':
            return 'Manual'

        if mode == 'passive':
            return 'Passive'

        # Auto mode - check for override first
        if mode == 'auto':
            # Check override mode via override manager
            override_mode = self.overrides.get_override_mode(room_id) if self.overrides else C.OVERRIDE_MODE_NONE

            if override_mode == C.OVERRIDE_MODE_ACTIVE:
                # Active override
                target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                override_target = None
                if self.ad.entity_exists(target_entity):
                    try:
                        override_target = float(self.ad.get_state(target_entity))
                    except (ValueError, TypeError):
                        pass

                # Get end time from timer
                timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
                finishes_at = self.ad.get_state(timer_entity, attribute="finishes_at")
                end_time_str = ""
                if finishes_at:
                    try:
                        end_dt = datetime.fromisoformat(finishes_at.replace('Z', '+00:00'))
                        end_time_str = f" until {end_dt.strftime('%H:%M')}"
                    except Exception as e:
                        self.ad.log(f"Error formatting override end time for {room_id}: {e}", level="WARNING")
                        end_time_str = " until ??:??"

                if override_target is not None:
                    return f"Override: {override_target:.1f}°{end_time_str}"
                else:
                    return f"Override{end_time_str}"

            elif override_mode == C.OVERRIDE_MODE_PASSIVE:
                # Passive override
                params = self.overrides.get_passive_override_params(room_id)
                timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)

                if params and self.ad.entity_exists(timer_entity):
                    finishes_at = self.ad.get_state(timer_entity, attribute="finishes_at")
                    end_time_str = ""
                    if finishes_at:
                        try:
                            end_dt = datetime.fromisoformat(finishes_at.replace('Z', '+00:00'))
                            end_time_str = f" until {end_dt.strftime('%H:%M')}"
                        except Exception as e:
                            self.ad.log(f"Error formatting passive override end time for {room_id}: {e}", level="WARNING")
                            end_time_str = " until ??:??"

                    return f"Override (Passive): {params['min_temp']:.0f}-{params['max_temp']:.0f}° ({params['valve_percent']:.0f}%){end_time_str}"

            # Auto mode without override - show next schedule
            # Get holiday mode
            holiday_mode = False
            if self.ad.entity_exists(C.HELPER_HOLIDAY_MODE):
                holiday_mode = self.ad.get_state(C.HELPER_HOLIDAY_MODE) == "on"

            # Get next schedule info
            next_info = self._get_next_schedule_info(room_id, now, holiday_mode)

            if next_info is None:
                # Forever (no schedule changes)
                return "Forever"

            # Build time string with day suffix
            time_str = next_info['time']
            day_offset = next_info['day_offset']

            if day_offset == 0:
                # Today - no suffix
                day_str = ""
            elif day_offset == 1:
                # Tomorrow
                day_str = " tomorrow"
            else:
                # Future day - use day name
                day_names_display = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                future_day_idx = (now.weekday() + day_offset) % 7
                day_name = day_names_display[future_day_idx]
                day_str = f" on {day_name}"

            # Build status based on next mode
            if next_info['mode'] == 'passive':
                # Next is passive: "At HH:MM [D]: [V%] L-U° (passive)"
                valve_pct = next_info['valve_percent']
                min_temp = next_info['target']
                max_temp = next_info['passive_max_temp']
                return f"At {time_str}{day_str}: {min_temp:.0f}-{max_temp:.0f}° ({valve_pct}%)"
            else:
                # Next is active: "At HH:MM [D]: S°"
                target = next_info['target']
                return f"At {time_str}{day_str}: {target:.1f}°"

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
        # Calculate passive rooms (receiving heat when boiler is on)
        passive_rooms = []
        if boiler_state == C.STATE_ON:
            for room_id, data in room_data.items():
                # Passive room: operating_mode='passive' AND valve open AND not naturally calling
                if (data.get('operating_mode') == 'passive' and
                    data.get('valve_percent', 0) > 0 and
                    room_id not in active_rooms):
                    passive_rooms.append(room_id)

        # Calculate load-sharing rooms by tier
        load_sharing_schedule_rooms = []
        load_sharing_fallback_rooms = []

        if hasattr(self.ad, 'load_sharing') and self.ad.load_sharing:
            ls_status = self.ad.load_sharing.get_status()
            ls_active_rooms = ls_status.get('active_rooms', [])

            for room in ls_active_rooms:
                if room['tier'] == 1:  # TIER_SCHEDULE
                    load_sharing_schedule_rooms.append(room['room_id'])
                elif room['tier'] == 2:  # TIER_FALLBACK
                    load_sharing_fallback_rooms.append(room['room_id'])

        # Calculate totals
        calling_count = len(active_rooms)
        passive_count = len(passive_rooms)
        schedule_count = len(load_sharing_schedule_rooms)
        fallback_count = len(load_sharing_fallback_rooms)
        total_heating = calling_count + passive_count + schedule_count + fallback_count

        # Build state string based on boiler state machine (like monolithic version)
        if boiler_state == C.STATE_ON:
            # Build progressive status text based on what's active
            parts = []

            # Always show calling count (even if 0 for edge cases)
            if calling_count == 1:
                parts.append("1 active")
            else:
                parts.append(f"{calling_count} active")

            # Add passive rooms if any
            if passive_count > 0:
                if passive_count == 1:
                    parts.append("1 passive")
                else:
                    parts.append(f"{passive_count} passive")

            # Add load-sharing schedule tier if any
            if schedule_count > 0:
                if schedule_count == 1:
                    parts.append("+1 pre-warming")
                else:
                    parts.append(f"+{schedule_count} pre-warming")

            # Add load-sharing fallback tier if any
            if fallback_count > 0:
                if fallback_count == 1:
                    parts.append("+1 fallback")
                else:
                    parts.append(f"+{fallback_count} fallback")

            state = f"heating ({', '.join(parts)})"
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
            'room_calling_count': len(active_rooms),  # Keep for backward compatibility
            'total_rooms': len(self.config.rooms),
            'rooms': {},
            'boiler_state': boiler_state,
            'boiler_reason': boiler_reason,
            'total_valve_percent': 0,
            'last_recompute': now.isoformat(),
            # Room heating counts
            'calling_count': calling_count,
            'passive_count': passive_count,
            'load_sharing_schedule_count': schedule_count,
            'load_sharing_fallback_count': fallback_count,
            'total_heating_count': total_heating,
            'passive_rooms': passive_rooms,
            'load_sharing_schedule_rooms': load_sharing_schedule_rooms,
            'load_sharing_fallback_rooms': load_sharing_fallback_rooms,
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
        
        # Add setpoint ramp state if available
        if hasattr(self.ad, 'setpoint_ramp'):
            ramp_state_dict = self.ad.setpoint_ramp.get_state_dict()
            attrs['setpoint_ramp'] = {
                'enabled': ramp_state_dict.get('enabled', False),
                'state': ramp_state_dict.get('state', 'INACTIVE'),
                'baseline_setpoint': ramp_state_dict.get('baseline_setpoint'),
                'current_ramped_setpoint': ramp_state_dict.get('current_ramped_setpoint'),
                'ramp_steps_applied': ramp_state_dict.get('ramp_steps_applied', 0),
                'max_setpoint': ramp_state_dict.get('max_setpoint')
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
                room_attrs['passive_min_temp'] = data.get('target')  # FIXED: target is now min_temp (comfort floor)
                room_attrs['passive_max_temp'] = data.get('passive_max_temp')  # Upper limit for valve control
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

    def publish_boiler_state(self, boiler_state: str) -> None:
        """Publish dedicated boiler state entity for reliable graph shading history.
        
        This entity provides reliable state transitions for determining when
        heat is available (for passive shading). Unlike the system-wide
        sensor.pyheat_status, this entity updates ONLY when boiler state changes,
        ensuring clean history entries for frontend graph shading.
        
        Args:
            boiler_state: Current boiler state (on, off, pending_on, pending_off, pump_overrun, interlock)
        """
        self.ad.set_state(
            C.BOILER_STATE_ENTITY,
            state=boiler_state,
            attributes={
                'friendly_name': 'PyHeat Boiler State',
                'icon': 'mdi:fire' if boiler_state == 'on' else 'mdi:fire-off'
            },
            replace=True
        )

    def _build_room_state_string(self, room_id: str, data: Dict,
                                  load_sharing_info: Dict = None) -> str:
        """Build structured room state string for reliable history entries.
        
        Format: $mode, $load_sharing, $calling, $valve
        
        Examples:
            "auto (active), LS off, not calling, 0%"
            "auto (passive), LS off, not calling, 65%"
            "auto (active), LS T1, not calling, 30%"
            "auto (active), LS off, calling, 100%"
            "manual, LS off, not calling, 80%"
            "off, LS off, not calling, 0%"
        
        This format ensures every flag change creates a new history entry,
        fixing sparse/patchy graph shading issues.
        
        Args:
            room_id: Room identifier
            data: Room state dictionary
            load_sharing_info: Load sharing state dict with 'active', 'tier' keys
            
        Returns:
            Formatted state string
        """
        mode = data.get('mode', 'off')
        operating_mode = data.get('operating_mode', 'off')
        
        # $mode component
        if mode == 'off':
            mode_str = 'off'
        elif mode == 'manual':
            mode_str = 'manual'
        elif mode == 'passive':
            mode_str = 'passive'
        elif mode == 'auto':
            if data.get('override_active', False):
                mode_str = 'auto (override)'
            elif operating_mode == 'passive':
                mode_str = 'auto (passive)'
            else:
                mode_str = 'auto (active)'
        else:
            mode_str = mode
        
        # $load_sharing component
        if load_sharing_info and load_sharing_info.get('active'):
            tier = load_sharing_info.get('tier', 1)
            ls_str = f"LS T{tier}"
        else:
            ls_str = "LS off"
        
        # $calling component
        calling_str = "calling" if data.get('calling', False) else "not calling"
        
        # $valve component
        valve_str = f"{data.get('valve_percent', 0)}%"
        
        return f"{mode_str}, {ls_str}, {calling_str}, {valve_str}"

    def publish_room_entities(self, room_id: str, data: Dict, now: datetime,
                              load_sharing_info: Dict = None) -> None:
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
        
        # Target sensor (in passive mode, this is min_temp - the heating target)
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

        # Passive max temp sensor (upper limit for passive mode valve control)
        passive_max_entity = f"sensor.pyheat_{room_id}_passive_max_temp"
        if data.get('operating_mode') == 'passive' and data.get('passive_max_temp') is not None:
            self.ad.set_state(passive_max_entity,
                         state=round(data['passive_max_temp'], precision),
                         attributes={
                             'unit_of_measurement': '°C',
                             'device_class': 'temperature',
                             'state_class': 'measurement',
                             'friendly_name': f'{room_name} Passive Max Temperature'
                         })
        else:
            self.ad.set_state(passive_max_entity, state="unavailable")

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
        
        # Add override_active flag to data for state string building
        data_with_override = dict(data)
        data_with_override['override_active'] = override_active
        
        # Build structured state string for reliable history entries
        # Format: "$mode, $load_sharing, $calling, $valve"
        state_str = self._build_room_state_string(room_id, data_with_override, load_sharing_info)

        # Generate next schedule information for display
        formatted_next_schedule = self._format_next_schedule_text(room_id, data, now)

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
            'formatted_next_schedule': formatted_next_schedule,  # Next schedule information for display
            'scheduled_temp': round(scheduled_temp, precision) if scheduled_temp is not None else None,
            'override_mode': self.overrides.get_override_mode(room_id) if self.overrides else C.OVERRIDE_MODE_NONE,  # NEW: "active", "passive", or "none"
            # Additional convenience attributes
            'load_sharing': f"T{load_sharing_info.get('tier', 1)}" if (load_sharing_info and load_sharing_info.get('active')) else "off",
            'valve': data.get('valve_percent', 0),
            'passive_low': data.get('passive_min_temp') if data.get('operating_mode') == 'passive' else None,
            'passive_high': round(data.get('passive_max_temp'), precision) if (data.get('operating_mode') == 'passive' and data.get('passive_max_temp') is not None) else None,
            'passive_valve': data.get('passive_valve_percent') if data.get('operating_mode') == 'passive' else None,  # Scheduled valve percent
            'calling': data.get('calling', False),
        }

        # Add passive override details if active
        override_mode = self.overrides.get_override_mode(room_id) if self.overrides else C.OVERRIDE_MODE_NONE
        if override_mode == C.OVERRIDE_MODE_PASSIVE:
            params = self.overrides.get_passive_override_params(room_id)
            if params:
                attributes['override_passive_min_temp'] = params['min_temp']
                attributes['override_passive_max_temp'] = params['max_temp']
                attributes['override_passive_valve_percent'] = params['valve_percent']
        
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
