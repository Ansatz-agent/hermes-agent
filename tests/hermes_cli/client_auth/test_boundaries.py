from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli.client_auth.runtime import (
    AuthRequired,
    BackendScopeTokenRegistry,
    RuntimeConsumer,
    RuntimeSnapshot,
    clear_runtime_consumer,
    install_runtime_consumer,
)


def _install_authenticated_consumer():
    snapshot = RuntimeSnapshot.new_authenticated(
        "test-user",
        now=0.0,
        ttl=10**12,
    )
    install_runtime_consumer(
        RuntimeConsumer(
            snapshot,
            liveness_probe=lambda: True,
            clock=lambda: 0.0,
        )
    )
    return snapshot.scope


def test_model_tool_dispatch_rejects_before_lookup_or_side_effect(monkeypatch):
    import model_tools

    clear_runtime_consumer()
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail(
            "locked tool must not reach registry dispatch"
        ),
    )

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        model_tools.handle_function_call(
            "terminal",
            {"command": "touch should-not-run"},
        )


def test_model_tool_dispatch_runs_with_authenticated_runtime(monkeypatch):
    import model_tools

    _install_authenticated_consumer()
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: json.dumps({"ok": True}),
    )

    assert json.loads(
        model_tools.handle_function_call(
            "web_search",
            {"q": "Hermes"},
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    ) == {"ok": True}


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("terminal", {"command": "touch should-not-run"}),
        ("write_file", {"path": "should-not-write", "content": "x"}),
        ("web_search", {"q": "should-not-reach-network"}),
        ("send_message", {"target": "should-not-send", "message": "x"}),
    ],
)
def test_locked_shared_dispatch_blocks_irreversible_effect_classes(
    monkeypatch,
    tool_name,
    arguments,
):
    import model_tools

    clear_runtime_consumer()
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail(
            f"locked {tool_name} reached irreversible dispatch"
        ),
    )

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        model_tools.handle_function_call(tool_name, arguments)


def test_expired_runtime_blocks_shared_tool_boundary(monkeypatch):
    import model_tools

    snapshot = RuntimeSnapshot.new_authenticated("test-user", now=0.0, ttl=1.0)
    install_runtime_consumer(
        RuntimeConsumer(snapshot, liveness_probe=lambda: True, clock=lambda: 2.0)
    )
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail("expired runtime dispatched tool"),
    )

    with pytest.raises(AuthRequired, match="session_expired"):
        model_tools.handle_function_call("terminal", {"command": "true"})


def test_owner_eof_blocks_next_shared_tool_boundary(monkeypatch):
    import model_tools

    snapshot = RuntimeSnapshot.new_authenticated("test-user", now=0.0, ttl=10**12)
    install_runtime_consumer(
        RuntimeConsumer(snapshot, liveness_probe=lambda: False, clock=lambda: 0.0)
    )
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail("dead owner dispatched tool"),
    )

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        model_tools.handle_function_call("terminal", {"command": "true"})


def test_lock_blocks_next_effect_without_rolling_back_completed_effect(monkeypatch):
    import model_tools

    completed_effects = []
    _install_authenticated_consumer()
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *_args, **_kwargs: completed_effects.append("sent") or '{"ok": true}',
    )

    assert json.loads(
        model_tools.handle_function_call(
            "send_message",
            {"target": "test", "message": "first"},
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    ) == {"ok": True}
    clear_runtime_consumer()

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        model_tools.handle_function_call(
            "send_message",
            {"target": "test", "message": "second"},
        )
    assert completed_effects == ["sent"]


def test_delegate_child_rejects_stale_parent_scope_before_child_state():
    from tools.delegate_tool import _run_single_child

    snapshot = RuntimeSnapshot.new_authenticated("test-user", now=0.0, ttl=10**12)
    install_runtime_consumer(
        RuntimeConsumer(snapshot, liveness_probe=lambda: True, clock=lambda: 0.0)
    )
    stale_scope = replace(snapshot.scope, epoch=snapshot.scope.epoch - 1)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        _run_single_child(
            0,
            "do not start",
            _ExplodingCapabilityObject(),
            _ExplodingCapabilityObject(),
            expected_auth_scope=stale_scope,
        )


def test_delegate_build_rejects_stale_scope_before_agent_import_or_parent_state():
    from tools.delegate_tool import _build_child_agent

    snapshot = RuntimeSnapshot.new_authenticated("test-user", now=0.0, ttl=10**12)
    install_runtime_consumer(
        RuntimeConsumer(snapshot, liveness_probe=lambda: True, clock=lambda: 0.0)
    )
    stale_scope = replace(snapshot.scope, epoch=snapshot.scope.epoch - 1)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        _build_child_agent(
            0,
            "do not build",
            None,
            None,
            None,
            1,
            1,
            _ExplodingCapabilityObject(),
            expected_auth_scope=stale_scope,
        )


class _ExplodingCapabilityObject:
    def __getattribute__(self, name):
        raise AssertionError(f"locked boundary touched capability state: {name}")


def test_agent_tool_helper_rejects_before_agent_state_access():
    from agent.agent_runtime_helpers import invoke_tool

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        invoke_tool(
            _ExplodingCapabilityObject(),
            "todo",
            {"todos": []},
            "task-1",
        )


@pytest.mark.parametrize(
    "executor_name",
    ["execute_tool_calls_concurrent", "execute_tool_calls_sequential"],
)
def test_tool_executors_reject_before_turn_or_agent_state_access(executor_name):
    from agent import tool_executor

    clear_runtime_consumer()
    executor = getattr(tool_executor, executor_name)
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        executor(
            _ExplodingCapabilityObject(),
            _ExplodingCapabilityObject(),
            [],
            "task-1",
        )


class _FakeMCPToolManager:
    def __init__(self):
        self.tools = {}


class _FakeFastMCP:
    def __init__(self, *_args, **_kwargs):
        self._tool_manager = _FakeMCPToolManager()

    def tool(self):
        def decorator(function):
            self._tool_manager.tools[function.__name__] = function
            return function

        return decorator

    def add_tool(self, function, *, name, description):
        del description
        self._tool_manager.tools[name] = function


def test_mcp_tool_rejects_before_reading_conversation_state(monkeypatch):
    import mcp_serve

    clear_runtime_consumer()
    monkeypatch.setattr(mcp_serve, "_MCP_SERVER_AVAILABLE", True)
    monkeypatch.setattr(mcp_serve, "FastMCP", _FakeFastMCP)
    monkeypatch.setattr(
        mcp_serve,
        "_load_sessions_index",
        lambda: pytest.fail("locked MCP tool must not read conversation state"),
    )
    server = mcp_serve.create_mcp_server()

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        server._tool_manager.tools["conversations_list"]()


def test_mcp_event_poll_rejects_before_reading_database_state():
    from mcp_serve import EventBridge

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        EventBridge()._poll_once(_ExplodingCapabilityObject())


def test_hermes_tools_mcp_request_preserves_auth_rejection(monkeypatch):
    from agent.transports import hermes_tools_mcp_server
    import mcp.server.fastmcp
    import model_tools

    clear_runtime_consumer()
    monkeypatch.setattr(mcp.server.fastmcp, "FastMCP", _FakeFastMCP)
    monkeypatch.setattr(hermes_tools_mcp_server, "EXPOSED_TOOLS", ("web_search",))
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(
        model_tools,
        "handle_function_call",
        lambda *_args, **_kwargs: pytest.fail(
            "locked Hermes-tools request must not dispatch"
        ),
    )
    server = hermes_tools_mcp_server._build_server()

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        server._tool_manager.tools["web_search"]()


class _ExplodingMapping:
    def __getitem__(self, key):
        raise AssertionError(f"locked boundary read mapping key: {key}")


def test_cron_job_rejects_before_reading_job_or_starting_agent():
    from cron.scheduler import run_job

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        run_job(_ExplodingMapping())


def test_cron_tick_rejects_before_opening_scheduler_state(monkeypatch):
    from cron import scheduler

    clear_runtime_consumer()
    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: pytest.fail("locked cron tick must not open scheduler state"),
    )
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        scheduler.tick()


@pytest.mark.parametrize("boundary", ["dispatch", "handle_request"])
def test_tui_rpc_rejects_before_parsing_or_scheduling_request(boundary):
    from tui_gateway import server

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        getattr(server, boundary)(_ExplodingMapping())


def test_gateway_http_rejects_without_optional_http_runtime():
    from gateway.platforms.api_server import _require_client_runtime_request

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        _require_client_runtime_request()


@pytest.mark.asyncio
async def test_gateway_http_middleware_returns_401_before_handler_state():
    from gateway.platforms.api_server import client_runtime_auth_middleware

    if client_runtime_auth_middleware is None:
        pytest.skip("aiohttp optional dependency is not installed")
    clear_runtime_consumer()
    response = await client_runtime_auth_middleware(
        _ExplodingCapabilityObject(),
        lambda _request: pytest.fail("locked gateway request reached handler"),
    )

    assert response.status == 401


@pytest.mark.asyncio
async def test_desktop_gateway_http_requires_registered_scope_header(monkeypatch):
    from gateway.platforms import api_server

    if api_server.client_runtime_auth_middleware is None:
        pytest.skip("aiohttp optional dependency is not installed")

    scope = _install_authenticated_consumer()
    registry = BackendScopeTokenRegistry()
    bearer = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI"
    registry.register(
        bearer,
        connection_id="local",
        expected=scope,
        ttl_seconds=60,
    )
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setattr(api_server, "backend_scope_tokens", registry)

    class Request:
        def __init__(self, headers):
            self.headers = headers

    async def handler(_request):
        return "allowed"

    assert (
        await api_server.client_runtime_auth_middleware(
            Request({"X-Hermes-Scope-Token": bearer}),
            handler,
        )
        == "allowed"
    )
    denied = await api_server.client_runtime_auth_middleware(
        Request({}),
        lambda _request: pytest.fail("missing scope bearer reached handler"),
    )
    assert denied.status == 401


@pytest.mark.asyncio
async def test_dashboard_api_rejects_before_downstream_handler():
    from hermes_cli.web_server import client_runtime_auth_middleware

    clear_runtime_consumer()
    request = SimpleNamespace(url=SimpleNamespace(path="/api/status"))
    response = await client_runtime_auth_middleware(
        request,
        lambda _request: pytest.fail("locked dashboard request reached handler"),
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_desktop_dashboard_api_requires_the_registered_scope_header(monkeypatch):
    from hermes_cli import web_server

    scope = _install_authenticated_consumer()
    registry = BackendScopeTokenRegistry()
    bearer = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE"
    registry.register(
        bearer,
        connection_id="local",
        expected=scope,
        ttl_seconds=60,
    )
    monkeypatch.setattr(web_server, "backend_scope_tokens", registry)

    async def handler(request):
        assert request.state.desktop_scope_authenticated is True
        assert request.state.desktop_scope_grant.auth == scope
        return "allowed"

    app = SimpleNamespace(
        state=SimpleNamespace(desktop_scope_tokens_required=True)
    )
    allowed = SimpleNamespace(
        app=app,
        headers={"X-Hermes-Session-Token": bearer},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/status"),
    )
    denied = SimpleNamespace(
        app=app,
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/status"),
    )

    assert await web_server.client_runtime_auth_middleware(allowed, handler) == "allowed"
    response = await web_server.client_runtime_auth_middleware(
        denied,
        lambda _request: pytest.fail("missing scope bearer reached handler"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_websocket_closes_when_runtime_is_locked():
    from hermes_cli.web_server import _ws_client_runtime_authorized

    class FakeWebSocket:
        def __init__(self):
            self.closed = None

        async def close(self, **kwargs):
            self.closed = kwargs

    clear_runtime_consumer()
    ws = FakeWebSocket()

    assert not await _ws_client_runtime_authorized(ws, "dashboard.ws.test")
    assert ws.closed == {"code": 4401, "reason": "Hermes login required"}


@pytest.mark.asyncio
async def test_owner_eof_closes_connected_tui_websocket(monkeypatch):
    from tui_gateway import server
    from tui_gateway import ws as ws_module

    snapshot = RuntimeSnapshot.new_authenticated("test-user", now=0.0, ttl=10**12)
    probe_calls = 0

    def owner_is_live():
        nonlocal probe_calls
        probe_calls += 1
        return probe_calls == 1

    install_runtime_consumer(
        RuntimeConsumer(snapshot, liveness_probe=owner_is_live, clock=lambda: 0.0)
    )
    monkeypatch.setattr(server, "resolve_skin", lambda: {})
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_release_wake_for_transport", lambda _transport: None)
    monkeypatch.setattr(
        server,
        "_close_sessions_for_transport",
        lambda *_args, **_kwargs: (0, 0),
    )

    class FakeWebSocket:
        client = None
        scope = {}

        def __init__(self):
            self.close_calls = []

        async def accept(self):
            return None

        async def send_text(self, _line):
            return None

        async def receive_text(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "setup.status"})

        async def close(self, **kwargs):
            self.close_calls.append(kwargs)

    ws = FakeWebSocket()
    await ws_module.handle_ws(ws)

    assert {"code": 4401, "reason": "Hermes login required"} in ws.close_calls


def test_agent_forwarder_rejects_before_reading_agent_or_creating_turn():
    from run_agent import AIAgent

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        AIAgent.run_conversation(_ExplodingCapabilityObject(), "hello")


def test_conversation_loop_rejects_before_reading_agent_state():
    from agent.conversation_loop import run_conversation

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        run_conversation(_ExplodingCapabilityObject(), "hello")


def test_codex_turn_rejects_before_starting_subprocess_or_client():
    from agent.transports.codex_app_server_session import CodexAppServerSession

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        CodexAppServerSession.run_turn(_ExplodingCapabilityObject(), "hello")


@pytest.mark.asyncio
async def test_acp_request_rejects_before_session_or_provider_work():
    pytest.importorskip("acp")
    from acp_adapter.server import HermesACPAgent

    clear_runtime_consumer()
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        await HermesACPAgent.initialize(_ExplodingCapabilityObject())


def teardown_function() -> None:
    clear_runtime_consumer()
