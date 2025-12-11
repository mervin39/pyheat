# -*- coding: utf-8 -*-
"""
persistence.py - Local file-based state persistence

Manages persistence of internal state to local JSON file instead of HA entities.
Provides atomic writes with temp file to prevent corruption.
"""

import json
import os
import tempfile
from typing import Any, Dict, Optional


class PersistenceManager:
    """Manages local file-based persistence for PyHeat state.
    
    Replaces HA input_text entities with local JSON file for:
    - Room state (valve positions, calling state, passive valve state)
    - Cycling protection state machine
    
    Benefits over HA entities:
    - No size limits (was 255 chars)
    - Faster I/O (direct file vs API calls)
    - No entity clutter in HA
    - Easier debugging (can inspect file directly)
    """
    
    def __init__(self, file_path: str):
        """Initialize persistence manager.
        
        Args:
            file_path: Absolute path to persistence file
        """
        self.file_path = file_path
        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    def load(self) -> Dict[str, Any]:
        """Load all persistence data from file.
        
        Returns:
            Dictionary with persistence data, empty dict if file doesn't exist
            
        Structure:
            {
                'room_state': {
                    'pete': {'valve_percent': 70, 'last_calling': True, 'passive_valve': 0},
                    ...
                },
                'cycling_protection': {
                    'mode': 'NORMAL',
                    'saved_setpoint': None,
                    'cooldown_start': None
                }
            }
        """
        try:
            if not os.path.exists(self.file_path):
                return {}
            
            with open(self.file_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            # Log error but return empty dict - will use safe defaults
            print(f"ERROR: Failed to load persistence file: {e}")
            return {}
    
    def save(self, data: Dict[str, Any]) -> None:
        """Save all persistence data to file using atomic write.

        Uses temp file + rename for atomicity to prevent corruption
        if write interrupted.

        Args:
            data: Complete persistence data dictionary
        """
        try:
            # Create temp file in same directory for atomic rename
            fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(self.file_path),
                prefix='.persistence_tmp_',
                suffix='.json'
            )

            try:
                # Write to temp file
                with os.fdopen(fd, 'w') as f:
                    json.dump(data, f, separators=(',', ':'))

                # Set permissions to 0o666 (rw-rw-rw-) for easy inspection/debugging
                # Actual permissions will be 0o666 & ~umask
                os.chmod(temp_path, 0o666)

                # Atomic rename
                os.replace(temp_path, self.file_path)
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

        except (IOError, OSError) as e:
            print(f"ERROR: Failed to save persistence file: {e}")
    
    def get_room_state(self, room_id: str) -> Optional[Dict[str, Any]]:
        """Get persistence data for a specific room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Dict with room state or None if not found
            Format: {'valve_percent': int, 'last_calling': bool, 'passive_valve': int}
        """
        data = self.load()
        room_state = data.get('room_state', {})
        return room_state.get(room_id)
    
    def update_room_state(self, room_id: str, **kwargs) -> None:
        """Update specific fields for a room's state.
        
        Args:
            room_id: Room identifier
            **kwargs: Fields to update (valve_percent, last_calling, passive_valve)
        """
        data = self.load()
        
        # Initialize room_state if missing
        if 'room_state' not in data:
            data['room_state'] = {}
        
        # Initialize room if missing
        if room_id not in data['room_state']:
            data['room_state'][room_id] = {
                'valve_percent': 0,
                'last_calling': False,
                'passive_valve': 0
            }
        
        # Update specified fields
        data['room_state'][room_id].update(kwargs)
        
        self.save(data)
    
    def get_cycling_protection_state(self) -> Dict[str, Any]:
        """Get cycling protection state.
        
        Returns:
            Dict with cycling protection state
            Format: {'mode': str, 'saved_setpoint': float|None, 'cooldown_start': str|None, 'cooldowns_count': int}
        """
        data = self.load()
        return data.get('cycling_protection', {
            'mode': 'NORMAL',
            'saved_setpoint': None,
            'cooldown_start': None,
            'cooldowns_count': 0
        })
    
    def update_cycling_protection_state(self, state: Dict[str, Any]) -> None:
        """Update cycling protection state.
        
        Args:
            state: Complete cycling protection state dict
        """
        data = self.load()
        data['cycling_protection'] = state
        self.save(data)
    
    def get_cooldowns_count(self) -> int:
        """Get persisted cooldowns count.
        
        Returns:
            Cooldowns count from persistence (0 if not found)
        """
        state = self.get_cycling_protection_state()
        return state.get('cooldowns_count', 0)
    
    def update_cooldowns_count(self, count: int) -> None:
        """Update persisted cooldowns count.
        
        Args:
            count: New cooldowns count value
        """
        data = self.load()
        if 'cycling_protection' not in data:
            data['cycling_protection'] = {
                'mode': 'NORMAL',
                'saved_setpoint': None,
                'cooldown_start': None,
                'cooldowns_count': count
            }
        else:
            data['cycling_protection']['cooldowns_count'] = count
        self.save(data)
    
    def get_setpoint_ramp_state(self) -> Dict[str, Any]:
        """Get setpoint ramp state.
        
        Returns:
            Dict with setpoint ramp state
            Format: {'baseline_setpoint': float|None, 'current_ramped_setpoint': float|None, 'ramp_steps_applied': int}
        """
        data = self.load()
        return data.get('setpoint_ramp', {
            'baseline_setpoint': None,
            'current_ramped_setpoint': None,
            'ramp_steps_applied': 0
        })
    
    def update_setpoint_ramp_state(self, state: Dict[str, Any]) -> None:
        """Update setpoint ramp state.
        
        Args:
            state: Complete setpoint ramp state dict
        """
        data = self.load()
        data['setpoint_ramp'] = state
        self.save(data)
