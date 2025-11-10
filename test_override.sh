#!/bin/bash
# Test script for unified override service
# Tests all parameter combinations on room 'pete'

set -e

source /opt/appdata/hass/homeassistant/.env.hass

APPDAEMON_URL="http://localhost:5050/api/appdaemon"

echo "=========================================="
echo "Testing Unified Override Service"
echo "=========================================="
echo

# Test 1: Absolute temperature with relative duration
echo "Test 1: target=21.0, minutes=5"
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "target": 21.0, "minutes": 5}' | jq '.'
echo
sleep 2

# Cancel override
echo "Cancelling override..."
curl -s -X POST "${APPDAEMON_URL}/pyheat_cancel_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete"}' | jq '.'
echo
sleep 2

# Test 2: Delta with relative duration
echo "Test 2: delta=+2.0, minutes=5"
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "delta": 2.0, "minutes": 5}' | jq '.'
echo
sleep 2

# Cancel override
echo "Cancelling override..."
curl -s -X POST "${APPDAEMON_URL}/pyheat_cancel_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete"}' | jq '.'
echo
sleep 2

# Test 3: Absolute temperature with absolute end time (5 minutes from now)
echo "Test 3: target=20.0, end_time=(now+5min)"
END_TIME=$(date -u -d '+5 minutes' +"%Y-%m-%dT%H:%M:%S")
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d "{\"room\": \"pete\", \"target\": 20.0, \"end_time\": \"${END_TIME}\"}" | jq '.'
echo
sleep 2

# Cancel override
echo "Cancelling override..."
curl -s -X POST "${APPDAEMON_URL}/pyheat_cancel_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete"}' | jq '.'
echo
sleep 2

# Test 4: Delta with absolute end time (5 minutes from now)
echo "Test 4: delta=-1.5, end_time=(now+5min)"
END_TIME=$(date -u -d '+5 minutes' +"%Y-%m-%dT%H:%M:%S")
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d "{\"room\": \"pete\", \"delta\": -1.5, \"end_time\": \"${END_TIME}\"}" | jq '.'
echo
sleep 2

# Cancel override
echo "Cancelling override..."
curl -s -X POST "${APPDAEMON_URL}/pyheat_cancel_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete"}' | jq '.'
echo

# Test 5: Error cases - both target and delta
echo "Test 5 (ERROR EXPECTED): both target and delta"
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "target": 21.0, "delta": 2.0, "minutes": 5}' | jq '.'
echo

# Test 6: Error cases - both minutes and end_time
echo "Test 6 (ERROR EXPECTED): both minutes and end_time"
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "target": 21.0, "minutes": 5, "end_time": "2025-11-10T17:00:00"}' | jq '.'
echo

# Test 7: Error cases - neither target nor delta
echo "Test 7 (ERROR EXPECTED): neither target nor delta"
curl -s -X POST "${APPDAEMON_URL}/pyheat_override" \
  -H "Content-Type: application/json" \
  -d '{"room": "pete", "minutes": 5}' | jq '.'
echo

echo "=========================================="
echo "All tests complete!"
echo "=========================================="
