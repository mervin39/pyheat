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
    # Room temperatures
    *[f"sensor.pyheat_{room}_temperature" for room in ROOMS],
    # Room setpoints
    *[f"input_number.pyheat_{room}_manual_setpoint" for room in ROOMS],
    # Room modes
    *[f"input_select.pyheat_{room}_mode" for room in ROOMS],
    # TRV valve positions from Z2M (actual hardware feedback)
    *[f"sensor.trv_{room}_valve_opening_degree_z2m" for room in ROOMS],
    # PyHeat calculated valve positions
    *[f"sensor.pyheat_{room}_valve_percent" for room in ROOMS],
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

# Entity abbreviations for compact output
ABBREVIATIONS = {
    # Room temperatures
    "sensor.pyheat_pete_temperature": "Temp-Pet",
    "sensor.pyheat_games_temperature": "Temp-Gam",
    "sensor.pyheat_lounge_temperature": "Temp-Lou",
    "sensor.pyheat_abby_temperature": "Temp-Abb",
    "sensor.pyheat_office_temperature": "Temp-Off",
    "sensor.pyheat_bathroom_temperature": "Temp-Bat",
    # Room setpoints
    "input_number.pyheat_pete_manual_setpoint": "Setp-Pet",
    "input_number.pyheat_games_manual_setpoint": "Setp-Gam",
    "input_number.pyheat_lounge_manual_setpoint": "Setp-Lou",
    "input_number.pyheat_abby_manual_setpoint": "Setp-Abb",
    "input_number.pyheat_office_manual_setpoint": "Setp-Off",
    "input_number.pyheat_bathroom_manual_setpoint": "Setp-Bat",
    # Room modes
    "input_select.pyheat_pete_mode": "Mode-Pet",
    "input_select.pyheat_games_mode": "Mode-Gam",
    "input_select.pyheat_lounge_mode": "Mode-Lou",
    "input_select.pyheat_abby_mode": "Mode-Abb",
    "input_select.pyheat_office_mode": "Mode-Off",
    "input_select.pyheat_bathroom_mode": "Mode-Bat",
    # TRV valves (Z2M feedback)
    "sensor.trv_pete_valve_opening_degree_z2m": "Vz-Pet",
    "sensor.trv_games_valve_opening_degree_z2m": "Vz-Gam",
    "sensor.trv_lounge_valve_opening_degree_z2m": "Vz-Lou",
    "sensor.trv_abby_valve_opening_degree_z2m": "Vz-Abb",
    "sensor.trv_office_valve_opening_degree_z2m": "Vz-Off",
    "sensor.trv_bathroom_valve_opening_degree_z2m": "Vz-Bat",
    # PyHeat calculated valves
    "sensor.pyheat_pete_valve_percent": "Vp-Pet",
    "sensor.pyheat_games_valve_percent": "Vp-Gam",
    "sensor.pyheat_lounge_valve_percent": "Vp-Lou",
    "sensor.pyheat_abby_valve_percent": "Vp-Abb",
    "sensor.pyheat_office_valve_percent": "Vp-Off",
    "sensor.pyheat_bathroom_valve_percent": "Vp-Bat",
    # Calling for heat
    "binary_sensor.pyheat_pete_calling_for_heat": "Call-Pet",
    "binary_sensor.pyheat_games_calling_for_heat": "Call-Gam",
    "binary_sensor.pyheat_lounge_calling_for_heat": "Call-Lou",
    "binary_sensor.pyheat_abby_calling_for_heat": "Call-Abb",
    "binary_sensor.pyheat_office_calling_for_heat": "Call-Off",
    "binary_sensor.pyheat_bathroom_calling_for_heat": "Call-Bat",
    # Timers
    "timer.pyheat_boiler_min_off_timer": "T-MinOff",
    "timer.pyheat_boiler_min_on_timer": "T-MinOn",
    "timer.pyheat_boiler_off_delay_timer": "T-OffDly",
    "timer.pyheat_boiler_pump_overrun_timer": "T-Pump",
    # Boiler
    "climate.dummy": "Boiler",
}

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
    
    def get_value(self, state_data: dict) -> str:
        """Extract displayable value from state data"""
        if state_data is None:
            return "NOT_FOUND"
        
        state = state_data.get('state', 'unknown')
        entity_id = state_data.get('entity_id', '')
        attrs = state_data.get('attributes', {})
        
        # For timers, show state and remaining if active
        if entity_id.startswith('timer.'):
            if state == 'active':
                remaining = attrs.get('remaining', '?')
                return f"active({remaining})"
            else:
                return state
        
        # For climate (boiler only), show state/action
        if entity_id.startswith('climate.'):
            hvac_action = attrs.get('hvac_action', '?')
            return f"{state}/{hvac_action}"
        
        # For binary_sensors, show on/off
        if entity_id.startswith('binary_sensor.'):
            return state
        
        # For input_select, just show the state
        if entity_id.startswith('input_select.'):
            return state
        
        # For sensors and input_numbers, show value with unit
        unit = attrs.get('unit_of_measurement', '')
        if unit:
            return f"{state}{unit}"
        else:
            return state
    
    def write_legend(self, f):
        """Write legend at top of log file"""
        f.write("LEGEND:\n")
        f.write("-------\n")
        f.write("Temp-XXX = Room Temperature (°C)        Setp-XXX = Manual Setpoint (°C)\n")
        f.write("Mode-XXX = Room Mode                    Vz-XXX = TRV Valve Z2M feedback (%)\n")
        f.write("Vp-XXX   = PyHeat Valve calc (%)        Call-XXX = Calling for Heat (on/off)\n")
        f.write("T-XXX    = Timer (idle/active)          Boiler = Boiler state/action\n")
        f.write("\nRooms: Pet=Pete, Gam=Games, Lou=Lounge, Abb=Abby, Off=Office, Bat=Bathroom\n")
        f.write("Timers: MinOff=Min Off, MinOn=Min On, OffDly=Off Delay, Pump=Pump Overrun\n")
        f.write("\n" + "=" * 100 + "\n\n")
    
    def log_all_states(self, reason: str = ""):
        """Log all monitored entities in compact table format"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Get all current states
        states = {}
        for entity_id in MONITORED_ENTITIES:
            state_data = self.get_state(entity_id)
            states[entity_id] = state_data
        
        # Write to log file in table format
        with open(self.output_file, 'a') as f:
            f.write(f"[{timestamp}] {reason}\n")
            f.write("-" * 100 + "\n")
            
            # Create compact table with 4 columns
            rows = []
            entities_list = list(MONITORED_ENTITIES)
            
            # Process in groups of 4
            for i in range(0, len(entities_list), 4):
                row_entities = entities_list[i:i+4]
                row_parts = []
                for entity_id in row_entities:
                    abbr = ABBREVIATIONS.get(entity_id, entity_id[:10])
                    value = self.get_value(states[entity_id])
                    row_parts.append(f"{abbr:8s} = {value:15s}")
                rows.append(" | ".join(row_parts))
            
            for row in rows:
                f.write(row + "\n")
            
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
            
            # Check each entity for changes (excluding temperature sensors)
            changed = False
            changed_entities = []
            
            for entity_id in MONITORED_ENTITIES:
                current_state = self.get_state(entity_id)
                
                # Compare with last known state
                last_state = self.last_states.get(entity_id)
                
                if current_state != last_state:
                    # Only trigger log if it's not a temperature sensor
                    if not entity_id.startswith('sensor.pyheat_') or not entity_id.endswith('_temperature'):
                        changed = True
                        changed_entities.append(entity_id)
                    # Always update last state though
                    self.last_states[entity_id] = current_state
            
            # If anything changed (excluding temp sensors), log everything
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
    
    # Clear existing log file and write legend
    with open(output_file, 'w') as f:
        f.write(f"PyHeat Debug Monitor - Started at {datetime.now()}\n")
        f.write(f"Monitoring {len(MONITORED_ENTITIES)} entities\n")
        f.write("=" * 100 + "\n\n")
    
    # Start monitoring
    monitor = HAMonitor(base_url, token, output_file)
    
    # Write legend
    with open(output_file, 'a') as f:
        monitor.write_legend(f)
    
    try:
        monitor.monitor_loop()
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user")
        print(f"Log file: {output_file}")

if __name__ == "__main__":
    main()
