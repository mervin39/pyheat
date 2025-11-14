#!/usr/bin/env python3
"""
Test script to set Pete's room to manual mode and monitor heating behavior.
Current temp: 18.2°C, Target: 18.4°C
Expected: Start heating, continue until temp > 18.5°C (target + 0.1°C off_delta)
"""

import requests
import time
import sys

# AppDaemon API endpoint
APPDAEMON_URL = "http://localhost:5050"

def call_pyheat_api(endpoint, **kwargs):
    """Call a pyheat API endpoint via AppDaemon."""
    url = f"{APPDAEMON_URL}/api/appdaemon/{endpoint}"
    response = requests.post(url, json=kwargs)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error calling API: {response.status_code} - {response.text}")
        return None

def main():
    print("=" * 60)
    print("HYSTERESIS FIX TEST - Manual Mode")
    print("=" * 60)
    print()
    print("Current temperature: 18.2°C")
    print("Target temperature: 18.4°C")
    print("Error: 0.2°C (below target)")
    print()
    print("Expected behavior with FIXED hysteresis:")
    print("  1. Target changes → bypass deadband")
    print("  2. Check: error (0.2) >= -off_delta (-0.1) → TRUE")
    print("  3. START heating immediately")
    print("  4. Continue heating until temp > 18.5°C")
    print()
    print("With OLD BUG, heating would have stopped at 18.3°C!")
    print()
    print("-" * 60)
    
    # Set manual mode with target 18.4°C
    print("\n1. Setting manual setpoint to 18.4°C...")
    result = call_pyheat_api("pyheat_set_mode", room="pete", mode="manual", manual_setpoint=18.4)
    if result:
        print(f"   ✓ Success: {result}")
    else:
        print("   ✗ Failed to set manual mode")
        sys.exit(1)
    
    print("\n2. Monitoring logs for 60 seconds...")
    print("   Watch for:")
    print("   - 'Target changed' message")
    print("   - 'making fresh heating decision'")
    print("   - Room should start calling for heat")
    print()
    print("   Tail the logs in another terminal:")
    print("   tail -f /opt/appdata/appdaemon/conf/logs/appdaemon.log | grep pete")
    print()
    
    time.sleep(60)
    
    print("\n3. Test complete!")
    print("   Check the logs to verify:")
    print("   - Heating started when error was 0.2°C")
    print("   - Heating continues (doesn't stop at 0.1°C below target)")
    print("=" * 60)

if __name__ == "__main__":
    main()
