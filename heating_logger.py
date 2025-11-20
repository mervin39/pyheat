# -*- coding: utf-8 -*-
"""
heating_logger.py - Comprehensive heating system state logging to CSV files

Responsibilities:
- Log complete heating system state to daily CSV files
- Monitor OpenTherm sensors, boiler state, room states
- Write to heating_logs/ directory (gitignored)
- Automatic daily file rotation at midnight
- Easy to remove once data collection is complete

NOTE: This is a temporary data collection module and will be removed
once we have sufficient data to develop OpenTherm optimization algorithms.
"""

import os
import csv
from datetime import datetime
from typing import Dict, Any, Optional


class HeatingLogger:
    """Logs comprehensive heating system state to CSV files for analysis."""
    
    def __init__(self, ad, config):
        """Initialize the heating logger.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
        """
        self.ad = ad
        self.config = config
        self.log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heating_logs")
        self.current_date = None
        self.csv_file = None
        self.csv_writer = None
        
        # Cache previous values to detect significant changes
        self.prev_heating_temp_rounded = None
        self.prev_return_temp_rounded = None
        self.prev_state = {}
        
        # Setup log directory and .gitignore
        self._setup_log_directory()
        
        # Get room IDs for column headers
        self.room_ids = sorted(list(config.rooms.keys()))
        
        self.ad.log(f"HeatingLogger initialized - logging to heating_logs/ ({len(self.room_ids)} rooms: {', '.join(self.room_ids)})")
    
    def _setup_log_directory(self):
        """Create log directory and .gitignore file."""
        # Create directory if it doesn't exist
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            self.ad.log(f"Created heating_logs directory: {self.log_dir}")
        
        # Create .gitignore to exclude log files
        gitignore_path = os.path.join(self.log_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, 'w') as f:
                f.write("# Ignore all log files\n")
                f.write("*.csv\n")
                f.write("*.jsonl\n")
            self.ad.log("Created .gitignore in heating_logs/")
    
    def _get_csv_headers(self):
        """Generate CSV header row based on configured rooms."""
        headers = [
            # Timestamp and metadata
            'date',
            'time',
            'trigger',
            
            # OpenTherm sensors
            'ot_flame',
            'ot_heating_temp',
            'ot_return_temp',
            'ot_modulation',
            'ot_power',
            'ot_burner_starts',
            'ot_dhw_burner_starts',
            'ot_climate_state',
            'ot_setpoint_temp',
            
            # Boiler state
            'boiler_state',
            'pump_overrun_active',
            
            # System aggregates
            'num_rooms_calling',
            'total_valve_pct',
        ]
        
        # Add per-room columns grouped by property type
        for room_id in self.room_ids:
            headers.append(f'{room_id}_temp')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_target')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_calling')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_valve_fb')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_valve_cmd')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_mode')
        for room_id in self.room_ids:
            headers.append(f'{room_id}_override')
        
        return headers
    
    def _check_date_rotation(self):
        """Check if we need to rotate to a new day's log file."""
        today = datetime.now().date()
        filename = f"{today.isoformat()}.csv"
        filepath = os.path.join(self.log_dir, filename)
        
        # Check if we need to open/create a file
        # (date changed, or file doesn't exist, or file handle not open)
        needs_file = (
            self.current_date != today or 
            not os.path.exists(filepath) or 
            self.csv_file is None
        )
        
        if needs_file:
            # Close existing file if open
            if self.csv_file:
                self.csv_file.close()
            
            # Open new file for today
            self.current_date = today
            
            # Check if file exists (append) or is new (write headers)
            file_exists = os.path.exists(filepath)
            
            self.csv_file = open(filepath, 'a', newline='')
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=self._get_csv_headers())
            
            # Write header if new file
            if not file_exists:
                self.csv_writer.writeheader()
                self.csv_file.flush()
                self.ad.log(f"Started new heating log: {filename}")
    
    def should_log(self, opentherm_data: Dict, boiler_state: str, room_data: Dict) -> bool:
        """Determine if current state warrants a log entry.
        
        Checks for significant changes in:
        - First run (baseline)
        - Boiler state changes
        - Flame status changes
        - Heating/return temps (rounded to nearest degree)
        - Room calling status changes
        - Valve feedback changes
        - Mode/override changes
        
        Args:
            opentherm_data: Current OpenTherm sensor values
            boiler_state: Current boiler FSM state
            room_data: Current room states
            
        Returns:
            True if state has changed significantly and should be logged
        """
        # First run - always log to establish baseline
        if not self.prev_state:
            self.ad.log("HeatingLogger: should_log=True (first run - establishing baseline)", level="DEBUG")
            return True
        
        # Always log on boiler state change
        prev_boiler = self.prev_state.get('boiler_state')
        if prev_boiler != boiler_state:
            self.ad.log(f"HeatingLogger: should_log=True (boiler state: {prev_boiler} -> {boiler_state})", level="DEBUG")
            return True
        
        # Check flame status change
        prev_flame = self.prev_state.get('ot_flame')
        curr_flame = opentherm_data.get('flame')
        if prev_flame != curr_flame:
            self.ad.log(f"HeatingLogger: should_log=True (flame: {prev_flame} -> {curr_flame})", level="DEBUG")
            return True
        
        # Check heating temp (rounded to nearest degree)
        heating_temp = opentherm_data.get('heating_temp')
        if heating_temp not in [None, '', 'unknown', 'unavailable']:
            try:
                heating_temp_rounded = round(float(heating_temp))
                if self.prev_heating_temp_rounded != heating_temp_rounded:
                    self.ad.log(f"HeatingLogger: should_log=True (heating_temp: {self.prev_heating_temp_rounded} -> {heating_temp_rounded})", level="DEBUG")
                    self.prev_heating_temp_rounded = heating_temp_rounded
                    return True
            except (ValueError, TypeError):
                pass
        
        # Check return temp (rounded to nearest degree)
        return_temp = opentherm_data.get('return_temp')
        if return_temp not in [None, '', 'unknown', 'unavailable']:
            try:
                return_temp_rounded = round(float(return_temp))
                if self.prev_return_temp_rounded != return_temp_rounded:
                    self.ad.log(f"HeatingLogger: should_log=True (return_temp: {self.prev_return_temp_rounded} -> {return_temp_rounded})", level="DEBUG")
                    self.prev_return_temp_rounded = return_temp_rounded
                    return True
            except (ValueError, TypeError):
                pass
        
        # Check setpoint temp (any change - this is a manual control input)
        prev_setpoint = self.prev_state.get('ot_setpoint_temp')
        curr_setpoint = opentherm_data.get('setpoint_temp')
        if prev_setpoint != curr_setpoint:
            self.ad.log(f"HeatingLogger: should_log=True (setpoint_temp: {prev_setpoint} -> {curr_setpoint})", level="DEBUG")
            return True
        
        # Check for room calling status changes
        for room_id in self.room_ids:
            room = room_data.get(room_id, {})
            prev_room = self.prev_state.get('rooms', {}).get(room_id, {})
            
            if room.get('calling') != prev_room.get('calling'):
                self.ad.log(f"HeatingLogger: should_log=True ({room_id} calling: {prev_room.get('calling')} -> {room.get('calling')})", level="DEBUG")
                return True
            
            if room.get('valve_fb') != prev_room.get('valve_fb'):
                self.ad.log(f"HeatingLogger: should_log=True ({room_id} valve_fb: {prev_room.get('valve_fb')} -> {room.get('valve_fb')})", level="DEBUG")
                return True
            
            if room.get('mode') != prev_room.get('mode'):
                self.ad.log(f"HeatingLogger: should_log=True ({room_id} mode: {prev_room.get('mode')} -> {room.get('mode')})", level="DEBUG")
                return True
            
            if room.get('override') != prev_room.get('override'):
                self.ad.log(f"HeatingLogger: should_log=True ({room_id} override: {prev_room.get('override')} -> {room.get('override')})", level="DEBUG")
                return True
        
        # No significant changes
        return False
    
    def log_state(self, trigger: str, opentherm_data: Dict, boiler_state: str, 
                  pump_overrun_active: bool, room_data: Dict, total_valve_pct: int):
        """Log current heating system state to CSV.
        
        Args:
            trigger: What triggered this log entry (e.g., "boiler_state_change", "flame_on")
            opentherm_data: Dict with OpenTherm sensor values
            boiler_state: Current boiler FSM state
            pump_overrun_active: Whether pump overrun is active
            room_data: Dict of room states {room_id: room_dict}
            total_valve_pct: Total valve opening percentage
        """
        # Check date rotation
        self._check_date_rotation()
        
        # Helper function to round temps to 2dp
        def round_temp(val):
            if val in [None, '', 'unknown', 'unavailable']:
                return ''
            try:
                return round(float(val), 2)
            except (ValueError, TypeError):
                return val
        
        # Helper function for OpenTherm flow/return temps (integer only)
        def round_temp_int(val):
            if val in [None, '', 'unknown', 'unavailable']:
                return ''
            try:
                return int(round(float(val)))
            except (ValueError, TypeError):
                return val
        
        # Get current datetime
        now = datetime.now()
        
        # Build row data
        row = {
            # Timestamp (separate date and time)
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M:%S'),
            'trigger': trigger,
            
            # OpenTherm sensors (flow/return temps as integers, others rounded to 2dp)
            'ot_flame': opentherm_data.get('flame', ''),
            'ot_heating_temp': round_temp_int(opentherm_data.get('heating_temp', '')),
            'ot_return_temp': round_temp_int(opentherm_data.get('return_temp', '')),
            'ot_modulation': opentherm_data.get('modulation', ''),
            'ot_power': opentherm_data.get('power', ''),
            'ot_burner_starts': opentherm_data.get('burner_starts', ''),
            'ot_dhw_burner_starts': opentherm_data.get('dhw_burner_starts', ''),
            'ot_climate_state': opentherm_data.get('climate_state', ''),
            'ot_setpoint_temp': round_temp(opentherm_data.get('setpoint_temp', '')),
            
            # Boiler state
            'boiler_state': boiler_state,
            'pump_overrun_active': pump_overrun_active,
            
            # System aggregates
            'num_rooms_calling': sum(1 for r in room_data.values() if r.get('calling', False)),
            'total_valve_pct': total_valve_pct,
        }
        
        # Add per-room data (round temps to 2dp)
        for room_id in self.room_ids:
            room = room_data.get(room_id, {})
            row[f'{room_id}_temp'] = round_temp(room.get('temp', ''))
            row[f'{room_id}_target'] = round_temp(room.get('target', ''))
            row[f'{room_id}_calling'] = room.get('calling', '')
            row[f'{room_id}_valve_fb'] = room.get('valve_fb', '')
            row[f'{room_id}_valve_cmd'] = room.get('valve_cmd', '')
            row[f'{room_id}_mode'] = room.get('mode', '')
            row[f'{room_id}_override'] = room.get('override', '')
        
        # Write row
        self.csv_writer.writerow(row)
        self.csv_file.flush()  # Ensure it's written immediately
        
        # Update previous state cache
        self.prev_state = {
            'boiler_state': boiler_state,
            'ot_flame': opentherm_data.get('flame'),
            'ot_setpoint_temp': opentherm_data.get('setpoint_temp'),
            'rooms': {
                room_id: {
                    'calling': room.get('calling'),
                    'valve_fb': room.get('valve_fb'),
                    'mode': room.get('mode'),
                    'override': room.get('override'),
                }
                for room_id, room in room_data.items()
            }
        }
    
    def close(self):
        """Close the current log file."""
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
            self.csv_writer = None
