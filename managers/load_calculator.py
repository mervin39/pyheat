# -*- coding: utf-8 -*-
"""
load_calculator.py - Radiator capacity estimation for load-based optimization

Responsibilities:
- Calculate estimated heat dump capacity per radiator using EN 442 thermal model
- Read delta_t50 ratings from room configs
- Track temperature conditions for capacity calculations
- Expose Home Assistant sensors for monitoring
- Provide query API for future selection logic

CRITICAL: All calculated capacities are ESTIMATES (±20-30% accuracy) suitable
for relative comparison only, not absolute thermal calculations. Do not use
these values for matching against actual boiler power output or precise energy
accounting.

Implementation Note: Uses helper setpoint (input_number.pyheat_opentherm_setpoint)
instead of climate entity to ensure calculations remain valid during cycling
protection cooldown periods when the climate setpoint is temporarily dropped to 30°C.
"""

from datetime import datetime
from typing import Dict, List, Optional
import constants as C


class LoadCalculator:
    """Manages radiator capacity estimation using EN 442 thermal model.
    
    Calculates estimated heat output for each radiator based on:
    - Manufacturer-rated capacity at ΔT50 (delta_t50 from room config)
    - Current room temperature (from sensor_manager)
    - Desired system setpoint (from helper, not climate entity)
    - System delta T assumption (configurable)
    - Radiator heat transfer exponent (per-room or global default)
    
    All values are ESTIMATES for relative comparison, not precise measurements.
    """
    
    def __init__(self, ad, config, sensors):
        """Initialize the load calculator.
        
        Args:
            ad: AppDaemon API reference
            config: ConfigLoader instance
            sensors: SensorManager instance for current room temperatures
        """
        self.ad = ad
        self.config = config
        self.sensors = sensors
        
        # Cached estimated capacities per room {room_id: watts}
        self.estimated_capacities = {}
        
        # Last known helper setpoint (cached for unavailability)
        self.last_known_setpoint = None
        
        # Load monitoring configuration
        self.enabled = False
        self.system_delta_t = C.LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT
        self.global_radiator_exponent = C.LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT
        
    def initialize_from_ha(self) -> None:
        """Load initial state and validate configuration.
        
        Validates that all non-disabled rooms have delta_t50 configured.
        Loads load_monitoring configuration from boiler.yaml.
        
        Raises:
            ValueError: If any non-disabled room missing delta_t50
        """
        # Load load_monitoring config
        load_config = self.config.boiler_config.get('load_monitoring', {})
        self.enabled = load_config.get('enabled', True)
        self.system_delta_t = load_config.get('system_delta_t', C.LOAD_MONITORING_SYSTEM_DELTA_T_DEFAULT)
        self.global_radiator_exponent = load_config.get('radiator_exponent', C.LOAD_MONITORING_RADIATOR_EXPONENT_DEFAULT)
        
        if not self.enabled:
            self.ad.log("LoadCalculator: Disabled via configuration", level="INFO")
            return
        
        # Validate all rooms have delta_t50
        self._validate_configuration()
        
        # Initialize helper setpoint cache
        self._update_setpoint_cache()
        
        self.ad.log(
            f"LoadCalculator initialized: system_delta_t={self.system_delta_t}°C, "
            f"global_exponent={self.global_radiator_exponent}",
            level="INFO"
        )
        
    def _validate_configuration(self) -> None:
        """Validate all non-disabled rooms have required delta_t50 configured.
        
        Raises:
            ValueError: If any non-disabled room missing delta_t50
        """
        missing_rooms = []
        
        for room_id, room_cfg in self.config.rooms.items():
            if room_cfg.get('disabled'):
                continue
                
            delta_t50 = room_cfg.get('delta_t50')
            if delta_t50 is None:
                missing_rooms.append(room_id)
        
        if missing_rooms:
            error_msg = (
                f"LoadCalculator validation FAILED: The following rooms are missing "
                f"required 'delta_t50' configuration: {', '.join(missing_rooms)}\n\n"
                f"Please add delta_t50 (manufacturer-rated capacity in watts at ΔT50) "
                f"to each room in config/rooms.yaml. See debug/radiators.md for values."
            )
            self.ad.error(error_msg)
            raise ValueError(error_msg)
    
    def _update_setpoint_cache(self) -> Optional[float]:
        """Update cached helper setpoint value.
        
        CRITICAL: Always reads from input_number.pyheat_opentherm_setpoint helper,
        NOT from climate entity. Cycling protection temporarily drops the climate
        setpoint to 30°C which would invalidate capacity calculations.
        
        Returns:
            Current setpoint in °C, or None if unavailable
        """
        try:
            setpoint_str = self.ad.get_state(C.HELPER_OPENTHERM_SETPOINT)
            if setpoint_str and setpoint_str not in ['unknown', 'unavailable']:
                setpoint = float(setpoint_str)
                self.last_known_setpoint = setpoint
                return setpoint
        except (ValueError, TypeError) as e:
            self.ad.log(
                f"LoadCalculator: Failed to read helper setpoint: {e}",
                level="WARNING"
            )
        
        return self.last_known_setpoint
    
    def calculate_estimated_dump_capacity(self, room_id: str) -> float:
        """Calculate ESTIMATED radiator heat output under current conditions.
        
        Uses EN 442 standard formula:
            P = P₅₀ × (ΔT / 50)^n
        
        Where:
            P = Estimated power output (watts)
            P₅₀ = Rated capacity at ΔT50 (delta_t50 from config)
            ΔT = Current temperature difference (mean_water_temp - room_temp)
            n = Radiator exponent (1.3 for panels, 1.2 for towel rails)
        
        WARNING: This is an ESTIMATE with ±20-30% uncertainty. Suitable for
        relative comparison and selection logic ONLY.
        
        DO NOT use for:
        - Matching against actual boiler power
        - Precise thermal calculations
        - Energy accounting
        - Safety-critical decisions
        
        Args:
            room_id: Room identifier
            
        Returns:
            Estimated capacity in watts, or 0.0 if calculation fails
        """
        if not self.enabled:
            return 0.0
        
        room_cfg = self.config.rooms.get(room_id)
        if not room_cfg or room_cfg.get('disabled'):
            return 0.0
        
        # Get delta_t50 rating (required, validated in initialize)
        delta_t50 = room_cfg.get('delta_t50', 0)
        if delta_t50 <= 0:
            return 0.0
        
        # Get radiator exponent (per-room override or global default)
        radiator_exponent = room_cfg.get('radiator_exponent', self.global_radiator_exponent)
        
        # Get current room temperature (use last known if stale)
        room_temp, is_stale = self.sensors.get_room_temperature(room_id)
        if room_temp is None:
            self.ad.log(
                f"LoadCalculator: No temperature available for {room_id}, cannot calculate capacity",
                level="DEBUG"
            )
            return 0.0
        
        # Get desired setpoint from helper (NOT climate entity)
        desired_setpoint = self._update_setpoint_cache()
        if desired_setpoint is None:
            self.ad.log(
                f"LoadCalculator: Helper setpoint unavailable, cannot calculate capacity",
                level="DEBUG"
            )
            return 0.0
        
        # Calculate estimated mean water temperature
        # Assumes: flow_temp ≈ setpoint, return_temp ≈ setpoint - system_delta_t
        estimated_mean_water_temp = desired_setpoint - (self.system_delta_t / 2.0)
        
        # Calculate temperature difference
        delta_t = estimated_mean_water_temp - room_temp
        
        # Validate delta_t (must be positive for heat transfer)
        if delta_t <= 0:
            return 0.0
        
        # Apply EN 442 formula: P = P₅₀ × (ΔT / 50)^n
        try:
            estimated_capacity = delta_t50 * pow(delta_t / 50.0, radiator_exponent)
            
            # Sanity check: capacity should be positive and reasonable
            if estimated_capacity < 0 or estimated_capacity > 50000:
                self.ad.log(
                    f"LoadCalculator: Unrealistic capacity for {room_id}: {estimated_capacity:.0f}W "
                    f"(delta_t={delta_t:.1f}, delta_t50={delta_t50})",
                    level="WARNING"
                )
                return 0.0
            
            return estimated_capacity
            
        except (ValueError, OverflowError) as e:
            self.ad.log(
                f"LoadCalculator: Calculation error for {room_id}: {e}",
                level="WARNING"
            )
            return 0.0
    
    def get_all_estimated_capacities(self) -> Dict[str, float]:
        """Get estimated capacities for all rooms.
        
        Returns:
            Dict mapping room_id -> estimated_watts
        """
        if not self.enabled:
            return {}
        
        capacities = {}
        for room_id in self.config.rooms.keys():
            if not self.config.rooms[room_id].get('disabled'):
                capacities[room_id] = self.calculate_estimated_dump_capacity(room_id)
        
        return capacities
    
    def get_sorted_by_estimated_capacity(self, room_ids: List[str]) -> List[str]:
        """Sort rooms by estimated capacity (highest first).
        
        Used as tiebreaker in selection logic when multiple candidate rooms exist.
        
        Args:
            room_ids: List of room IDs to sort
            
        Returns:
            List of room IDs sorted by estimated capacity (descending)
        """
        if not self.enabled or not room_ids:
            return room_ids
        
        # Calculate capacities for requested rooms
        capacities = {
            room_id: self.calculate_estimated_dump_capacity(room_id)
            for room_id in room_ids
        }
        
        # Sort by capacity (highest first)
        sorted_rooms = sorted(
            room_ids,
            key=lambda rid: capacities.get(rid, 0),
            reverse=True
        )
        
        return sorted_rooms
    
    def update_capacities(self) -> None:
        """Recalculate all capacities and update cached values.
        
        Called during periodic recompute to refresh capacity estimates.
        Cache is used by status_publisher for sensor updates.
        """
        if not self.enabled:
            self.estimated_capacities = {}
            return
        
        self.estimated_capacities = self.get_all_estimated_capacities()
    
    def get_total_estimated_capacity(self) -> float:
        """Get sum of all room estimated capacities.
        
        Returns:
            Total estimated system capacity in watts
        """
        if not self.enabled:
            return 0.0
        
        return sum(self.estimated_capacities.values())
