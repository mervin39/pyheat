# Home Assistant API Schema Reference

This document describes the actual API response schemas for pyheat entities in Home Assistant.

## Purpose

When querying HA API with curl/jq, use this reference for correct field names. The internal Python code uses different variable names than what's exposed in the API.

---

## sensor.pyheat_status

**Base URL:** `$HA_BASE_URL/api/states/sensor.pyheat_status`

**Authentication:** Requires `Authorization: Bearer $HA_TOKEN` header

### Top-Level Structure

```json
{
  "entity_id": "sensor.pyheat_status",
  "state": "heating|idle|dhw",
  "attributes": { ... },
  "last_changed": "ISO8601 timestamp",
  "last_reported": "ISO8601 timestamp",
  "last_updated": "ISO8601 timestamp",
  "context": { "id": "...", "parent_id": null, "user_id": "..." }
}
```

### attributes.rooms.<room_id>

Each room object contains:

```json
{
  "mode": "auto|off|override",
  "temperature": 19.4,              // Current room temp in °C (may be missing if stale)
  "target": 14.0,                   // Target temp in °C
  "estimated_dump_capacity": 1684.0, // Radiator capacity in watts
  "is_stale": "true|false"          // Optional: present if temp sensor stale
}
```

**Field Names:**
- ✅ `mode` (not `heating_mode`)
- ✅ `temperature` (not `current_temp`)
- ✅ `target` (not `target_temp` or `setpoint`)
- ✅ `estimated_dump_capacity` (not `capacity`)

**Notable Absences:**
- ❌ No `calling` or `call_for_heat` field
- ❌ No `valve_pct` or `valve` field
- ❌ No `override_target` field
- These are internal state only, not exposed in API

### attributes (top level)

```json
{
  "active_rooms": ["pete", "lounge"],  // Array of room IDs currently calling for heat
  "total_rooms": 6,
  "rooms": { ... },                     // Per-room details (see above)
  "boiler_state": "off|heating|dhw",
  "boiler_reason": "Off: no demand",    // Human-readable state explanation
  "last_recompute": "2025-11-27T09:08:36.660640",
  "cycling_protection": {
    "state": "READY|COOLDOWN|RECOVERY",
    "cooldown_start": "ISO8601 timestamp",  // Present in COOLDOWN/RECOVERY
    "saved_setpoint": 70.0,                  // Present in COOLDOWN/RECOVERY
    "recovery_threshold": 55.0               // Present in RECOVERY
  },
  "total_estimated_dump_capacity": 7667.0,
  "load_sharing": {
    "state": "inactive|tier1_active|tier1_escalated|tier2_active|tier2_escalated|tier3_active|tier3_escalated",
    "active_rooms": [],                // Rooms activated by load sharing (detailed array)
    "trigger_rooms": [],               // Rooms that triggered load sharing
    "trigger_capacity": 2100,          // Capacity of trigger rooms (watts)
    "master_enabled": "true|false",    // String, not boolean!
    "decision_explanation": "Active: 1 room(s) calling (bathroom) with 2100W < 3500W threshold. Added 2 schedule-aware room(s) to reach 4000W target.",
    "decision_details": {              // Detailed structured breakdown
      "status": "active",
      "state": "tier1_active",
      "activation_reason": {
        "type": "low_capacity_with_cycling_risk",
        "trigger_rooms": ["bathroom"],
        "trigger_capacity_w": 2100,
        "capacity_threshold_w": 3500,
        "activated_at": "2025-11-27T10:30:15",
        "duration_s": 180
      },
      "room_selections": [
        {
          "room_id": "bedroom",
          "tier": 1,
          "tier_name": "Schedule-aware pre-warming",
          "selection_reason": "schedule_45m",
          "valve_pct": 70,
          "activated_at": "2025-11-27T10:30:15",
          "duration_s": 180
        }
      ],
      "capacity_status": {
        "target_capacity_w": 4000,
        "active_room_count": 2,
        "tier_breakdown": {
          "tier1_count": 2,
          "tier2_count": 0,
          "tier3_count": 0
        }
      }
    }
  }
}
```

**Field Names:**
- ✅ `active_rooms` array at top level (rooms calling for heat)
- ✅ `boiler_state` (not `state` - that's at entity level)
- ✅ `boiler_reason` (not `reason`)
- ✅ `cycling_protection.state` (not `cycling_protection_active` - check for != "READY")
- ✅ `load_sharing.decision_explanation` (human-readable one-liner)
- ✅ `load_sharing.decision_details` (structured breakdown)

### Common Query Examples

```bash
# Check if any rooms are calling for heat
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq -r '.attributes.active_rooms | length'

# Get room temperature and target
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq '.attributes.rooms.bathroom | "temp: \(.temperature)°C, target: \(.target)°C"'

# Check if cycling protection is active
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq -r '.attributes.cycling_protection.state'

# Check load sharing status
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq '.attributes.load_sharing'

# Get human-readable load sharing explanation
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq -r '.attributes.load_sharing.decision_explanation'

# Get detailed load sharing breakdown
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq '.attributes.load_sharing.decision_details'

# Get total system capacity
curl -s -H "Authorization: Bearer $HA_TOKEN" "$HA_BASE_URL/api/states/sensor.pyheat_status" \
  | jq -r '.attributes.total_estimated_dump_capacity'
```

---

## input_select.pyheat_load_sharing_mode

**Base URL:** `$HA_BASE_URL/api/states/input_select.pyheat_load_sharing_mode`

### Structure

```json
{
  "entity_id": "input_select.pyheat_load_sharing_mode",
  "state": "Off|Conservative|Balanced|Aggressive",
  "attributes": {
    "options": ["Off", "Conservative", "Balanced", "Aggressive"],
    "editable": true,
    "friendly_name": "Load Sharing Mode"
  },
  "last_changed": "ISO8601 timestamp",
  "last_updated": "ISO8601 timestamp"
}
```

**Mode Descriptions:**
- **Off**: Load sharing completely disabled
- **Conservative**: Tier 1 only (schedule pre-warming)
- **Balanced**: Tier 1 + Tier 2 Phase A (passive rooms)
- **Aggressive**: All tiers (includes Phase B fallback priority list)

**Query Example:**

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_BASE_URL/api/states/input_select.pyheat_load_sharing_mode" \
  | jq -r '.state'
```

---

## Best Practices

1. **Always explore first:** Use `jq '.'` or `jq '.attributes'` to see full structure before filtering
2. **Check field types:** Many boolean-looking fields are strings (`"true"` not `true`)
3. **Handle missing fields:** Some fields like `temperature` may be absent (stale sensors)
4. **Use raw output:** Add `-r` flag to `jq` to get unquoted strings

## Common Mistakes

❌ **Don't assume field names from Python code:**
- Python: `room.call_for_heat` → API: check `active_rooms` array
- Python: `room.valve_pct` → API: not exposed
- Python: `cycling_protection_active` → API: check `cycling_protection.state != "READY"`

❌ **Don't assume boolean types:**
- `load_sharing.master_enabled` is `"true"` (string), not `true` (boolean)
- `rooms.<id>.is_stale` is `"true"` (string), not `true` (boolean)

✅ **Do check the schema first:**
- Reference this document
- Or query with `jq '.'` to see actual structure
