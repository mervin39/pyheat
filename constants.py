"""
constants.py - Centralized configuration and defaults for Pyheat

Responsibilities:
- Single source of truth for defaults, limits, and tuning knobs
- Define namespaced constants (no hardcoded magic numbers elsewhere)
- Read-only at runtime (no mutation)

All values here can be overridden per-room in rooms.yaml where applicable.
"""

from typing import Dict, Any

# ============================================================================
# Timezone & General
# ============================================================================

# Home Assistant's configured timezone (set at runtime by orchestrator)
# This is a placeholder; actual timezone comes from hass.config.time_zone
TIMEZONE = "UTC"

# ============================================================================
# Holiday Mode
# ============================================================================

# Target temperature (°C) used when holiday mode is active
# Applies as the base schedule target; overrides/boosts still work
HOLIDAY_TARGET_C = 15.0

# ============================================================================
# Temperature Targets & Bounds
# ============================================================================

# Allowed range for target temperatures (°C)
TARGET_MIN_C = 5.0
TARGET_MAX_C = 35.0

# Allowed precision values (decimal places for room temperature display)
PRECISION_ALLOWED = {0, 1, 2}

# Minimum timeout for sensor staleness (minutes)
TIMEOUT_MIN_M = 1

# ============================================================================
# Per-Room Hysteresis (Call-for-Heat Deadband)
# ============================================================================

# Asymmetric hysteresis thresholds for call-for-heat decisions
# e = target - temp (°C; positive means below target)
# - Start calling when e ≥ on_delta_c (turn-on threshold)
# - Stop calling when e ≤ off_delta_c (turn-off threshold)
# - If off_delta_c < e < on_delta_c, keep previous call state (no flip)
# - Must have: on_delta_c ≥ off_delta_c
#
# HOWEVER: Hysteresis deadband is bypassed when target changes.
# This ensures user overrides/schedule changes respond immediately.

HYSTERESIS_DEFAULT: Dict[str, float] = {
    "on_delta_c": 0.30,   # Start heating when 0.3°C below target
    "off_delta_c": 0.10,  # Stop heating when 0.1°C below target (near enough)
}

# Target change detection - bypass hysteresis when target changes
TARGET_CHANGE_EPSILON = 0.01  # °C - target changes smaller than this are ignored (floating point tolerance)
FRESH_DECISION_THRESHOLD = 0.05  # °C - when target changes, heat if error >= this (prevents noise-triggered heating)

# ============================================================================
# Smart TRV Control - Valve Bands
# ============================================================================

# Stepped valve percentage control based on error from target
# e = target - temp (°C; positive means below target)
#
# Bands:
#   e < t_low           → valve = 0%
#   t_low ≤ e < t_mid   → valve = low_percent
#   t_mid ≤ e < t_max   → valve = mid_percent
#   e ≥ t_max           → valve = max_percent
#
# step_hysteresis_c: requires crossing threshold by this amount before
# changing bands (dampens flapping between bands)

VALVE_BANDS_DEFAULT: Dict[str, float] = {
    # Error thresholds (°C)
    "t_low": 0.30,   # Threshold to start heating (low power)
    "t_mid": 0.80,   # Threshold to increase to medium power
    "t_max": 1.50,   # Threshold to go to maximum power
    
    # Valve percentages (0-100)
    "low_percent": 40,   # Gentle heat when slightly below target
    "mid_percent": 70,   # Moderate heat when clearly below target
    "max_percent": 100,  # Full heat when well below target
    
    # Step hysteresis (°C) - dampen band transitions
    "step_hysteresis_c": 0.05,
}

# ============================================================================
# Valve Update Rate Limiting
# ============================================================================

# Minimum interval between valve position updates (seconds)
# Prevents excessive TRV commands
VALVE_UPDATE_DEFAULT: Dict[str, float] = {
    "min_interval_s": 30,
}

# ============================================================================
# Boiler Safety & Anti Short-Cycling
# ============================================================================

# Anti short-cycling and TRV-open interlock parameters
SAFETY_DEFAULT: Dict[str, float] = {
    # Anti short-cycling
    "min_on_s": 180,       # Minimum time boiler must stay on (3 minutes)
    "min_off_s": 180,      # Minimum time boiler must stay off (3 minutes)
    
    # TRV-open interlock
    "min_open_percent": 10,      # Minimum valve opening to consider TRV "open"
    "feedback_timeout_s": 30,    # Max time to wait for TRV feedback
    "pump_overrun_s": 180,       # Post-shutdown circulation time (3 minutes)
    "off_delay_s": 30,           # Brief delay before turning boiler off
}

# Boiler Configuration Defaults
BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT = 100  # Minimum total valve opening required
BOILER_MIN_ON_TIME_DEFAULT = 180             # 3 minutes minimum on time
BOILER_MIN_OFF_TIME_DEFAULT = 180            # 3 minutes minimum off time
BOILER_OFF_DELAY_DEFAULT = 30                # 30 second delay before turning off
BOILER_PUMP_OVERRUN_DEFAULT = 180            # 3 minutes to dissipate residual heat
BOILER_BINARY_ON_SETPOINT_DEFAULT = 30.0     # °C - setpoint to command when we want heat
BOILER_BINARY_OFF_SETPOINT_DEFAULT = 5.0     # °C - setpoint to command when we want boiler off

# Boiler State Machine States
STATE_OFF = "off"
STATE_PENDING_ON = "pending_on"
STATE_ON = "on"
STATE_PENDING_OFF = "pending_off"
STATE_PUMP_OVERRUN = "pump_overrun"
STATE_INTERLOCK_BLOCKED = "interlock_blocked"

# ============================================================================
# TRV Entity Derivation Patterns
# ============================================================================

# TRV Setpoint Lock Strategy:
# By locking TRV internal setpoint to maximum (35°C), we force the TRV into "always open" mode.
# The TRV's internal controller will think "room is cold, I should be open", allowing us
# to control the actual valve position via opening_degree only.
# No need to control closing_degree since the TRV will never be in "closing" state.

TRV_LOCKED_SETPOINT_C = 35.0          # Lock TRV internal setpoint to maximum (35°C)
TRV_SETPOINT_CHECK_INTERVAL_S = 300   # Check/correct setpoints every 5 minutes

# Patterns for deriving TRV command/feedback entities from climate.<trv_base>
# The trv_base is extracted from the climate entity ID (e.g., "trv_pete" from "climate.trv_pete")
TRV_ENTITY_PATTERNS = {
    "cmd_valve":  "number.{trv_base}_valve_opening_degree",      # Only control opening degree
    "fb_valve":   "sensor.{trv_base}_valve_opening_degree_z2m",  # Only monitor opening degree
    "climate":    "climate.{trv_base}",                          # Climate entity for setpoint control
}

# Commands are rounded to nearest 0–100 integer
VALVE_PERCENT_INTEGER = True

# TRV Command Control (Simplified - Non-blocking)
# With locked setpoint, we only send opening_degree commands
# Use scheduler-based delays instead of blocking sleep()
TRV_COMMAND_RETRY_INTERVAL_S = 2    # Wait time between command and feedback check (seconds)
TRV_COMMAND_MAX_RETRIES = 3         # Max retries per command
TRV_COMMAND_FEEDBACK_TOLERANCE = 5  # Percent tolerance for feedback match

# ============================================================================
# Home Assistant Helper Entities (Expected to Exist)
# ============================================================================

# Master control
HELPER_MASTER_ENABLE = "input_boolean.pyheat_master_enable"
HELPER_HOLIDAY_MODE = "input_boolean.pyheat_holiday_mode"

# Boiler actor (on/off control)
HELPER_BOILER_ACTOR = "input_boolean.pyheat_boiler_actor"

# Per-room helpers (format strings - use .format(room=room_id))
HELPER_ROOM_MODE = "input_select.pyheat_{room}_mode"  # auto, manual, off
HELPER_ROOM_MANUAL_SETPOINT = "input_number.pyheat_{room}_manual_setpoint"
HELPER_ROOM_OVERRIDE_TIMER = "timer.pyheat_{room}_override"
HELPER_ROOM_OVERRIDE_TARGET = "input_number.pyheat_{room}_override_target"

# Pump overrun persistence
HELPER_PUMP_OVERRUN_TIMER = "timer.pyheat_boiler_pump_overrun_timer"
HELPER_PUMP_OVERRUN_VALVES = "input_text.pyheat_pump_overrun_valves"

# Boiler anti-cycling timers (event-driven using timer helpers)
HELPER_BOILER_MIN_ON_TIMER = "timer.pyheat_boiler_min_on_timer"
HELPER_BOILER_MIN_OFF_TIMER = "timer.pyheat_boiler_min_off_timer"
HELPER_BOILER_OFF_DELAY_TIMER = "timer.pyheat_boiler_off_delay_timer"

# Status entity
STATUS_ENTITY = "sensor.pyheat_status"

# ============================================================================
# Scheduling & Timing
# ============================================================================

# Recompute interval (seconds) - how often to check and update everything
RECOMPUTE_INTERVAL_S = 60  # Once per minute

# Startup delays
STARTUP_INITIAL_DELAY_S = 2   # Initial recompute delay
STARTUP_SECOND_DELAY_S = 10   # Second recompute for late-restoring sensors

# ============================================================================
# Logging
# ============================================================================

# Log level for debugging specific modules
LOG_LEVEL_DEFAULT = "INFO"
LOG_LEVEL_VERBOSE = "DEBUG"
