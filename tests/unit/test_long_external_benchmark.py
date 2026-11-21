"""Mixed-provider trials must count and cap every billable HTTP request."""

import json
from pathlib import Path

import httpx
import pytest


def test_ultrafast_decision_and_helper_share_one_budget(monkeypatch):
    directory = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(directory))
    from long_external import OPENAI_TEXT, OPENCODE_DECISIONS, Provider

    provider = Provider({"max_calls": 2, "max_input_bytes": 131072}, "opencode", "helper", "decision")
    sent = []

    def handle(request):
        url = str(request.url)
        sent.append(url)
        expected = "decision" if url == OPENCODE_DECISIONS else "helper"
        assert request.headers["authorization"] == "Bearer " + expected
        model = json.loads(request.content)["model"]
        usage = {"input_tokens": 5, "output_tokens": 2,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
        if url == OPENCODE_DECISIONS:
            return httpx.Response(200, json={"model": model, "answers": {}, "usage": usage})
        assert url == OPENAI_TEXT and model == "gpt-6-luna"
        return httpx.Response(200, json={"model": model, "status": "completed", "usage": usage,
                                        "output": [{"type": "message", "role": "assistant", "content": [
                                            {"type": "output_text", "text": '{"text":"ice"}'}]}]})

    with provider.meter(), httpx.Client(transport=httpx.MockTransport(handle)) as client:
        post = provider.ultrafast_transport(client)
        decision = {"model": "jev-1.13"}
        assert post("https://api.typesafe.ai/v1/systemone", "", decision)["model"] == "jev-1.13"
        result = post("https://api.openai.com/v1/chat/completions", "",
                      {"messages": [{"role": "user", "content": "Enter the query."}]})
        assert result["choices"][0]["message"]["content"] == '{"text": "ice"}'
        with pytest.raises(httpx.RequestError, match="physical_request_budget"):
            post("https://api.typesafe.ai/v1/systemone", "", decision)
    assert sent == [OPENCODE_DECISIONS, OPENAI_TEXT]
    usage = provider.snapshot()
    assert [row["endpoint"] for row in usage["calls"]] == ["systemone", "responses"]
    assert usage["usage_complete"]


@pytest.fixture
def terra(monkeypatch):
    import importlib.util

    directory = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "long_terra_under_test", directory / "long_terra.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_agent()


def terra_reply(usage):
    return {
        "model": "gpt-6.1-terra",
        "status": "completed",
        "usage": usage,
        "output": [
            {
                "type": "function_call",
                "name": "done",
                "arguments": "{}",
                "status": "completed",
            }
        ],
    }


@pytest.mark.parametrize(
    "usage,reason",
    [
        (
            {
                "input_tokens": 1000,
                "output_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            },
            "cost_limit",
        ),
        ({"input_tokens": 10, "output_tokens": 1}, "incomplete_provider_usage"),
    ],
)
async def test_terra_stops_next_request_on_cost_or_unknown_prior_usage(
    terra, usage, reason
):
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json=terra_reply(usage))

    data = {
        "goal": "fixture",
        "values": {},
        "max_calls": 3,
        "max_input_bytes": 131072,
        "max_cost_usd": 0.00001,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        agent = terra.Terra(client, "TEST_ONLY_KEY", data)
        assert await agent.choose({}) == ("done", {})
        with pytest.raises(terra.Failure, match=reason):
            await agent.choose({})
    assert len(sent) == 1
    assert json.loads(sent[0].content)["max_output_tokens"] == 1024


async def test_terra_cost_below_stop_keeps_final_allowed_call(terra):
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(
            200,
            json=terra_reply(
                {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "input_tokens_details": {
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                }
            ),
        )

    data = {
        "goal": "fixture",
        "values": {},
        "max_calls": 2,
        "max_input_bytes": 131072,
        "max_cost_usd": 0.25,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        agent = terra.Terra(client, "TEST_ONLY_KEY", data)
        assert await agent.choose({}) == await agent.choose({}) == ("done", {})
        with pytest.raises(terra.Failure, match="call_limit"):
            await agent.choose({})
    assert len(sent) == 2


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_terra_keeps_page_checks_and_reports_cleanup_separately(
    terra, monkeypatch, cleanup_failure
):
    from types import SimpleNamespace

    class Page:
        url = "http://127.0.0.1/fixture/"

        def set_default_timeout(self, _):
            pass

        async def goto(self, *_args, **_kwargs):
            pass

        async def aria_snapshot(self, **_kwargs):
            return "fixture complete"

        async def evaluate(self, _source, *args):
            return (
                {"checks": [True], "final_url": self.url}
                if args
                else {"y": 0, "height": 720, "pageHeight": 720}
            )

    class Context:
        async def new_page(self):
            return Page()

        async def route(self, *_args):
            pass

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

        async def close(self):
            if cleanup_failure:
                raise RuntimeError("PRIVATE_CLEANUP_CANARY")

    async def launch(**kwargs):
        assert kwargs["chromium_sandbox"] is True
        return Browser()

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(terra, "async_playwright", Playwright)
    data = {
        "goal": "fixture",
        "values": {},
        "max_calls": 1,
        "max_input_bytes": 131072,
        "max_cost_usd": 0.25,
        "timeout_s": 10,
        "browser_executable": "owned-fixture-fake",
        "max_steps": 1,
        "checks": [],
        "start_url": Page.url,
    }
    usage = {
        "input_tokens": 10,
        "output_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    }
    result = await terra.run(
        data,
        "TEST_ONLY_KEY",
        httpx.MockTransport(lambda _: httpx.Response(200, json=terra_reply(usage))),
    )
    assert result["status"] == "done" and result["checks"] == [True]
    assert result["cleanup"] == {
        "browser": "cleanup_error" if cleanup_failure else "closed"
    }
    assert result["usage"]["usage_complete"] is True
    assert "PRIVATE_CLEANUP_CANARY" not in json.dumps(result)
