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
import constants as C


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
        self.system_config = {}  # System-wide configuration
        self.config_file_mtimes = {}  # {filepath: mtime} for change detection
        
    def load_all(self) -> None:
        """Load all configuration files (rooms, schedules, boiler)."""
        # Get the path to the config directory (parent of core directory)
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
            # Extract the 'system' key for system-wide configuration
            self.system_config = boiler_yaml.get('system', {})
        
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
                'delta_t50': room.get('delta_t50'),  # Required for load calculation, validated later
                'radiator_exponent': room.get('radiator_exponent'),  # Optional per-room override
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
            
            # Validate and apply defaults for valve_bands with cascading
            vb = room_cfg['valve_bands']
            room_cfg['valve_bands'] = self._load_valve_bands(room_id, vb)
            
            # Validate and apply defaults for valve_update
            vu = room_cfg['valve_update']
            if 'min_interval_s' not in vu:
                vu['min_interval_s'] = C.VALVE_UPDATE_DEFAULT['min_interval_s']
            
            # Load and validate load_sharing configuration (Phase 0)
            ls_cfg = room.get('load_sharing', {})
            room_cfg['load_sharing'] = {
                'schedule_lookahead_m': ls_cfg.get('schedule_lookahead_m', C.LOAD_SHARING_SCHEDULE_LOOKAHEAD_M_DEFAULT),
                'fallback_priority': ls_cfg.get('fallback_priority', None),  # None = not in fallback list
            }
            
            # Validate sensor timeout_m (must be >= TIMEOUT_MIN_M)
            for sensor in room_cfg['sensors']:
                timeout_m = sensor.get('timeout_m', 180)
                if timeout_m < C.TIMEOUT_MIN_M:
                    raise ValueError(
                        f"Room '{room_id}' sensor '{sensor.get('entity_id', 'unknown')}': "
                        f"timeout_m ({timeout_m}) must be >= {C.TIMEOUT_MIN_M} minute(s)"
                    )
            
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
                'default_mode': room_schedule.get('default_mode', 'active'),
                'default_valve_percent': room_schedule.get('default_valve_percent'),
                'default_min_temp': room_schedule.get('default_min_temp'),
                'week': room_schedule.get('week', {}),
            }
            
            self.ad.log(f"Loaded schedule for room: {room_id}")
        
        # Validate and apply defaults for boiler config
        bc = self.boiler_config
        
        # Validate required fields
        if 'entity_id' not in bc:
            raise ValueError(
                "Boiler configuration error: 'entity_id' is required in boiler.yaml. "
                "This is the climate entity used for boiler control (e.g., climate.opentherm_heating)"
            )
        
        # Apply optional defaults
        bc.setdefault('opentherm', False)
        bc.setdefault('pump_overrun_s', C.BOILER_PUMP_OVERRUN_DEFAULT)
        
        # Anti-cycling defaults (reasonable defaults if not specified)
        anti_cfg = bc.setdefault('anti_cycling', {})
        anti_cfg.setdefault('min_on_time_s', C.BOILER_MIN_ON_TIME_DEFAULT)
        anti_cfg.setdefault('min_off_time_s', C.BOILER_MIN_OFF_TIME_DEFAULT)
        anti_cfg.setdefault('off_delay_s', C.BOILER_OFF_DELAY_DEFAULT)
        
        # Interlock defaults (reasonable default if not specified)
        interlock_cfg = bc.setdefault('interlock', {})
        interlock_cfg.setdefault('min_valve_open_percent', C.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT)
        
        # Load monitoring defaults (for capacity estimation)
        load_cfg = bc.setdefault('load_monitoring', {})
        load_cfg.setdefault('enabled', True)
        load_cfg.setdefault('system_delta_t', C.LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT)
        load_cfg.setdefault('radiator_exponent', C.LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT)
        
        # Load sharing defaults
        # Note: Enable/disable controlled via input_boolean.pyheat_load_sharing_enable in HA
        ls_cfg = bc.setdefault('load_sharing', {})
        ls_cfg.setdefault('min_calling_capacity_w', C.LOAD_SHARING_MIN_CALLING_CAPACITY_W_DEFAULT)
        ls_cfg.setdefault('target_capacity_w', C.LOAD_SHARING_TARGET_CAPACITY_W_DEFAULT)
        ls_cfg.setdefault('min_activation_duration_s', C.LOAD_SHARING_MIN_ACTIVATION_DURATION_S_DEFAULT)
        ls_cfg.setdefault('fallback_timeout_s', C.LOAD_SHARING_FALLBACK_TIMEOUT_S_DEFAULT)
        ls_cfg.setdefault('fallback_cooldown_s', C.LOAD_SHARING_FALLBACK_COOLDOWN_S_DEFAULT)
        
        # Validate fallback cooldown
        if ls_cfg['fallback_cooldown_s'] < 0:
            raise ValueError("load_sharing.fallback_cooldown_s must be >= 0")
        
        if ls_cfg['fallback_cooldown_s'] < ls_cfg['fallback_timeout_s']:
            self.ad.log(
                f"WARNING: fallback_cooldown_s ({ls_cfg['fallback_cooldown_s']}s) is less than "
                f"fallback_timeout_s ({ls_cfg['fallback_timeout_s']}s). "
                f"Rooms may be re-selected quickly after timeout.",
                level="WARNING"
            )
        
        self.ad.log(f"Loaded boiler config: entity_id={bc['entity_id']}")
        
        # Validate and apply defaults for system config
        sc = self.system_config
        
        # Frost protection temperature (with default)
        frost_temp = sc.get('frost_protection_temp_c', C.FROST_PROTECTION_TEMP_C_DEFAULT)
        if not (C.FROST_PROTECTION_TEMP_MIN_C <= frost_temp <= C.FROST_PROTECTION_TEMP_MAX_C):
            raise ValueError(
                f"System configuration error: frost_protection_temp_c must be between "
                f"{C.FROST_PROTECTION_TEMP_MIN_C}C and {C.FROST_PROTECTION_TEMP_MAX_C}C, "
                f"got {frost_temp}C"
            )
        sc['frost_protection_temp_c'] = frost_temp
        
        self.ad.log(f"Loaded system config: frost_protection_temp_c={frost_temp}C")
    
    def _load_valve_bands(self, room_id: str, bands_config: dict) -> dict:
        """Load and validate valve band configuration with cascading defaults.
        
        Supports 0, 1, or 2 thresholds (flexible band structure):
        - 0 thresholds: on/off only (band_0_percent, band_max_percent)
        - 1 threshold: 3 states (band_0, band_1, band_max)
        - 2 thresholds: 4 states (band_0, band_1, band_2, band_max)
        
        Missing percentages cascade to next higher band:
        - band_2_percent missing → uses band_max_percent
        - band_1_percent missing → uses band_2_percent (which may have cascaded)
        - band_0_percent missing → defaults to 0.0 (never cascades)
        - band_max_percent missing → defaults to 100.0
        
        Args:
            room_id: Room identifier for error messages
            bands_config: Raw valve_bands config dict from YAML
            
        Returns:
            Validated and completed valve_bands dict
            
        Raises:
            ValueError: If configuration is invalid
        """
        # Discover which thresholds are defined
        thresholds = {}
        if 'band_1_error' in bands_config:
            thresholds['band_1'] = bands_config['band_1_error']
        if 'band_2_error' in bands_config:
            thresholds['band_2'] = bands_config['band_2_error']
        
        # Check for old naming (migration helper)
        old_keys = ['t_low', 't_mid', 't_max', 'low_percent', 'mid_percent', 'max_percent']
        if any(key in bands_config for key in old_keys):
            raise ValueError(
                f"Room {room_id}: Old valve_bands naming detected. "
                f"Please update config to use band_1_error, band_2_error, "
                f"band_1_percent, band_2_percent, band_max_percent"
            )
        
        # Validate thresholds are positive and ordered
        if thresholds:
            for name, val in thresholds.items():
                if val <= 0:
                    raise ValueError(
                        f"Room {room_id}: {name}_error must be positive, got {val}"
                    )
            
            # If multiple thresholds, ensure ordered
            if len(thresholds) > 1:
                if thresholds['band_2'] <= thresholds['band_1']:
                    raise ValueError(
                        f"Room {room_id}: band_2_error ({thresholds['band_2']}) "
                        f"must be > band_1_error ({thresholds['band_1']})"
                    )
        
        # Check for orphaned percentages (percent defined but no threshold)
        if 'band_1_percent' in bands_config and 'band_1_error' not in bands_config:
            raise ValueError(
                f"Room {room_id}: band_1_percent defined but band_1_error missing"
            )
        
        if 'band_2_percent' in bands_config and 'band_2_error' not in bands_config:
            raise ValueError(
                f"Room {room_id}: band_2_percent defined but band_2_error missing"
            )
        
        # Resolve percentages with cascading defaults
        band_max_pct = bands_config.get('band_max_percent', C.VALVE_BANDS_DEFAULT['band_max_percent'])
        band_2_pct = bands_config.get('band_2_percent', band_max_pct)
        band_1_pct = bands_config.get('band_1_percent', band_2_pct)
        band_0_pct = bands_config.get('band_0_percent', C.VALVE_BANDS_DEFAULT['band_0_percent'])
        
        # Validate percentages are in range [0, 100]
        for name, val in [('band_0_percent', band_0_pct), 
                          ('band_1_percent', band_1_pct),
                          ('band_2_percent', band_2_pct),
                          ('band_max_percent', band_max_pct)]:
            if not 0 <= val <= 100:
                raise ValueError(
                    f"Room {room_id}: {name} ({val}) must be between 0 and 100"
                )
        
        # Log if cascading occurred
        if 'band_1_percent' not in bands_config and 'band_1_error' in bands_config:
            source = 'band_2_percent' if 'band_2_percent' in bands_config else 'band_max_percent'
            self.ad.log(
                f"Room {room_id}: band_1_percent not defined, using {band_1_pct}% "
                f"(cascaded from {source})",
                level="INFO"
            )
        
        if 'band_2_percent' not in bands_config and 'band_2_error' in bands_config:
            self.ad.log(
                f"Room {room_id}: band_2_percent not defined, using {band_2_pct}% "
                f"(cascaded from band_max_percent)",
                level="INFO"
            )
        
        # Get step hysteresis
        step_hyst = bands_config.get('step_hysteresis_c', C.VALVE_BANDS_DEFAULT['step_hysteresis_c'])
        
        # Build complete config with resolved values
        result = {
            'thresholds': thresholds,  # {'band_1': 0.30, 'band_2': 0.80} or subset
            'percentages': {
                0: band_0_pct,
                1: band_1_pct,
                2: band_2_pct,
                'max': band_max_pct
            },
            'step_hysteresis_c': step_hyst,
            'num_bands': len(thresholds)  # 0, 1, or 2
        }
        
        return result
        
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
    
    def get_changed_files(self) -> list:
        """Get list of configuration files that have been modified.
        
        Returns:
            List of file paths that have changed since last check
        """
        changed_files = []
        for filepath, old_mtime in self.config_file_mtimes.items():
            if os.path.exists(filepath):
                new_mtime = os.path.getmtime(filepath)
                if new_mtime != old_mtime:
                    changed_files.append(filepath)
        return changed_files
    
    def reload(self) -> None:
        """Reload all configuration files."""
        self.ad.log("Reloading configuration...")
        self.rooms.clear()
        self.schedules.clear()
        self.boiler_config.clear()
        self.system_config.clear()
        self.load_all()
        self.ad.log("Configuration reloaded successfully")
