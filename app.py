# -*- coding: utf-8 -*-
"""
Pyheat - Home Heating Controller for AppDaemon (Modular Architecture)

A comprehensive heating control system that manages:
- Per-room temperature control with smart TRV management
- Schedule-based and manual temperature setpoints
- Boiler control with safety interlocks and anti-cycling
- Temperature sensor fusion and staleness detection
- Override functionality with flexible temperature and duration modes

Architecture:
- Thin orchestrator (this file) coordinates modular components
- Each module has single responsibility and clear interfaces
- Event-driven with 1-minute periodic recompute
- Stateful with persistence across restarts

For full documentation, see docs/pyheat-spec.md
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime
from typing import Dict, List, Optional

# Import PyHeat modules
from pyheat.config_loader import ConfigLoader
from pyheat.sensor_manager import SensorManager
from pyheat.scheduler import Scheduler
from pyheat.trv_controller import TRVController
from pyheat.room_controller import RoomController
from pyheat.boiler_controller import BoilerController
from pyheat.status_publisher import StatusPublisher
from pyheat.service_handler import ServiceHandler
from pyheat.api_handler import APIHandler
import pyheat.constants as C


class PyHeat(hass.Hass):
    """Main PyHeat heating controller app for AppDaemon (Modular Architecture)."""

    def initialize(self):
        """Initialize the PyHeat app.
        
        Called by AppDaemon when the app is loaded or reloaded.
        Sets up modular components, loads configuration, registers callbacks,
        and starts the control loop.
        """
        self.log("=" * 60)
        self.log("PyHeat initializing (Modular Architecture)...")
        self.log("=" * 60)
        
        # Initialize modules
        self.config = ConfigLoader(self)
        self.sensors = SensorManager(self, self.config)
        self.scheduler = Scheduler(self, self.config)
        self.trvs = TRVController(self, self.config)
        self.rooms = RoomController(self, self.config, self.sensors, self.scheduler, self.trvs)
        self.boiler = BoilerController(self, self.config)
        self.status = StatusPublisher(self, self.config)
        self.status.scheduler_ref = self.scheduler  # Allow status publisher to get scheduled temps
        self.services = ServiceHandler(self, self.config)
        self.api = APIHandler(self, self.services)
        
        # Timing and state tracking
        self.last_recompute = None
        self.recompute_count = 0
        self.first_boot = True
        
        # Track last published rounded temperature per room
        # Used to skip recomputes when sensor changes don't affect displayed value
        self.last_published_temps = {}  # {room_id: rounded_temp}
        
        # Load configuration
        try:
            self.config.load_all()
        except Exception as e:
            self.error(f"Failed to load configuration: {e}")
            self.log("PyHeat initialization failed - configuration error")
            return
        
        # Initialize sensor values from current state
        self.sensors.initialize_from_ha()
        
        # Initialize TRV state from current valve positions
        self.trvs.initialize_from_ha()
        
        # Initialize room call-for-heat state from current valve positions (CRITICAL for startup)
        self.rooms.initialize_from_ha()
        
        # Initialize last published temps from current sensor values
        # This prevents false "changed" detection on first sensor update after restart
        now = datetime.now()
        for room_id in self.config.rooms.keys():
            precision = self.config.rooms[room_id].get('precision', 1)
            temp, is_stale = self.sensors.get_room_temperature(room_id, now)
            if temp is not None:
                self.last_published_temps[room_id] = round(temp, precision)
        
        # Setup callbacks for helper entities
        self.setup_callbacks()
        
        # Schedule periodic recompute
        self.run_every(self.periodic_recompute, "now+5", C.RECOMPUTE_INTERVAL_S)
        
        # Schedule TRV setpoint monitoring (check every 5 minutes)
        self.run_every(self.check_trv_setpoints, "now+10", C.TRV_SETPOINT_CHECK_INTERVAL_S)
        
        # Schedule config file monitoring (check every 30 seconds)
        self.run_every(self.check_config_files, "now+15", 30)
        
        # Lock all TRV setpoints immediately
        self.run_in(self.lock_all_trv_setpoints, 3)
        
        # Perform initial recomputes (with delays for sensor restoration)
        self.run_in(self.initial_recompute, C.STARTUP_INITIAL_DELAY_S)
        self.run_in(self.second_recompute, C.STARTUP_SECOND_DELAY_S)
        
        # Register service handlers
        self.services.register_all(self.trigger_recompute, self.scheduler)
        
        # Register HTTP API endpoints for external access (e.g., pyheat-web)
        self.api.register_all()
        
        # Log startup summary
        self.log(f"PyHeat initialized successfully")
        self.log(f"  Rooms: {len(self.config.rooms)}")
        self.log(f"  Schedules: {len(self.config.schedules)}")
        master_enable = self.get_state(C.HELPER_MASTER_ENABLE)
        holiday_mode = self.get_state(C.HELPER_HOLIDAY_MODE)
        self.log(f"  Master enable: {master_enable}")
        self.log(f"  Holiday mode: {holiday_mode}")
        self.log("=" * 60)

    # ========================================================================
    # Callback Setup
    # ========================================================================

    def setup_callbacks(self):
        """Register state change callbacks for all helper entities and sensors."""
        # Master controls
        if self.entity_exists(C.HELPER_MASTER_ENABLE):
            self.listen_state(self.master_enable_changed, C.HELPER_MASTER_ENABLE)
        
        if self.entity_exists(C.HELPER_HOLIDAY_MODE):
            self.listen_state(self.holiday_mode_changed, C.HELPER_HOLIDAY_MODE)
        
        # Per-room callbacks
        for room_id in self.config.rooms.keys():
            # Mode changes
            mode_entity = C.HELPER_ROOM_MODE.format(room=room_id)
            if self.entity_exists(mode_entity):
                self.listen_state(self.room_mode_changed, mode_entity, room_id=room_id)
            
            # Manual setpoint changes
            setpoint_entity = C.HELPER_ROOM_MANUAL_SETPOINT.format(room=room_id)
            if self.entity_exists(setpoint_entity):
                self.listen_state(self.room_setpoint_changed, setpoint_entity, room_id=room_id)
            
            # Override timer changes
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
            if self.entity_exists(timer_entity):
                self.listen_state(self.room_timer_changed, timer_entity, room_id=room_id)
            
            # Temperature sensor callbacks
            room_cfg = self.config.rooms[room_id]
            for sensor_cfg in room_cfg['sensors']:
                entity_id = sensor_cfg['entity_id']
                temp_attribute = sensor_cfg.get('temperature_attribute')
                
                # If temperature_attribute is specified, listen to that attribute
                # Otherwise listen to state changes
                if temp_attribute:
                    self.listen_state(self.sensor_changed, entity_id, 
                                    room_id=room_id, attribute=temp_attribute)
                else:
                    self.listen_state(self.sensor_changed, entity_id, room_id=room_id)
            
            # TRV feedback callbacks
            trv = room_cfg['trv']
            self.listen_state(self.trv_feedback_changed, trv['fb_valve'], room_id=room_id)
            
            # TRV setpoint monitoring (detect if someone changes it manually)
            self.listen_state(self.trv_setpoint_changed, trv['climate'], room_id=room_id, attribute='temperature')
        
        self.log(f"Registered callbacks for {len(self.config.rooms)} rooms")

    # ========================================================================
    # State Change Callbacks
    # ========================================================================

    def master_enable_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Master enable changed: {old} -> {new}")
        self.trigger_recompute("master_enable_changed")

    def holiday_mode_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Holiday mode changed: {old} -> {new}")
        self.trigger_recompute("holiday_mode_changed")

    def room_mode_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' mode changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_mode_changed")

    def room_setpoint_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' manual setpoint changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_setpoint_changed")

    def room_timer_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        if old != new:
            if new in ["active", "paused"]:
                self.log(f"Room '{room_id}' override started")
            elif old in ["active", "paused"] and new == "idle":
                self.log(f"Room '{room_id}' override expired")
                # Clear the override target
                target_entity = C.HELPER_ROOM_OVERRIDE_TARGET.format(room=room_id)
                if self.entity_exists(target_entity):
                    # Set to sentinel value (entity min is 5, so 0 indicates cleared)
                    self.call_service("input_number/set_value",
                                    entity_id=target_entity, value=0)
                # Clear the override type to ensure status is updated
                self.service_handler._set_override_type(room_id, "none")
            self.trigger_recompute(f"room_{room_id}_timer_changed")

    def sensor_changed(self, entity, attribute, old, new, kwargs):
        """Temperature sensor state changed.
        
        Optimized to skip recomputes when sensor changes don't affect the
        displayed (precision-rounded) temperature value. This reduces unnecessary
        recomputes by 80-90% when sensors report small fluctuations.
        
        Deadband threshold: To prevent boundary flipping when fused sensors hover
        around rounding boundaries (e.g., 17.745C <-> 17.755C flipping between
        17.7C and 17.8C), only trigger recompute if the change exceeds 0.5 * precision.
        This adds hysteresis without affecting control accuracy (boiler hysteresis >> 0.05C).
        """
        room_id = kwargs.get('room_id')
        if new and new not in ['unknown', 'unavailable']:
            try:
                temp = float(new)
                now = datetime.now()
                
                # Always update sensor manager with new raw value
                self.sensors.update_sensor(entity, temp, now)
                self.log(f"Sensor {entity} updated: {temp}C (room: {room_id})", level="DEBUG")
                
                # Get room precision and fused temperature
                precision = self.config.rooms[room_id].get('precision', 1)
                fused_temp, is_stale = self.sensors.get_room_temperature(room_id, now)
                
                # Always recompute if sensors are stale (safety)
                if fused_temp is None or is_stale:
                    self.trigger_recompute(f"sensor_{room_id}_changed")
                    return
                
                # Round fused temp to display precision
                new_rounded = round(fused_temp, precision)
                old_rounded = self.last_published_temps.get(room_id)
                
                # Deadband: Only recompute if change exceeds half a display unit
                # Prevents flipping at boundaries (e.g., 17.745C vs 17.755C)
                if old_rounded is not None:
                    deadband = 0.5 * (10 ** -precision)  # 0.05C for precision=1
                    temp_delta = abs(new_rounded - old_rounded)
                    
                    if temp_delta < deadband:
                        self.log(f"Sensor {entity} recompute skipped - change below deadband "
                                f"({old_rounded}C -> {new_rounded}C, delta={temp_delta:.3f}C < {deadband:.3f}C)", 
                                level="DEBUG")
                        return
                
                # Temp changed significantly - update tracking and trigger recompute
                self.last_published_temps[room_id] = new_rounded
                self.trigger_recompute(f"sensor_{room_id}_changed")
                
            except (ValueError, TypeError):
                self.log(f"Invalid sensor value for {entity}: {new}", level="WARNING")

    def trv_feedback_changed(self, entity, attribute, old, new, kwargs):
        """TRV valve feedback sensor changed."""
        room_id = kwargs.get('room_id')
        if new and new not in ['unknown', 'unavailable']:
            try:
                feedback_percent = int(float(new))
                now = datetime.now()
                self.log(f"TRV feedback updated: {entity} = {feedback_percent}", level="DEBUG")
                
                # Check for unexpected valve position (pass current boiler state to prevent fighting)
                self.trvs.check_feedback_for_unexpected_position(room_id, feedback_percent, now, self.boiler.boiler_state)
                
                # If unexpected position detected, trigger immediate recompute
                if room_id in self.trvs.unexpected_valve_positions:
                    self.trigger_recompute(f"trv_{room_id}_unexpected_position")
                    
            except (ValueError, TypeError):
                self.log(f"Invalid TRV feedback for {entity}: {new}", level="WARNING")

    def trv_setpoint_changed(self, entity, attribute, old, new, kwargs):
        """TRV climate entity setpoint changed (someone changed it manually)."""
        room_id = kwargs.get('room_id')
        if new and new != C.TRV_LOCKED_SETPOINT_C:
            self.log(f"TRV setpoint for '{room_id}' changed to {new}C (should be locked at {C.TRV_LOCKED_SETPOINT_C}C), correcting...", level="WARNING")
            self.run_in(lambda kwargs: self.trvs.lock_setpoint(room_id), 1)

    # ========================================================================
    # TRV Setpoint Management
    # ========================================================================

    def lock_all_trv_setpoints(self, kwargs=None):
        """Lock all TRV setpoints to maximum (35C)."""
        self.trvs.lock_all_setpoints()

    def check_trv_setpoints(self, kwargs):
        """Periodic check to ensure TRV setpoints remain locked."""
        self.trvs.check_all_setpoints()

    def check_config_files(self, kwargs):
        """Periodic check for configuration file changes."""
        if self.config.check_for_changes():
            self.log("Configuration files changed, reloading...")
            self.config.reload()
            self.trigger_recompute("config_files_changed")

    # ========================================================================
    # Recompute Logic
    # ========================================================================

    def periodic_recompute(self, kwargs):
        """Periodic recompute callback (runs every minute)."""
        self.recompute_count += 1
        now = datetime.now()
        self.last_recompute = now
        self.log(f"Periodic recompute #{self.recompute_count}", level="DEBUG")
        self.recompute_all(now)

    def initial_recompute(self, kwargs):
        """Initial recompute after startup."""
        self.log("Running initial recompute...")
        self.recompute_all(datetime.now())

    def second_recompute(self, kwargs):
        """Second recompute after startup (for late-restoring sensors)."""
        self.log("Running second recompute (for late-restoring sensors)...")
        self.first_boot = False
        self.recompute_all(datetime.now())

    def trigger_recompute(self, reason: str):
        """Trigger an immediate recompute.
        
        Args:
            reason: Description of why recompute was triggered
        """
        # Call recompute synchronously to avoid race conditions with rapid triggers
        # (multiple sensor updates could queue up 10+ delayed recomputes)
        self.recompute_count += 1
        now = datetime.now()
        self.last_recompute = now
        
        self.log(f"Recompute #{self.recompute_count} triggered: {reason}", level="DEBUG")
        self.recompute_all(now)

    def recompute_all(self, now: datetime):
        """Main recompute logic - calculates and applies heating decisions for all rooms.
        
        Args:
            now: Current datetime
        """
        # Note: recompute_count is incremented in trigger_recompute, not here
        # (to avoid double-counting when called directly from periodic_recompute)
        if not hasattr(self, 'last_recompute'):
            self.last_recompute = now
            self.recompute_count = 0
            
        self.last_recompute = now
        
        # Check master enable
        if self.entity_exists(C.HELPER_MASTER_ENABLE):
            master_enable = self.get_state(C.HELPER_MASTER_ENABLE)
            if master_enable != "on":
                self.log("Master enable is OFF, system idle")
                # Turn off boiler if running
                if self.entity_exists(C.HELPER_BOILER_ACTOR):
                    if self.get_state(C.HELPER_BOILER_ACTOR) == "on":
                        self.call_service("input_boolean/turn_off", entity_id=C.HELPER_BOILER_ACTOR)
                        self.log("Boiler turned OFF (master disabled)")
                # Close all valves
                for room_id in self.config.rooms.keys():
                    self.rooms.set_room_valve(room_id, 0, now)
                return
        
        # Compute each room
        room_data = {}
        active_rooms = []
        any_calling = False
        
        for room_id in self.config.rooms.keys():
            data = self.rooms.compute_room(room_id, now)
            room_data[room_id] = data
            
            if data['calling']:
                any_calling = True
                active_rooms.append(room_id)
        
        # Update boiler state (returns persisted valve positions if needed)
        boiler_state, boiler_reason, persisted_valves, valves_must_stay_open = \
            self.boiler.update_state(any_calling, active_rooms, room_data, now)
        
        # Apply valve commands with persistence priority
        # CRITICAL: If there are persisted valves (pump overrun or interlock), use them for affected rooms
        # and use normal calculations for rooms NOT in persistence dict
        if persisted_valves:
            # Send persistence commands first (critical for pump overrun safety)
            for room_id, valve_percent in persisted_valves.items():
                self.log(f"Room '{room_id}': using persisted valve {valve_percent}% (boiler state: {boiler_state})", level="DEBUG")
                self.rooms.set_room_valve(room_id, valve_percent, now)
                
                # Publish room entities with persisted valve
                data_for_publish = room_data[room_id].copy()
                data_for_publish['valve_percent'] = valve_percent
                self.status.publish_room_entities(room_id, data_for_publish, now)
            
            # Send normal commands for rooms NOT in persistence dict
            for room_id in self.config.rooms.keys():
                if room_id not in persisted_valves:
                    data = room_data[room_id]
                    valve_percent = data['valve_percent']
                    self.rooms.set_room_valve(room_id, valve_percent, now)
                    self.status.publish_room_entities(room_id, data, now)
        else:
            # No persistence - send all normal valve commands
            for room_id in self.config.rooms.keys():
                data = room_data[room_id]
                valve_percent = data['valve_percent']
                self.rooms.set_room_valve(room_id, valve_percent, now)
                self.status.publish_room_entities(room_id, data, now)
        
        # Publish system status
        self.status.publish_system_status(any_calling, active_rooms, room_data, 
                                         boiler_state, boiler_reason, now)
        
        # Log summary
        if any_calling:
            self.log(f"Recompute #{self.recompute_count}: Heating {len(active_rooms)} room(s) - {', '.join(active_rooms)}")
        else:
            self.log(f"Recompute #{self.recompute_count}: System idle", level="DEBUG")
