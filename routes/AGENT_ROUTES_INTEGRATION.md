"""FastAPI integration example showing how to add multi-agent endpoints to existing app.py.

Add this to your app.py after line 630 (after other router includes):

    from routes.agent_routes import setup_agent_routes
    app.include_router(setup_agent_routes(app.state))
    logger.info("Multi-agent orchestration routes initialized")
"""
