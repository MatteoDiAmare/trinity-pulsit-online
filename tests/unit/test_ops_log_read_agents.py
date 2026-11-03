"""Opt-in, per-agent read of FINISHED execution transcripts for `ops` keys (OPS_LOG_READ_AGENTS).

The base `ops` fence (#2323) does not include `GET /api/agents/{name}/executions/{id}/log`: a transcript holds an agent's reasoning and every
tool call and result. An operator who must archive ONE agent's transcripts with an unattended bounded credential names that agent in
`OPS_LOG_READ_AGENTS`. These tests pin: default-off, fail-closed on bad input, per-agent (not fleet-wide), GET only, anchored (no prefix
match), and that nothing else widened. No MagicMock, same reasoning as test_2323_machine_identities.py.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _deps():
    try:
        import dependencies
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return dependencies


def _request(method: str, path: str):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def _fenced(d, routes, method, path):
    """Run the real fence function against an arbitrary allowlist."""
    from fastapi import HTTPException
    old = d._OPS_ALLOWED_ROUTES
    d._OPS_ALLOWED_ROUTES = routes
    try:
        d._enforce_ops_key_fence(_request(method, path))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    finally:
        d._OPS_ALLOWED_ROUTES = old


LOG = "/api/agents/saba/executions/exec-1/log"


def test_default_is_off_the_shipped_fence_has_no_log_route(monkeypatch):
    monkeypatch.delenv("OPS_LOG_READ_AGENTS", raising=False)
    d = _deps()
    base = importlib.reload(d)._OPS_ALLOWED_ROUTES
    assert not any(p.pattern.endswith("/log$") for _, p in base)
    assert not _fenced(d, base, "GET", LOG)


@pytest.mark.parametrize("raw", [None, "", " ", ",,", "UPPER", "-bad", "sa ba", "saba/..", "../x", "a" * 80, "sa*ba", "(saba)"])
def test_empty_or_malformed_names_leave_the_log_route_closed(raw):
    d = _deps()
    assert d._ops_log_read_routes(raw) == ()


def test_named_agent_only_not_the_fleet():
    d = _deps()
    routes = d._ops_log_read_routes("saba")
    assert len(routes) == 1
    base = tuple(r for r in d._OPS_ALLOWED_ROUTES if not r[1].pattern.endswith("/log$"))
    allowed = base + routes
    assert _fenced(d, allowed, "GET", LOG)
    assert not _fenced(d, allowed, "GET", "/api/agents/vera/executions/exec-1/log")            # another agent's transcript stays closed
    assert not _fenced(d, allowed, "GET", "/api/agents/saba-two/executions/exec-1/log")         # no prefix match
    assert not _fenced(d, allowed, "GET", "/api/agents/xsaba/executions/exec-1/log")            # no suffix match
    assert not _fenced(d, allowed, "GET", "/api/agents/saba/executions/exec-1/log/extra")       # anchored
    assert not _fenced(d, allowed, "GET", "/api/agents/saba/executions/a/b/log")                # one id segment only
    assert not _fenced(d, allowed, "GET", "/api/agents/saba/executions//log")                   # id required


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_only_get_is_admitted(method):
    d = _deps()
    allowed = d._OPS_ALLOWED_ROUTES + d._ops_log_read_routes("saba")
    assert not _fenced(d, allowed, method, LOG)


def test_several_agents_dedupe_and_special_characters_are_escaped():
    d = _deps()
    routes = d._ops_log_read_routes("saba, vera ,saba,a.b,under_score")
    assert [p.pattern for _, p in routes] == [
        r"^/api/agents/saba/executions/[^/]+/log$",
        r"^/api/agents/vera/executions/[^/]+/log$",
        r"^/api/agents/a\.b/executions/[^/]+/log$",
        r"^/api/agents/under_score/executions/[^/]+/log$",
    ]
    allowed = d._OPS_ALLOWED_ROUTES + routes
    assert _fenced(d, allowed, "GET", "/api/agents/a.b/executions/x/log")
    assert not _fenced(d, allowed, "GET", "/api/agents/aXb/executions/x/log")                   # '.' is literal, not a wildcard


def test_nothing_else_is_widened_by_the_opt_in():
    d = _deps()
    allowed = d._OPS_ALLOWED_ROUTES + d._ops_log_read_routes("saba")
    for method, path in (
        ("GET", "/api/agents/saba/files/download"), ("GET", "/api/agents/saba/executions/exec-1"),
        ("GET", "/api/agents/saba/executions/exec-1/response"), ("POST", "/api/agents/saba/chat"),
        ("POST", "/api/agents/saba/task"), ("GET", "/api/mcp/keys"), ("POST", "/api/mcp/keys"),
        ("POST", "/api/mcp/keys/k1/revoke"), ("GET", "/api/settings/retention"), ("POST", "/api/ops/fleet/stop"),
    ):
        assert not _fenced(d, allowed, method, path), (method, path)


def test_every_entry_the_opt_in_adds_is_a_GET():
    d = _deps()
    for method, _ in d._ops_log_read_routes("saba,vera"):
        assert method == "GET"


def test_module_level_wiring_reads_the_environment_once_at_import(monkeypatch):
    monkeypatch.setenv("OPS_LOG_READ_AGENTS", "saba")
    d = importlib.reload(_deps())
    try:
        assert _fenced(d, d._OPS_ALLOWED_ROUTES, "GET", LOG)
        assert not _fenced(d, d._OPS_ALLOWED_ROUTES, "GET", "/api/agents/vera/executions/exec-1/log")
        monkeypatch.setenv("OPS_LOG_READ_AGENTS", "vera")                  # changing the env at runtime changes nothing: no request-time lookup
        assert _fenced(d, d._OPS_ALLOWED_ROUTES, "GET", LOG)
    finally:
        monkeypatch.delenv("OPS_LOG_READ_AGENTS", raising=False)
        importlib.reload(d)
