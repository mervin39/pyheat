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

HYSTERESIS_DEFAULT: Dict[str, float] = {
    "on_delta_c": 0.30,   # Start heating when 0.3°C below target
    "off_delta_c": 0.10,  # Stop heating when 0.1°C below target (near enough)
}

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
# Boiler Safety & Anti-Cycling
# ============================================================================

# Safety parameters for boiler control
SAFETY_DEFAULT: Dict[str, float] = {
    # Anti short-cycling
    "min_on_s": 180,      # Minimum boiler ON duration (3 minutes)
    "min_off_s": 180,     # Minimum boiler OFF duration (3 minutes)
    
    # TRV-open interlock
    "min_open_percent": 10,      # Minimum valve opening to consider "open"
    "feedback_timeout_s": 30,    # Max time to wait for TRV feedback confirmation
}

# ============================================================================
# Boiler Configuration
# ============================================================================

# Default minimum total valve opening percentage for boiler safety interlock
# Sum of all TRV open percentages must be >= this value before boiler can turn on
# This ensures water always has somewhere to go
BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT = 100

# Default anti-cycling times (seconds)
BOILER_MIN_ON_TIME_DEFAULT = 180   # 3 minutes minimum on time
BOILER_MIN_OFF_TIME_DEFAULT = 180  # 3 minutes minimum off time
BOILER_OFF_DELAY_DEFAULT = 30      # 30 second delay before turning off

# Default pump overrun time (seconds)
BOILER_PUMP_OVERRUN_DEFAULT = 180  # 3 minutes to dissipate residual heat

# Default binary control setpoints (when not using OpenTherm modulation)
BOILER_BINARY_ON_SETPOINT_DEFAULT = 30.0   # °C
BOILER_BINARY_OFF_SETPOINT_DEFAULT = 5.0   # °C

# ============================================================================
# TRV Entity Patterns (Zigbee2MQTT via Home Assistant)
# ============================================================================

# Entity derivation from climate.<trv_base>
# Example: climate.living_trv → trv_base = "living_trv"
#
# Derived entities:
#   Commands (number.*):
#     - number.living_trv_valve_opening_degree
#     - number.living_trv_valve_closing_degree
#   Feedback (sensor.* via MQTT):
#     - sensor.living_trv_valve_opening_degree_z2m
#     - sensor.living_trv_valve_closing_degree_z2m

TRV_ENTITY_PATTERNS: Dict[str, str] = {
    "cmd_open":  "number.{trv_base}_valve_opening_degree",
    "cmd_close": "number.{trv_base}_valve_closing_degree",
    "fb_open":   "sensor.{trv_base}_valve_opening_degree_z2m",
    "fb_close":  "sensor.{trv_base}_valve_closing_degree_z2m",
}

# Valve percentages are sent as integers (0-100)
VALVE_PERCENT_INTEGER = True

# TRV Command Sequencing (Anti-Thrashing)
# To avoid inconsistent feedback states, commands are sent sequentially:
# 1. Set opening degree, wait for confirmation
# 2. Set closing degree, wait for confirmation
TRV_COMMAND_SEQUENCE_ENABLED = True
TRV_COMMAND_RETRY_INTERVAL_S = 10  # Retry interval if feedback not confirmed
TRV_COMMAND_MAX_RETRIES = 6        # Max retries (60s total)
TRV_COMMAND_FEEDBACK_TOLERANCE = 5  # Percent tolerance for feedback match

# ============================================================================
# Helper Entity Patterns
# ============================================================================

# Per-room helpers created in configuration.yaml
HELPER_PATTERNS: Dict[str, str] = {
    "mode": "input_select.pyheat_{room}_mode",
    "manual_setpoint": "input_number.pyheat_{room}_manual_setpoint",
    "override_timer": "timer.pyheat_{room}_override",
}

# Global helpers
HELPER_GLOBAL: Dict[str, str] = {
    "master_enable": "input_boolean.pyheat_master_enable",
    "holiday_mode": "input_boolean.pyheat_holiday_mode",
    "boiler_actor": "input_boolean.pyheat_boiler_actor",
}

# ============================================================================
# Pyheat Entity Patterns (Created by App)
# ============================================================================

# Per-room entities created and managed by Pyheat
PYHEAT_ENTITY_PATTERNS: Dict[str, str] = {
    "temperature": "sensor.pyheat_{room}_temperature",
    "target": "sensor.pyheat_{room}_target",
    "valve_percent": "number.pyheat_{room}_valve_percent",
    "calling_for_heat": "binary_sensor.pyheat_{room}_calling_for_heat",
    "state": "sensor.pyheat_{room}_state",
    "status": "sensor.pyheat_{room}_status",
}

# Global entities
PYHEAT_ENTITY_GLOBAL: Dict[str, str] = {
    "any_call_for_heat": "binary_sensor.pyheat_any_call_for_heat",
    "status": "sensor.pyheat_status",
}

# ============================================================================
# Mode Values
# ============================================================================

# Valid room modes
MODES = {"auto", "manual", "off"}

# Room states (internal)
ROOM_STATES = {"auto", "manual", "off", "stale"}

# ============================================================================
# Schedule Validation
# ============================================================================

# Time format for schedule blocks
TIME_FORMAT = "%H:%M"

# Valid weekday keys for schedules
WEEKDAY_KEYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# ============================================================================
# Startup & Recompute
# ============================================================================

# Grace period before first boiler ON after startup (seconds)
# Allows TRV feedback to settle for interlock check
STARTUP_GRACE_S = 10

# Delayed recompute after startup for late-restoring sensors (seconds)
STARTUP_DELAYED_RECOMPUTE_S = 5

# ============================================================================
# Utility Functions
# ============================================================================

def get_trv_entities(trv_base: str) -> Dict[str, str]:
    """Derive all TRV entity IDs from the base name.
    
    Args:
        trv_base: Base name from climate entity (e.g., "living_trv")
        
    Returns:
        Dict with keys: cmd_open, cmd_close, fb_open, fb_close
        
    Example:
        >>> get_trv_entities("living_trv")
        {
            'cmd_open': 'number.living_trv_valve_opening_degree',
            'cmd_close': 'number.living_trv_valve_closing_degree',
            'fb_open': 'sensor.living_trv_valve_opening_degree_z2m',
            'fb_close': 'sensor.living_trv_valve_closing_degree_z2m'
        }
    """
    return {
        key: pattern.format(trv_base=trv_base)
        for key, pattern in TRV_ENTITY_PATTERNS.items()
    }


def get_helper_entities(room: str) -> Dict[str, str]:
    """Get all helper entity IDs for a room.
    
    Args:
        room: Room ID
        
    Returns:
        Dict with keys: mode, manual_setpoint, override_timer
    """
    return {
        key: pattern.format(room=room)
        for key, pattern in HELPER_PATTERNS.items()
    }


def get_pyheat_entities(room: str) -> Dict[str, str]:
    """Get all Pyheat-created entity IDs for a room.
    
    Args:
        room: Room ID
        
    Returns:
        Dict with keys: temperature, target, valve_percent, etc.
    """
    return {
        key: pattern.format(room=room)
        for key, pattern in PYHEAT_ENTITY_PATTERNS.items()
    }


def validate_target(target: float) -> bool:
    """Check if a target temperature is within valid bounds.
    
    Args:
        target: Temperature in °C
        
    Returns:
        True if valid, False otherwise
    """
    return TARGET_MIN_C <= target <= TARGET_MAX_C


def validate_precision(precision: int) -> bool:
    """Check if precision value is valid.
    
    Args:
        precision: Number of decimal places
        
    Returns:
        True if valid, False otherwise
    """
    return precision in PRECISION_ALLOWED


def validate_mode(mode: str) -> bool:
    """Check if mode is valid.
    
    Args:
        mode: Mode string
        
    Returns:
        True if valid, False otherwise
    """
    return mode in MODES
