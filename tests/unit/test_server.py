import json
import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from decision_browser.runner import RunnerError
from decision_browser.server import create_server, production_factory

REQUEST = {
    "goal": "Read artificial page",
    "start_url": "https://example.test/",
    "mode": "isolated",
    "values": {},
    "checks": [],
    "viewports": [{"width": 390, "height": 844}],
    "limits": {
        "max_steps": 12,
        "max_seconds": 120.0,
        "max_provider_calls": 12,
        "max_input_bytes": 262144,
        "max_output_bytes": 262144,
        "max_stored_bytes": 10485760,
    },
    "privacy": {"blocked_literals": [], "send_images": False},
    "visual": False,
}
CLASS_ORDER = [
    "fixture_mapped",
    "observe_driver",
    "ordinary_admitted",
    "ordinary_task_scoped",
    "ordinary_beta_admitted",
    "consequential",
    "unresolved",
    "owned_loopback_document",
]
CLASSES = set(CLASS_ORDER)
OPS = {
    "click",
    "fill",
    "select",
    "scroll",
    "navigate",
    "enter",
    "submit",
    "screenshot",
    "wait",
}
NAMES = (
    "start_task",
    "get_task",
    "cancel_task",
    "read_artifact",
    "capabilities",
    "present_admission",
)
CALLS = {
    "start_task": {"request": REQUEST},
    "get_task": {"run_id": "run1"},
    "cancel_task": {"run_id": "run1"},
    "read_artifact": {"run_id": "run1", "artifact_id": "art1"},
    "capabilities": {},
    "present_admission": {"run_id": "run1", "proposal_id": "prop1", "digest": "a" * 64},
}
EXTRAS = ("approval", "grant", "effect_scope", "source", "receipt", "content_id")


@pytest.fixture(autouse=True)
def clear_provider_environment(monkeypatch, isolated_provider_profile):
    for name in os.environ:
        if name == "DECISION_BROWSER_CONFIG_FILE":
            continue
        if name.startswith("DECISION_BROWSER_") or name in {"OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY"}:
            monkeypatch.delenv(name)


async def test_six_closed_schemas():
    tools = await create_server(manager=None).list_tools()
    assert {tool.name for tool in tools} == set(NAMES)
    schemas = {tool.name: tool.input_schema for tool in tools}
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert (
        schemas["start_task"]["$defs"]["TaskRequest"]["additionalProperties"] is False
    )


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("extra", EXTRAS)
async def test_extra_rejected_before_body(name, extra):
    payload = {**CALLS[name], extra: True}
    with pytest.raises(ToolError):
        await create_server(manager=None).call_tool(name, payload)


async def test_nested_task_request_approval_rejected():
    request = {**REQUEST, "approval": True}
    with pytest.raises(ToolError):
        await create_server(manager=None).call_tool("start_task", {"request": request})


@pytest.mark.parametrize("field", ["content_id", "grant", "approval"])
async def test_nested_task_request_caller_fields_rejected(field):
    request = {**REQUEST, field: "owned"}
    with pytest.raises(ToolError):
        await create_server(manager=None).call_tool("start_task", {"request": request})


@pytest.mark.parametrize("run_id", ["../x", "/tmp/foo", "a" * 129, True])
async def test_get_task_malformed_id(run_id):
    with pytest.raises(ToolError):
        await create_server(manager=None).call_tool("get_task", {"run_id": run_id})


async def test_caps_no_server_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = (
        await create_server(manager=None).call_tool(
            "capabilities", CALLS["capabilities"]
        )
    ).structured_content
    assert body["provider_ready"] is False
    assert body["live_status"] == "BLOCKED"
    assert body["production_ready"] is False
    assert body["native_human"] == "NOT_RUN"
    assert body["classes"] == CLASS_ORDER
    assert set(body["classes"]) == CLASSES
    assert set(body["ops"]) == OPS
    assert len(NAMES) == 6


def test_production_factory_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RunnerError) as caught:
        production_factory()
    assert caught.value.code == "missing_api_key"


@pytest.mark.parametrize("provider,key_name", [("typesafe", "TYPESAFE_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"), ("local", None)])
def test_production_factory_routes_selected_provider(monkeypatch, provider, key_name):
    import decision_browser.server as server_module

    calls = []
    sentinel = object()

    def factory(**settings):
        calls.append(settings)
        return sentinel

    monkeypatch.setattr(server_module, "SystemOneProvider", factory)
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", provider)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-openai-key")
    if key_name:
        monkeypatch.setenv(key_name, "chosen-key")
    assert production_factory() is sentinel
    assert len(calls) == 1
    assert calls[0]["api_key"] == ("chosen-key" if key_name else None)
    assert calls[0]["max_choices"] == (26 if provider == "local" else 255)


def test_production_factory_default_still_uses_original_openai(monkeypatch):
    import decision_browser.server as server_module

    calls = []
    monkeypatch.setattr(server_module, "OpenAIProvider", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    production_factory()
    assert calls == [{"api_key": "openai-key"}]


async def test_local_capabilities_are_prerequisites_not_live_evidence(monkeypatch):
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "CANARY-secret")
    body = (await create_server().call_tool("capabilities", {})).structured_content
    assert body["provider"] == "local"
    assert body["provider_ready"] is True and body["api_key_present"] is False
    assert body["live_status"] == "PREREQUISITES_PRESENT"
    assert body["production_ready"] is False and body["native_human"] == "NOT_RUN"
    assert body["text_generation"] == "unconfigured" and body["visual"] == "unsupported"
    assert "CANARY-secret" not in json.dumps(body)


async def test_invalid_provider_config_is_sanitized_and_blocks_factory_and_caps(monkeypatch):
    monkeypatch.setenv("DECISION_BROWSER_PROVIDER", "CANARY-secret")
    with pytest.raises(RunnerError) as caught:
        production_factory()
    assert caught.value.code == "invalid_provider_config"
    body = (await create_server().call_tool("capabilities", {})).structured_content
    assert body["provider_ready"] is False and body["live_status"] == "BLOCKED"
    assert body["provider_error"] == "invalid_provider_config"
    assert "CANARY-secret" not in json.dumps(body)
