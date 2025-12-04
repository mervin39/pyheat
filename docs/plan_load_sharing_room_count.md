# Plan: Display All Heating Rooms in Boiler Status

## Problem Statement

The boiler status currently shows "heating (N rooms)" where N only includes rooms naturally calling for heat (active mode, below target). This count doesn't include:
1. **Passive rooms receiving heat** - rooms in passive mode with valves open when boiler is on
2. **Load-sharing rooms** - rooms activated through load-sharing (schedule pre-warming or fallback priority)

This creates confusion as:
- The boiler may be running for more rooms than displayed
- Users can't see the full picture of which rooms are receiving heat
- Passive heating and load-sharing activity is hidden unless you drill into detailed status

## Room Heating Categories

When the boiler is running, rooms can be receiving heat in three distinct ways:

### 1. Naturally Calling Rooms (Active Heating)
- Room is in active heating mode (auto or manual)
- Temperature is below target
- Room is calling for heat
- **This is what's currently shown**

### 2. Passive Rooms Receiving Heat (Opportunistic Heating)
- Room's `operating_mode == 'passive'` (either mode='passive' OR mode='auto' in a passive schedule block)
- Room temperature < max_temp (valve is open, valve_percent > 0)
- Boiler is running (supplying heat)
- Room is accepting opportunistic heating from the boiler
- **Currently NOT shown in boiler status**

### 3. Load-Sharing Rooms (Cycling Protection)
- Rooms activated by load-sharing manager to prevent boiler short-cycling
- Two tiers:
  - **Schedule tier (Tier 1)**: Pre-warming rooms with upcoming heating schedules
  - **Fallback tier (Tier 2)**: Priority list for emergency heat dumping
- **Currently NOT shown in boiler status**

## Current Implementation Analysis

### Where Room Count is Used

**1. Status Publisher (status_publisher.py:452)**
```python
if boiler_state == C.STATE_ON:
    state = f"heating ({len(active_rooms)} room{'s' if len(active_rooms) != 1 else ''})"
```
This creates the main status text shown in `sensor.pyheat_status`.

**2. API Response (api_handler.py:496-509)**
```python
system = {
    "master_enabled": master_enabled,
    "holiday_mode": holiday_mode,
    "any_call_for_heat": status_attrs.get("any_call_for_heat", False),
    "boiler_state": status_attrs.get("boiler_state", "unknown"),
    "last_recompute": status_attrs.get("last_recompute"),
    # ... timer end times ...
    "cooldown_active": cooldown_active,
    "load_sharing": status_attrs.get("load_sharing"),
}
```
The API already exposes `load_sharing` status with active rooms.

**3. Sensor Attributes (status_publisher.py:465-475)**
```python
attrs = {
    'any_call_for_heat': any_calling,
    'active_rooms': active_rooms,
    'room_calling_count': len(active_rooms),
    'total_rooms': len(self.config.rooms),
    # ... more attributes ...
}
```

### Load-Sharing Room Data

Load-sharing status includes:
```python
'active_rooms': [
    {
        'room_id': room.room_id,
        'tier': room.tier,              # 1=schedule, 2=fallback
        'valve_pct': room.valve_pct,
        'reason': room.reason,
        'duration_s': (datetime.now() - room.activated_at).total_seconds()
    }
    for room in self.context.active_rooms.values()
]
```

## Design Options

Now we need to decide how to display up to FOUR categories of rooms:
1. **Naturally calling** (active heating)
2. **Passive heating** (opportunistic)
3. **Load-sharing schedule** (pre-warming)
4. **Load-sharing fallback** (emergency dump)

### Option A: Simple Total Count
**Display**: "heating (7 rooms)"

**Example Breakdown** (not shown in status):
- 3 calling + 2 passive + 1 schedule + 1 fallback = 7 total

**Pros**:
- Very simple, clean
- Shows total rooms receiving heat

**Cons**:
- No visibility into categories
- Can't tell if passive/load-sharing is active
- Loses useful information

---

### Option B: Active + Additional
**Display**: "heating (3 rooms + 4 additional)"

Where "additional" includes passive + load-sharing rooms.

**Pros**:
- Shows primary calling rooms prominently
- Indicates there are other rooms heating

**Cons**:
- Vague - doesn't explain what "additional" means
- Still hides useful information

---

### Option C: Full Breakdown
**Display**: "heating (3 active, 2 passive, 1 pre-warming, 1 fallback)"

**Pros**:
- Complete information
- Clear categories

**Cons**:
- Very verbose
- May be too long for UI display
- Overwhelming

---

### Option D: Compact Breakdown with Symbols
**Display**: "heating (3 + 2p + 1s + 1f)"

Where: p=passive, s=schedule, f=fallback

**Pros**:
- Compact
- Shows all categories

**Cons**:
- Requires users to learn symbols
- Not intuitive

---

### Option E: Contextual Progressive Display
**Display adapts based on what's active:**

- Only calling: "heating (3 rooms)"
- Calling + passive: "heating (3 rooms, 2 passive)"
- Calling + load-sharing: "heating (3 rooms, +2 pre-warming)"
- All categories: "heating (3 rooms, 2 passive, +1 pre-warming, +1 fallback)"

**Pros**:
- Descriptive when needed
- Compact when simple
- Progressive detail

**Cons**:
- Variable format may be confusing
- Can still get long with all categories

---

### Option F: Two-Level Display
**Primary**: "heating (7 rooms)"
**Detail** (in tooltip/attributes): "3 active, 2 passive, 1 pre-warming, 1 fallback"

**Pros**:
- Clean primary display
- Full detail available on hover/expand
- Best of both worlds

**Cons**:
- Requires UI support for two-level display
- Detail not visible at glance

---

## Design Decisions (Finalized)

### 1. Display Format: **Option E - Contextual Progressive Display**

The status text adapts based on what's active:
- Only calling: `"heating (3 active)"`
- Calling + passive: `"heating (3 active, 2 passive)"`
- Calling + load-sharing: `"heating (3 active, +2 pre-warming)"`
- All categories: `"heating (3 active, 2 passive, +1 pre-warming, +1 fallback)"`

### 2. Terminology

- **Naturally calling rooms**: "active" → `"3 active"`
- **Passive rooms**: "passive" → `"2 passive"`
- **Load-sharing schedule tier**: "pre-warming" → `"+1 pre-warming"`
- **Load-sharing fallback tier**: "fallback" → `"+1 fallback"`

### 3. Passive Room Display: **Show prominently**

Passive rooms will be shown explicitly in the status text when present.

### 4. Edge Cases

**Scenario A**: Load-sharing but no natural calling
- **Won't happen** - Load sharing exits when no rooms are calling

**Scenario B**: Only passive rooms (no calling, no load-sharing)
- **Won't happen** - Passive rooms open but no calling means boiler won't be heating

**Scenario C**: All four categories active
- **Full detail** - Show all categories: `"heating (3 active, 2 passive, +1 pre-warming, +1 fallback)"`
- If too long for UI, wrap text (frontend responsibility)

### 5. API Response Structure: **Option 1 - Separate counts**

```json
{
  "calling_count": 3,
  "passive_count": 2,
  "load_sharing_schedule_count": 1,
  "load_sharing_fallback_count": 1,
  "total_heating_count": 7
}
```

Provides maximum detail for pyheat-web to use flexibly.

---

## Implementation Details

### Files to Modify

1. **`services/status_publisher.py`**
   - Method: `publish_system_status()` (~line 437)
   - Changes: Calculate passive/load-sharing counts, format status text, add attributes

2. **`services/api_handler.py`**
   - Method: `api_get_status()` (~line 496)
   - Changes: Add room counts to system object in API response

3. **Future: pyheat-web repository**
   - Boiler status component (display room counts)
   - Room list component (visual indicators for room types)

---

### Change 1: Status Publisher - Calculate Room Counts

**File**: `services/status_publisher.py`
**Method**: `publish_system_status()` (~line 437)

**Add before building state string**:

```python
def publish_system_status(self, any_calling: bool, active_rooms: List[str],
                         room_data: Dict, boiler_state: str, boiler_reason: str,
                         now: datetime) -> None:
    """Publish main system status entity.

    Args:
        any_calling: Whether any room is calling for heat
        active_rooms: List of calling room IDs
        room_data: Dict of room states
        boiler_state: Current boiler state
        boiler_reason: Reason for boiler state
        now: Current datetime
    """

    # === NEW CODE START ===
    # Calculate passive rooms (receiving heat when boiler is on)
    passive_rooms = []
    if boiler_state == C.STATE_ON:
        for room_id, data in room_data.items():
            # Passive room: operating_mode='passive' AND valve open AND not naturally calling
            if (data.get('operating_mode') == 'passive' and
                data.get('valve_percent', 0) > 0 and
                room_id not in active_rooms):
                passive_rooms.append(room_id)

    # Calculate load-sharing rooms by tier
    load_sharing_schedule_rooms = []
    load_sharing_fallback_rooms = []

    if hasattr(self.ad, 'load_sharing') and self.ad.load_sharing:
        ls_status = self.ad.load_sharing.get_status()
        ls_active_rooms = ls_status.get('active_rooms', [])

        for room in ls_active_rooms:
            if room['tier'] == 1:  # TIER_SCHEDULE
                load_sharing_schedule_rooms.append(room['room_id'])
            elif room['tier'] == 2:  # TIER_FALLBACK
                load_sharing_fallback_rooms.append(room['room_id'])

    # Calculate totals
    calling_count = len(active_rooms)
    passive_count = len(passive_rooms)
    schedule_count = len(load_sharing_schedule_rooms)
    fallback_count = len(load_sharing_fallback_rooms)
    total_heating = calling_count + passive_count + schedule_count + fallback_count
    # === NEW CODE END ===

    # Build state string based on boiler state machine
    if boiler_state == C.STATE_ON:
        # === MODIFIED CODE START ===
        # Build progressive status text based on what's active
        parts = []

        # Always show calling count (even if 0 for edge cases)
        if calling_count == 1:
            parts.append("1 active")
        else:
            parts.append(f"{calling_count} active")

        # Add passive rooms if any
        if passive_count > 0:
            if passive_count == 1:
                parts.append("1 passive")
            else:
                parts.append(f"{passive_count} passive")

        # Add load-sharing schedule tier if any
        if schedule_count > 0:
            if schedule_count == 1:
                parts.append("+1 pre-warming")
            else:
                parts.append(f"+{schedule_count} pre-warming")

        # Add load-sharing fallback tier if any
        if fallback_count > 0:
            if fallback_count == 1:
                parts.append("+1 fallback")
            else:
                parts.append(f"+{fallback_count} fallback")

        state = f"heating ({', '.join(parts)})"
        # === MODIFIED CODE END ===
    elif boiler_state == C.STATE_PUMP_OVERRUN:
        state = "pump overrun"
    elif boiler_state == C.STATE_PENDING_ON:
        state = "pending on (waiting for TRVs)"
    elif boiler_state == C.STATE_PENDING_OFF:
        state = "pending off (delay)"
    elif boiler_state == C.STATE_INTERLOCK_BLOCKED:
        state = "blocked (interlock)"
    else:
        state = "idle"

    # Build attributes
    attrs = {
        'any_call_for_heat': any_calling,
        'active_rooms': active_rooms,
        'room_calling_count': len(active_rooms),  # Keep for backward compatibility
        'total_rooms': len(self.config.rooms),
        'rooms': {},
        'boiler_state': boiler_state,
        'boiler_reason': boiler_reason,
        'total_valve_percent': 0,
        'last_recompute': now.isoformat(),
        # === NEW ATTRIBUTES START ===
        'calling_count': calling_count,
        'passive_count': passive_count,
        'load_sharing_schedule_count': schedule_count,
        'load_sharing_fallback_count': fallback_count,
        'total_heating_count': total_heating,
        'passive_rooms': passive_rooms,
        'load_sharing_schedule_rooms': load_sharing_schedule_rooms,
        'load_sharing_fallback_rooms': load_sharing_fallback_rooms,
        # === NEW ATTRIBUTES END ===
    }

    # ... rest of method continues unchanged ...
```

---

### Change 2: API Handler - Add Room Counts to Response

**File**: `services/api_handler.py`
**Method**: `api_get_status()` (~line 496)

**Modify system object construction**:

```python
def api_get_status(self, namespace, data: Dict[str, Any]) -> tuple:
    """API endpoint: GET/POST /api/appdaemon/pyheat_get_status"""
    try:
        # ... existing code to get status_attrs ...

        # Get cooldown active state from cycling protection
        cycling_protection = status_attrs.get("cycling_protection")
        if cycling_protection:
            cooldown_active = cycling_protection.get("state") == "COOLDOWN"
        else:
            cooldown_active = False

        # === NEW CODE START ===
        # Get room counts from status attributes
        calling_count = status_attrs.get("calling_count", len(status_attrs.get("active_rooms", [])))
        passive_count = status_attrs.get("passive_count", 0)
        schedule_count = status_attrs.get("load_sharing_schedule_count", 0)
        fallback_count = status_attrs.get("load_sharing_fallback_count", 0)
        total_heating = status_attrs.get("total_heating_count", calling_count)
        # === NEW CODE END ===

        system = {
            "master_enabled": master_enabled,
            "holiday_mode": holiday_mode,
            "any_call_for_heat": status_attrs.get("any_call_for_heat", False),
            "boiler_state": status_attrs.get("boiler_state", "unknown"),
            "last_recompute": status_attrs.get("last_recompute"),
            "boiler_off_delay_end_time": boiler_off_delay_end_time,
            "boiler_min_on_end_time": boiler_min_on_end_time,
            "boiler_min_off_end_time": boiler_min_off_end_time,
            "boiler_pump_overrun_end_time": boiler_pump_overrun_end_time,
            "cooldown_active": cooldown_active,
            "load_sharing": status_attrs.get("load_sharing"),
            # === NEW FIELDS START ===
            "calling_count": calling_count,
            "passive_count": passive_count,
            "load_sharing_schedule_count": schedule_count,
            "load_sharing_fallback_count": fallback_count,
            "total_heating_count": total_heating,
            # === NEW FIELDS END ===
        }

        return {
            "rooms": rooms,
            "system": system
        }, 200

    except Exception as e:
        # ... error handling ...
```

---

### Status Text Examples

Based on the implementation above, here are example outputs:

| Scenario | Status Text |
|----------|-------------|
| 3 calling only | `"heating (3 active)"` |
| 1 calling only | `"heating (1 active)"` |
| 3 calling + 2 passive | `"heating (3 active, 2 passive)"` |
| 3 calling + 1 schedule | `"heating (3 active, +1 pre-warming)"` |
| 3 calling + 1 schedule + 1 fallback | `"heating (3 active, +1 pre-warming, +1 fallback)"` |
| 3 calling + 2 passive + 1 schedule + 1 fallback | `"heating (3 active, 2 passive, +1 pre-warming, +1 fallback)"` |
| Boiler off | `"idle"` |
| Pump overrun | `"pump overrun"` |

---

### API Response Example

```json
{
  "rooms": [ /* room array */ ],
  "system": {
    "master_enabled": true,
    "holiday_mode": false,
    "any_call_for_heat": true,
    "boiler_state": "on",
    "calling_count": 3,
    "passive_count": 2,
    "load_sharing_schedule_count": 1,
    "load_sharing_fallback_count": 1,
    "total_heating_count": 7,
    // ... other fields ...
  }
}
```

---

### Backward Compatibility

- **Preserved fields**:
  - `room_calling_count` - still present, unchanged
  - `active_rooms` - still present, unchanged

- **New fields** (additive):
  - `calling_count` - same as `room_calling_count`
  - `passive_count` - new
  - `load_sharing_schedule_count` - new
  - `load_sharing_fallback_count` - new
  - `total_heating_count` - new
  - `passive_rooms` - new (list of room IDs)
  - `load_sharing_schedule_rooms` - new (list of room IDs)
  - `load_sharing_fallback_rooms` - new (list of room IDs)

Existing code using `room_calling_count` will continue to work unchanged.
