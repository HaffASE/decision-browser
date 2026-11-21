"""Exercise the same model-visible browser refs as the standalone Terra worker."""
import importlib.util
import json
import re
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright


@pytest.mark.asyncio
async def test_new_document_reference_is_offered_and_stale_reference_is_rejected(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts/benchmarks'))
    from long_scenarios import ScenarioSite
    from long_terra import BASE_AGENT, load_agent

    spec = importlib.util.spec_from_file_location("short_terra", BASE_AGENT)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    agent = load_agent()
    with ScenarioSite() as site:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(chromium_sandbox=True, channel="chromium")
            try:
                page = await browser.new_page()
                start = site.start("rail_itinerary")
                await page.goto(start)
                await agent.observe(page)
                await page.get_by_label("From station").fill("York")
                await page.get_by_label("To station").fill("Durham")
                await page.get_by_role("button", name="Find trains").click()
                observed = await agent.observe(page)
                line = next(s for s in observed["snapshot"].splitlines() if 'link "Express 09:20"' in s)
                ref = line.split("[ref=", 1)[1].split("]", 1)[0]
                with pytest.raises(original.Failure, match="unoffered_ref"):
                    await original.perform(page, start, observed, "click", {"ref": ref})
                await agent.perform(page, start, observed, "click", {"ref": ref})
                next_page = await agent.observe(page)
                assert ref not in next_page["snapshot"]
                with pytest.raises(agent.Failure, match="unoffered_ref"):
                    await agent.perform(page, start, next_page, "click", {"ref": ref})
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_unsupported_press_gets_feedback_before_a_valid_press(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(scripts))
    from baseline_site import FixtureSite
    from long_terra import load_agent

    terra = load_agent()
    observed = []

    def respond(request):
        payload = json.loads(request.content)
        state = json.loads(payload["input"])
        observed.append(state)
        index = len(observed)
        if index in {1, 2}:
            match = re.search(r'button "Activate".*?\[ref=((?:f[0-9]+)?e[0-9]+)\]', state["browser"]["snapshot"])
            assert match
            name = "press"
            args = {"ref": match[1], "key": "Alt+Left" if index == 1 else "Space"}
        else:
            name, args = "done", {}
        return httpx.Response(200, json={
            "status": "completed", "model": terra.MODEL,
            "output": [{"type": "function_call", "name": name, "arguments": json.dumps(args)}],
            "usage": {"input_tokens": 100, "output_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
        })

    async with async_playwright() as pw:
        executable = pw.chromium.executable_path
    with FixtureSite() as site:
        fixture_id, url = site.register("click")
        result = await terra.run({
            "goal": "Activate", "values": {}, "start_url": url, "timeout_s": 20,
            "max_steps": 4, "max_calls": 4, "max_input_bytes": 32768,
            "browser_executable": executable,
            "checks": [{"type": "text_equals", "target": "#status", "value": "Activated"}],
        }, "synthetic-key", transport=httpx.MockTransport(respond))
        assert site.verify(fixture_id)["success"]
    assert result["status"] == "done" and result["checks"] == [True]
    assert result["rejected_actions"] == ["unsupported_press_key"]
    assert observed[1]["actions"] == [{"tool": "press", "error": "unsupported_press_key"}]


def test_terra_remembers_bounded_previous_page_evidence_without_refs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/benchmarks"))
    from long_terra import load_agent

    module = load_agent()
    agent = module.Terra(None, "synthetic-key", {})
    for i in range(5):
        agent.remember_page({"snapshot": f"- paragraph: Platform {i} [ref=f3e8]\n" + "á" * 2048})
    assert len(agent.previous_pages) == 3
    assert agent.previous_pages[0].startswith("- paragraph: Platform 2\n")
    assert all("[ref=" not in page and len(page.encode()) <= 2048 for page in agent.previous_pages)
