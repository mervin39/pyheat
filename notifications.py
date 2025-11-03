"""
notifications.py - Notification helper for surfacing serious errors

Responsibilities:
- Send persistent notifications for critical/serious errors
- Prevent notification spam by tracking what's been notified
- Auto-dismiss notifications when issues resolve
- Categorize notifications by severity and type
"""

from typing import Dict, Set, Optional
from datetime import datetime


class NotificationManager:
    """Manages persistent notifications for system errors and warnings."""
    
    # Notification severity levels
    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_ERROR = "error"
    SEVERITY_CRITICAL = "critical"
    
    # Notification categories (used for tracking and ID generation)
    CATEGORY_BOILER = "boiler"
    CATEGORY_TRV = "trv"
    CATEGORY_SENSOR = "sensor"
    CATEGORY_CONFIG = "config"
    CATEGORY_SYSTEM = "system"
    
    def __init__(self):
        """Initialize notification manager."""
        # Track active notifications: {notification_id: timestamp}
        self.active_notifications: Dict[str, datetime] = {}
        
        # Track dismissed notifications to avoid re-showing: {notification_id}
        self.dismissed_notifications: Set[str] = set()
        
        log.info("NotificationManager: initialized")
    
    def _generate_id(self, category: str, key: str) -> str:
        """Generate notification ID from category and key.
        
        Args:
            category: Notification category (e.g., "boiler", "trv")
            key: Unique key within category (e.g., "interlock_blocked", "pete_feedback_timeout")
            
        Returns:
            Notification ID string
        """
        return f"pyheat_{category}_{key}"
    
    def _get_title(self, category: str, severity: str) -> str:
        """Get notification title based on category and severity.
        
        Args:
            category: Notification category
            severity: Severity level
            
        Returns:
            Title string
        """
        severity_prefix = {
            self.SEVERITY_CRITICAL: "🔴 CRITICAL",
            self.SEVERITY_ERROR: "🟠 Error",
            self.SEVERITY_WARNING: "🟡 Warning",
            self.SEVERITY_INFO: "ℹ️ Info"
        }.get(severity, "")
        
        category_name = {
            self.CATEGORY_BOILER: "Boiler",
            self.CATEGORY_TRV: "TRV",
            self.CATEGORY_SENSOR: "Sensor",
            self.CATEGORY_CONFIG: "Configuration",
            self.CATEGORY_SYSTEM: "System"
        }.get(category, "PyHeat")
        
        return f"{severity_prefix} PyHeat {category_name}"
    
    def notify(
        self,
        category: str,
        key: str,
        message: str,
        severity: str = SEVERITY_WARNING
    ) -> None:
        """Send a persistent notification if not already shown.
        
        Args:
            category: Notification category (e.g., CATEGORY_BOILER)
            key: Unique key within category
            message: Notification message body
            severity: Severity level (default: WARNING)
        """
        notification_id = self._generate_id(category, key)
        
        # Skip if already notified and not dismissed
        if notification_id in self.active_notifications:
            log.debug(f"NotificationManager: skipping duplicate notification {notification_id}")
            return
        
        # Skip if user dismissed this notification
        if notification_id in self.dismissed_notifications:
            log.debug(f"NotificationManager: skipping dismissed notification {notification_id}")
            return
        
        # Create notification
        title = self._get_title(category, severity)
        
        try:
            persistent_notification.create(
                title=title,
                message=message,
                notification_id=notification_id
            )
            
            self.active_notifications[notification_id] = task.executor(datetime.now)
            log.info(f"NotificationManager: created notification {notification_id}")
            
        except Exception as e:
            log.error(f"NotificationManager: failed to create notification {notification_id}: {e}")
    
    def dismiss(self, category: str, key: str) -> None:
        """Dismiss a notification (e.g., when issue is resolved).
        
        Args:
            category: Notification category
            key: Unique key within category
        """
        notification_id = self._generate_id(category, key)
        
        # Remove from active notifications
        if notification_id in self.active_notifications:
            try:
                persistent_notification.dismiss(notification_id=notification_id)
                del self.active_notifications[notification_id]
                log.info(f"NotificationManager: dismissed notification {notification_id}")
            except Exception as e:
                log.error(f"NotificationManager: failed to dismiss notification {notification_id}: {e}")
    
    def mark_dismissed(self, category: str, key: str) -> None:
        """Mark a notification as dismissed by user (won't re-show).
        
        Args:
            category: Notification category
            key: Unique key within category
        """
        notification_id = self._generate_id(category, key)
        self.dismissed_notifications.add(notification_id)
        
        if notification_id in self.active_notifications:
            del self.active_notifications[notification_id]
    
    def clear_all(self) -> None:
        """Clear all active pyheat notifications."""
        for notification_id in list(self.active_notifications.keys()):
            try:
                persistent_notification.dismiss(notification_id=notification_id)
            except Exception as e:
                log.error(f"NotificationManager: failed to dismiss {notification_id}: {e}")
        
        self.active_notifications.clear()
        log.info("NotificationManager: cleared all notifications")


# Module-level singleton
_notification_manager: Optional[NotificationManager] = None


def get_notification_manager() -> NotificationManager:
    """Get or create the notification manager singleton.
    
    Returns:
        NotificationManager instance
    """
    global _notification_manager
    if _notification_manager is None:
        _notification_manager = NotificationManager()
    return _notification_manager


def notify_error(category: str, key: str, message: str) -> None:
    """Convenience function to send an error notification.
    
    Args:
        category: Notification category
        key: Unique key within category
        message: Notification message
    """
    get_notification_manager().notify(category, key, message, NotificationManager.SEVERITY_ERROR)


def notify_critical(category: str, key: str, message: str) -> None:
    """Convenience function to send a critical notification.
    
    Args:
        category: Notification category
        key: Unique key within category
        message: Notification message
    """
    get_notification_manager().notify(category, key, message, NotificationManager.SEVERITY_CRITICAL)


def notify_warning(category: str, key: str, message: str) -> None:
    """Convenience function to send a warning notification.
    
    Args:
        category: Notification category
        key: Unique key within category
        message: Notification message
    """
    get_notification_manager().notify(category, key, message, NotificationManager.SEVERITY_WARNING)


def dismiss_notification(category: str, key: str) -> None:
    """Convenience function to dismiss a notification.
    
    Args:
        category: Notification category
        key: Unique key within category
    """
    get_notification_manager().dismiss(category, key)
