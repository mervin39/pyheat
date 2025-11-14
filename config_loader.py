# -*- coding: utf-8 -*-
"""
config_loader.py - Configuration loading and validation for PyHeat

Responsibilities:
- Load rooms.yaml, schedules.yaml, and boiler.yaml
- Validate configuration data
- Monitor config files for changes
- Provide structured access to configuration
"""

import os
import yaml
from typing import Dict, Any, Optional
import pyheat.constants as C


class ConfigLoader:
    """Handles loading and monitoring of PyHeat configuration files."""
    
    def __init__(self, ad):
        """Initialize the config loader.
        
        Args:
            ad: AppDaemon API reference
        """
        self.ad = ad
        self.rooms = {}  # Room registry: {room_id: room_data}
        self.schedules = {}  # Schedules: {room_id: schedule_data}
        self.boiler_config = {}  # Boiler configuration
        self.config_file_mtimes = {}  # {filepath: mtime} for change detection
        
    def load_all(self) -> None:
        """Load all configuration files (rooms, schedules, boiler)."""
        # Get the path to the config directory
        app_dir = os.path.dirname(os.path.abspath(__file__))
        config_dir = os.path.join(app_dir, "config")
        
        rooms_file = os.path.join(config_dir, "rooms.yaml")
        schedules_file = os.path.join(config_dir, "schedules.yaml")
        boiler_file = os.path.join(config_dir, "boiler.yaml")
        
        # Store modification times for file monitoring
        for filepath in [rooms_file, schedules_file, boiler_file]:
            if os.path.exists(filepath):
                self.config_file_mtimes[filepath] = os.path.getmtime(filepath)
        
        # Load rooms
        with open(rooms_file, 'r') as f:
            rooms_data = yaml.safe_load(f) or {}
        
        # Load schedules  
        with open(schedules_file, 'r') as f:
            schedules_data = yaml.safe_load(f) or {}
        
        # Load boiler
        with open(boiler_file, 'r') as f:
            boiler_yaml = yaml.safe_load(f) or {}
            # Extract the 'boiler' key from the YAML structure
            self.boiler_config = boiler_yaml.get('boiler', {})
        
        # Process rooms
        for room in rooms_data.get('rooms', []):
            room_id = room['id']
            
            # Derive TRV entity IDs from climate entity
            trv_base = room['trv']['entity_id'].replace('climate.', '')
            
            # Build full room config
            room_cfg = {
                'id': room_id,
                'name': room.get('name', room_id.capitalize()),
                'precision': room.get('precision', 1),
                'smoothing': room.get('smoothing', {}),  # Optional temperature smoothing config
                'sensors': room.get('sensors', []),
                'trv': {
                    'entity_id': room['trv']['entity_id'],
                    'cmd_valve': C.TRV_ENTITY_PATTERNS['cmd_valve'].format(trv_base=trv_base),
                    'fb_valve': C.TRV_ENTITY_PATTERNS['fb_valve'].format(trv_base=trv_base),
                    'climate': C.TRV_ENTITY_PATTERNS['climate'].format(trv_base=trv_base),
                },
                'hysteresis': room.get('hysteresis', C.HYSTERESIS_DEFAULT.copy()),
                'valve_bands': room.get('valve_bands', C.VALVE_BANDS_DEFAULT.copy()),
                'valve_update': room.get('valve_update', C.VALVE_UPDATE_DEFAULT.copy()),
            }
            
            # Validate and apply defaults for hysteresis
            h = room_cfg['hysteresis']
            if 'on_delta_c' not in h:
                h['on_delta_c'] = C.HYSTERESIS_DEFAULT['on_delta_c']
            if 'off_delta_c' not in h:
                h['off_delta_c'] = C.HYSTERESIS_DEFAULT['off_delta_c']
            
            # Validate and apply defaults for valve_bands
            vb = room_cfg['valve_bands']
            for key, default_val in C.VALVE_BANDS_DEFAULT.items():
                if key not in vb:
                    vb[key] = default_val
            
            # Validate and apply defaults for valve_update
            vu = room_cfg['valve_update']
            if 'min_interval_s' not in vu:
                vu['min_interval_s'] = C.VALVE_UPDATE_DEFAULT['min_interval_s']
            
            self.rooms[room_id] = room_cfg
            
            self.ad.log(f"Loaded room config: {room_id} ({room_cfg['name']})")
        
        # Process schedules
        for room_schedule in schedules_data.get('rooms', []):
            room_id = room_schedule['id']
            if room_id not in self.rooms:
                self.ad.log(f"Warning: Schedule defined for unknown room '{room_id}'", level="WARNING")
                continue
            
            self.schedules[room_id] = {
                'default_target': room_schedule.get('default_target', 16.0),
                'week': room_schedule.get('week', {}),
            }
            
            self.ad.log(f"Loaded schedule for room: {room_id}")
        
        # Store boiler config with defaults
        bc = self.boiler_config
        
        # Top-level defaults
        bc.setdefault('entity_id', 'climate.boiler')
        bc.setdefault('opentherm', False)
        bc.setdefault('pump_overrun_s', C.BOILER_PUMP_OVERRUN_DEFAULT)
        
        # Binary control defaults
        binary_cfg = bc.setdefault('binary_control', {})
        binary_cfg.setdefault('on_setpoint_c', C.BOILER_BINARY_ON_SETPOINT_DEFAULT)
        binary_cfg.setdefault('off_setpoint_c', C.BOILER_BINARY_OFF_SETPOINT_DEFAULT)
        
        # Anti-cycling defaults
        anti_cfg = bc.setdefault('anti_cycling', {})
        anti_cfg.setdefault('min_on_time_s', C.BOILER_MIN_ON_TIME_DEFAULT)
        anti_cfg.setdefault('min_off_time_s', C.BOILER_MIN_OFF_TIME_DEFAULT)
        anti_cfg.setdefault('off_delay_s', C.BOILER_OFF_DELAY_DEFAULT)
        
        # Interlock defaults
        interlock_cfg = bc.setdefault('interlock', {})
        interlock_cfg.setdefault('min_valve_open_percent', C.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT)
        
        self.ad.log(f"Loaded boiler config")
        
    def check_for_changes(self) -> bool:
        """Check if any configuration files have been modified.
        
        Returns:
            True if any config file has changed, False otherwise
        """
        changed = False
        for filepath, old_mtime in self.config_file_mtimes.items():
            if os.path.exists(filepath):
                new_mtime = os.path.getmtime(filepath)
                if new_mtime != old_mtime:
                    self.ad.log(f"Config file changed: {filepath}", level="INFO")
                    changed = True
        return changed
    
    def reload(self) -> None:
        """Reload all configuration files."""
        self.ad.log("Reloading configuration...")
        self.rooms.clear()
        self.schedules.clear()
        self.boiler_config.clear()
        self.load_all()
        self.ad.log("Configuration reloaded successfully")
