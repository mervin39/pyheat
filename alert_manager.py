# -*- coding: utf-8 -*-
"""
alert_manager.py - Alert and notification management for PyHeat

Responsibilities:
- Track error states with debouncing to avoid spam
- Create Home Assistant persistent notifications for critical issues
- Auto-clear alerts when conditions resolve
- Rate limit notifications to prevent notification flooding
- Provide structured alert tracking with severity levels
"""

from datetime import datetime, timedelta
from typing import Dict, Optional, Set
import pyheat.constants as C


class AlertManager:
    """Manages alerts and notifications for PyHeat critical issues."""
    
    # Alert severity levels
    SEVERITY_CRITICAL = "critical"  # Immediate attention required
    SEVERITY_WARNING = "warning"    # Issue detected but not urgent
    
    # Alert types (unique identifiers)
    ALERT_BOILER_INTERLOCK_FAILURE = "boiler_interlock_failure"
    ALERT_BOILER_STATE_DESYNC = "boiler_state_desync"
    ALERT_SAFETY_ROOM_ACTIVE = "safety_room_active"
    ALERT_TRV_FEEDBACK_TIMEOUT = "trv_feedback_timeout"
    ALERT_TRV_UNAVAILABLE = "trv_unavailable"
    ALERT_CONFIG_LOAD_FAILURE = "config_load_failure"
    ALERT_BOILER_CONTROL_FAILURE = "boiler_control_failure"
    
    def __init__(self, ad):
        """Initialize the alert manager.
        
        Args:
            ad: AppDaemon API reference
        """
        self.ad = ad
        
        # Active alerts: {alert_id: {severity, message, timestamp, room_id, consecutive_count}}
        self.active_alerts: Dict[str, Dict] = {}
        
        # Alert history for rate limiting: {alert_id: last_notified_timestamp}
        self.notification_history: Dict[str, datetime] = {}
        
        # Debouncing: require N consecutive errors before alerting
        self.debounce_threshold = 3  # Require 3 consecutive errors
        
        # Rate limiting: max 1 notification per alert per hour
        self.rate_limit_seconds = 3600
        
        # Track error counts for debouncing: {alert_id: count}
        self.error_counts: Dict[str, int] = {}
        
    def report_error(self, alert_id: str, severity: str, message: str, 
                    room_id: Optional[str] = None, auto_clear: bool = True) -> None:
        """Report an error condition that may trigger an alert.
        
        This implements debouncing - the alert will only be created after
        the error has been reported consecutively debounce_threshold times.
        
        Args:
            alert_id: Unique identifier for this alert type
            severity: SEVERITY_CRITICAL or SEVERITY_WARNING
            message: Human-readable description of the issue
            room_id: Room affected (if applicable)
            auto_clear: Whether this alert can auto-clear when condition resolves
        """
        now = datetime.now()
        
        # Increment consecutive error count
        self.error_counts[alert_id] = self.error_counts.get(alert_id, 0) + 1
        
        # Check if we've hit the debounce threshold
        if self.error_counts[alert_id] >= self.debounce_threshold:
            # Check if alert already active
            if alert_id not in self.active_alerts:
                # Create new alert
                self.active_alerts[alert_id] = {
                    'severity': severity,
                    'message': message,
                    'timestamp': now,
                    'room_id': room_id,
                    'auto_clear': auto_clear,
                    'consecutive_count': self.error_counts[alert_id]
                }
                
                # Send notification if not rate-limited
                self._send_notification(alert_id)
            else:
                # Update existing alert's consecutive count
                self.active_alerts[alert_id]['consecutive_count'] = self.error_counts[alert_id]
                
    def clear_error(self, alert_id: str) -> None:
        """Clear an error condition.
        
        If the alert is marked as auto_clear, this will dismiss the alert
        and remove the persistent notification.
        
        Args:
            alert_id: Unique identifier for the alert to clear
        """
        # Reset error count
        self.error_counts[alert_id] = 0
        
        # Check if alert is active and can auto-clear
        if alert_id in self.active_alerts:
            alert = self.active_alerts[alert_id]
            if alert.get('auto_clear', True):
                self._dismiss_notification(alert_id)
                del self.active_alerts[alert_id]
                self.ad.log(f"Alert cleared: {alert_id}")
                
    def _send_notification(self, alert_id: str) -> None:
        """Send a Home Assistant persistent notification for an alert.
        
        Implements rate limiting to prevent notification spam.
        
        Args:
            alert_id: Unique identifier for the alert
        """
        now = datetime.now()
        alert = self.active_alerts[alert_id]
        
        # Check rate limiting
        last_notified = self.notification_history.get(alert_id)
        if last_notified:
            elapsed = (now - last_notified).total_seconds()
            if elapsed < self.rate_limit_seconds:
                self.ad.log(f"Alert {alert_id} rate-limited (sent {elapsed:.0f}s ago)")
                return
        
        # Build notification content
        severity = alert['severity']
        message = alert['message']
        room_id = alert.get('room_id')
        
        # Determine title and icon based on severity
        if severity == self.SEVERITY_CRITICAL:
            title = "⚠️ PyHeat Critical Alert"
            icon = "mdi:alert-circle"
        else:
            title = "⚠️ PyHeat Warning"
            icon = "mdi:alert"
        
        # Add room context if available
        if room_id:
            room_name = self._get_room_name(room_id)
            full_message = f"**Room:** {room_name}\n\n{message}"
        else:
            full_message = message
        
        # Add timestamp
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        full_message += f"\n\n*{timestamp}*"
        
        try:
            # Create persistent notification
            self.ad.call_service(
                "persistent_notification/create",
                title=title,
                message=full_message,
                notification_id=f"pyheat_{alert_id}"
            )
            
            # Update notification history
            self.notification_history[alert_id] = now
            
            self.ad.log(f"Notification sent for alert: {alert_id} ({severity})")
            
        except Exception as e:
            self.ad.log(f"Failed to send notification for {alert_id}: {e}", level="ERROR")
            
    def _dismiss_notification(self, alert_id: str) -> None:
        """Dismiss a Home Assistant persistent notification.
        
        Args:
            alert_id: Unique identifier for the alert
        """
        try:
            self.ad.call_service(
                "persistent_notification/dismiss",
                notification_id=f"pyheat_{alert_id}"
            )
            self.ad.log(f"Notification dismissed for alert: {alert_id}")
        except Exception as e:
            self.ad.log(f"Failed to dismiss notification for {alert_id}: {e}", level="WARNING")
            
    def _get_room_name(self, room_id: str) -> str:
        """Get the friendly name for a room.
        
        Args:
            room_id: Room identifier
            
        Returns:
            Friendly room name or room_id if not found
        """
        # Try to get room name from entity state
        state_entity = f"sensor.pyheat_{room_id}_state"
        if self.ad.entity_exists(state_entity):
            state = self.ad.get_state(state_entity, attribute="all")
            if state and 'attributes' in state:
                friendly_name = state['attributes'].get('friendly_name', '')
                # Extract just the room name (remove "PyHeat {room} State" suffix)
                if friendly_name.startswith("PyHeat "):
                    parts = friendly_name.split()
                    if len(parts) >= 2:
                        return parts[1]  # Return the room name part
        
        # Fallback to formatted room_id
        return room_id.replace("_", " ").title()
        
    def get_active_alerts(self) -> Dict[str, Dict]:
        """Get all currently active alerts.
        
        Returns:
            Dictionary of active alerts
        """
        return self.active_alerts.copy()
        
    def get_alert_count(self, severity: Optional[str] = None) -> int:
        """Get count of active alerts, optionally filtered by severity.
        
        Args:
            severity: Optional severity filter (SEVERITY_CRITICAL or SEVERITY_WARNING)
            
        Returns:
            Count of matching alerts
        """
        if severity:
            return sum(1 for alert in self.active_alerts.values() 
                      if alert['severity'] == severity)
        return len(self.active_alerts)
