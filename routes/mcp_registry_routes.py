"""
mcp_registry_routes.py — read-only HTTP for the config-backed MCP registry.

Separate from mcp_routes.py (DB-backed manual servers) on purpose. Admin-only,
status-only: it never starts servers and never returns env values.
"""

import logging

from fastapi import APIRouter, Request

from core.middleware import require_admin
from src import mcp_registry

logger = logging.getLogger(__name__)


def setup_mcp_registry_routes() -> APIRouter:
    router = APIRouter(prefix="/api/mcp", tags=["mcp-registry"])

    @router.get("/registry")
    def get_registry(request: Request):
        """List config-backed MCP servers + status. No env values returned."""
        require_admin(request)
        return mcp_registry.registry_view()

    @router.post("/registry/reload")
    def reload_registry(request: Request):
        """Re-read the config from disk (local → example). Does NOT start servers."""
        require_admin(request)
        view = mcp_registry.registry_view()  # registry_view reads fresh each call
        logger.info("MCP registry reloaded from %s (%d servers)",
                    view.get("source"), len(view.get("servers", [])))
        return {"reloaded": True, **view}

    return router
