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
        self.boiler_config = {}  # Boiler configuration
        self.boiler_state = C.STATE_OFF
        self.boiler_state_entry_time = None
        self.boiler_last_on = None
        self.boiler_last_off = None
        self.boiler_last_valve_positions = {}  # For pump overrun: {room_id: percent}
        self.sensor_last_values = {}  # {entity_id: (value, timestamp)}
        self.trv_last_commanded = {}  # {room_id: percent}
        self.trv_last_update = {}  # {room_id: timestamp}
        self.room_call_for_heat = {}  # {room_id: bool}
        self.room_last_valve = {}  # {room_id: percent}
        self.room_current_band = {}  # {room_id: band_index}
        self.first_boot = True  # Flag for startup behavior
        
        # Valve command state tracking (for non-blocking commands)
        self._valve_command_state: Dict[str, Dict] = {}
        
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
        
        # Initialize sensor values from current state
        self.initialize_sensor_values()
        
        # Schedule periodic recompute
        self.run_every(self.periodic_recompute, "now+5", C.RECOMPUTE_INTERVAL_S)
        
        # Schedule TRV setpoint monitoring (check every 5 minutes)
        self.run_every(self.check_trv_setpoints, "now+10", C.TRV_SETPOINT_CHECK_INTERVAL_S)
        
        # Lock all TRV setpoints immediately (before initial recompute)
        self.run_in(self.lock_all_trv_setpoints, 3)
        
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
        """Load rooms.yaml, schedules.yaml, and boiler.yaml from the config directory."""
        # Get the path to the config directory (same directory as this file)
        app_dir = os.path.dirname(os.path.abspath(__file__))
        config_dir = os.path.join(app_dir, "config")
        
        rooms_file = os.path.join(config_dir, "rooms.yaml")
        schedules_file = os.path.join(config_dir, "schedules.yaml")
        boiler_file = os.path.join(config_dir, "boiler.yaml")
        
        # Load rooms
        with open(rooms_file, 'r') as f:
            rooms_data = yaml.safe_load(f) or {}
        
        # Load schedules  
        with open(schedules_file, 'r') as f:
            schedules_data = yaml.safe_load(f) or {}
        
        # Load boiler
        with open(boiler_file, 'r') as f:
            self.boiler_config = yaml.safe_load(f) or {}
        
        # Process rooms
        for room in rooms_data.get('rooms', []):
            room_id = room['id']
            
            # Derive TRV entity IDs from climate entity
            trv_base = room['trv']['entity_id'].replace('climate.', '')
            trv_entities = {
                'cmd_valve': C.TRV_ENTITY_PATTERNS['cmd_valve'].format(trv_base=trv_base),
                'fb_valve': C.TRV_ENTITY_PATTERNS['fb_valve'].format(trv_base=trv_base),
                'climate': C.TRV_ENTITY_PATTERNS['climate'].format(trv_base=trv_base),
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
        
        # Extract boiler configuration
        boiler_cfg = self.boiler_config.get('boiler', {})
        self.boiler_entity = boiler_cfg.get('entity_id', 'climate.boiler')
        self.boiler_opentherm = boiler_cfg.get('opentherm', False)
        
        # Binary control settings
        binary_cfg = boiler_cfg.get('binary_control', {})
        self.boiler_on_setpoint = binary_cfg.get('on_setpoint_c', C.BOILER_BINARY_ON_SETPOINT_DEFAULT)
        self.boiler_off_setpoint = binary_cfg.get('off_setpoint_c', C.BOILER_BINARY_OFF_SETPOINT_DEFAULT)
        
        # Pump overrun
        self.boiler_pump_overrun_s = boiler_cfg.get('pump_overrun_s', C.BOILER_PUMP_OVERRUN_DEFAULT)
        
        # Anti-cycling settings
        anti_cycling = boiler_cfg.get('anti_cycling', {})
        self.boiler_min_on_time_s = anti_cycling.get('min_on_time_s', C.BOILER_MIN_ON_TIME_DEFAULT)
        self.boiler_min_off_time_s = anti_cycling.get('min_off_time_s', C.BOILER_MIN_OFF_TIME_DEFAULT)
        self.boiler_off_delay_s = anti_cycling.get('off_delay_s', C.BOILER_OFF_DELAY_DEFAULT)
        
        # Interlock configuration
        interlock_cfg = boiler_cfg.get('interlock', {})
        self.boiler_min_valve_open_percent = interlock_cfg.get(
            'min_valve_open_percent',
            C.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        # Safety room configuration (emergency hot water flow path)
        self.boiler_safety_room = boiler_cfg.get('safety_room')
        
        self.log(f"Configuration loaded: {len(self.rooms)} rooms, {len(self.schedules)} schedules")
        self.log(f"Boiler: {self.boiler_entity}, OpenTherm={self.boiler_opentherm}, min_valve={self.boiler_min_valve_open_percent}%")

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
                                self.rooms[room_id]['trv']['fb_valve'], room_id=room_id)

    def initialize_sensor_values(self):
        """Initialize sensor_last_values from current state on startup.
        
        This ensures we have sensor data immediately rather than waiting for first update.
        """
        self.log("Initializing sensor values from current state...")
        now = datetime.now()
        
        for room_id, room_config in self.rooms.items():
            for sensor_cfg in room_config['sensors']:
                entity_id = sensor_cfg['entity_id']
                
                try:
                    # Get current state
                    state = self.get_state(entity_id, attribute='all')
                    if state is None:
                        self.log(f"Sensor {entity_id} not found during initialization", level="WARNING")
                        continue
                    
                    # Get value
                    value = float(state['state'])
                    
                    # Get last_updated timestamp (comes as string like '2025-11-04T19:07:28.811317+00:00')
                    last_updated_str = state['last_updated']
                    last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                    
                    # Convert to naive datetime (remove timezone info) to match datetime.now()
                    last_updated_naive = last_updated.replace(tzinfo=None)
                    
                    # Store in sensor_last_values
                    self.sensor_last_values[entity_id] = (value, last_updated_naive)
                    
                    age_minutes = (now - last_updated_naive).total_seconds() / 60
                    self.log(f"Initialized {entity_id} for room '{room_id}': {value}°C (age: {age_minutes:.1f}min)", 
                            level="DEBUG")
                    
                except (ValueError, TypeError, KeyError) as e:
                    self.log(f"Failed to initialize sensor {entity_id}: {e}", level="WARNING")
        
        self.log(f"Initialized {len(self.sensor_last_values)} sensors")

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
    # TRV Setpoint Locking
    # ========================================================================

    def lock_all_trv_setpoints(self, kwargs=None):
        """Lock all TRV setpoints to 5°C to force them into 'open' mode.
        
        This allows us to control valve position directly via opening_degree only.
        Called on startup and periodically to handle accidental user changes.
        """
        self.log("Locking all TRV setpoints to 5°C...")
        for room_id, room in self.rooms.items():
            if room.get('disabled'):
                continue
            self.lock_trv_setpoint(room_id)

    def lock_trv_setpoint(self, room_id: str):
        """Lock a single TRV setpoint to 5°C.
        
        Args:
            room_id: Room identifier
        """
        room = self.rooms.get(room_id)
        if not room or room.get('disabled'):
            return
        
        climate_entity = room['trv']['climate']
        
        # Get current setpoint
        try:
            current_temp = self.get_state(climate_entity, attribute='temperature')
            if current_temp is not None:
                current_temp = float(current_temp)
            else:
                current_temp = None
        except (ValueError, TypeError):
            current_temp = None
        
        # Set to locked value if different
        if current_temp is None or abs(current_temp - C.TRV_LOCKED_SETPOINT_C) > 0.1:
            self.log(f"Locking TRV setpoint for room '{room_id}': {current_temp}°C -> {C.TRV_LOCKED_SETPOINT_C}°C")
            self.call_service("climate/set_temperature",
                            entity_id=climate_entity,
                            temperature=C.TRV_LOCKED_SETPOINT_C)
        else:
            self.log(f"TRV setpoint for room '{room_id}' already locked at {C.TRV_LOCKED_SETPOINT_C}°C", level="DEBUG")

    def check_trv_setpoints(self, kwargs):
        """Periodic callback to check and correct TRV setpoints.
        
        Handles cases where users accidentally change TRV setpoints via the UI.
        """
        self.log("Checking TRV setpoints...", level="DEBUG")
        for room_id, room in self.rooms.items():
            if room.get('disabled'):
                continue
            
            climate_entity = room['trv']['climate']
            try:
                current_temp = self.get_state(climate_entity, attribute='temperature')
                if current_temp is not None:
                    current_temp = float(current_temp)
                    
                    # Check if setpoint has drifted
                    if abs(current_temp - C.TRV_LOCKED_SETPOINT_C) > 0.1:
                        self.log(f"TRV setpoint drift detected for room '{room_id}': {current_temp}°C (expected {C.TRV_LOCKED_SETPOINT_C}°C)", level="WARNING")
                        self.lock_trv_setpoint(room_id)
            except (ValueError, TypeError) as e:
                self.log(f"Failed to check TRV setpoint for room '{room_id}': {e}", level="WARNING")

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
        room_valve_percents = {}  # Track valve percentages for boiler interlock
        room_data = {}  # Collect room data for per-room entity publishing
        
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
            # Allow heating if: target exists, temp exists, and either not stale OR in manual mode
            can_heat = (target is not None and temp is not None and room_mode != "off" 
                       and (not is_stale or room_mode == "manual"))
            
            if can_heat:
                calling = self.compute_call_for_heat(room_id, target, temp)
                self.room_call_for_heat[room_id] = calling
                
                if calling:
                    any_calling = True
                    active_rooms.append(room_id)
                
                # 5. Compute valve percentage
                valve_percent = self.compute_valve_percent(room_id, target, temp, calling)
                room_valve_percents[room_id] = valve_percent
                
                self.log(f"Room '{room_id}': temp={temp:.1f}°C, target={target:.1f}°C, "
                        f"calling={calling}, valve={valve_percent}%", level="DEBUG")
            else:
                # Room is off, stale, or has no target
                self.room_call_for_heat[room_id] = False
                room_valve_percents[room_id] = 0
                if room_mode == "off" or (is_stale and room_mode != "manual"):
                    self.set_trv_valve(room_id, 0, now)
                self.log(f"Room '{room_id}': mode={room_mode}, stale={is_stale}, "
                        f"target={target}, no heating", level="DEBUG")
            
            # Store room data for per-room entity publishing
            room_data[room_id] = {
                'temp': temp,
                'target': target,
                'mode': room_mode,
                'is_stale': is_stale,
                'calling': self.room_call_for_heat.get(room_id, False),
                'valve_percent': room_valve_percents.get(room_id, 0),
                'name': room_config.get('name', room_id)
            }
        
        # 7. Update boiler state machine (includes valve override calculation)
        boiler_status = self.update_boiler_state(active_rooms, room_valve_percents, now)
        
        # 8. Apply valve overrides if boiler requires it (e.g., pump overrun or interlock override)
        if boiler_status.get('valves_must_stay_open') or boiler_status.get('overridden_valve_percents'):
            overridden = boiler_status['overridden_valve_percents']
            for room_id, valve_percent in overridden.items():
                # Always send valve command when there's an override (boiler needs confirmation)
                self.set_trv_valve(room_id, valve_percent, now)
        else:
            # No overrides - send normal valve commands
            for room_id, valve_percent in room_valve_percents.items():
                self.set_trv_valve(room_id, valve_percent, now)
        
        # 9. Publish global status
        self.publish_status_with_boiler(any_calling, active_rooms, boiler_status)
        
        # 10. Publish per-room entities
        for room_id, data in room_data.items():
            self.publish_room_entities(room_id, data, boiler_status)

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
            Target temperature in °C, or None if room is off
            Note: Manual mode returns target even if sensors are stale
        """
        # Room off → no target
        if room_mode == "off":
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
        """Set TRV valve position with non-blocking feedback confirmation.
        
        With TRV setpoint locked at 5°C, the TRV is always in "open" mode,
        so we only need to control opening_degree (not closing_degree).
        
        Uses scheduler-based delays instead of blocking sleep() to avoid
        tying up AppDaemon threads.
        
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
                self.log(f"TRV {room_id}: Rate limited (elapsed={elapsed:.1f}s < min={min_interval}s)", level="DEBUG")
                return
        
        # Check if value actually changed
        last_commanded = self.trv_last_commanded.get(room_id)
        if last_commanded == percent:
            # No change needed
            return
        
        self.log(f"Setting TRV for room '{room_id}': {percent}% open (was {last_commanded}%)")
        
        # Start non-blocking valve command sequence
        self._start_valve_command(room_id, percent, now)
    
    def _start_valve_command(self, room_id: str, percent: int, now: datetime):
        """Initiate a non-blocking valve command with feedback confirmation.
        
        Args:
            room_id: Room identifier
            percent: Desired valve percentage (0-100)
            now: Current datetime for rate limiting
        """
        room_config = self.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            return
        
        trv = room_config['trv']
        state_key = f"valve_cmd_{room_id}"
        
        # Cancel any existing command for this room
        if state_key in self._valve_command_state:
            old_state = self._valve_command_state[state_key]
            if 'handle' in old_state and old_state['handle']:
                self.cancel_timer(old_state['handle'])
        
        # Initialize command state
        self._valve_command_state[state_key] = {
            'room_id': room_id,
            'target_percent': percent,
            'attempt': 0,
            'start_time': now,
            'handle': None,
        }
        
        # Send the command immediately
        self._execute_valve_command(state_key)
    
    def _execute_valve_command(self, state_key: str):
        """Execute a valve command and schedule feedback check.
        
        Args:
            state_key: State dictionary key for this command
        """
        if state_key not in self._valve_command_state:
            return
        
        state = self._valve_command_state[state_key]
        room_id = state['room_id']
        target_percent = state['target_percent']
        attempt = state['attempt']
        
        room_config = self.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            del self._valve_command_state[state_key]
            return
        
        trv = room_config['trv']
        max_retries = C.TRV_COMMAND_MAX_RETRIES
        
        self.log(f"TRV {room_id}: Setting valve to {target_percent}%, attempt {attempt+1}/{max_retries}", level="DEBUG")
        
        try:
            # Send command (only opening_degree, since TRV is locked in "open" mode)
            self.call_service("number/set_value",
                            entity_id=trv['cmd_valve'],
                            value=target_percent)
            
            # Schedule feedback check
            handle = self.run_in(self._check_valve_feedback, 
                               C.TRV_COMMAND_RETRY_INTERVAL_S, 
                               state_key=state_key)
            state['handle'] = handle
            
        except Exception as e:
            self.log(f"TRV {room_id}: Failed to send valve command: {e}", level="ERROR")
            del self._valve_command_state[state_key]
    
    def _check_valve_feedback(self, kwargs):
        """Callback to check valve feedback after a command.
        
        Args:
            kwargs: Callback kwargs containing state_key
        """
        state_key = kwargs.get('state_key')
        if not state_key or state_key not in self._valve_command_state:
            return
        
        state = self._valve_command_state[state_key]
        room_id = state['room_id']
        target_percent = state['target_percent']
        attempt = state['attempt']
        
        room_config = self.rooms.get(room_id)
        if not room_config or room_config.get('disabled'):
            del self._valve_command_state[state_key]
            return
        
        trv = room_config['trv']
        max_retries = C.TRV_COMMAND_MAX_RETRIES
        tolerance = C.TRV_COMMAND_FEEDBACK_TOLERANCE
        
        # Check feedback
        fb_valve_str = self.get_state(trv['fb_valve'])
        
        if fb_valve_str in [None, "unknown", "unavailable"]:
            self.log(f"TRV {room_id}: No valid feedback available, attempt {attempt+1}/{max_retries}", level="WARNING")
            
            if attempt < max_retries - 1:
                # Retry
                state['attempt'] += 1
                self._execute_valve_command(state_key)
            else:
                # Max retries reached
                self.log(f"TRV {room_id}: Failed to confirm valve position after {max_retries} attempts", level="WARNING")
                del self._valve_command_state[state_key]
            return
        
        try:
            fb_valve_val = int(float(fb_valve_str))
        except (ValueError, TypeError):
            self.log(f"TRV {room_id}: Invalid feedback value '{fb_valve_str}'", level="WARNING")
            
            if attempt < max_retries - 1:
                # Retry
                state['attempt'] += 1
                self._execute_valve_command(state_key)
            else:
                self.log(f"TRV {room_id}: Failed to confirm valve position after {max_retries} attempts", level="WARNING")
                del self._valve_command_state[state_key]
            return
        
        # Check if within tolerance
        if abs(fb_valve_val - target_percent) <= tolerance:
            # Success!
            self.log(f"TRV {room_id}: Valve position confirmed at {fb_valve_val}%", level="DEBUG")
            
            # Update tracking
            self.trv_last_commanded[room_id] = target_percent
            self.trv_last_update[room_id] = state['start_time']
            
            # Clean up state
            del self._valve_command_state[state_key]
        else:
            # Feedback doesn't match
            self.log(f"TRV {room_id}: Valve mismatch (target={target_percent}%, fb={fb_valve_val}%), attempt {attempt+1}/{max_retries}", level="DEBUG")
            
            if attempt < max_retries - 1:
                # Retry
                state['attempt'] += 1
                self._execute_valve_command(state_key)
            else:
                # Max retries reached
                self.log(f"TRV {room_id}: Failed to confirm valve position after {max_retries} attempts", level="WARNING")
                del self._valve_command_state[state_key]

    # ========================================================================
    # Boiler Helper Methods
    # ========================================================================

    def _set_boiler_setpoint(self, setpoint: float) -> None:
        """Set boiler setpoint (binary control mode).
        
        Args:
            setpoint: Target temperature in °C
        """
        try:
            # Determine if we want boiler ON or OFF based on setpoint
            want_on = (setpoint >= self.boiler_on_setpoint)
            
            if want_on:
                # Turn ON: set mode to heat and high setpoint
                self.call_service("climate/set_hvac_mode",
                                entity_id=self.boiler_entity,
                                hvac_mode="heat")
                self.call_service("climate/set_temperature",
                                entity_id=self.boiler_entity,
                                temperature=setpoint)
                self.log(f"Boiler: set hvac_mode=heat, temperature={setpoint}°C", level="DEBUG")
            else:
                # Turn OFF: set mode to off (setpoint doesn't matter when off)
                self.call_service("climate/set_hvac_mode",
                                entity_id=self.boiler_entity,
                                hvac_mode="off")
                self.log(f"Boiler: set hvac_mode=off", level="DEBUG")
        except Exception as e:
            self.log(f"Boiler: failed to set boiler state: {e}", level="ERROR")
    
    def _get_hvac_action(self) -> str:
        """Get current HVAC action from boiler.
        
        Returns:
            'heating', 'idle', 'off', or 'unknown'
        """
        try:
            attrs = self.get_state(self.boiler_entity, attribute="all")
            if attrs and 'attributes' in attrs:
                action = attrs['attributes'].get("hvac_action", "unknown")
                return action
            return "unknown"
        except Exception:
            return "unknown"
    
    def _start_timer(self, timer_entity: str, duration_seconds: int) -> None:
        """Start a timer helper with the specified duration.
        
        Args:
            timer_entity: Entity ID of timer
            duration_seconds: Duration in seconds
        """
        try:
            # Convert seconds to HH:MM:SS format
            hours = duration_seconds // 3600
            minutes = (duration_seconds % 3600) // 60
            seconds = duration_seconds % 60
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            self.call_service("timer/start",
                            entity_id=timer_entity,
                            duration=duration_str)
            self.log(f"Boiler: started {timer_entity} for {duration_str}", level="DEBUG")
        except Exception as e:
            self.log(f"Boiler: failed to start timer {timer_entity}: {e}", level="WARNING")
    
    def _cancel_timer(self, timer_entity: str) -> None:
        """Cancel a running timer.
        
        Args:
            timer_entity: Entity ID of timer
        """
        try:
            self.call_service("timer/cancel", entity_id=timer_entity)
            self.log(f"Boiler: cancelled {timer_entity}", level="DEBUG")
        except Exception as e:
            self.log(f"Boiler: failed to cancel timer {timer_entity}: {e}", level="DEBUG")
    
    def _is_timer_active(self, timer_entity: str) -> bool:
        """Check if a timer is currently active (running).
        
        Args:
            timer_entity: Entity ID of timer
            
        Returns:
            True if timer is active, False otherwise
        """
        try:
            timer_state = self.get_state(timer_entity)
            return timer_state == "active"
        except Exception as e:
            self.log(f"Boiler: failed to check timer {timer_entity}: {e}", level="DEBUG")
            return False
    
    def _save_pump_overrun_valves(self) -> None:
        """Persist valve positions to survive AppDaemon reload during pump overrun.
        
        Saves boiler_last_valve_positions to an input_text helper so they can be
        restored if AppDaemon reloads while pump overrun timer is active.
        """
        try:
            positions_json = json.dumps(self.boiler_last_valve_positions)
            self.call_service("input_text/set_value",
                            entity_id=C.HELPER_PUMP_OVERRUN_VALVES,
                            value=positions_json)
            self.log(f"Boiler: saved pump overrun valves: {self.boiler_last_valve_positions}", level="DEBUG")
        except Exception as e:
            self.log(f"Boiler: failed to save pump overrun valves: {e}", level="WARNING")
    
    def _clear_pump_overrun_valves(self) -> None:
        """Clear persisted pump overrun valve positions."""
        try:
            self.call_service("input_text/set_value",
                            entity_id=C.HELPER_PUMP_OVERRUN_VALVES,
                            value="")
            self.log("Boiler: cleared pump overrun valves", level="DEBUG")
        except Exception as e:
            self.log(f"Boiler: failed to clear pump overrun valves: {e}", level="DEBUG")
    
    def _check_min_on_time_elapsed(self) -> bool:
        """Check if minimum on time constraint is satisfied.
        
        Returns:
            True if min_on_time timer is not active (constraint satisfied)
        """
        # If timer is active, constraint is NOT satisfied
        # If timer is idle/finished, constraint IS satisfied
        return not self._is_timer_active(C.HELPER_BOILER_MIN_ON_TIMER)
    
    def _check_min_off_time_elapsed(self) -> bool:
        """Check if minimum off time constraint is satisfied.
        
        Returns:
            True if min_off_time timer is not active (constraint satisfied)
        """
        # If timer is active, constraint is NOT satisfied
        # If timer is idle/finished, constraint IS satisfied
        return not self._is_timer_active(C.HELPER_BOILER_MIN_OFF_TIMER)
    
    def _transition_to(self, new_state: str, now: datetime, reason: str) -> None:
        """Transition to new boiler state with logging.
        
        Args:
            new_state: Target state
            now: Current datetime
            reason: Reason for transition
        """
        if new_state != self.boiler_state:
            self.log(f"Boiler: {self.boiler_state} → {new_state} ({reason})")
            self.boiler_state = new_state
            self.boiler_state_entry_time = now

    def calculate_valve_overrides(
        self,
        rooms_calling: List[str],
        room_valve_percents: Dict[str, int]
    ) -> Tuple[Dict[str, int], bool, str]:
        """Calculate valve overrides if needed to meet minimum total opening.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            room_valve_percents: Dict mapping room_id -> calculated valve percent from bands
            
        Returns:
            Tuple of:
            - overridden_valve_percents: Dict[room_id, valve_percent] with overrides applied
            - interlock_ok: True if total >= min_valve_open_percent
            - reason: Explanation string
        """
        if not rooms_calling:
            return {}, False, "No rooms calling for heat"
        
        # Calculate total from band-calculated percentages
        total_from_bands = sum(room_valve_percents.get(room_id, 0) for room_id in rooms_calling)
        
        # Check if we need to override
        if total_from_bands >= self.boiler_min_valve_open_percent:
            # Valve bands are sufficient
            self.log(
                f"Boiler: total valve opening {total_from_bands}% >= "
                f"min {self.boiler_min_valve_open_percent}%, using valve bands",
                level="DEBUG"
            )
            return room_valve_percents.copy(), True, f"Total {total_from_bands}% >= min {self.boiler_min_valve_open_percent}%"
        
        # Need to override - distribute evenly across calling rooms
        n_rooms = len(rooms_calling)
        override_percent = int((self.boiler_min_valve_open_percent + n_rooms - 1) / n_rooms)  # Round up
        
        # Safety clamp: never command valve >100% even if config is misconfigured
        override_percent = min(100, override_percent)
        
        overridden = {
            room_id: override_percent
            for room_id in rooms_calling
        }
        
        new_total = override_percent * n_rooms
        
        self.log(
            f"Boiler: INTERLOCK OVERRIDE: total from bands {total_from_bands}% < "
            f"min {self.boiler_min_valve_open_percent}% -> setting {n_rooms} room(s) to {override_percent}% "
            f"each (new total: {new_total}%)",
            level="INFO"
        )
        
        return overridden, True, f"Override: {n_rooms} rooms @ {override_percent}% = {new_total}%"
    
    def _check_trv_feedback_confirmed(
        self,
        rooms_calling: List[str],
        valve_overrides: Dict[str, int]
    ) -> bool:
        """Check if TRV feedback confirms valves are at commanded positions.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            valve_overrides: Dict of commanded valve percentages
            
        Returns:
            True if all calling rooms have TRV feedback matching commanded position
        """
        if not rooms_calling:
            return True  # No rooms calling, trivially satisfied
        
        for room_id in rooms_calling:
            room_config = self.rooms.get(room_id)
            if not room_config or room_config.get('disabled'):
                self.log(f"Boiler: room {room_id} disabled, skipping feedback check", level="WARNING")
                return False
            
            commanded = valve_overrides.get(room_id, 0)
            trv = room_config['trv']
            
            # Get TRV feedback
            fb_valve_str = self.get_state(trv['fb_valve'])
            if fb_valve_str in [None, "unknown", "unavailable"]:
                self.log(f"Boiler: room {room_id} TRV feedback unavailable", level="DEBUG")
                return False
            
            try:
                feedback = int(float(fb_valve_str))
            except (ValueError, TypeError):
                self.log(f"Boiler: room {room_id} TRV feedback invalid: {fb_valve_str}", level="DEBUG")
                return False
            
            # Check if feedback matches commanded (with tolerance)
            tolerance = C.TRV_COMMAND_FEEDBACK_TOLERANCE
            if abs(feedback - commanded) > tolerance:
                self.log(f"Boiler: room {room_id} TRV feedback {feedback}% != commanded {commanded}%", level="DEBUG")
                return False
        
        return True

    # ========================================================================
    # Boiler State Machine
    # ========================================================================

    def update_boiler_state(
        self,
        rooms_calling_for_heat: List[str],
        room_valve_percents: Dict[str, int],
        now: datetime
    ) -> Dict[str, Any]:
        """Update boiler state machine based on demand and conditions.
        
        Args:
            rooms_calling_for_heat: List of room IDs calling for heat
            room_valve_percents: Dict of room_id -> valve percent (from bands, before override)
            now: Current datetime
            
        Returns:
            Dict with boiler status:
            {
                "state": str (current state machine state),
                "boiler_on": bool (is boiler commanded on),
                "hvac_action": str (actual boiler status),
                "rooms_calling": List[str],
                "reason": str,
                "interlock_ok": bool,
                "overridden_valve_percents": Dict[str, int],
                "total_valve_percent": int,
                "valves_must_stay_open": bool (true during pump overrun)
            }
        """
        # Initialize state_entry_time on first call
        if self.boiler_state_entry_time is None:
            self.boiler_state_entry_time = now
        
        # Calculate valve overrides if needed
        overridden_valves, interlock_ok, interlock_reason = self.calculate_valve_overrides(
            rooms_calling_for_heat,
            room_valve_percents
        )
        
        # Calculate total valve opening
        total_valve = sum(overridden_valves.get(room_id, 0) for room_id in rooms_calling_for_heat)
        
        # Merge overridden valves with all room valve percents for pump overrun tracking
        # This ensures we save ALL room valve positions, not just calling rooms
        all_valve_positions = room_valve_percents.copy()
        all_valve_positions.update(overridden_valves)
        
        # Check TRV feedback confirmation
        trv_feedback_ok = self._check_trv_feedback_confirmed(
            rooms_calling_for_heat,
            overridden_valves
        )
        
        # Read current HVAC action
        hvac_action = self._get_hvac_action()
        
        # Determine if we have demand
        has_demand = len(rooms_calling_for_heat) > 0
        
        # Time in current state
        time_in_state = (now - self.boiler_state_entry_time).total_seconds() if self.boiler_state_entry_time else 0
        
        # State machine logic
        reason = ""
        valves_must_stay_open = False
        
        if self.boiler_state == C.STATE_OFF:
            if has_demand and interlock_ok:
                # Demand exists, check anti-cycling and TRV feedback
                if not self._check_min_off_time_elapsed():
                    self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "min_off_time not elapsed")
                    reason = f"Blocked: min_off_time ({self.boiler_min_off_time_s}s) not elapsed"
                elif not trv_feedback_ok:
                    self._transition_to(C.STATE_PENDING_ON, now, "waiting for TRV confirmation")
                    reason = "Waiting for TRV feedback confirmation"
                else:
                    # All conditions met, turn on
                    self._transition_to(C.STATE_ON, now, "demand and conditions met")
                    self._set_boiler_setpoint(self.boiler_on_setpoint)
                    # Start min_on_time timer to enforce minimum on duration
                    self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self.boiler_min_on_time_s)
                    reason = f"Turned ON: {len(rooms_calling_for_heat)} room(s) calling"
            elif has_demand and not interlock_ok:
                self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "insufficient valve opening")
                reason = f"Interlock blocked: {interlock_reason}"
            else:
                reason = "Off: no demand"
        
        elif self.boiler_state == C.STATE_PENDING_ON:
            if not has_demand:
                self._transition_to(C.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased while pending"
            elif not interlock_ok:
                self._transition_to(C.STATE_INTERLOCK_BLOCKED, now, "interlock failed")
                reason = f"Interlock blocked: {interlock_reason}"
            elif trv_feedback_ok:
                # TRVs confirmed, turn on
                self._transition_to(C.STATE_ON, now, "TRV feedback confirmed")
                self._set_boiler_setpoint(self.boiler_on_setpoint)
                # Start min_on_time timer to enforce minimum on duration
                self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self.boiler_min_on_time_s)
                reason = f"Turned ON: TRVs confirmed at {total_valve}%"
            else:
                reason = f"Pending ON: waiting for TRV confirmation ({time_in_state:.0f}s)"
                # Log warning if stuck for >5 minutes
                if time_in_state > 300:
                    self.log(
                        f"Boiler has been waiting for TRV feedback for {int(time_in_state/60)} minutes. "
                        f"Rooms: {', '.join(rooms_calling_for_heat)}",
                        level="WARNING"
                    )
        
        elif self.boiler_state == C.STATE_ON:
            # Save ALL valve positions (not just calling rooms) for pump overrun safety
            if has_demand and all_valve_positions:
                self.boiler_last_valve_positions = all_valve_positions.copy()
                self.log(f"Boiler: STATE_ON saved valve positions: {self.boiler_last_valve_positions}", level="DEBUG")
            
            if not has_demand:
                # Demand stopped, enter off-delay period
                self.log(f"Boiler: STATE_ON → PENDING_OFF, preserved valve positions: {self.boiler_last_valve_positions}", level="DEBUG")
                self._transition_to(C.STATE_PENDING_OFF, now, "demand ceased, entering off-delay")
                # Start off-delay timer
                self._start_timer(C.HELPER_BOILER_OFF_DELAY_TIMER, self.boiler_off_delay_s)
                reason = f"Pending OFF: off-delay ({self.boiler_off_delay_s}s) started"
                # CRITICAL: Valves must stay open immediately upon entering PENDING_OFF
                valves_must_stay_open = True
                overridden_valves = self.boiler_last_valve_positions.copy()
            elif not interlock_ok:
                # Interlock failed while running - turn off immediately
                self.log("Boiler: interlock failed while ON, turning off immediately", level="WARNING")
                self._transition_to(C.STATE_PUMP_OVERRUN, now, "interlock failed")
                self._set_boiler_setpoint(self.boiler_off_setpoint)
                # Cancel min_on_time timer and start min_off_time + pump_overrun timers
                self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self.boiler_min_off_time_s)
                self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self.boiler_pump_overrun_s)
                reason = "Turned OFF: interlock failed"
                valves_must_stay_open = True
                self.log(
                    f"🔴 CRITICAL: Boiler interlock failed while running! Boiler turned off. "
                    f"Total valve opening dropped below {self.boiler_min_valve_open_percent}%.",
                    level="ERROR"
                )
            else:
                reason = f"ON: heating {len(rooms_calling_for_heat)} room(s), total valve {total_valve}%"
        
        elif self.boiler_state == C.STATE_PENDING_OFF:
            # CRITICAL: Valves must stay open during pending_off because boiler is still ON
            valves_must_stay_open = True
            # Use last known valve positions instead of current (which would be 0%)
            overridden_valves = self.boiler_last_valve_positions.copy()
            self.log(f"Boiler: STATE_PENDING_OFF using saved positions: {overridden_valves}", level="DEBUG")
            
            if has_demand and interlock_ok:
                # Demand returned during off-delay, return to ON
                self._transition_to(C.STATE_ON, now, "demand returned")
                # Cancel off-delay timer since we're returning to ON
                self._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
                reason = f"Returned to ON: demand resumed ({len(rooms_calling_for_heat)} room(s))"
            elif not self._is_timer_active(C.HELPER_BOILER_OFF_DELAY_TIMER):
                # Off-delay timer completed, check min_on_time
                if not self._check_min_on_time_elapsed():
                    reason = f"Pending OFF: waiting for min_on_time ({self.boiler_min_on_time_s}s)"
                else:
                    # Turn off and enter pump overrun
                    self._transition_to(C.STATE_PUMP_OVERRUN, now, "off-delay elapsed, turning off")
                    self._set_boiler_setpoint(self.boiler_off_setpoint)
                    # Cancel min_on_time timer and start min_off_time + pump_overrun timers
                    self._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
                    self._start_timer(C.HELPER_BOILER_MIN_OFF_TIMER, self.boiler_min_off_time_s)
                    self._start_timer(C.HELPER_PUMP_OVERRUN_TIMER, self.boiler_pump_overrun_s)
                    # Persist valve positions for pump overrun
                    self._save_pump_overrun_valves()
                    reason = "Pump overrun: boiler commanded off"
                    valves_must_stay_open = True
            else:
                # Still waiting for off-delay timer
                reason = f"Pending OFF: off-delay timer active"
        
        elif self.boiler_state == C.STATE_PUMP_OVERRUN:
            valves_must_stay_open = True
            # Use last known valve positions to keep valves open during pump overrun
            overridden_valves = self.boiler_last_valve_positions.copy()
            
            if has_demand and interlock_ok and trv_feedback_ok:
                # New demand during pump overrun, can return to ON
                self._transition_to(C.STATE_ON, now, "demand resumed during pump overrun")
                self._set_boiler_setpoint(self.boiler_on_setpoint)
                # Cancel pump_overrun timer, restart min_on_time timer
                self._cancel_timer(C.HELPER_PUMP_OVERRUN_TIMER)
                self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self.boiler_min_on_time_s)
                reason = f"Returned to ON: demand during pump overrun"
                valves_must_stay_open = False
            elif not self._is_timer_active(C.HELPER_PUMP_OVERRUN_TIMER):
                # Pump overrun timer completed
                self._transition_to(C.STATE_OFF, now, "pump overrun complete")
                self._clear_pump_overrun_valves()
                reason = "Pump overrun complete, now OFF"
                valves_must_stay_open = False
                overridden_valves = {}  # Clear overrides so valves can close
            else:
                # Still in pump overrun
                reason = f"Pump overrun: timer active (valves must stay open)"
        
        elif self.boiler_state == C.STATE_INTERLOCK_BLOCKED:
            if has_demand and interlock_ok and trv_feedback_ok:
                # Interlock now satisfied
                if not self._check_min_off_time_elapsed():
                    reason = f"Interlock OK but min_off_time not elapsed"
                else:
                    self._transition_to(C.STATE_ON, now, "interlock satisfied")
                    self._set_boiler_setpoint(self.boiler_on_setpoint)
                    # Start min_on_time timer
                    self._start_timer(C.HELPER_BOILER_MIN_ON_TIMER, self.boiler_min_on_time_s)
                    reason = f"Turned ON: interlock now satisfied"
            elif not has_demand:
                self._transition_to(C.STATE_OFF, now, "demand ceased")
                reason = "Demand ceased"
            else:
                reason = f"Blocked: {interlock_reason}"
                # Log warning if blocked for >5 minutes
                if time_in_state > 300:
                    self.log(
                        f"Boiler interlock has been blocked for {int(time_in_state/60)} minutes. "
                        f"Total valve opening insufficient (minimum {self.boiler_min_valve_open_percent}% required). "
                        f"Rooms calling: {', '.join(rooms_calling_for_heat) if rooms_calling_for_heat else 'none'}",
                        level="WARNING"
                    )
        
        # CRITICAL SAFETY: Emergency valve override
        # If boiler is physically ON (heating) but no rooms calling for heat,
        # force safety room valve open to ensure there's a path for hot water
        if self.boiler_safety_room and hvac_action in ("heating", "idle"):
            if len(rooms_calling_for_heat) == 0:
                # Boiler is ON but no demand - EMERGENCY!
                overridden_valves[self.boiler_safety_room] = 100
                self.log(
                    f"🔴 EMERGENCY: Boiler ON with no demand! Forcing {self.boiler_safety_room} valve to 100% for safety",
                    level="ERROR"
                )
        
        # Determine boiler_on flag
        boiler_on = self.boiler_state in (C.STATE_ON, C.STATE_PENDING_OFF)
        
        return {
            "state": self.boiler_state,
            "boiler_on": boiler_on,
            "hvac_action": hvac_action,
            "rooms_calling": rooms_calling_for_heat,
            "reason": reason,
            "interlock_ok": interlock_ok,
            "overridden_valve_percents": overridden_valves,
            "total_valve_percent": total_valve,
            "valves_must_stay_open": valves_must_stay_open,
            "time_in_state_s": time_in_state,
        }

    # ========================================================================
    # Status Publishing
    # ========================================================================

    def publish_status_with_boiler(self, any_calling: bool, active_rooms: List[str], 
                                   boiler_status: Dict[str, Any]):
        """Publish status entity with detailed boiler state information.
        
        Args:
            any_calling: True if any room is calling for heat
            active_rooms: List of room IDs calling for heat
            boiler_status: Dict from update_boiler_state containing state machine info
        """
        # Build status string based on boiler state
        boiler_state = boiler_status.get('state', C.STATE_OFF)
        if boiler_state == C.STATE_ON:
            status_str = f"heating ({len(active_rooms)} rooms)"
        elif boiler_state == C.STATE_PUMP_OVERRUN:
            status_str = "pump overrun"
        elif boiler_state == C.STATE_PENDING_ON:
            status_str = "pending on (waiting for TRVs)"
        elif boiler_state == C.STATE_PENDING_OFF:
            status_str = "pending off (delay)"
        elif boiler_state == C.STATE_INTERLOCK_BLOCKED:
            status_str = "blocked (interlock)"
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
            # Boiler state machine info
            "boiler_state": boiler_state,
            "boiler_on": boiler_status.get('boiler_on', False),
            "boiler_hvac_action": boiler_status.get('hvac_action', 'unknown'),
            "boiler_reason": boiler_status.get('reason', ''),
            "boiler_interlock_ok": boiler_status.get('interlock_ok', False),
            "boiler_total_valve_percent": boiler_status.get('total_valve_percent', 0),
            "boiler_time_in_state_s": boiler_status.get('time_in_state_s', 0),
            "valves_must_stay_open": boiler_status.get('valves_must_stay_open', False),
        }
        
        # Set state
        self.set_state(C.STATUS_ENTITY, state=status_str, attributes=attributes)
    
    def publish_room_entities(self, room_id: str, data: Dict[str, Any], 
                             boiler_status: Dict[str, Any]):
        """Publish per-room entities for detailed room status.
        
        Publishes the following entities for each room:
        - sensor.pyheat_<room>_temperature (°C)
        - sensor.pyheat_<room>_target (°C)
        - sensor.pyheat_<room>_state (off/manual/auto/stale)
        - number.pyheat_<room>_valve_percent (0-100)
        - binary_sensor.pyheat_<room>_calling_for_heat (on/off)
        
        Args:
            room_id: Room identifier
            data: Room data dict with temp, target, mode, etc.
            boiler_status: Boiler status dict (for potential overrides)
        """
        room_name = data.get('name', room_id.replace('_', ' ').title())
        temp = data.get('temp')
        target = data.get('target')
        mode = data.get('mode', 'auto')
        is_stale = data.get('is_stale', False)
        calling = data.get('calling', False)
        valve_percent = data.get('valve_percent', 0)
        
        # Check if valve was overridden by boiler (e.g., pump overrun)
        overridden_valves = boiler_status.get('overridden_valve_percents', {})
        if room_id in overridden_valves:
            valve_percent = overridden_valves[room_id]
        
        # 1. Publish temperature (fused) - always, even if None/stale
        temp_entity = f"sensor.pyheat_{room_id}_temperature"
        temp_state = round(temp, 1) if temp is not None else "unavailable"
        self.set_state(
            temp_entity,
            state=temp_state,
            attributes={
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "friendly_name": f"{room_name} Temperature"
            }
        )
        
        # 2. Publish target temperature (always, even if None)
        target_entity = f"sensor.pyheat_{room_id}_target"
        target_state = round(target, 1) if target is not None else "unknown"
        self.set_state(
            target_entity,
            state=target_state,
            attributes={
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "friendly_name": f"{room_name} Target"
            }
        )
        
        # 3. Publish room state
        if is_stale:
            state_str = "stale"
        else:
            state_str = mode
        
        state_entity = f"sensor.pyheat_{room_id}_state"
        self.set_state(
            state_entity,
            state=state_str,
            attributes={
                "friendly_name": f"{room_name} State"
            }
        )
        
        # 4. Publish valve percentage as number entity
        valve_entity = f"number.pyheat_{room_id}_valve_percent"
        try:
            valve_state = str(int(valve_percent))  # Convert to string to avoid AppDaemon issues with 0
            self.set_state(
                valve_entity,
                state=valve_state,
                attributes={
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "unit_of_measurement": "%",
                    "friendly_name": f"{room_name} Valve"
                }
            )
            self.log(f"DEBUG: Successfully set {valve_entity} to {valve_state}", level="DEBUG")
        except Exception as e:
            self.log(f"ERROR: Failed to set {valve_entity}: {type(e).__name__}: {e}", level="ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
        
        # 5. Publish calling for heat as binary_sensor (no device_class to avoid on/off override)
        cfh_entity = f"binary_sensor.pyheat_{room_id}_calling_for_heat"
        try:
            self.set_state(
                cfh_entity,
                state="on" if calling else "off",
                attributes={
                    "friendly_name": f"{room_name} Calling For Heat"
                }
            )
            self.log(f"DEBUG: Successfully set {cfh_entity} to {'on' if calling else 'off'}", level="DEBUG")
        except Exception as e:
            self.log(f"ERROR: Failed to set {cfh_entity}: {type(e).__name__}: {e}", level="ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", level="ERROR")

