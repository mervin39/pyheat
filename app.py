# -*- coding: utf-8 -*-
"""
Pyheat - Home Heating Controller for AppDaemon (Modular Architecture)

A comprehensive heating control system that manages:
- Per-room temperature control with smart TRV management
- Schedule-based and manual temperature setpoints
- Boiler control with safety interlocks and anti-cycling
- Temperature sensor fusion and staleness detection
- Override and boost functionality

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
        self.services = ServiceHandler(self, self.config)
        
        # Timing and state tracking
        self.last_recompute = None
        self.recompute_count = 0
        self.first_boot = True
        
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
        self.services.register_all(self.trigger_recompute)
        
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
        self.log(f"Master enable changed: {old} → {new}")
        self.trigger_recompute("master_enable_changed")

    def holiday_mode_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Holiday mode changed: {old} → {new}")
        self.trigger_recompute("holiday_mode_changed")

    def room_mode_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' mode changed: {old} → {new}")
        self.trigger_recompute(f"room_{room_id}_mode_changed")

    def room_setpoint_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' manual setpoint changed: {old} → {new}")
        self.trigger_recompute(f"room_{room_id}_setpoint_changed")

    def room_timer_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        if old != new:
            if new in ["active", "paused"]:
                self.log(f"Room '{room_id}' override started")
            elif old in ["active", "paused"] and new == "idle":
                self.log(f"Room '{room_id}' override expired")
            self.trigger_recompute(f"room_{room_id}_timer_changed")

    def sensor_changed(self, entity, attribute, old, new, kwargs):
        """Temperature sensor state changed."""
        room_id = kwargs.get('room_id')
        if new and new not in ['unknown', 'unavailable']:
            try:
                temp = float(new)
                now = datetime.now()
                self.sensors.update_sensor(entity, temp, now)
                self.log(f"Sensor {entity} updated: {temp}°C (room: {room_id})", level="DEBUG")
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
                
                # Check for unexpected valve position
                self.trvs.check_feedback_for_unexpected_position(room_id, feedback_percent, now)
                
                # If unexpected position detected, trigger immediate recompute
                if room_id in self.trvs.unexpected_valve_positions:
                    self.trigger_recompute(f"trv_{room_id}_unexpected_position")
                    
            except (ValueError, TypeError):
                self.log(f"Invalid TRV feedback for {entity}: {new}", level="WARNING")

    def trv_setpoint_changed(self, entity, attribute, old, new, kwargs):
        """TRV climate entity setpoint changed (someone changed it manually)."""
        room_id = kwargs.get('room_id')
        if new and new != C.TRV_LOCKED_SETPOINT_C:
            self.log(f"TRV setpoint for '{room_id}' changed to {new}°C (should be locked at {C.TRV_LOCKED_SETPOINT_C}°C), correcting...", level="WARNING")
            self.run_in(lambda kwargs: self.trvs.lock_setpoint(room_id), 1)

    # ========================================================================
    # TRV Setpoint Management
    # ========================================================================

    def lock_all_trv_setpoints(self, kwargs=None):
        """Lock all TRV setpoints to maximum (35°C)."""
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
        self.recompute_all(datetime.now())

    def initial_recompute(self, kwargs):
        """Initial recompute after startup."""
        self.log("Running initial recompute...")
        self.recompute_all(datetime.now())
        self.first_boot = False

    def second_recompute(self, kwargs):
        """Second recompute after startup (for late-restoring sensors)."""
        self.log("Running second recompute (for late-restoring sensors)...")
        self.recompute_all(datetime.now())

    def trigger_recompute(self, reason: str):
        """Trigger an immediate recompute.
        
        Args:
            reason: Description of why recompute was triggered
        """
        self.log(f"Recompute triggered: {reason}")
        self.run_in(lambda kwargs: self.recompute_all(datetime.now()), 0.1)

    def recompute_all(self, now: datetime):
        """Main recompute logic - calculates and applies heating decisions for all rooms.
        
        Args:
            now: Current datetime
        """
        self.recompute_count += 1
        self.last_recompute = now
        
        # Check master enable
        if self.entity_exists(C.HELPER_MASTER_ENABLE):
            master_enable = self.get_state(C.HELPER_MASTER_ENABLE)
            if master_enable != "on":
                self.log("Master enable is OFF, system idle")
                # TODO: Set all valves to 0 and turn off boiler
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
            
            # Set TRV valve
            self.rooms.set_room_valve(room_id, data['valve_percent'], now)
            
            # Publish room entities
            self.status.publish_room_entities(room_id, data, now)
        
        # Update boiler state
        boiler_state, boiler_reason = self.boiler.update_state(any_calling, active_rooms, room_data, now)
        
        # Publish system status
        self.status.publish_system_status(any_calling, active_rooms, room_data, 
                                         boiler_state, boiler_reason, now)
        
        # Log summary
        if any_calling:
            self.log(f"Recompute #{self.recompute_count}: Heating {len(active_rooms)} room(s) - {', '.join(active_rooms)}")
        else:
            self.log(f"Recompute #{self.recompute_count}: System idle", level="DEBUG")
