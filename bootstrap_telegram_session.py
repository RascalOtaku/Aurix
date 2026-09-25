"""bootstrap_telegram_session.py — one-time script to create the DB-backed
chat session the Telegram listener needs for its agent-loop calls."""
import uuid

from core.database import SessionLocal, ModelEndpoint
from core.session_manager import SessionManager
from src.endpoint_resolver import normalize_base, build_chat_url

OWNER = "rascal"

def main():
    db = SessionLocal()
    try:
        # Prefer the configured default endpoint (the GPU box) over "whichever
        # enabled endpoint comes first", now that a local fallback endpoint exists too.
        from src.settings import load_settings
        default_id = (load_settings().get("default_endpoint_id") or "").strip()
        ep = None
        if default_id:
            ep = db.query(ModelEndpoint).filter(
                ModelEndpoint.id == default_id, ModelEndpoint.is_enabled == True).first()
        if ep is None:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).first()
    finally:
        db.close()

    if not ep:
        print("No enabled ModelEndpoint found — configure one in Admin first.")
        return

    base_url = normalize_base(ep.base_url)
    endpoint_url = build_chat_url(base_url)
    model = ep.model if hasattr(ep, "model") and ep.model else "auto"

    session_manager = SessionManager()
    sid = str(uuid.uuid4())
    session_manager.create_session(
        session_id=sid,
        name="Telegram Agent",
        endpoint_url=endpoint_url,
        model=model,
        owner=OWNER,
    )
    if ep.api_key:
        from src.endpoint_resolver import build_headers
        sess = session_manager.get_session(sid)
        sess.headers = build_headers(ep.api_key, base_url)
        session_manager.save_sessions()

    print(f"Created session: {sid}")
    print(f"Add to .env: TELEGRAM_AGENT_SESSION_ID={sid}")

if __name__ == "__main__":
    main()
