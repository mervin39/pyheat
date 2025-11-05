#!/usr/bin/env python3
"""
PyHeat Debug Monitor
Monitors key entities and logs all their states whenever ANY of them changes.
Useful for debugging interlock, short-cycling protection, and timing behavior.

Usage:
    ./debug_monitor.py [output_file]
    
If output_file is not specified, logs to debug_monitor.log in the same directory.
Press Ctrl+C to stop monitoring.
"""

import requests
import time
import sys
from datetime import datetime
from pathlib import Path

# Load Home Assistant connection details from .env file
def load_env():
    """Load environment variables from /opt/appdata/hass/homeassistant/.env.hass"""
    env_file = Path("/opt/appdata/hass/homeassistant/.env.hass")
    if not env_file.exists():
        print(f"ERROR: {env_file} not found")
        sys.exit(1)
    
    env_vars = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                # Remove 'export ' prefix if present
                if line.startswith('export '):
                    line = line[7:]
                key, value = line.split('=', 1)
                env_vars[key] = value.strip('"').strip("'")
    
    return env_vars

# Entities to monitor
ROOMS = ['pete', 'games', 'lounge', 'abby', 'office', 'bathroom']
MONITORED_ENTITIES = [
    # TRV valve positions (actual hardware state)
    *[f"sensor.trv_{room}_valve_opening_degree_z2m" for room in ROOMS],
    # PyHeat calling for heat status
    *[f"binary_sensor.pyheat_{room}_calling_for_heat" for room in ROOMS],
    # Boiler timers
    "timer.pyheat_boiler_min_off_timer",
    "timer.pyheat_boiler_min_on_timer",
    "timer.pyheat_boiler_off_delay_timer",
    "timer.pyheat_boiler_pump_overrun_timer",
    # Boiler state
    "climate.dummy",
]

class HAMonitor:
    def __init__(self, base_url: str, token: str, output_file: str):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.output_file = output_file
        self.last_states = {}
        
    def get_state(self, entity_id: str) -> dict:
        """Get current state of an entity"""
        url = f"{self.base_url}/api/states/{entity_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return None
            else:
                print(f"ERROR: HTTP {resp.status_code} for {entity_id}")
                return None
        except Exception as e:
            print(f"ERROR: Failed to get {entity_id}: {e}")
            return None
    
    def format_state(self, entity_id: str, state_data: dict) -> str:
        """Format entity state for logging"""
        if state_data is None:
            return f"{entity_id:60s} = NOT FOUND"
        
        state = state_data.get('state', 'unknown')
        
        # For timers, include remaining time attribute if active
        if entity_id.startswith('timer.'):
            attrs = state_data.get('attributes', {})
            if state == 'active':
                remaining = attrs.get('remaining', 'unknown')
                duration = attrs.get('duration', 'unknown')
                return f"{entity_id:60s} = {state:12s} (remaining: {remaining}, duration: {duration})"
            else:
                return f"{entity_id:60s} = {state:12s}"
        
        # For climate, include hvac_action
        if entity_id.startswith('climate.'):
            attrs = state_data.get('attributes', {})
            hvac_action = attrs.get('hvac_action', 'unknown')
            return f"{entity_id:60s} = {state:12s} (action: {hvac_action})"
        
        # For binary_sensors, show on/off
        if entity_id.startswith('binary_sensor.'):
            return f"{entity_id:60s} = {state:12s}"
        
        # For sensors (valve positions), show value with unit
        attrs = state_data.get('attributes', {})
        unit = attrs.get('unit_of_measurement', '')
        if unit:
            return f"{entity_id:60s} = {state:>6s}{unit}"
        else:
            return f"{entity_id:60s} = {state}"
    
    def log_all_states(self, reason: str = ""):
        """Log all monitored entities"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Get all current states
        states = {}
        for entity_id in MONITORED_ENTITIES:
            state_data = self.get_state(entity_id)
            states[entity_id] = state_data
        
        # Write to log file
        with open(self.output_file, 'a') as f:
            separator = "=" * 100
            f.write(f"\n{separator}\n")
            f.write(f"[{timestamp}] {reason}\n")
            f.write(f"{separator}\n")
            
            # Group by category
            f.write("\n### TRV Valve Positions ###\n")
            for entity_id in MONITORED_ENTITIES:
                if entity_id.startswith('sensor.trv_'):
                    f.write(f"  {self.format_state(entity_id, states[entity_id])}\n")
            
            f.write("\n### PyHeat Calling for Heat ###\n")
            for entity_id in MONITORED_ENTITIES:
                if entity_id.startswith('binary_sensor.pyheat_'):
                    f.write(f"  {self.format_state(entity_id, states[entity_id])}\n")
            
            f.write("\n### Boiler Timers ###\n")
            for entity_id in MONITORED_ENTITIES:
                if entity_id.startswith('timer.'):
                    f.write(f"  {self.format_state(entity_id, states[entity_id])}\n")
            
            f.write("\n### Boiler State ###\n")
            for entity_id in MONITORED_ENTITIES:
                if entity_id.startswith('climate.'):
                    f.write(f"  {self.format_state(entity_id, states[entity_id])}\n")
            
            f.write("\n")
        
        # Also print to console
        print(f"[{timestamp}] {reason}")
        
        return states
    
    def monitor_loop(self):
        """Main monitoring loop - check for changes every second"""
        # Initial log
        print(f"Starting monitor, logging to {self.output_file}")
        print(f"Monitoring {len(MONITORED_ENTITIES)} entities...")
        print("Press Ctrl+C to stop\n")
        
        self.last_states = self.log_all_states("INITIAL STATE")
        
        # Monitor for changes
        while True:
            time.sleep(1)
            
            # Check each entity for changes
            changed = False
            changed_entities = []
            
            for entity_id in MONITORED_ENTITIES:
                current_state = self.get_state(entity_id)
                
                # Compare with last known state
                last_state = self.last_states.get(entity_id)
                
                if current_state != last_state:
                    changed = True
                    changed_entities.append(entity_id)
                    self.last_states[entity_id] = current_state
            
            # If anything changed, log everything
            if changed:
                reason = f"CHANGE DETECTED: {', '.join(changed_entities)}"
                self.log_all_states(reason)

def main():
    # Load environment
    env = load_env()
    base_url = env.get('HA_BASE_URL_LOCAL') or env.get('HA_BASE_URL')
    token = env.get('HA_TOKEN')
    
    if not base_url or not token:
        print(f"ERROR: HA_BASE_URL_LOCAL/HA_BASE_URL and HA_TOKEN must be set in .env file")
        print(f"Found: base_url={base_url}, token={'<set>' if token else '<not set>'}")
        sys.exit(1)
    
    # Determine output file
    if len(sys.argv) > 1:
        output_file = sys.argv[1]
    else:
        script_dir = Path(__file__).parent
        output_file = script_dir / "debug_monitor.log"
    
    # Clear existing log file
    with open(output_file, 'w') as f:
        f.write(f"PyHeat Debug Monitor - Started at {datetime.now()}\n")
        f.write(f"Monitoring {len(MONITORED_ENTITIES)} entities\n")
        f.write("=" * 100 + "\n")
    
    # Start monitoring
    monitor = HAMonitor(base_url, token, output_file)
    try:
        monitor.monitor_loop()
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user")
        print(f"Log file: {output_file}")

if __name__ == "__main__":
    main()
