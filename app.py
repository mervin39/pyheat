"""
Pyheat - Home Heating Controller for AppDaemon

A comprehensive heating control system that manages:
- Per-room temperature control with smart TRV management
- Schedule-based and manual temperature setpoints
- Boiler control with safety interlocks and anti-cycling
- Temperature sensor fusion and staleness detection
- Override and boost functionality

Architecture:
- Single AppDaemon app (this file) that coordinates all functionality
- Helper modules for specific concerns (config, sensors, scheduling, etc.)
- Event-driven with 1-minute periodic recompute
- Stateful with persistence across restarts

For full documentation, see docs/pyheat-spec.md
"""

import appdaemon.plugins.hass.hassapi as hass
import os
import yaml
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import json

# Import constants module from same package
import pyheat.constants as C

class PyHeat(hass.Hass):
    """Main PyHeat heating controller app for AppDaemon."""

    def initialize(self):
        """Initialize the PyHeat app.
        
        Called by AppDaemon when the app is loaded or reloaded.
        Sets up state, loads configuration, registers callbacks, and starts the control loop.
        """
        self.log("=" * 60)
        self.log("PyHeat initializing...")
        self.log("=" * 60)
        
        # Application state
        self.rooms = {}  # Room registry: {room_id: room_data}
        self.schedules = {}  # Schedules: {room_id: schedule_data}
        self.boiler_state = C.STATE_OFF
        self.boiler_last_on = None
        self.boiler_last_off = None
        self.sensor_last_values = {}  # {entity_id: (value, timestamp)}
        self.trv_last_commanded = {}  # {room_id: percent}
        self.trv_last_update = {}  # {room_id: timestamp}
        self.room_call_for_heat = {}  # {room_id: bool}
        self.room_last_valve = {}  # {room_id: percent}
        self.room_current_band = {}  # {room_id: band_index}
        self.first_boot = True  # Flag for startup behavior
        
        # Timing
        self.last_recompute = None
        self.recompute_count = 0
        
        # Load configuration
        try:
            self.load_configuration()
        except Exception as e:
            self.error(f"Failed to load configuration: {e}")
            self.log("PyHeat initialization failed - configuration error")
            return
        
        # Setup callbacks for helper entities
        self.setup_callbacks()
        
        # Schedule periodic recompute
        self.run_every(self.periodic_recompute, "now+5", C.RECOMPUTE_INTERVAL_S)
        
        # Perform initial recomputes (with delays for sensor restoration)
        self.run_in(self.initial_recompute, C.STARTUP_INITIAL_DELAY_S)
        self.run_in(self.second_recompute, C.STARTUP_SECOND_DELAY_S)
        
        # Log startup summary
        self.log(f"PyHeat initialized successfully")
        self.log(f"  Rooms: {len(self.rooms)}")
        self.log(f"  Schedules: {len(self.schedules)}")
        master_enable = self.get_state(C.HELPER_MASTER_ENABLE)
        holiday_mode = self.get_state(C.HELPER_HOLIDAY_MODE)
        self.log(f"  Master enable: {master_enable}")
        self.log(f"  Holiday mode: {holiday_mode}")
        self.log("=" * 60)

    def load_configuration(self):
        """Load rooms.yaml and schedules.yaml from the config directory."""
        # Get the path to the config directory (same directory as this file)
        app_dir = os.path.dirname(os.path.abspath(__file__))
        config_dir = os.path.join(app_dir, "config")
        
        rooms_file = os.path.join(config_dir, "rooms.yaml")
        schedules_file = os.path.join(config_dir, "schedules.yaml")
        
        # Load rooms
        with open(rooms_file, 'r') as f:
            rooms_data = yaml.safe_load(f) or {}
        
        # Load schedules  
        with open(schedules_file, 'r') as f:
            schedules_data = yaml.safe_load(f) or {}
        
        # Process rooms
        for room in rooms_data.get('rooms', []):
            room_id = room['id']
            
            # Derive TRV entity IDs from climate entity
            trv_base = room['trv']['entity_id'].replace('climate.', '')
            trv_entities = {
                'cmd_open': C.TRV_ENTITY_PATTERNS['cmd_open'].format(trv_base=trv_base),
                'cmd_close': C.TRV_ENTITY_PATTERNS['cmd_close'].format(trv_base=trv_base),
                'fb_open': C.TRV_ENTITY_PATTERNS['fb_open'].format(trv_base=trv_base),
                'fb_close': C.TRV_ENTITY_PATTERNS['fb_close'].format(trv_base=trv_base),
            }
            
            # Merge room config with defaults
            room_config = {
                'id': room_id,
                'name': room.get('name', room_id),
                'precision': room.get('precision', 1),
                'sensors': room['sensors'],
                'trv': {
                    'climate_entity': room['trv']['entity_id'],
                    **trv_entities
                },
                'hysteresis': {**C.HYSTERESIS_DEFAULT, **room.get('hysteresis', {})},
                'valve_bands': {**C.VALVE_BANDS_DEFAULT, **room.get('valve_bands', {})},
                'valve_update': {**C.VALVE_UPDATE_DEFAULT, **room.get('valve_update', {})},
            }
            
            # Verify TRV entities exist
            missing_entities = []
            for key, entity_id in trv_entities.items():
                if not self.entity_exists(entity_id):
                    missing_entities.append(f"{key}={entity_id}")
            
            if missing_entities:
                self.log(f"Warning: Room '{room_id}' has missing TRV entities: {', '.join(missing_entities)}", level="WARNING")
                self.log(f"  Room will be disabled until entities are available", level="WARNING")
                room_config['disabled'] = True
            else:
                room_config['disabled'] = False
            
            self.rooms[room_id] = room_config
        
        # Process schedules
        for sched in schedules_data.get('rooms', []):
            room_id = sched['id']
            self.schedules[room_id] = sched
            
            # Warn if room doesn't exist
            if room_id not in self.rooms:
                self.log(f"Warning: Schedule for unknown room '{room_id}'", level="WARNING")
        
        # Warn if room has no schedule
        for room_id in self.rooms:
            if room_id not in self.schedules:
                self.log(f"Warning: Room '{room_id}' has no schedule", level="WARNING")
        
        self.log(f"Configuration loaded: {len(self.rooms)} rooms, {len(self.schedules)} schedules")

    def setup_callbacks(self):
        """Register state change callbacks for all relevant entities."""
        # Master enable
        self.listen_state(self.master_enable_changed, C.HELPER_MASTER_ENABLE)
        
        # Holiday mode
        self.listen_state(self.holiday_mode_changed, C.HELPER_HOLIDAY_MODE)
        
        # Per-room callbacks
        for room_id in self.rooms:
            # Mode selector
            mode_entity = C.HELPER_ROOM_MODE.format(room=room_id)
            if self.entity_exists(mode_entity):
                self.listen_state(self.room_mode_changed, mode_entity, room_id=room_id)
            
            # Manual setpoint
            setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.entity_exists(setpoint_entity):
                self.listen_state(self.room_setpoint_changed, setpoint_entity, room_id=room_id)
            
            # Override timer
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
            if self.entity_exists(timer_entity):
                self.listen_state(self.room_timer_changed, timer_entity, room_id=room_id)
            
            # Temperature sensors
            for sensor_cfg in self.rooms[room_id]['sensors']:
                entity_id = sensor_cfg['entity_id']
                self.listen_state(self.sensor_changed, entity_id, room_id=room_id)
            
            # TRV feedback sensors
            if not self.rooms[room_id].get('disabled'):
                self.listen_state(self.trv_feedback_changed, 
                                self.rooms[room_id]['trv']['fb_open'], room_id=room_id)
                self.listen_state(self.trv_feedback_changed,
                                self.rooms[room_id]['trv']['fb_close'], room_id=room_id)

    # ========================================================================
    # Callback Handlers
    # ========================================================================

    def master_enable_changed(self, entity, attribute, old, new, kwargs):
        """Handle master enable toggle."""
        self.log(f"Master enable changed: {old} -> {new}")
        self.trigger_recompute("master_enable_changed")

    def holiday_mode_changed(self, entity, attribute, old, new, kwargs):
        """Handle holiday mode toggle."""
        self.log(f"Holiday mode changed: {old} -> {new}")
        self.trigger_recompute("holiday_mode_changed")

    def room_mode_changed(self, entity, attribute, old, new, kwargs):
        """Handle room mode selector change."""
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' mode changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_mode_changed")

    def room_setpoint_changed(self, entity, attribute, old, new, kwargs):
        """Handle manual setpoint change."""
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' manual setpoint changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_setpoint_changed")

    def room_timer_changed(self, entity, attribute, old, new, kwargs):
        """Handle override/boost timer state change."""
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' override timer changed: {old} -> {new}")
        # Timer events: started, paused, cancelled, finished (idle)
        if new == "idle" and old in ["active", "paused"]:
            self.log(f"Override/boost for room '{room_id}' expired")
            # Clear the override target
            target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
            if self.entity_exists(target_entity):
                # Set to a sentinel value (entity min is 5, so 0 indicates cleared)
                self.call_service("input_number/set_value", 
                                entity_id=target_entity, value=0)
        self.trigger_recompute(f"room_{room_id}_timer_changed")

    def sensor_changed(self, entity, attribute, old, new, kwargs):
        """Handle temperature sensor update."""
        room_id = kwargs.get('room_id')
        try:
            value = float(new)
            now = datetime.now()
            self.sensor_last_values[entity] = (value, now)
            self.log(f"Sensor '{entity}' for room '{room_id}' updated: {value}°C", level="DEBUG")
            self.trigger_recompute(f"sensor_{entity}_changed")
        except (ValueError, TypeError):
            self.log(f"Invalid sensor value for '{entity}': {new}", level="WARNING")

    def trv_feedback_changed(self, entity, attribute, old, new, kwargs):
        """Handle TRV feedback sensor update."""
        room_id = kwargs.get('room_id')
        self.log(f"TRV feedback for room '{room_id}' updated: {entity} = {new}", level="DEBUG")
        # Trigger recompute to check interlock status
        self.trigger_recompute(f"trv_feedback_{room_id}_changed")

    # ========================================================================
    # Periodic Recompute
    # ========================================================================

    def periodic_recompute(self, kwargs):
        """Periodic recompute callback (called every minute)."""
        self.trigger_recompute("periodic")

    def initial_recompute(self, kwargs):
        """Initial recompute on startup."""
        self.log("Performing initial recompute...")
        self.trigger_recompute("startup_initial")

    def second_recompute(self, kwargs):
        """Second recompute after startup delay."""
        self.log("Performing second recompute (late sensor restoration)...")
        self.first_boot = False
        self.trigger_recompute("startup_second")

    def trigger_recompute(self, reason: str):
        """Trigger a full system recompute.
        
        Args:
            reason: Description of why recompute was triggered (for logging)
        """
        self.recompute_count += 1
        now = datetime.now()
        self.last_recompute = now
        
        self.log(f"Recompute #{self.recompute_count} triggered: {reason}", level="DEBUG")
        
        # Perform the recompute
        self.recompute_all(now)

    def recompute_all(self, now: datetime):
        """Main orchestration function - recompute all room states and boiler demand.
        
        This is where the core heating logic runs:
        1. Get current sensor values and check staleness
        2. Resolve target temperature for each room
        3. Apply hysteresis to determine call-for-heat
        4. Compute valve percentages
        5. Send valve commands if needed
        6. Aggregate demands and control boiler
        7. Publish status entities
        """
        # Check master enable
        master_enable = self.get_state(C.HELPER_MASTER_ENABLE) == "on"
        if not master_enable:
            self.log("Master enable is OFF - system disabled")
            # Turn off boiler if running
            if self.get_state(C.HELPER_BOILER_ACTOR) == "on":
                self.call_service("input_boolean/turn_off", entity_id=C.HELPER_BOILER_ACTOR)
                self.log("Boiler turned OFF (master disabled)")
            return
        
        # Check holiday mode
        holiday_mode = self.get_state(C.HELPER_HOLIDAY_MODE) == "on"
        
        # Process each room
        any_calling = False
        active_rooms = []
        
        for room_id, room_config in self.rooms.items():
            if room_config.get('disabled'):
                continue
            
            # 1. Get current temperature
            temp, is_stale = self.get_room_temperature(room_id, now)
            
            # 2. Get room mode
            mode_entity = C.HELPER_ROOM_MODE.format(room=room_id)
            room_mode = self.get_state(mode_entity) if self.entity_exists(mode_entity) else "auto"
            # Normalize to lowercase for comparison
            room_mode = room_mode.lower() if room_mode else "auto"
            
            # 3. Resolve target temperature
            target = self.resolve_room_target(room_id, now, room_mode, holiday_mode, is_stale)
            
            # 4. Determine call-for-heat
            if target is not None and temp is not None and not is_stale and room_mode != "off":
                calling = self.compute_call_for_heat(room_id, target, temp)
                self.room_call_for_heat[room_id] = calling
                
                if calling:
                    any_calling = True
                    active_rooms.append(room_id)
                
                # 5. Compute valve percentage
                valve_percent = self.compute_valve_percent(room_id, target, temp, calling)
                
                # 6. Send valve command if needed
                self.set_trv_valve(room_id, valve_percent, now)
                
                self.log(f"Room '{room_id}': temp={temp:.1f}°C, target={target:.1f}°C, "
                        f"calling={calling}, valve={valve_percent}%", level="DEBUG")
            else:
                # Room is off, stale, or has no target
                self.room_call_for_heat[room_id] = False
                if room_mode == "off" or is_stale:
                    self.set_trv_valve(room_id, 0, now)
                self.log(f"Room '{room_id}': mode={room_mode}, stale={is_stale}, "
                        f"target={target}, no heating", level="DEBUG")
        
        # 7. Control boiler based on aggregated demand
        self.control_boiler(any_calling, active_rooms, now)
        
        # 8. Publish status (simplified for now)
        self.publish_status(any_calling, active_rooms)

    # ========================================================================
    # Sensor Fusion & Staleness Detection
    # ========================================================================

    def get_room_temperature(self, room_id: str, now: datetime) -> Tuple[Optional[float], bool]:
        """Get fused temperature for a room.
        
        Returns:
            Tuple of (temperature, is_stale)
            - temperature: Average of available sensors, or None if all stale/unavailable
            - is_stale: True if all sensors are stale or unavailable
        """
        room_config = self.rooms.get(room_id)
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

    # ========================================================================
    # Target Resolution
    # ========================================================================

    def resolve_room_target(self, room_id: str, now: datetime, room_mode: str, 
                           holiday_mode: bool, is_stale: bool) -> Optional[float]:
        """Resolve the target temperature for a room.
        
        Precedence (highest wins): off → manual → override → schedule/default
        
        Returns:
            Target temperature in °C, or None if room is off or stale
        """
        # Room off or stale → no target
        if room_mode == "off" or is_stale:
            return None
        
        # Manual mode → use manual setpoint
        if room_mode == "manual":
            setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.entity_exists(setpoint_entity):
                try:
                    setpoint = float(self.get_state(setpoint_entity))
                    # Round to room precision
                    precision = self.rooms[room_id].get('precision', 1)
                    return round(setpoint, precision)
                except (ValueError, TypeError):
                    self.log(f"Invalid manual setpoint for room '{room_id}'", level="WARNING")
                    return None
            return None
        
        # Auto mode → check for override, then schedule
        
        # Check for active override/boost
        timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
        if self.entity_exists(timer_entity):
            timer_state = self.get_state(timer_entity)
            if timer_state in ["active", "paused"]:
                # Override is active, get the target
                target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                if self.entity_exists(target_entity):
                    try:
                        override_target = float(self.get_state(target_entity))
                        # Sentinel value 0 means cleared (entity min is 5)
                        if override_target >= C.TARGET_MIN_C:
                            return override_target
                    except (ValueError, TypeError):
                        pass
        
        # No override → use scheduled target
        return self.get_scheduled_target(room_id, now, holiday_mode)

    def get_scheduled_target(self, room_id: str, now: datetime, holiday_mode: bool) -> Optional[float]:
        """Get the scheduled target temperature for a room.
        
        Args:
            room_id: Room identifier
            now: Current datetime
            holiday_mode: Whether holiday mode is active
            
        Returns:
            Target temperature from schedule or default, or None if no schedule
        """
        schedule = self.schedules.get(room_id)
        if not schedule:
            return None
        
        # If holiday mode, return holiday target
        if holiday_mode:
            return C.HOLIDAY_TARGET_C
        
        # Get day of week (0=Monday, 6=Sunday)
        day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        day_name = day_names[now.weekday()]
        
        # Get blocks for today
        week_schedule = schedule.get('week', {})
        blocks = week_schedule.get(day_name, [])
        
        # Find active block
        current_time = now.strftime("%H:%M")
        
        for block in blocks:
            start_time = block['start']
            end_time = block.get('end', '23:59')
            
            # Convert end='23:59' to '24:00' for comparison
            if end_time == '23:59':
                end_time = '24:00'
            
            # Check if current time is within block (start inclusive, end exclusive)
            if start_time <= current_time < end_time:
                return block['target']
        
        # No active block → return default
        return schedule.get('default_target')

    # ========================================================================
    # Call-for-Heat Logic with Hysteresis
    # ========================================================================

    def compute_call_for_heat(self, room_id: str, target: float, temp: float) -> bool:
        """Determine if a room should call for heat using asymmetric hysteresis.
        
        Args:
            room_id: Room identifier
            target: Target temperature (°C)
            temp: Current temperature (°C)
            
        Returns:
            True if room should call for heat, False otherwise
        """
        # Get hysteresis config
        hysteresis = self.rooms[room_id]['hysteresis']
        on_delta = hysteresis['on_delta_c']
        off_delta = hysteresis['off_delta_c']
        
        # Calculate error (positive = below target)
        error = target - temp
        
        # Get previous state
        prev_calling = self.room_call_for_heat.get(room_id, False)
        
        # Apply asymmetric hysteresis
        if error >= on_delta:
            # Clearly below target → call for heat
            return True
        elif error <= off_delta:
            # At or above target → stop calling
            return False
        else:
            # In deadband → maintain previous state
            return prev_calling

    # ========================================================================
    # Valve Control with Stepped Bands
    # ========================================================================

    def compute_valve_percent(self, room_id: str, target: float, temp: float, 
                             calling: bool) -> int:
        """Compute valve percentage using stepped bands.
        
        Args:
            room_id: Room identifier
            target: Target temperature (°C)
            temp: Current temperature (°C)
            calling: Whether room is calling for heat
            
        Returns:
            Valve percentage (0-100)
        """
        if not calling:
            return 0
        
        # Get valve band config
        bands = self.rooms[room_id]['valve_bands']
        t_low = bands['t_low']
        t_mid = bands['t_mid']
        t_max = bands['t_max']
        low_pct = int(bands['low_percent'])
        mid_pct = int(bands['mid_percent'])
        max_pct = int(bands['max_percent'])
        
        # Calculate error
        error = target - temp
        
        # Determine band (with simple logic for now, step hysteresis later)
        if error < t_low:
            return 0
        elif error < t_mid:
            return low_pct
        elif error < t_max:
            return mid_pct
        else:
            return max_pct

    # ========================================================================
    # TRV Control (Simplified)
    # ========================================================================

    def set_trv_valve(self, room_id: str, percent: int, now: datetime):
        """Set TRV valve position (simplified version without full feedback).
        
        Args:
            room_id: Room identifier
            percent: Desired valve percentage (0-100)
            now: Current datetime
        """
        room_config = self.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return
        
        # Check rate limiting
        min_interval = room_config['valve_update']['min_interval_s']
        last_update = self.trv_last_update.get(room_id)
        
        if last_update:
            elapsed = (now - last_update).total_seconds()
            if elapsed < min_interval:
                # Too soon since last update
                return
        
        # Check if value actually changed
        last_commanded = self.trv_last_commanded.get(room_id)
        if last_commanded == percent:
            # No change needed
            return
        
        # Send commands
        trv = room_config['trv']
        opening = percent
        closing = 100 - percent
        
        self.log(f"Setting TRV for room '{room_id}': {percent}% open")
        
        # Send opening degree command
        self.call_service("number/set_value",
                         entity_id=trv['cmd_open'],
                         value=opening)
        
        # Send closing degree command
        self.call_service("number/set_value",
                         entity_id=trv['cmd_close'],
                         value=closing)
        
        # Update tracking
        self.trv_last_commanded[room_id] = percent
        self.trv_last_update[room_id] = now

    # ========================================================================
    # Boiler Control (Simplified)
    # ========================================================================

    def control_boiler(self, any_calling: bool, active_rooms: List[str], now: datetime):
        """Control boiler based on room demand (simplified version).
        
        Args:
            any_calling: True if any room is calling for heat
            active_rooms: List of room IDs calling for heat
            now: Current datetime
        """
        current_state = self.get_state(C.HELPER_BOILER_ACTOR)
        is_on = current_state == "on"
        
        # Simple logic: turn on if any room calling, off if none calling
        # (Full state machine with anti-cycling will be added later)
        
        if any_calling and not is_on:
            self.log(f"Boiler ON - {len(active_rooms)} room(s) calling for heat: {', '.join(active_rooms)}")
            self.call_service("input_boolean/turn_on", entity_id=C.HELPER_BOILER_ACTOR)
            self.boiler_last_on = now
            
        elif not any_calling and is_on:
            self.log(f"Boiler OFF - no rooms calling for heat")
            self.call_service("input_boolean/turn_off", entity_id=C.HELPER_BOILER_ACTOR)
            self.boiler_last_off = now

    # ========================================================================
    # Status Publishing (Simplified)
    # ========================================================================

    def publish_status(self, any_calling: bool, active_rooms: List[str]):
        """Publish status entity (simplified version).
        
        Args:
            any_calling: True if any room is calling for heat
            active_rooms: List of room IDs calling for heat
        """
        # Build status string
        if any_calling:
            status_str = f"heating ({len(active_rooms)} rooms)"
        else:
            status_str = "idle"
        
        # Build attributes
        attributes = {
            "any_call_for_heat": any_calling,
            "active_rooms": active_rooms,
            "room_calling_count": len(active_rooms),
            "total_rooms": len(self.rooms),
            "last_recompute": self.last_recompute.isoformat() if self.last_recompute else None,
            "recompute_count": self.recompute_count,
        }
        
        # Set state
        self.set_state(C.STATUS_ENTITY, state=status_str, attributes=attributes)
