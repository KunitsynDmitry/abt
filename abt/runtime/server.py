"""Lightweight HTTP server for webhook triggers and scheduler."""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def create_app(trigger_manager: Any) -> Starlette:
    """Build a Starlette app with routes for each webhook trigger.

    Utility routes:
    - GET /triggers — list all triggers
    - POST /trigger/{name} — activate any trigger by name
    - GET /health — healthcheck
    """

    async def make_webhook_handler(trigger_name: str, method: str):
        async def handler(request: Request) -> JSONResponse:
            body = {}
            if method in ("POST", "PUT", "PATCH"):
                try:
                    body = await request.json()
                except Exception:
                    body = {}
            query = dict(request.query_params)
            event_data = {"body": body, "query": query}
            try:
                result = trigger_manager.activate(trigger_name, event_data)
            except Exception as e:
                return JSONResponse(
                    {"error": str(e), "trigger": trigger_name},
                    status_code=500,
                )
            return JSONResponse(result)

        return handler

    routes: list[Route] = []

    for trigger in trigger_manager.triggers.values():
        if trigger.type.value == "webhook" and trigger.path:
            handler = make_webhook_handler(trigger.name, trigger.method)
            routes.append(
                Route(trigger.path, handler, methods=[trigger.method.upper()])
            )

    async def list_triggers(request: Request) -> JSONResponse:
        return JSONResponse(trigger_manager.list_triggers())

    async def activate_by_name(request: Request) -> JSONResponse:
        trigger_name = request.path_params["name"]
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        result = trigger_manager.activate(trigger_name, {"body": body})
        return JSONResponse(result)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes.append(Route("/triggers", list_triggers, methods=["GET"]))
    routes.append(Route("/trigger/{name}", activate_by_name, methods=["POST"]))
    routes.append(Route("/health", health, methods=["GET"]))

    return Starlette(routes=routes)
