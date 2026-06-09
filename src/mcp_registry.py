"""
mcp_registry.py — repo-backed MCP server registry (read/status only).

Loads known MCP servers from config so Odysseus doesn't depend on manual UI entry.
Prefers config/mcp_servers.local.json (gitignored, machine-specific) and falls
back to config/mcp_servers.example.json.

This module ONLY reads + reports status. It does NOT start servers and NEVER
returns env values (only env key NAMES). No secrets are read or emitted.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
LOCAL = CONFIG_DIR / "mcp_servers.local.json"
EXAMPLE = CONFIG_DIR / "mcp_servers.example.json"

# Filesystem scopes that are too broad to expose by default.
_BROAD_ROOTS = {"/users/growthgod", str(Path.home()).lower(), "/"}


def _config_path() -> Path:
    return LOCAL if LOCAL.exists() else EXAMPLE


def load_raw() -> tuple[Path, Optional[dict], Optional[str]]:
    """Return (path, data, error). Bad/missing JSON fails safely (data=None)."""
    p = _config_path()
    try:
        return p, json.loads(p.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return p, None, "no MCP config file found (expected config/mcp_servers.local.json or .example.json)"
    except json.JSONDecodeError as e:
        return p, None, f"invalid JSON in {p.name}: {e}"
    except OSError as e:
        return p, None, f"could not read {p.name}: {e}"


def _command_present(cfg: dict) -> bool:
    transport = cfg.get("transport", "stdio")
    if transport != "stdio":
        return bool(cfg.get("url"))
    cmd = cfg.get("command")
    if not cmd:
        return False
    if "/" in cmd:
        return Path(os.path.expanduser(cmd)).exists()
    return shutil.which(cmd) is not None


def _status(cfg: dict) -> str:
    """configured | disabled | missing_command | invalid"""
    if not isinstance(cfg, dict):
        return "invalid"
    transport = cfg.get("transport", "stdio")
    if transport == "stdio" and not cfg.get("command"):
        return "invalid"
    if transport != "stdio" and not cfg.get("url"):
        return "invalid"
    if cfg.get("enabled") is False:
        return "disabled"
    if not _command_present(cfg):
        return "missing_command"
    return "configured"


def _warnings(name: str, cfg: dict) -> list[str]:
    out: list[str] = []
    for a in (cfg.get("args") or []):
        ae = os.path.expanduser(str(a)).rstrip("/").lower()
        if ae in _BROAD_ROOTS:
            out.append(f"filesystem scope '{a}' is the home/root — too broad; scope to a project directory")
    if "macos-desktop" in name.lower() and cfg.get("enabled"):
        out.append("macOS desktop-control MCP is ENABLED — grants full desktop control; enable only if intended")
    return out


def registry_view() -> dict[str, Any]:
    """Read the config fresh and return a safe, env-value-free status view."""
    path, data, err = load_raw()
    if err:
        return {"source": str(path), "source_kind": None, "error": err, "servers": [], "warnings": []}

    servers: list[dict] = []
    all_warnings: list[str] = []
    for name, cfg in (data.get("mcpServers") or {}).items():
        cfg = cfg or {}
        warns = _warnings(name, cfg)
        all_warnings += [f"{name}: {w}" for w in warns]
        servers.append({
            "name": name,
            "enabled": bool(cfg.get("enabled", False)),
            "transport": cfg.get("transport", "stdio"),
            "command_present": _command_present(cfg),
            "args_count": len(cfg.get("args") or []),
            "env_keys": sorted((cfg.get("env") or {}).keys()),  # NAMES only — never values
            "description": cfg.get("description", ""),
            "status": _status(cfg),
            "source": "config",
            "warnings": warns,
        })
    return {
        "source": str(path),
        "source_kind": "local" if path == LOCAL else "example",
        "servers": servers,
        "warnings": all_warnings,
        "started": False,  # this registry never starts servers
    }
