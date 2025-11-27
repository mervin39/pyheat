# -*- coding: utf-8 -*-
"""
load_sharing_state.py - State machine infrastructure for load sharing

Responsibilities:
- Define state enums for load sharing state machine
- Define data structures for room activations and context
- Provide computed properties for tier-based queries
- Track activation timing and exit conditions

Phase 0: Infrastructure only - no behavioral logic
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set


class LoadSharingState(Enum):
    """Load sharing state machine states.
    
    State progression:
    DISABLED → (never activates)
    INACTIVE → TIER1_ACTIVE → [TIER1_ESCALATED] → TIER2_ACTIVE → [TIER2_ESCALATED] → TIER3_ACTIVE → [TIER3_ESCALATED] → INACTIVE
    """
    DISABLED = "disabled"                    # Feature disabled via config or master switch
    INACTIVE = "inactive"                    # Monitoring but not active
    TIER1_ACTIVE = "tier1_active"           # Schedule-aware pre-warming active
    TIER1_ESCALATED = "tier1_escalated"     # Tier 1 rooms escalated to higher valve %
    TIER2_ACTIVE = "tier2_active"           # Extended lookahead active
    TIER2_ESCALATED = "tier2_escalated"     # Tier 2 rooms escalated to higher valve %
    TIER3_ACTIVE = "tier3_active"           # Fallback priority list active
    TIER3_ESCALATED = "tier3_escalated"     # Tier 3 rooms escalated to higher valve %


@dataclass
class RoomActivation:
    """Represents a single room activated for load sharing.
    
    Attributes:
        room_id: Room identifier
        tier: Which tier selected this room (1, 2, or 3)
        valve_pct: Current valve opening percentage
        activated_at: Timestamp when room was added to load sharing
        reason: Human-readable reason (e.g., "schedule_60m", "fallback_p1")
    """
    room_id: str
    tier: int
    valve_pct: int
    activated_at: datetime
    reason: str


@dataclass
class LoadSharingContext:
    """Single source of truth for load sharing state.
    
    Tracks current state, trigger conditions, and active room selections.
    Provides computed properties for tier-based queries.
    
    Attributes:
        state: Current state machine state
        trigger_calling_rooms: Set of room IDs that triggered activation
        trigger_capacity: Combined capacity of trigger rooms (watts)
        trigger_timestamp: When load sharing was activated
        active_rooms: Dictionary of currently active load sharing rooms
        last_evaluation: Timestamp of last evaluation (for debugging)
    """
    state: LoadSharingState = LoadSharingState.DISABLED
    trigger_calling_rooms: Set[str] = field(default_factory=set)
    trigger_capacity: float = 0.0
    trigger_timestamp: Optional[datetime] = None
    active_rooms: Dict[str, RoomActivation] = field(default_factory=dict)
    last_evaluation: Optional[datetime] = None
    
    # Computed properties
    
    @property
    def tier1_rooms(self) -> List[RoomActivation]:
        """Get all Tier 1 (schedule-aware) activated rooms."""
        return [room for room in self.active_rooms.values() if room.tier == 1]
    
    @property
    def tier2_rooms(self) -> List[RoomActivation]:
        """Get all Tier 2 (extended lookahead) activated rooms."""
        return [room for room in self.active_rooms.values() if room.tier == 2]
    
    @property
    def tier3_rooms(self) -> List[RoomActivation]:
        """Get all Tier 3 (fallback priority) activated rooms."""
        return [room for room in self.active_rooms.values() if room.tier == 3]
    
    def activation_duration(self, now: datetime) -> float:
        """Get duration in seconds since load sharing was activated.
        
        Args:
            now: Current timestamp
            
        Returns:
            Duration in seconds, or 0 if not activated
        """
        if self.trigger_timestamp is None:
            return 0.0
        return (now - self.trigger_timestamp).total_seconds()
    
    def can_exit(self, now: datetime, min_duration_s: float = 300) -> bool:
        """Check if minimum activation duration has elapsed.
        
        Prevents rapid oscillation by enforcing minimum active period.
        
        Args:
            now: Current timestamp
            min_duration_s: Minimum activation duration (default 300s = 5 minutes)
            
        Returns:
            True if minimum duration elapsed or never activated
        """
        if self.trigger_timestamp is None:
            return True
        return self.activation_duration(now) >= min_duration_s
    
    def has_tier3_timeouts(self, now: datetime, timeout_s: float = 900) -> bool:
        """Check if any Tier 3 rooms have exceeded their timeout.
        
        Tier 3 fallback rooms have a maximum activation duration to prevent
        long-term unwanted heating.
        
        Args:
            now: Current timestamp
            timeout_s: Maximum Tier 3 duration (default 900s = 15 minutes)
            
        Returns:
            True if any Tier 3 room has exceeded timeout
        """
        for room in self.tier3_rooms:
            duration = (now - room.activated_at).total_seconds()
            if duration >= timeout_s:
                return True
        return False
    
    def is_active(self) -> bool:
        """Check if load sharing is currently active (any tier)."""
        return self.state not in [LoadSharingState.DISABLED, LoadSharingState.INACTIVE]
    
    def reset(self) -> None:
        """Reset context to inactive state (clear all activations)."""
        self.state = LoadSharingState.INACTIVE
        self.trigger_calling_rooms.clear()
        self.trigger_capacity = 0.0
        self.trigger_timestamp = None
        self.active_rooms.clear()
