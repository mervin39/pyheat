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
from config_loader import ConfigLoader
from sensor_manager import SensorManager
from load_calculator import LoadCalculator
from load_sharing_manager import LoadSharingManager
from load_sharing_state import LoadSharingState
from scheduler import Scheduler
from override_manager import OverrideManager
from room_controller import RoomController
from trv_controller import TRVController
from valve_coordinator import ValveCoordinator
from boiler_controller import BoilerController
from cycling_protection import CyclingProtection
from status_publisher import StatusPublisher
from service_handler import ServiceHandler
from api_handler import APIHandler
from alert_manager import AlertManager
from heating_logger import HeatingLogger
import constants as C


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
        self.alerts = AlertManager(self)  # Initialize alert manager first
        self.sensors = SensorManager(self, self.config)
        self.load_calculator = LoadCalculator(self, self.config, self.sensors)
        self.overrides = OverrideManager(self, self.config)
        self.scheduler = Scheduler(self, self.config, self.overrides)
        self.load_sharing = LoadSharingManager(self, self.config, self.scheduler, self.load_calculator, self.sensors)
        self.trvs = TRVController(self, self.config, self.alerts)
        self.valve_coordinator = ValveCoordinator(self, self.trvs)
        self.valve_coordinator.initialize_from_ha()  # Initialize pump overrun state from HA
        self.rooms = RoomController(self, self.config, self.sensors, self.scheduler, self.trvs)
        self.boiler = BoilerController(self, self.config, self.alerts, self.valve_coordinator, self.trvs)
        self.cycling = CyclingProtection(self, self.config, self.alerts, self.boiler)
        self.status = StatusPublisher(self, self.config)
        self.status.scheduler_ref = self.scheduler  # Allow status publisher to get scheduled temps
        self.status.load_calculator_ref = self.load_calculator  # Allow status publisher to get capacity data
        self.services = ServiceHandler(self, self.config, self.overrides)
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
            # Report critical alert for config failure
            self.alerts.report_error(
                AlertManager.ALERT_CONFIG_LOAD_FAILURE,
                AlertManager.SEVERITY_CRITICAL,
                f"Failed to load PyHeat configuration: {e}\n\nPlease check your YAML files for syntax errors.",
                auto_clear=False  # Requires manual intervention
            )
            return
        
        # Initialize heating logger if enabled (must be after config.load_all())
        self.heating_logger = None
        if C.ENABLE_HEATING_LOGS:
            self.heating_logger = HeatingLogger(self, self.config)
        
        # Initialize cycling protection state from persistence
        self.cycling.initialize_from_ha()
        
        # Sync climate setpoint to helper (unless in cooldown)
        self.cycling.sync_setpoint_on_startup()
        
        # Initialize sensor values from current state
        self.sensors.initialize_from_ha()
        
        # Initialize load calculator (validates delta_t50 configuration)
        try:
            self.load_calculator.initialize_from_ha()
        except ValueError as e:
            self.error(f"LoadCalculator initialization failed: {e}")
            self.log("PyHeat initialization failed - load calculator configuration error")
            # Report critical alert for config failure
            self.alerts.report_error(
                AlertManager.ALERT_CONFIG_LOAD_FAILURE,
                AlertManager.SEVERITY_CRITICAL,
                f"LoadCalculator initialization failed:\n\n{e}",
                auto_clear=False  # Requires manual intervention
            )
            return
        
        # Initialize load sharing manager (Phase 0: disabled by default)
        self.load_sharing.initialize_from_ha()
        
        # Initialize TRV state from current valve positions
        self.trvs.initialize_from_ha()
        
        # Initialize room call-for-heat state from current valve positions (CRITICAL for startup)
        self.rooms.initialize_from_ha()
        
        # Initialize last published temps from current sensor values
        # This prevents false "changed" detection on first sensor update after restart
        now = datetime.now()
        for room_id in self.config.rooms.keys():
            precision = self.config.rooms[room_id].get('precision', 1)
            temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
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
        
        # Check master enable state and apply appropriate startup behavior
        master_enable = self.get_state(C.HELPER_MASTER_ENABLE)
        if master_enable == "off":
            # System is disabled at startup - apply master OFF behavior immediately
            self.log("Master enable is OFF at startup - opening valves and shutting down")
            now = datetime.now()  # Use naive datetime (consistent with rest of code)
            for room_id in self.config.rooms.keys():
                # Force valve to 100% using is_correction=True to bypass rate limiting and change checks
                self.trvs.set_valve(room_id, 100, now, is_correction=True)
                
                # Update status sensor to reflect the 100% valve position
                try:
                    temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
                    room_data_for_status = {
                        'valve_percent': 100,
                        'calling': False,
                        'target': None,
                        'mode': 'off',
                        'temp': temp,
                        'is_stale': is_stale
                    }
                    self.status.publish_room_entities(room_id, room_data_for_status, now)
                except Exception as e:
                    self.log(f"Failed to update status for {room_id}: {e}", level="WARNING")
                
            self.boiler._set_boiler_off()
            # DO NOT lock TRV setpoints (allows manual control)
        else:
            # System is enabled - lock TRV setpoints for normal operation
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
        # Note: master_enable already read during initialization above
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
        
        if self.entity_exists(C.HELPER_LOAD_SHARING_MODE):
            self.listen_state(self.load_sharing_mode_changed, C.HELPER_LOAD_SHARING_MODE)
        
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
            
            # Passive mode setting changes
            passive_max_entity = C.HELPER_ROOM_PASSIVE_MAX_TEMP.format(room=room_id)
            if self.entity_exists(passive_max_entity):
                self.listen_state(self.room_passive_setting_changed, passive_max_entity, room_id=room_id)
            
            passive_valve_entity = C.HELPER_ROOM_PASSIVE_VALVE_PERCENT.format(room=room_id)
            if self.entity_exists(passive_valve_entity):
                self.listen_state(self.room_passive_setting_changed, passive_valve_entity, room_id=room_id)
            
            passive_min_entity = C.HELPER_ROOM_PASSIVE_MIN_TEMP.format(room=room_id)
            if self.entity_exists(passive_min_entity):
                self.listen_state(self.room_passive_setting_changed, passive_min_entity, room_id=room_id)
            
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
        
        # OpenTherm sensor monitoring (debug logging only, no recomputes)
        opentherm_sensors = [
            (C.OPENTHERM_FLAME, "flame"),
            (C.OPENTHERM_HEATING_TEMP, "heating_temp"),
            (C.OPENTHERM_HEATING_RETURN_TEMP, "heating_return_temp"),
            (C.OPENTHERM_HEATING_SETPOINT_TEMP, "heating_setpoint_temp"),
            (C.OPENTHERM_POWER, "power"),
            (C.OPENTHERM_MODULATION, "modulation"),
            (C.OPENTHERM_BURNER_STARTS, "burner_starts"),
            (C.OPENTHERM_DHW_BURNER_STARTS, "dhw_burner_starts"),
            (C.OPENTHERM_DHW, "dhw"),
            (C.OPENTHERM_DHW_FLOW_RATE, "dhw_flow_rate"),
            (C.OPENTHERM_CLIMATE, "climate_state"),
        ]
        
        opentherm_count = 0
        for entity_id, sensor_name in opentherm_sensors:
            if self.entity_exists(entity_id):
                self.listen_state(self.opentherm_sensor_changed, entity_id, sensor_name=sensor_name)
                opentherm_count += 1
        
        if opentherm_count > 0:
            self.log(f"Registered {opentherm_count} OpenTherm sensors for monitoring")
        
        # Flame sensor for cycling protection (triggers cooldown detection)
        # and boiler pump overrun (timer starts from flame off, not command off)
        if self.entity_exists(C.OPENTHERM_FLAME):
            self.listen_state(self.cycling.on_flame_off, C.OPENTHERM_FLAME)
            self.listen_state(self.boiler.on_flame_off, C.OPENTHERM_FLAME)
            self.log("Registered flame sensor for cycling protection and pump overrun")
        
        # DHW sensors for cycling protection history tracking
        dhw_sensor_count = 0
        if self.entity_exists(C.OPENTHERM_DHW):
            self.listen_state(self.cycling.on_dhw_state_change, C.OPENTHERM_DHW)
            dhw_sensor_count += 1
        if self.entity_exists(C.OPENTHERM_DHW_FLOW_RATE):
            self.listen_state(self.cycling.on_dhw_state_change, C.OPENTHERM_DHW_FLOW_RATE)
            dhw_sensor_count += 1
        if dhw_sensor_count > 0:
            self.log(f"Registered {dhw_sensor_count} DHW sensors for cycling protection history tracking")
        
        # Setpoint helper for user control of OpenTherm flow temperature
        if self.entity_exists(C.HELPER_OPENTHERM_SETPOINT):
            self.listen_state(self.cycling.on_setpoint_changed, C.HELPER_OPENTHERM_SETPOINT)
            self.log("Registered OpenTherm setpoint control")
        
        # ====================================================================
        # Timer event listeners (immediate response to timer completion)
        # ====================================================================
        # Note: State polling continues as safety net for missed events
        
        # Room override timers (per-room)
        override_timer_count = 0
        for room_id in self.config.rooms.keys():
            timer_entity = C.HELPER_ROOM_OVERRIDE_TIMER.format(room=room_id)
            if self.entity_exists(timer_entity):
                self.listen_event(self.timer_finished, "timer.finished", entity_id=timer_entity)
                self.listen_event(self.timer_cancelled, "timer.cancelled", entity_id=timer_entity)
                override_timer_count += 1
        
        if override_timer_count > 0:
            self.log(f"Registered timer events for {override_timer_count} room override timers")
        
        # Boiler FSM timers (system-wide)
        boiler_timers = [
            C.HELPER_BOILER_MIN_ON_TIMER,
            C.HELPER_BOILER_MIN_OFF_TIMER,
            C.HELPER_BOILER_OFF_DELAY_TIMER,
            C.HELPER_PUMP_OVERRUN_TIMER
        ]
        
        boiler_timer_count = 0
        for timer_entity in boiler_timers:
            if self.entity_exists(timer_entity):
                self.listen_event(self.timer_finished, "timer.finished", entity_id=timer_entity)
                self.listen_event(self.timer_cancelled, "timer.cancelled", entity_id=timer_entity)
                boiler_timer_count += 1
        
        if boiler_timer_count > 0:
            self.log(f"Registered timer events for {boiler_timer_count} boiler FSM timers")
        
        self.log(f"Registered callbacks for {len(self.config.rooms)} rooms")

    # ========================================================================
    # State Change Callbacks
    # ========================================================================

    def master_enable_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Master enable changed: {old} -> {new}")
        
        if new == "off":
            # System being disabled - open all valves to 100% for safe water circulation
            # This allows manual boiler control and prevents pressure buildup
            self.log("Master enable OFF - opening all valves to 100% and shutting down system")
            now = datetime.now()  # Use naive datetime (consistent with rest of code)
            for room_id in self.config.rooms.keys():
                # Force valve to 100% using is_correction=True to bypass rate limiting and change checks
                self.trvs.set_valve(room_id, 100, now, is_correction=True)
                
                # Update status sensor to reflect the 100% valve position
                try:
                    temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
                    room_data_for_status = {
                        'valve_percent': 100,
                        'calling': False,
                        'target': None,
                        'mode': 'off',
                        'temp': temp,
                        'is_stale': is_stale
                    }
                    self.status.publish_room_entities(room_id, room_data_for_status, now)
                except Exception as e:
                    self.log(f"Failed to update status for {room_id}: {e}", level="WARNING")
            
            # Turn off boiler and reset state machine to prevent desync
            self.boiler._set_boiler_off()
            # CRITICAL: Reset boiler state machine to STATE_OFF to prevent state desync
            # when master enable is turned back on. Without this, the state machine
            # remains in its previous state (e.g., STATE_ON) and won't send turn_on
            # command when master enable is re-enabled.
            self.boiler._transition_to(C.STATE_OFF, now, "master enable disabled")
            # Cancel all boiler timers to fully reset state
            self.boiler._cancel_timer(C.HELPER_BOILER_MIN_ON_TIMER)
            self.boiler._cancel_timer(C.HELPER_BOILER_OFF_DELAY_TIMER)
            self.boiler._cancel_timer(C.HELPER_PUMP_OVERRUN_TIMER)
            self.boiler._cancel_timer(C.HELPER_BOILER_MIN_OFF_TIMER)
            # Don't trigger recompute - system is disabled and recompute would overwrite status
        
        elif new == "on":
            # System being re-enabled - sync setpoint, lock TRVs, and resume normal operation
            self.log("Master enable ON - syncing OpenTherm setpoint, locking TRV setpoints to 35C and resuming operation")
            # Sync OpenTherm setpoint from helper (unless in cooldown)
            self.cycling.sync_setpoint_on_startup()
            self.run_in(self.lock_all_trv_setpoints, 1)
            # Trigger recompute to resume normal heating operation
            self.trigger_recompute("master_enable_changed")

    def holiday_mode_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Holiday mode changed: {old} -> {new}")
        self.trigger_recompute("holiday_mode_changed")

    def load_sharing_mode_changed(self, entity, attribute, old, new, kwargs):
        self.log(f"Load sharing mode changed: {old} -> {new}")
        # If mode changed to Off, treat like master disable
        if new == C.LOAD_SHARING_MODE_OFF:
            if self.load_sharing.context.is_active():
                self.load_sharing._deactivate("mode changed to Off")
            self.load_sharing.context.state = LoadSharingState.DISABLED
            self.valve_coordinator.clear_load_sharing_overrides()
        else:
            # Mode changed while enabled - deactivate current load sharing and re-evaluate
            if self.load_sharing.context.is_active():
                self.load_sharing._deactivate(f"mode changed to {new}")
            # Ensure state is INACTIVE (not DISABLED) so evaluation can proceed
            if self.load_sharing.context.state == LoadSharingState.DISABLED:
                self.load_sharing.context.state = LoadSharingState.INACTIVE
        
        # Trigger recompute to re-evaluate with new mode
        self.trigger_recompute("load_sharing_mode_changed")

    def room_mode_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' mode changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_mode_changed")

    def room_setpoint_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        self.log(f"Room '{room_id}' manual setpoint changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_setpoint_changed")

    def room_passive_setting_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        # Determine which setting changed (max_temp, valve_percent, or min_temp)
        if 'passive_max_temp' in entity:
            setting = 'max_temp'
        elif 'passive_min_temp' in entity:
            setting = 'min_temp'
        else:
            setting = 'valve_percent'
        self.log(f"Room '{room_id}' passive {setting} changed: {old} -> {new}")
        self.trigger_recompute(f"room_{room_id}_passive_{setting}_changed")

    def room_timer_changed(self, entity, attribute, old, new, kwargs):
        room_id = kwargs.get('room_id')
        if old != new:
            if new in ["active", "paused"]:
                self.log(f"Room '{room_id}' override started")
            elif old in ["active", "paused"] and new == "idle":
                self.log(f"Room '{room_id}' override expired")
                # Clear override via override manager
                self.overrides.handle_timer_expired(room_id)
            self.trigger_recompute(f"room_{room_id}_timer_changed")

    def sensor_changed(self, entity, attribute, old, new, kwargs):
        """Temperature sensor state changed.
        
        Optimized to skip recomputes when sensor changes don't affect the
        displayed (precision-rounded) temperature value. This reduces unnecessary
        recomputes by 80-90% when sensors report small fluctuations.
        
        Temperature entity updates happen immediately on every sensor change for
        real-time visibility in Home Assistant, independent of recompute logic.
        
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
                smoothed_temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
                
                # Always update temperature entity immediately (real-time display)
                # This happens BEFORE recompute decision, ensuring instant UI updates
                # and maintaining regular entity history for HA recorder/automations
                self.status.update_room_temperature(room_id, smoothed_temp, is_stale)
                
                # Always recompute if sensors are stale (safety)
                if smoothed_temp is None or is_stale:
                    self.trigger_recompute(f"sensor_{room_id}_changed")
                    return
                
                # Round smoothed temp to display precision for deadband check
                new_rounded = round(smoothed_temp, precision)
                old_rounded = self.last_published_temps.get(room_id)
                
                # Deadband: Only recompute if change exceeds half a display unit
                # Prevents flipping at boundaries (e.g., 17.745C vs 17.755C)
                if old_rounded is not None:
                    deadband = 0.5 * (10 ** -precision)  # 0.05C for precision=1
                    temp_delta = abs(new_rounded - old_rounded)
                    
                    if temp_delta < deadband:
                        # Reduce log noise: only log at DEBUG level, no need for detailed message
                        # Entity was updated above, but recompute is skipped (working as intended)
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
                
                # Check for unexpected valve position (pass persistence_active flag)
                persistence_active = self.valve_coordinator.is_persistence_active()
                self.trvs.check_feedback_for_unexpected_position(room_id, feedback_percent, now, persistence_active)
                
                # If unexpected position detected, trigger immediate recompute
                if room_id in self.trvs.unexpected_valve_positions:
                    self.trigger_recompute(f"trv_{room_id}_unexpected_position")
                    
            except (ValueError, TypeError):
                self.log(f"Invalid TRV feedback for {entity}: {new}", level="WARNING")

    def trv_setpoint_changed(self, entity, attribute, old, new, kwargs):
        """TRV climate entity setpoint changed (someone changed it manually).
        
        Skip correction when master enable is OFF to allow manual control during maintenance.
        """
        # Skip setpoint enforcement when system is disabled
        if self.entity_exists(C.HELPER_MASTER_ENABLE):
            if self.get_state(C.HELPER_MASTER_ENABLE) != "on":
                return
        
        room_id = kwargs.get('room_id')
        if new and new != C.TRV_LOCKED_SETPOINT_C:
            self.log(f"TRV setpoint for '{room_id}' changed to {new}C (should be locked at {C.TRV_LOCKED_SETPOINT_C}C), correcting...", level="WARNING")
            self.run_in(lambda kwargs: self.trvs.lock_setpoint(room_id), 1)

    def opentherm_sensor_changed(self, entity, attribute, old, new, kwargs):
        """OpenTherm sensor changed - log for debugging, no recompute.
        
        These sensors are monitored to understand boiler behavior and prepare
        for future OpenTherm integration features. Changes are logged at DEBUG
        level and do not trigger heating control recomputation.
        """
        sensor_name = kwargs.get('sensor_name', 'unknown')
        
        # Skip logging if value is unknown or unavailable
        if new in ['unknown', 'unavailable', None]:
            return
        
        # Format the log message based on sensor type
        if sensor_name == "flame":
            # Binary sensor - on/off
            self.log(f"OpenTherm [{sensor_name}]: {new}", level="DEBUG")
        elif sensor_name == "dhw":
            # DHW binary sensor
            self.log(f"OpenTherm [{sensor_name}]: {new}", level="DEBUG")
        elif sensor_name in ["burner_starts", "dhw_burner_starts"]:
            # Counter - only log when it changes
            if old != new:
                self.log(f"OpenTherm [{sensor_name}]: {old} -> {new}", level="DEBUG")
        else:
            # Numeric sensors - log with correct units
            try:
                value = float(new)
                if sensor_name == "modulation":
                    self.log(f"OpenTherm [{sensor_name}]: {value}%", level="DEBUG")
                elif sensor_name == "power":
                    self.log(f"OpenTherm [{sensor_name}]: {value}kW", level="DEBUG")
                elif sensor_name == "dhw_flow_rate":
                    self.log(f"OpenTherm [{sensor_name}]: {value}L/min", level="DEBUG")
                else:
                    # Temperature sensors (heating_temp, heating_return_temp, heating_setpoint_temp)
                    self.log(f"OpenTherm [{sensor_name}]: {value}C", level="DEBUG")
            except (ValueError, TypeError):
                self.log(f"OpenTherm [{sensor_name}]: {new}", level="DEBUG")
        
        # Trigger heating log for significant sensor changes
        # (setpoint, modulation, heating temp, return temp, dhw, dhw_flow_rate)
        if self.heating_logger and sensor_name in ['heating_setpoint_temp', 'modulation', 'heating_temp', 'heating_return_temp', 'dhw', 'dhw_flow_rate']:
            try:
                now = datetime.now()
                
                # Get current boiler state and room data
                boiler_state = self.boiler.boiler_state
                
                # Get room data - simplified version for logging only
                room_data = {}
                for room_id in self.config.rooms.keys():
                    data = self.rooms.compute_room(room_id, now)
                    valve_fb = self.trvs.get_valve_feedback(room_id)
                    valve_cmd = self.trvs.get_valve_command(room_id)
                    override_active = self.overrides.is_override_active(room_id)
                    
                    room_data[room_id] = {
                        'temp': data.get('temp'),
                        'target': data.get('target'),
                        'calling': data.get('calling', False),
                        'valve_fb': valve_fb if valve_fb is not None else '',
                        'valve_cmd': valve_cmd if valve_cmd is not None else '',
                        'mode': data.get('mode', 'auto'),
                        'operating_mode': data.get('operating_mode', 'off'),
                        'frost_protection': data.get('frost_protection', False),
                        'passive_min_temp': data.get('passive_min_temp'),
                        'override': override_active,
                    }
                # Let should_log() filter heating_temp/return_temp (only log on whole degree changes)
                # dhw_flow_rate will be filtered by should_log() to detect zero/nonzero transitions
                force_log = sensor_name in ['heating_setpoint_temp', 'modulation', 'dhw']
                self._log_heating_state(f"opentherm_{sensor_name}", boiler_state, room_data, now, force_log=force_log)
            except Exception as e:
                self.log(f"ERROR in OpenTherm logging: {e}", level="ERROR")
                import traceback
                self.log(f"TRACEBACK: {traceback.format_exc()}", level="ERROR")

    # ========================================================================
    # Timer Event Handlers
    # ========================================================================

    def timer_finished(self, event_name, data, kwargs):
        """Timer finished event - triggers immediate recompute.
        
        This provides immediate response to timer expiration. State polling
        in recompute_all() acts as safety net for missed events (e.g., during
        AppDaemon restart).
        
        Args:
            event_name: "timer.finished"
            data: Event data containing entity_id
            kwargs: Additional keyword arguments
        """
        entity_id = data.get('entity_id', 'unknown')
        self.log(f"Timer finished event: {entity_id}", level="DEBUG")
        self.trigger_recompute(f"timer_finished_{entity_id}")

    def timer_cancelled(self, event_name, data, kwargs):
        """Timer cancelled event - triggers immediate recompute.
        
        Timer cancellation (manual or programmatic) means the timer is no
        longer active, which may affect FSM state transitions.
        
        Args:
            event_name: "timer.cancelled"
            data: Event data containing entity_id
            kwargs: Additional keyword arguments
        """
        entity_id = data.get('entity_id', 'unknown')
        self.log(f"Timer cancelled event: {entity_id}", level="DEBUG")
        self.trigger_recompute(f"timer_cancelled_{entity_id}")

    # ========================================================================
    # TRV Setpoint Management
    # ========================================================================

    def lock_all_trv_setpoints(self, kwargs=None):
        """Lock all TRV setpoints to maximum (35C)."""
        self.trvs.lock_all_setpoints()

    def check_trv_setpoints(self, kwargs):
        """Periodic check to ensure TRV setpoints remain locked.
        
        Skip when master enable is OFF to allow manual control during maintenance.
        """
        # Skip setpoint enforcement when system is disabled
        if self.entity_exists(C.HELPER_MASTER_ENABLE):
            if self.get_state(C.HELPER_MASTER_ENABLE) != "on":
                return
        
        self.trvs.check_all_setpoints()

    def check_config_files(self, kwargs):
        """Periodic check for configuration file changes.
        
        Strategy:
        - schedules.yaml only: Hot reload (no service interruption)
        - Other config files: Restart app for clean state
        """
        if self.config.check_for_changes():
            changed_files = self.config.get_changed_files()
            
            # Check if ONLY schedules.yaml changed
            schedules_only = all('schedules.yaml' in f for f in changed_files)
            
            if schedules_only:
                # Safe to hot reload - schedules don't affect callbacks or sensors
                self.log("Schedules changed, hot reloading...")
                self.config.reload()
                self.trigger_recompute("schedules_changed")
            else:
                # Structural changes (rooms, boiler, sensors, etc.) - restart for clean state
                import os
                filenames = ', '.join([os.path.basename(f) for f in changed_files])
                self.log(f"Config files changed ({filenames}), restarting app for clean reload...")
                
                # Use AppDaemon's restart_app() to trigger a clean reload
                # This will re-initialize all components, callbacks, and sensors
                self.restart_app("pyheat")

    # ========================================================================
    # Recompute Logic
    # ========================================================================

    def periodic_recompute(self, kwargs):
        """Periodic recompute callback (runs every minute)."""
        self.recompute_count += 1
        now = datetime.now()
        self.last_recompute = now
        self.log(f"Periodic recompute #{self.recompute_count}", level="DEBUG")
        self.recompute_all(now, "periodic")

    def initial_recompute(self, kwargs):
        """Initial recompute after startup."""
        self.log("Running initial recompute...")
        # Ensure cooldowns sensor exists (delayed from init to avoid HA API errors)
        self.cycling.ensure_cooldowns_sensor()
        self.recompute_all(datetime.now(), "initial")

    def second_recompute(self, kwargs):
        """Second recompute after startup (for late-restoring sensors)."""
        self.log("Running second recompute (for late-restoring sensors)...")
        self.first_boot = False
        self.recompute_all(datetime.now(), "second_boot")

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
        self.recompute_all(now, reason)

    def recompute_all(self, now: datetime, reason: str = "unknown"):
        """Main recompute logic - calculates and applies heating decisions for all rooms.
        
        Args:
            now: Current datetime
            reason: Description of why recompute was triggered (for logging)
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
                # System is disabled - only update temperature sensors for HA automations
                # No heating control, valve commands, or boiler management
                for room_id in self.config.rooms.keys():
                    # Get smoothed fused temperature
                    temp, is_stale = self.sensors.get_room_temperature_smoothed(room_id, now)
                    
                    # Update temperature entity with smoothed value
                    self.status.update_room_temperature(room_id, temp, is_stale)
                
                # System is idle - no further processing
                return
        
        # Periodic validation: Check OpenTherm setpoint matches helper (unless in cooldown)
        self.cycling.validate_setpoint_vs_helper()
        
        # Update load calculator capacities
        self.load_calculator.update_capacities()
        
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
        
        # Update boiler state
        try:
            boiler_state, boiler_reason, persisted_valves, valves_must_stay_open = \
                self.boiler.update_state(any_calling, active_rooms, room_data, now)
        except Exception as e:
            self.log(f"ERROR: Exception in boiler.update_state(): {e}", level="ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            raise
        
        # Evaluate load sharing needs
        cycling_state = self.cycling.state if hasattr(self.cycling, 'state') else 'NORMAL'
        load_sharing_commands = self.load_sharing.evaluate(room_data, boiler_state, cycling_state)
        
        # Apply load sharing overrides to valve coordinator
        if load_sharing_commands:
            self.valve_coordinator.set_load_sharing_overrides(load_sharing_commands)
        else:
            # Bug #6 Fix: Explicitly close valves from deactivated load sharing rooms
            # This prevents valve persistence when pump overrun captures stale positions
            rooms_to_close = getattr(self.load_sharing, 'last_deactivated_rooms', [])
            if rooms_to_close:
                closed_rooms = []
                for room_id in rooms_to_close:
                    # Only close if room is not naturally calling for heat
                    if room_id in room_data and not room_data[room_id]['calling']:
                        # Force immediate closure by updating current_commands
                        self.valve_coordinator.current_commands[room_id] = 0
                        closed_rooms.append(room_id)
                
                if closed_rooms:
                    self.log(
                        f"Load sharing deactivation: Explicitly closed valves for {closed_rooms}",
                        level="INFO"
                    )
                
                # Clear for next time
                self.load_sharing.last_deactivated_rooms = []
            
            self.valve_coordinator.clear_load_sharing_overrides()
        
        # Build load-sharing info map for each room (for state string publishing)
        load_sharing_info_map = {}
        if hasattr(self, 'load_sharing') and self.load_sharing:
            for room_id, activation in self.load_sharing.context.active_rooms.items():
                load_sharing_info_map[room_id] = {
                    'active': True,
                    'tier': activation.tier,
                    'reason': activation.reason
                }
        
        # Apply all valve commands through valve coordinator
        # The coordinator handles persistence overrides, load sharing, corrections, and normal commands
        for room_id in self.config.rooms.keys():
            data = room_data[room_id]
            desired_valve = data['valve_percent']
            
            # Coordinator applies all overrides and sends final command
            final_valve = self.valve_coordinator.apply_valve_command(room_id, desired_valve, now)
            
            # Update room_data with final valve for status publishing
            data['valve_percent'] = final_valve
            
            # Get load-sharing info for this room (if any)
            ls_info = load_sharing_info_map.get(room_id)
            
            # Publish room entities with load-sharing info
            self.status.publish_room_entities(room_id, data, now, load_sharing_info=ls_info)
        
        # Publish boiler state entity (for reliable graph shading history)
        self.status.publish_boiler_state(boiler_state)
        
        # Publish system status
        self.status.publish_system_status(any_calling, active_rooms, room_data, 
                                         boiler_state, boiler_reason, now)
        
        # Log to heating logs if enabled
        if self.heating_logger:
            try:
                self.log(f"HeatingLogger: Calling _log_heating_state (trigger={reason})", level="DEBUG")
                self._log_heating_state(reason, boiler_state, room_data, now)
                self.log("HeatingLogger: _log_heating_state completed", level="DEBUG")
            except Exception as e:
                self.log(f"HeatingLogger ERROR: {e}", level="ERROR")
                import traceback
                self.log(f"HeatingLogger TRACEBACK: {traceback.format_exc()}", level="ERROR")
        else:
            self.log("HeatingLogger: Logger is None, skipping", level="DEBUG")
        
        # Log summary
        if any_calling:
            self.log(f"Recompute #{self.recompute_count}: Heating {len(active_rooms)} room(s) - {', '.join(active_rooms)}")
        else:
            self.log(f"Recompute #{self.recompute_count}: System idle", level="DEBUG")

    def _get_opentherm_data(self) -> Dict:
        """Gather current OpenTherm sensor data.
        
        Returns:
            Dict with OpenTherm sensor values
        """
        data = {}
        
        # Helper to safely get state
        def safe_get(entity_id, default=''):
            if self.entity_exists(entity_id):
                state = self.get_state(entity_id)
                if state not in ['unknown', 'unavailable', None]:
                    return state
            return default
        
        data['flame'] = safe_get(C.OPENTHERM_FLAME)
        data['heating_temp'] = safe_get(C.OPENTHERM_HEATING_TEMP)
        data['return_temp'] = safe_get(C.OPENTHERM_HEATING_RETURN_TEMP)
        data['setpoint_temp'] = safe_get(C.OPENTHERM_HEATING_SETPOINT_TEMP)
        data['power'] = safe_get(C.OPENTHERM_POWER)
        data['modulation'] = safe_get(C.OPENTHERM_MODULATION)
        data['burner_starts'] = safe_get(C.OPENTHERM_BURNER_STARTS)
        data['dhw_burner_starts'] = safe_get(C.OPENTHERM_DHW_BURNER_STARTS)
        data['dhw'] = safe_get(C.OPENTHERM_DHW)
        data['dhw_flow_rate'] = safe_get(C.OPENTHERM_DHW_FLOW_RATE)
        data['climate_state'] = safe_get(C.OPENTHERM_CLIMATE)
        
        return data
    
    def _log_heating_state(self, trigger: str, boiler_state: str, room_data: Dict, now: datetime, force_log: bool = False):
        """Log current heating system state to CSV.
        
        Args:
            trigger: What triggered this recompute
            boiler_state: Current boiler FSM state
            room_data: Room states from compute_room()
            now: Current datetime
            force_log: If True, bypass should_log() filtering (for direct sensor triggers)
        """
        # Get OpenTherm data
        opentherm_data = self._get_opentherm_data()
        
        # Check if pump overrun is active
        pump_overrun_active = boiler_state == C.STATE_PUMP_OVERRUN
        
        # Build room data with additional fields for logging
        log_room_data = {}
        for room_id, data in room_data.items():
            # Get valve feedback
            valve_fb = self.trvs.get_valve_feedback(room_id)
            valve_cmd = self.trvs.get_valve_command(room_id)
            
            # Get override status
            override_active = self.overrides.is_override_active(room_id)
            
            log_room_data[room_id] = {
                'temp': data.get('temp'),
                'target': data.get('target'),
                'calling': data.get('calling', False),
                'valve_fb': valve_fb if valve_fb is not None else '',
                'valve_cmd': valve_cmd if valve_cmd is not None else '',
                'mode': data.get('mode', 'auto'),
                'operating_mode': data.get('operating_mode', 'off'),
                'frost_protection': data.get('frost_protection', False),
                'passive_min_temp': data.get('passive_min_temp'),
                'override': override_active,
            }
        
        # Calculate total valve opening
        total_valve_pct = sum(
            self.trvs.get_valve_feedback(room_id) or 0
            for room_id in self.config.rooms.keys()
        )
        
        # Check if we should log (significant changes only)
        if force_log or self.heating_logger.should_log(opentherm_data, boiler_state, log_room_data, self.load_sharing.get_status()):
            # Get cycling protection state for logging
            cycling_data = self.cycling.get_state_dict()
            
            # Get load calculator data for logging
            load_data = None
            if self.load_calculator.enabled:
                load_data = {
                    'total_estimated_capacity': self.load_calculator.get_total_estimated_capacity(),
                    'estimated_capacities': self.load_calculator.estimated_capacities.copy()
                }
            
            # Get load sharing data for logging
            load_sharing_data = self.load_sharing.get_status()
            
            self.heating_logger.log_state(
                trigger=trigger,
                opentherm_data=opentherm_data,
                boiler_state=boiler_state,
                pump_overrun_active=pump_overrun_active,
                room_data=log_room_data,
                total_valve_pct=total_valve_pct,
                cycling_data=cycling_data,
                load_data=load_data,
                load_sharing_data=load_sharing_data
            )

