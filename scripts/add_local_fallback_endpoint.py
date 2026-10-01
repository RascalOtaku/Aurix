"""One-off: make the 7070's own Ollama the default-model fallback for the 3431 GPU box.

Adds a second ModelEndpoint for local Ollama (host.docker.internal) and points
`default_model_fallbacks` at it, so when the gaming PC is off Telegram degrades
to slow-but-alive instead of failing. Also fixes a stale `default_model`
(qwen2.5:14b does not exist on the 3431) and the misleading endpoint name.

DRY RUN by default - prints the plan and changes nothing. Idempotent.
Run inside the container, feeding this file on stdin:

    docker compose exec -T odysseus python - < scripts/add_local_fallback_endpoint.py
    docker compose exec -T odysseus python - --apply < scripts/add_local_fallback_endpoint.py
"""
import json
import sys
import uuid

from core.database import SessionLocal, ModelEndpoint
from src.endpoint_resolver import normalize_base
from src.settings import load_settings, save_settings

APPLY = "--apply" in sys.argv

LOCAL_URL = "http://host.docker.internal:11434/v1"
LOCAL_NAME = "7070 local (CPU fallback)"
LOCAL_MODEL = "qwen2.5:3b"       # what the 7070's Ollama is known to have
STALE_DEFAULT = "qwen2.5:14b"    # not present on the 3431 GPU box
NEW_DEFAULT = "qwen2.5:7b"
MISLEADING_NAME = "127.0.0.1:11434"
GPU_NAME = "3431 GPU (Ollama)"


def main():
    db = SessionLocal()
    try:
        settings = dict(load_settings())
        endpoints = db.query(ModelEndpoint).all()
        print("== current ==")
        for e in endpoints:
            print(f"  endpoint {e.id} | {e.name} | {e.base_url} | enabled={e.is_enabled} | owner={e.owner}")
        for k in ("default_endpoint_id", "default_model", "utility_endpoint_id",
                  "utility_model", "default_model_fallbacks"):
            print(f"  setting {k} = {settings.get(k)}")

        try:
            from routes.prefs_routes import _load_for_user
            prefs = _load_for_user("rascal") or {}
            overrides = {k: prefs[k] for k in ("default_endpoint_id", "default_model",
                                                "default_model_fallbacks") if prefs.get(k)}
            if overrides:
                print(f"  WARNING per-user prefs for 'rascal' override globals: {overrides}")
        except Exception as e:
            print(f"  (could not read per-user prefs: {e})")

        # 1. local endpoint (reuse if one already points at the same base)
        want = normalize_base(LOCAL_URL).rstrip("/")
        local = next((e for e in endpoints
                      if normalize_base(e.base_url or "").rstrip("/") == want), None)
        plan = []
        if local is None:
            local_id = f"local-{uuid.uuid4().hex[:8]}"
            plan.append(f"ADD endpoint {local_id} '{LOCAL_NAME}' -> {LOCAL_URL} (shared, tools=yes)")
        else:
            local_id = local.id
            plan.append(f"REUSE existing endpoint {local_id} '{local.name}' -> {local.base_url}")

        # 2. give the GPU endpoint an honest name
        for e in endpoints:
            if e.name == MISLEADING_NAME and "127.0.0.1" not in (e.base_url or "") \
                    and "localhost" not in (e.base_url or ""):
                plan.append(f"RENAME endpoint {e.id} '{e.name}' -> '{GPU_NAME}'")

        # 3. settings
        new_fallbacks = [{"endpoint_id": local_id, "model": LOCAL_MODEL}]
        if settings.get("default_model_fallbacks") != new_fallbacks:
            plan.append(f"SET default_model_fallbacks = {new_fallbacks}")
        if settings.get("default_model") == STALE_DEFAULT:
            plan.append(f"SET default_model {STALE_DEFAULT} -> {NEW_DEFAULT}")

        print("\n== plan ==")
        for p in plan:
            print("  " + p)
        if all(p.startswith("REUSE") for p in plan):
            print("  (nothing to change)")

        if not APPLY:
            print("\nDRY RUN - nothing written. Re-run with --apply to make these changes.")
            return

        if local is None:
            db.add(ModelEndpoint(
                id=local_id, name=LOCAL_NAME, base_url=LOCAL_URL, api_key=None,
                is_enabled=True, model_type="llm",
                cached_models=json.dumps([LOCAL_MODEL]), supports_tools=True, owner=None,
            ))
        for e in endpoints:
            if e.name == MISLEADING_NAME and "127.0.0.1" not in (e.base_url or "") \
                    and "localhost" not in (e.base_url or ""):
                e.name = GPU_NAME
        db.commit()

        settings["default_model_fallbacks"] = new_fallbacks
        if settings.get("default_model") == STALE_DEFAULT:
            settings["default_model"] = NEW_DEFAULT
        save_settings(settings)

        print("\n== applied; verifying ==")
        from src.endpoint_resolver import resolve_chat_fallback_candidates
        for url, model, _headers in resolve_chat_fallback_candidates():
            print(f"  fallback candidate: {model} @ {url}")
        print("Done. Run `docker compose restart odysseus` so the running app reloads.")
    finally:
        db.close()


main()
