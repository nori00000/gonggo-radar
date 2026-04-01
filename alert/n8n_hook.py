"""n8n 웹훅 연동 -- HTTP POST 이벤트 발송."""

import logging

__all__ = ["N8nHook"]
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class N8nHook:
    """n8n 웹훅 이벤트 발송기."""

    def __init__(self, config=None):
        """Initialize with N8nConfig.

        Args:
            config: N8nConfig instance (optional)
        """
        self._webhook_url = config.webhook_url if config else ""
        self._notify_on = config.notify_on if config else ["new_relevant", "status_change"]
        self._client = None

    def _get_client(self):
        """Lazy-initialize httpx client."""
        if self._client is None:
            try:
                import httpx
                self._client = httpx.Client(timeout=5.0)
            except ImportError:
                logger.warning("httpx not installed, n8n webhooks disabled")
                return None
        return self._client

    def send_event(self, event_type: str, data: Any = None, obsidian_path: str = "") -> bool:
        """Send a webhook event to n8n.

        Args:
            event_type: Event name (e.g., "new_relevant_announcement")
            data: Announcement or other data object
            obsidian_path: Path to the Obsidian note (if any)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._webhook_url:
            return False

        # Check if this event type should be notified
        event_category = self._event_to_category(event_type)
        if event_category not in self._notify_on:
            logger.debug(f"Skipping n8n event {event_type} (not in notify_on)")
            return False

        client = self._get_client()
        if client is None:
            return False

        payload = {
            "event": event_type,
            "timestamp": datetime.now().isoformat(),
        }

        # Build announcement data if available
        if data is not None and hasattr(data, "title"):
            payload["announcement"] = {
                "id": getattr(data, "id", None),
                "title": data.title,
                "source": getattr(data, "source", ""),
                "business_domain": getattr(data, "business_domain", ""),
                "relevance_score": getattr(data, "relevance_score", 0.0),
                "url": getattr(data, "url", ""),
                "period_end": getattr(data, "period_end", ""),
            }

        if obsidian_path:
            payload["obsidian_path"] = obsidian_path

        # Fire-and-forget with 1 retry
        for attempt in range(2):
            try:
                response = client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code < 400:
                    logger.info(f"n8n webhook sent: {event_type}")
                    return True
                else:
                    logger.warning(
                        f"n8n webhook returned {response.status_code} (attempt {attempt + 1})"
                    )
            except Exception as e:
                logger.warning(f"n8n webhook failed (attempt {attempt + 1}): {e}")

        return False

    _EVENT_CATEGORY_MAP = {
        "new_relevant_announcement": "new_relevant",
        "application_status_changed": "status_change",
        "research_report_generated": "research_generated",
    }

    @classmethod
    def _event_to_category(cls, event_type: str) -> str:
        """Map event type to notify_on category."""
        return cls._EVENT_CATEGORY_MAP.get(event_type, event_type)

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None
