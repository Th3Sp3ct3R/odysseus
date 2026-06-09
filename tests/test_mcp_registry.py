"""Tests for the config-backed MCP registry (read/status only; no live starts)."""

import json

import pytest

from src import mcp_registry


def _write(monkeypatch, tmp_path, data, *, raw=None):
    p = tmp_path / "mcp_servers.local.json"
    p.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "LOCAL", p)
    monkeypatch.setattr(mcp_registry, "EXAMPLE", tmp_path / "mcp_servers.example.json")
    return p


def test_valid_config_loads(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "echo-srv": {"transport": "stdio", "command": "python3", "args": ["-V"], "enabled": True},
    }})
    view = mcp_registry.registry_view()
    assert not view.get("error")
    assert view["source_kind"] == "local"
    assert view["started"] is False
    names = [s["name"] for s in view["servers"]]
    assert "echo-srv" in names
    s = view["servers"][0]
    assert s["status"] == "configured" and s["command_present"] is True and s["args_count"] == 1


def test_bad_json_fails_safely(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, None, raw="{ this is not valid json ")
    view = mcp_registry.registry_view()
    assert view["servers"] == [] and view["error"]  # no crash


def test_env_values_never_returned(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "secret-srv": {"transport": "stdio", "command": "python3",
                       "env": {"SECRET_TOKEN": "supersecretvalue123"}, "enabled": True},
    }})
    view = mcp_registry.registry_view()
    blob = json.dumps(view)
    assert "supersecretvalue123" not in blob          # value never leaks
    assert view["servers"][0]["env_keys"] == ["SECRET_TOKEN"]  # name only


def test_broad_filesystem_root_warns(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "filesystem": {"transport": "stdio", "command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/growthgod"],
                       "enabled": True},
    }})
    view = mcp_registry.registry_view()
    warns = view["servers"][0]["warnings"]
    assert any("too broad" in w for w in warns)
    assert any("filesystem" in w for w in view["warnings"])


def test_scoped_filesystem_passes(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "filesystem": {"transport": "stdio", "command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem",
                                "/Users/growthgod/Desktop/VAN/odysseus",
                                "/Users/growthgod/.hermes/skills/creative"],
                       "enabled": True},
    }})
    view = mcp_registry.registry_view()
    assert view["servers"][0]["warnings"] == []


def test_status_computation(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "off": {"transport": "stdio", "command": "python3", "enabled": False},
        "ghost": {"transport": "stdio", "command": "definitely-not-a-real-cmd-xyz", "enabled": True},
        "bad": {"transport": "stdio", "enabled": True},  # no command → invalid
        "ok": {"transport": "stdio", "command": "python3", "enabled": True},
    }})
    by = {s["name"]: s["status"] for s in mcp_registry.registry_view()["servers"]}
    assert by["off"] == "disabled"
    assert by["ghost"] == "missing_command"
    assert by["bad"] == "invalid"
    assert by["ok"] == "configured"


def test_macos_desktop_enabled_warns(monkeypatch, tmp_path):
    _write(monkeypatch, tmp_path, {"mcpServers": {
        "macos-desktop-control": {"transport": "stdio", "command": "node",
                                  "args": ["/x/index.js"], "enabled": True},
    }})
    warns = mcp_registry.registry_view()["servers"][0]["warnings"]
    assert any("desktop" in w.lower() for w in warns)
