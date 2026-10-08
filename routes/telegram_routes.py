# routes/telegram_routes.py
"""Routes for the Telegram notification service."""

from fastapi import APIRouter
from pydantic import BaseModel

from services.telegram.service import TelegramService

_service = TelegramService()


class TelegramSendRequest(BaseModel):
    text: str
    parse_mode: str = "HTML"


class TelegramAlertRequest(BaseModel):
    text: str


class TelegramReportRequest(BaseModel):
    subject: str
    body: str


def setup_telegram_routes() -> APIRouter:
    router = APIRouter(prefix="/api/telegram", tags=["telegram"])

    @router.get("/status")
    async def status():
        return {
            "enabled": _service.enabled,
            "configured": bool(_service.bot_token and _service.chat_id),
        }

    # Messages from scripts and other services follow the owner's quiet hours and hourly cap (src/foundation/outbox.py);
    # /alert is an emergency: it always goes through (repeats within 30 min are dropped).
    @router.post("/send")
    async def send(req: TelegramSendRequest):
        from src.foundation import outbox
        verdict = outbox.decide(req.text)
        if verdict != "send":
            return {"ok": True, "error": None, "held": verdict == "hold", "dropped": verdict == "drop"}
        result = _service.send(req.text, parse_mode=req.parse_mode)
        return {"ok": result.ok, "error": result.error or None}

    @router.post("/alert")
    async def alert(req: TelegramAlertRequest):
        from src.foundation import outbox
        if outbox.decide(req.text, emergency=True) == "drop":
            return {"ok": True, "error": None, "dropped": True}
        result = _service.send_alert(req.text)
        return {"ok": result.ok, "error": result.error or None}

    @router.post("/report")
    async def report(req: TelegramReportRequest):
        from src.foundation import outbox
        verdict = outbox.decide(f"<b>{req.subject}</b>\n{req.body}", whole=True)
        if verdict != "send":
            return {"ok": True, "error": None, "held": verdict == "hold"}
        result = _service.send_report(req.subject, req.body)
        return {"ok": result.ok, "error": result.error or None}

    return router
