# Working Version Marker

**Date:** 2025-11-19  
**Commit:** 95e690d (fix: Add missing RoomController import in app.py)  
**Tag:** working-2025-11-19

## Status
✅ **VERIFIED WORKING**

## Test Results
- ✅ Master enable: ON
- ✅ Targets resolving correctly (Pete: 14.0°C)
- ✅ Rooms calling for heat (games: ON, lounge: ON)
- ✅ Boiler turned ON successfully
- ✅ Min-on timer started (3 minutes)
- ✅ No startup errors
- ✅ Recompute cycles running normally

## Notes
This version is AFTER:
- Valve band refactor (bea2d2d)
- RoomController import fix (95e690d)

This version is BEFORE:
- Hysteresis state persistence (31343f8)
- TRV responsibility encapsulation (e3d9334) ⚠️ **This commit broke the system**

## Problem Identified
Commits after 95e690d introduced bugs:
- e3d9334: TRV encapsulation introduced logic error in `_check_trv_feedback_confirmed()`
- The method checked TRV feedback against NEW valve positions instead of LAST commanded positions
- This caused boiler to wait for feedback that would never come (commands not sent yet)

## Recovery
To restore working state:
```bash
git checkout 95e690d
# or
git checkout working-2025-11-19
```

To try the fixed version with TRV encapsulation:
```bash
git checkout 6499b68  # Contains fix for TRV feedback bug
```
