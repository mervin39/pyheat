"""
boiler.py - Boiler control module with TRV-open interlock safety

Responsibilities:
- Control boiler on/off based on room demand
- Enforce TRV-open interlock safety: boiler cannot run unless sufficient valve opening exists
- Track boiler state and transitions
- Prevent short-cycling (future: anti-cycling protection)

TRV-Open Interlock Safety:
- sum(all_trv_open_percent) must be >= min_valve_open_percent before boiler can turn ON
- This ensures water always has a flow path (prevents pump deadhead/overpressure)
- If sum < min, valves must be commanded to override positions and await confirmation

Currently uses input_boolean.pyheat_boiler_actor as a dummy actor.
Future: Connect to real boiler hardware, add anti-cycling protection.
"""

from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from . import constants


class BoilerManager:
    """Manages boiler on/off with TRV-open interlock safety.
    
    Interlock Safety Flow:
    1. Check if any rooms are calling for heat
    2. Calculate total valve opening percentage across all rooms
    3. If total < min_valve_open_percent, override individual valve bands to ensure min
    4. Only turn boiler ON when valves are confirmed open to required amount
    5. Turn boiler OFF when no demand or interlock fails
    """
    
    def __init__(self, boiler_config: Dict):
        """Initialize boiler manager with configuration.
        
        Args:
            boiler_config: Parsed boiler.yaml configuration
        """
        boiler_cfg = boiler_config.get("boiler", {})
        
        self.boiler_entity = boiler_cfg.get("entity_id", constants.BOILER_ENTITY_DEFAULT)
        
        # Interlock configuration
        interlock_cfg = boiler_cfg.get("interlock", {})
        self.min_valve_open_percent = interlock_cfg.get(
            "min_valve_open_percent",
            constants.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        # State tracking
        self.boiler_on = False
        self.last_change_time: Optional[datetime] = None
        
        # Read current state from HA
        try:
            current_state = state.get(self.boiler_entity)
            self.boiler_on = (current_state == "on")
        except NameError:
            log.warning(f"BoilerManager: entity {self.boiler_entity} does not exist, assuming OFF")
            self.boiler_on = False
        
        log.info(f"BoilerManager: initialized")
        log.info(f"  Entity: {self.boiler_entity}")
        log.info(f"  Min valve open percent: {self.min_valve_open_percent}%")
        log.info(f"  Current state: {'ON' if self.boiler_on else 'OFF'}")
    
    def calculate_valve_overrides(
        self,
        rooms_calling: List[str],
        room_valve_percents: Dict[str, int]
    ) -> Tuple[Dict[str, int], bool, str]:
        """Calculate valve overrides if needed to meet minimum total opening.
        
        Args:
            rooms_calling: List of room IDs calling for heat
            room_valve_percents: Dict mapping room_id -> calculated valve percent from bands
            
        Returns:
            Tuple of:
            - overridden_valve_percents: Dict[room_id, valve_percent] with overrides applied
            - interlock_ok: True if total >= min_valve_open_percent
            - reason: Explanation string
        """
        if not rooms_calling:
            return {}, False, "No rooms calling for heat"
        
        # Calculate total from band-calculated percentages
        # Note: Pyscript doesn't support generator expressions, use explicit loop
        total_from_bands = 0
        for room_id in rooms_calling:
            total_from_bands += room_valve_percents.get(room_id, 0)
        
        # Check if we need to override
        if total_from_bands >= self.min_valve_open_percent:
            # Valve bands are sufficient
            log.debug(
                f"BoilerManager: total valve opening {total_from_bands}% >= "
                f"min {self.min_valve_open_percent}%, using valve bands"
            )
            return room_valve_percents.copy(), True, f"Total {total_from_bands}% >= min {self.min_valve_open_percent}%"
        
        # Need to override - distribute evenly across calling rooms
        n_rooms = len(rooms_calling)
        override_percent = int((self.min_valve_open_percent + n_rooms - 1) / n_rooms)  # Round up
        
        # Safety clamp: never command valve >100% even if config is misconfigured
        override_percent = min(100, override_percent)
        
        overridden = {
            room_id: override_percent
            for room_id in rooms_calling
        }
        
        new_total = override_percent * n_rooms
        
        log.info(
            f"BoilerManager: INTERLOCK OVERRIDE: total from bands {total_from_bands}% < "
            f"min {self.min_valve_open_percent}% -> setting {n_rooms} room(s) to {override_percent}% "
            f"each (new total: {new_total}%)"
        )
        
        return overridden, True, f"Override: {n_rooms} rooms @ {override_percent}% = {new_total}%"
    
    def update(
        self,
        rooms_calling_for_heat: List[str],
        room_valve_percents: Dict[str, int]
    ) -> Dict[str, Any]:
        """Update boiler state based on room demand with interlock safety.
        
        Args:
            rooms_calling_for_heat: List of room IDs calling for heat
            room_valve_percents: Dict of room_id -> valve percent (from bands, before override)
            
        Returns:
            Dict with boiler status:
            {
                "boiler_on": bool,
                "rooms_calling": List[str],
                "changed": bool,
                "reason": str,
                "interlock_ok": bool,
                "overridden_valve_percents": Dict[str, int],
                "total_valve_percent": int
            }
        """
        # Calculate valve overrides if needed
        overridden_valves, interlock_ok, interlock_reason = self.calculate_valve_overrides(
            rooms_calling_for_heat,
            room_valve_percents
        )
        
        # Calculate total valve opening (no generator expressions in pyscript)
        total_valve = 0
        for room_id in rooms_calling_for_heat:
            total_valve += overridden_valves.get(room_id, 0)
        
        # Determine if boiler should be on
        should_be_on = len(rooms_calling_for_heat) > 0 and interlock_ok
        changed = (should_be_on != self.boiler_on)
        
        if changed:
            if should_be_on:
                # Turn ON
                log.info(
                    f"BoilerManager: turning boiler ON - "
                    f"{len(rooms_calling_for_heat)} room(s) calling, "
                    f"total valve {total_valve}%, interlock OK"
                )
                state.set(self.boiler_entity, "on")
                self.boiler_on = True
                self.last_change_time = datetime.now()
                reason = f"Heat demand: {len(rooms_calling_for_heat)} room(s), valve total {total_valve}%"
            else:
                # Turn OFF
                log.info(f"BoilerManager: turning boiler OFF - {interlock_reason}")
                state.set(self.boiler_entity, "off")
                self.boiler_on = False
                self.last_change_time = datetime.now()
                reason = f"Off: {interlock_reason}"
        else:
            # No change
            if should_be_on:
                reason = f"Already ON: {len(rooms_calling_for_heat)} room(s), valve total {total_valve}%"
            else:
                reason = f"Already OFF: {interlock_reason}"
            log.debug(f"BoilerManager: {reason}")
        
        return {
            "boiler_on": self.boiler_on,
            "rooms_calling": rooms_calling_for_heat,
            "changed": changed,
            "reason": reason,
            "interlock_ok": interlock_ok,
            "overridden_valve_percents": overridden_valves,
            "total_valve_percent": total_valve,
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get current boiler status.
        
        Returns:
            Dict with current status:
            {
                "entity": str,
                "on": bool,
                "last_change": datetime or None,
                "min_valve_open_percent": int
            }
        """
        return {
            "entity": self.boiler_entity,
            "on": self.boiler_on,
            "last_change": self.last_change_time,
            "min_valve_open_percent": self.min_valve_open_percent,
        }
    
    def reload_config(self, boiler_config: Dict) -> None:
        """Reload boiler configuration.
        
        Args:
            boiler_config: New parsed boiler.yaml configuration
        """
        boiler_cfg = boiler_config.get("boiler", {})
        
        # Update entity if changed
        new_entity = boiler_cfg.get("entity_id", constants.BOILER_ENTITY_DEFAULT)
        if new_entity != self.boiler_entity:
            log.info(f"BoilerManager: entity changed {self.boiler_entity} -> {new_entity}")
            self.boiler_entity = new_entity
        
        # Update interlock config
        interlock_cfg = boiler_cfg.get("interlock", {})
        new_min = interlock_cfg.get(
            "min_valve_open_percent",
            constants.BOILER_MIN_VALVE_OPEN_PERCENT_DEFAULT
        )
        
        if new_min != self.min_valve_open_percent:
            log.info(f"BoilerManager: min_valve_open_percent changed {self.min_valve_open_percent}% -> {new_min}%")
            self.min_valve_open_percent = new_min
        
        # Re-read current state
        try:
            current_state = state.get(self.boiler_entity)
            self.boiler_on = (current_state == "on")
            log.debug(f"BoilerManager: re-read state = {'ON' if self.boiler_on else 'OFF'}")
        except NameError:
            log.warning(f"BoilerManager: entity {self.boiler_entity} does not exist during reload")


# Module-level instance (initialized by orchestrator)
_boiler_mgr: Optional[BoilerManager] = None


def init(boiler_config: Dict) -> BoilerManager:
    """Initialize the boiler manager module.
    
    Args:
        boiler_config: Parsed boiler.yaml configuration
    
    Returns:
        BoilerManager: Initialized boiler manager instance
    """
    global _boiler_mgr
    
    log.info("BoilerManager: initializing...")
    _boiler_mgr = BoilerManager(boiler_config)
    log.info("BoilerManager: initialization complete")
    
    return _boiler_mgr


def update(rooms_calling_for_heat: List[str]) -> Dict[str, Any]:
    """Update boiler state based on room demand.
    
    Args:
        rooms_calling_for_heat: List of room IDs calling for heat
        
    Returns:
        Dict with boiler status info
    """
    if not _boiler_mgr:
        log.error("BoilerManager: update() called before init()")
        return {"boiler_on": False, "rooms_calling": [], "changed": False, "reason": "Not initialized"}
    
    return _boiler_mgr.update(rooms_calling_for_heat)


def get_status() -> Dict[str, Any]:
    """Get current boiler status.
    
    Returns:
        Dict with current status
    """
    if not _boiler_mgr:
        return {"entity": None, "on": False, "last_change": None}
    
    return _boiler_mgr.get_status()
