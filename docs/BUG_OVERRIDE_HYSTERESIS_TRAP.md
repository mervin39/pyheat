# Bug: Override Hysteresis Trap

## Status
**RESOLVED** - Fixed 2025-11-10

## Resolution
Implemented target change detection with hysteresis bypass. When the target temperature changes (override, boost, schedule transition, or mode change), the hysteresis deadband is bypassed and a fresh heating decision is made based on the current error. This ensures all target changes respond immediately while preserving hysteresis anti-flapping for temperature drift.

**Solution:** Track previous target per room. When `abs(target - prev_target) > 0.01°C`, bypass deadband and heat if error >= 0.05°C.

**See:** `docs/changelog.md` - 2025-11-10: Fix Override Hysteresis Trap

---

## Original Issue Description

## Summary
When an override is set with a target temperature only slightly above current temperature (within hysteresis deadband), the room may fail to call for heat even though it's below target.

## Reproduction
1. Room at 17.3°C, not calling for heat (valve 0%)
2. Set override to 17.5°C (error = 0.2°C)
3. Room does NOT call for heat despite being below target
4. Room will only start heating if temperature drops to 17.2°C (error ≥ 0.3°C)

## Root Cause
The `compute_call_for_heat()` method in `room_controller.py` uses asymmetric hysteresis with deadband logic:

```python
# Apply asymmetric hysteresis
if error >= on_delta:           # error >= 0.3°C
    return True
elif error <= off_delta:        # error <= 0.1°C
    return False
else:                           # 0.1°C < error < 0.3°C
    # In deadband → maintain previous state
    return prev_calling
```

**The Problem:**
- When error is between `off_delta` (0.1°C) and `on_delta` (0.3°C), the function maintains the **previous state**
- If room was not calling before override (valve at 0%), it stays not calling
- Override is an explicit user request to reach a specific temperature, but it's blocked by historical state

## Example Case: Abby's Room
- Current temp: 17.3°C
- Override target: 17.5°C
- Error: 0.2°C (in deadband)
- Previous state: `calling_for_heat = False` (valve was 0%)
- Result: Room does NOT call for heat
- Expected: Room should call for heat to reach override target

## Impact
- **Severity**: Medium
- Affects any override set within 0.3°C of current temperature when room was not previously calling
- User expectation: setting override should always attempt to reach target
- Actual behavior: override may be silently ignored until temperature drops naturally

## Affected Code
- **File**: `pyheat/room_controller.py`
- **Method**: `compute_call_for_heat()` (lines 167-198)
- **Logic**: Hysteresis deadband maintains previous state

## Possible Fixes

### Option 1: Ignore Previous State on Override Activation (Recommended)
Track when an override is newly activated and force a fresh heating decision based purely on error thresholds, ignoring previous state for the first recompute cycle.

**Pros:**
- Overrides always work as expected
- Minimal code change
- Preserves hysteresis benefits for normal operation

**Cons:**
- Requires tracking "override just activated" state
- Adds complexity to room state tracking

### Option 2: Remove Deadband Logic Entirely
Change hysteresis to always make fresh decisions based on error:
```python
if error >= on_delta:
    return True
else:
    return False
```

**Pros:**
- Simpler logic
- No state dependency issues

**Cons:**
- Loses anti-flapping benefits of hysteresis
- May cause rapid on/off cycling near threshold
- Not recommended for heating systems

### Option 3: Lower on_delta Threshold for Override Mode
Use tighter hysteresis when room is in override mode (e.g., `on_delta = 0.1°C`).

**Pros:**
- More responsive to override requests
- Simple to implement

**Cons:**
- May cause cycling if override target is very close to current temp
- Different behavior between auto and override modes may be confusing

### Option 4: Document as Expected Behavior
Document that override targets should be set at least 0.3°C above current temperature for immediate effect.

**Pros:**
- No code changes needed
- System behavior is technically correct

**Cons:**
- Poor user experience
- Unintuitive behavior
- Not recommended

## Recommended Solution
**Option 1**: Track when override is newly activated and force fresh decision on first recompute.

Implementation approach:
1. Add `room_override_just_activated` dict to track new overrides
2. In `service_handler.py` override service, set flag when override starts
3. In `compute_call_for_heat()`, if flag is set for room:
   - Make fresh decision (ignore prev_calling)
   - Clear flag after first computation
4. This gives override a "fresh start" while preserving hysteresis for ongoing operation

## Workaround
For now, when setting an override:
- Set target at least 0.5°C above current temperature to ensure immediate heating
- OR wait for natural temperature drop to trigger heating threshold
- OR restart pyheat after setting override (clears state)

## Related Files
- `pyheat/room_controller.py` - Hysteresis implementation
- `pyheat/service_handler.py` - Override activation
- `pyheat/constants.py` - Default hysteresis values

## Test Case
After fix, verify:
1. Room at 17.3°C, valve 0%, not calling
2. Set override to 17.5°C (error = 0.2°C)
3. Expected: `calling_for_heat` should become True on next recompute
4. Room should start heating immediately
5. Once error drops below 0.1°C, heating should stop (normal hysteresis resumes)
