# Bug: Status Shows Wrong Next Schedule Change Time

**Date Reported:** 2025-11-07  
**Status:** Identified, Not Fixed  
**Severity:** Low (cosmetic issue in status display)

## Issue Description

When a schedule block has no end time (treated as midnight per spec) and the next day starts with a block at 00:00 with the **same temperature**, the status text shows the wrong next change time.

### Example

**Games Room (Dining Room) on Thursday 15:54:**
- Current: Friday 15:00 block at 12.0° (no end time = runs until midnight)
- Next: Saturday 00:00-09:00 block at 12.0° (same temp)
- Then: Default target 14.0° starting at 09:00

**Expected Status:**
```
Auto: 12.0° until 09:00 on Saturday (14.0°)
```

**Actual Status:**
```
Auto: 12.0° until 00:00 on Saturday (12.0°)
```

### Root Cause

The `get_next_schedule_change()` method in `scheduler.py` finds the next block that *starts*, not the next time the temperature actually *changes*.

When Friday's 15:00 block (no end) transitions to Saturday's 00:00 block (same temp), it reports 00:00 as the "next change" even though the temperature doesn't change until 09:00.

### Relevant Schedule

```yaml
- id: games
  default_target: 14.0
  week:
    fri:
    - start: 07:00
      end: '10:30'
      target: 10.0
    - start: '15:00'      # ← No end time = until midnight
      target: 12.0
    sat:
    - start: 00:00        # ← Same temp as previous block
      end: 09:00
      target: 12.0        # ← Still 12.0°, no change!
```

### Specification Note

Per current implementation spec (STATUS_FORMAT_SPEC.md and `scheduler.py`):
- Blocks without an explicit `end` time are treated as running until midnight (24:00)
- This is converted from `end: '23:59'` to `end: '24:00'` in code
- Gap detection checks if next block starts immediately after current ends

### Expected Fix

The `get_next_schedule_change()` method should:
1. Find the next block start time
2. Check if that block's temperature differs from current
3. If same temp, continue searching for next temp change
4. Consider transitions to default_target (gaps) as valid changes

This would require looking ahead through multiple blocks to find the first *temperature change*, not just the first *schedule block*.

### Impact

- Low priority - status text is slightly misleading but system behavior is correct
- Temperature control unaffected - room maintains correct target
- Only affects display text shown to users
- More confusing when blocks span midnight with same temperature

### Workaround

None needed - system functions correctly, just status text could be clearer.

### Test Cases to Consider When Fixing

1. **Same temp across midnight:** Friday block (no end) → Saturday 00:00 block (same temp)
2. **Different temp across midnight:** Friday block (no end) → Saturday 00:00 block (different temp)
3. **Multiple same-temp blocks:** Three consecutive blocks with same temperature
4. **Gap after midnight block:** Saturday 00:00-09:00 block → gap → next block at 15:00
5. **Forever detection:** Should still work when no temp changes exist
