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

    @router.post("/send")
    async def send(req: TelegramSendRequest):
        result = _service.send(req.text, parse_mode=req.parse_mode)
        return {"ok": result.ok, "error": result.error or None}

    @router.post("/alert")
    async def alert(req: TelegramAlertRequest):
        result = _service.send_alert(req.text)
        return {"ok": result.ok, "error": result.error or None}

    @router.post("/report")
    async def report(req: TelegramReportRequest):
        result = _service.send_report(req.subject, req.body)
        return {"ok": result.ok, "error": result.error or None}

    return router
