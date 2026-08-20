from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import repair_service as base
from repair_status import dashboard_document, github_repair_summary, record_claude_call, record_scan, snapshot


app = base.app

# Replace only the routes whose activity we want to measure. All auth and proxy
# logic remains in repair_service.py and is called unchanged below.
_REPLACED_PATHS = {"/", "/real-events", "/v1/messages", "/v1/messages/count_tokens"}
app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") not in _REPLACED_PATHS]


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(dashboard_document(), headers={"Cache-Control": "no-store"})


async def _production_reachable() -> bool | None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.5)) as client:
            response = await client.get(
                f"{base._PRODUCTION_URL}/debug/last-production-events",
                params={"limit": 1},
            )
        return response.status_code == 200
    except Exception:
        return False


@app.get("/status.json")
async def status_json() -> dict[str, Any]:
    reachable, prs = await _production_reachable(), await github_repair_summary()
    return snapshot(production_reachable=reachable, prs=prs)


@app.get("/real-events")
async def tracked_real_events(
    request: Request,
    minutes: int = Query(default=20, ge=5, le=60),
) -> dict[str, Any]:
    try:
        result = await base.real_events(request, minutes)
    except HTTPException as exc:
        record_scan(error=f"HTTP {exc.status_code}: {exc.detail}")
        raise
    except Exception as exc:
        record_scan(error=f"{type(exc).__name__}: production event read failed")
        raise
    record_scan(result)
    return result


@app.post("/v1/messages")
async def tracked_proxy_messages(request: Request) -> Response:
    response = await base._proxy_anthropic(request, "/v1/messages")
    record_claude_call(response.status_code)
    return response


@app.post("/v1/messages/count_tokens")
async def tracked_proxy_count_tokens(request: Request) -> Response:
    # Token counting is not counted as a real Claude reasoning call.
    return await base._proxy_anthropic(request, "/v1/messages/count_tokens")
