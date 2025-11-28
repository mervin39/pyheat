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
# Debug Flags
# ============================================================================

# Enable verbose API debug logging (set False for normal operation)
# When True: logs all API requests/responses and timer states
# When False: only logs API errors and warnings
DEBUG_API_LOGGING = False

# ============================================================================
# Holiday Mode
# ============================================================================

# Target temperature (°C) used when holiday mode is active
# Applies as the base schedule target; overrides still work
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
#
# Temperature zones:
# - Zone 1 (too cold): t < S - on_delta_c → START/Continue heating
# - Zone 2 (deadband): S - on_delta_c ≤ t ≤ S + off_delta_c → MAINTAIN state
# - Zone 3 (too warm): t > S + off_delta_c → STOP heating
#
# Where:
#   t = current room temperature
#   S = setpoint (target temperature)
#   error = S - t (positive when below target, negative when above)
#
# Normal operation uses all three zones with state persistence in deadband.
# When target changes: bypass deadband, heat until t > S + off_delta_c

HYSTERESIS_DEFAULT: Dict[str, float] = {
    "on_delta_c": 0.30,   # Start heating when temp falls below target - 0.30°C
    "off_delta_c": 0.10,  # Stop heating when temp rises above target + 0.10°C
}

# Target change detection - bypass hysteresis deadband when target changes
TARGET_CHANGE_EPSILON = 0.01  # °C - target changes smaller than this are ignored (floating point tolerance)

# ============================================================================
# Smart TRV Control - Valve Bands
# ============================================================================

# Proportional valve control with 3 heating bands (0/1/2 thresholds supported)
# error = target - temp (°C; positive means below target, negative means above)
#
# Band Logic (2 thresholds = 3 bands + Band 0):
#   Band 0: not calling                    → band_0_percent (default 0%)
#   Band 1: error < band_1_error           → band_1_percent (gentle, close to target)
#   Band 2: band_1_error ≤ error < band_2_error → band_2_percent (moderate distance)
#   Band Max: error ≥ band_2_error         → band_max_percent (far from target)
#
# Thresholds define UPPER bounds for gentler bands (not lower bounds)
# Missing percentages cascade to next higher band (graceful degradation)
# step_hysteresis_c dampens band transitions to prevent oscillation

VALVE_BANDS_DEFAULT: Dict[str, float] = {
    # Error thresholds (temperature °C below setpoint)
    "band_1_error": 0.30,   # Band 1 applies when error < 0.30°C
    "band_2_error": 0.80,   # Band 2 applies when 0.30 ≤ error < 0.80°C
                            # Band Max applies when error ≥ 0.80°C
    
    # Valve opening percentages (0-100)
    "band_0_percent": 0.0,      # Not calling (default 0%, configurable for slight opening)
    "band_1_percent": 40.0,     # Close to target (gentle heating)
    "band_2_percent": 70.0,     # Moderate distance (moderate heating)
    "band_max_percent": 100.0,  # Far from target (maximum heating)
    
    # Band transition hysteresis (°C) - prevents oscillation
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

# Boiler Configuration Defaults
BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT = 100  # Minimum total valve opening required
BOILER_MIN_ON_TIME_DEFAULT = 180             # 3 minutes minimum on time
BOILER_MIN_OFF_TIME_DEFAULT = 180            # 3 minutes minimum off time
BOILER_OFF_DELAY_DEFAULT = 30                # 30 second delay before turning off
BOILER_PUMP_OVERRUN_DEFAULT = 180            # 3 minutes to dissipate residual heat

# Boiler State Machine States
STATE_OFF = "off"
STATE_PENDING_ON = "pending_on"
STATE_ON = "on"
STATE_PENDING_OFF = "pending_off"
STATE_PUMP_OVERRUN = "pump_overrun"
STATE_INTERLOCK_BLOCKED = "interlock_blocked"

# ============================================================================
# Short-Cycling Protection (OpenTherm Return Temperature Monitoring)
# ============================================================================

# DHW detection - sensor delay for state stabilization
CYCLING_SENSOR_DELAY_S = 2  # Wait for OpenTherm sensors to update after flame OFF

# High return temp detection threshold (delta from setpoint)
# When return_temp >= (setpoint - delta), cooldown is triggered
CYCLING_HIGH_RETURN_DELTA_C = 10  # e.g., 60°C when setpoint is 70°C

# Cooldown setpoint (must be climate entity minimum)
CYCLING_COOLDOWN_SETPOINT = 30  # °C - prevents re-ignition during cooldown

# Recovery threshold calculation
CYCLING_RECOVERY_DELTA_C = 15  # °C below saved setpoint
CYCLING_RECOVERY_MIN_C = 35    # °C absolute minimum (safety margin above cooldown)

# Recovery monitoring interval
CYCLING_RECOVERY_MONITORING_INTERVAL_S = 10  # Check every 10 seconds

# Timeout protection (force recovery if stuck)
CYCLING_COOLDOWN_MAX_DURATION_S = 1800  # 30 minutes

# Excessive cycling detection
CYCLING_EXCESSIVE_COUNT = 3      # Cooldowns to trigger alert
CYCLING_EXCESSIVE_WINDOW_S = 3600  # Time window (1 hour)

# Helper entities
HELPER_OPENTHERM_SETPOINT = "input_number.pyheat_opentherm_setpoint"
HELPER_CYCLING_STATE = "input_text.pyheat_cycling_protection_state"

# ============================================================================
# Load-Based Capacity Estimation (EN 442 Thermal Model)
# ============================================================================

# System delta T assumption for mean water temperature estimation
# Mean water temp = setpoint - (system_delta_t / 2)
# Observed range: 7-16°C, default to middle of range
LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT = 10  # °C

# Radiator heat transfer exponent (EN 442 standard)
# Panel radiators: 1.3 (standard double panel convector)
# Towel rails: 1.2-1.25 (tube geometry, air gaps)
# Can be overridden per-room in rooms.yaml
LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT = 1.3

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

# Per-room helpers (format strings - use .format(room=room_id))
HELPER_ROOM_MODE = "input_select.pyheat_{room}_mode"  # auto, manual, off
HELPER_ROOM_MANUAL_SETPOINT = "input_number.pyheat_{room}_manual_setpoint"
HELPER_ROOM_OVERRIDE_TIMER = "timer.pyheat_{room}_override"
HELPER_ROOM_OVERRIDE_TARGET = "input_number.pyheat_{room}_override_target"

# Pump overrun persistence
HELPER_PUMP_OVERRUN_TIMER = "timer.pyheat_boiler_pump_overrun_timer"
HELPER_PUMP_OVERRUN_VALVES = "input_text.pyheat_pump_overrun_valves"  # Deprecated - use HELPER_ROOM_PERSISTENCE

# Room state persistence (unified: valve positions + calling state)
HELPER_ROOM_PERSISTENCE = "input_text.pyheat_room_persistence"

# Boiler anti-cycling timers (event-driven using timer helpers)
HELPER_BOILER_MIN_ON_TIMER = "timer.pyheat_boiler_min_on_timer"
HELPER_BOILER_MIN_OFF_TIMER = "timer.pyheat_boiler_min_off_timer"
HELPER_BOILER_OFF_DELAY_TIMER = "timer.pyheat_boiler_off_delay_timer"

# Status entity
STATUS_ENTITY = "sensor.pyheat_status"

# ============================================================================
# OpenTherm Integration Sensors (Monitoring Only)
# ============================================================================

# OpenTherm sensors to monitor (do not trigger recomputes, debug logging only)
OPENTHERM_FLAME = "binary_sensor.opentherm_flame"
OPENTHERM_HEATING_TEMP = "sensor.opentherm_heating_temp"
OPENTHERM_HEATING_RETURN_TEMP = "sensor.opentherm_heating_return_temp"
OPENTHERM_HEATING_SETPOINT_TEMP = "sensor.opentherm_heating_setpoint_temp"
OPENTHERM_POWER = "sensor.opentherm_power"
OPENTHERM_MODULATION = "sensor.opentherm_modulation_level"
OPENTHERM_BURNER_STARTS = "sensor.opentherm_burner_starts"
OPENTHERM_DHW_BURNER_STARTS = "sensor.opentherm_dhw_burner_starts"
OPENTHERM_DHW = "binary_sensor.opentherm_dhw"
OPENTHERM_DHW_FLOW_RATE = "sensor.opentherm_dhw_flow_rate"
OPENTHERM_CLIMATE = "climate.opentherm_heating"

# ============================================================================
# System State Logging (Temporary - for data collection)
# ============================================================================

# Enable comprehensive heating system logging to CSV files
# This is a temporary feature for collecting data to develop OpenTherm
# optimization algorithms. Will be removed once sufficient data is collected.
ENABLE_HEATING_LOGS = True  # Set to False to disable

# ============================================================================
# Scheduling & Timing
# ============================================================================

# Recompute interval (seconds) - how often to check and update everything
RECOMPUTE_INTERVAL_S = 60  # Once per minute

# Startup delays
STARTUP_INITIAL_DELAY_S = 2   # Initial recompute delay
STARTUP_SECOND_DELAY_S = 10   # Second recompute for late-restoring sensors

# ============================================================================
# Temperature Smoothing (Exponential Moving Average)
# ============================================================================

# Default EMA smoothing parameters for fused temperature display
# Smoothing reduces visual noise when sensors in different room locations
# report slightly different temperatures that cause averaged result to flip
# across rounding boundaries.
#
# Applied AFTER sensor fusion, preserves spatial averaging intent while
# reducing temporal noise in the displayed temperature.
#
# alpha = smoothing factor (0.0 to 1.0)
#   - 0.0 = maximum smoothing (100% history, 0% new reading) - TOO SLOW
#   - 1.0 = no smoothing (0% history, 100% new reading) - current behavior
#   - 0.3 = recommended (30% new, 70% history) - balances responsiveness and stability
#
# Time constant: ~3 sensor updates for 95% of step change to reflect
# With sensors updating every 30-60s, this means 1.5-3 minutes to fully respond
TEMPERATURE_SMOOTHING_ALPHA_DEFAULT = 0.3

# ============================================================================
# Load Sharing Configuration
# ============================================================================

# Load sharing master enable switch (Home Assistant helper)
HELPER_LOAD_SHARING_ENABLE = "input_boolean.pyheat_load_sharing_enable"

# Load sharing capacity thresholds (watts)
LOAD_SHARING_MIN_CALLING_CAPACITY_W_DEFAULT = 3500  # Activation threshold
LOAD_SHARING_TARGET_CAPACITY_W_DEFAULT = 4000       # Target capacity to reach

# Load sharing timing constraints (seconds)
LOAD_SHARING_MIN_ACTIVATION_DURATION_S_DEFAULT = 300  # 5 minutes minimum
LOAD_SHARING_TIER3_TIMEOUT_S_DEFAULT = 900           # 15 minutes max for Tier 3
LOAD_SHARING_TIER3_COOLDOWN_S_DEFAULT = 1800         # 30 minutes before re-eligible

# Load sharing valve opening defaults (percent)
LOAD_SHARING_TIER1_INITIAL_PCT = 70   # Schedule-aware rooms start here
LOAD_SHARING_TIER1_ESCALATED_PCT = 80 # Escalated if insufficient
LOAD_SHARING_TIER2_INITIAL_PCT = 40   # Extended window rooms (gentle)
LOAD_SHARING_TIER2_ESCALATED_PCT = 50 # Escalated if insufficient
LOAD_SHARING_TIER3_INITIAL_PCT = 50   # Fallback rooms (compromise)
LOAD_SHARING_TIER3_ESCALATED_PCT = 60 # Escalated if insufficient

# Load sharing schedule lookahead defaults (minutes)
LOAD_SHARING_SCHEDULE_LOOKAHEAD_M_DEFAULT = 60  # Default lookahead window

# ============================================================================
# Logging
# ============================================================================

# Log level for debugging specific modules
LOG_LEVEL_DEFAULT = "INFO"
LOG_LEVEL_VERBOSE = "DEBUG"
