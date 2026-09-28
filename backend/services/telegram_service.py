import httpx
import logging
from typing import Optional, Any

logger = logging.getLogger("ping_monitor.telegram")

class TelegramNotificationService:
    async def send_message(self, token: str, chat_id: str, text: str) -> bool:
        """Sends a message via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            raise Exception(f"Failed to send Telegram message: {e}")

    def format_message(self, target: dict[str, Any], event: str, result: Any, 
                       custom_template: Optional[str] = None) -> str:
        """Formats the message using either default or custom template."""
        
        # Build available variables dictionary
        # PingResult stores latency in a nested LatencyStats object
        latency_avg = result.latency.avg if hasattr(result, 'latency') and hasattr(result.latency, 'avg') else None
        latency = f"{latency_avg:.1f}" if latency_avg is not None else "—"
        packet_loss = f"{result.packet_loss:.1f}" if result.packet_loss is not None else "—"
        
        variables = {
            "target_name": target.get("name", "Unknown"),
            "target_address": target.get("host", "Unknown"),
            "status": event,
            "latency": latency,
            "packet_loss": packet_loss,
            "timestamp": result.timestamp if hasattr(result, 'timestamp') else "—"
        }

        if custom_template and custom_template.strip():
            msg = custom_template
            # Extremely simple replacement logic
            for k, v in variables.items():
                msg = msg.replace(f"{{{k}}}", str(v))
            return msg

        # Default Templates
        if event == "DOWN":
            return (
                f"🔴 <b>PingOn Alert</b>\n\n"
                f"Target: {variables['target_name']}\n"
                f"IP: {variables['target_address']}\n"
                f"Status: DOWN\n"
                f"Time: {variables['timestamp']}"
            )
        else:
            return (
                f"🟢 <b>PingOn Recovery</b>\n\n"
                f"Target: {variables['target_name']}\n"
                f"IP: {variables['target_address']}\n"
                f"Status: UP\n"
                f"Time: {variables['timestamp']}\n"
                f"Latency: {variables['latency']} ms"
            )

telegram_service = TelegramNotificationService()