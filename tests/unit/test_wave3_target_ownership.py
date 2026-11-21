"""Browser protocol ownership controls independent of arrival order and inventory deltas."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from decision_browser.browser import BrowserDriver, _snapshot_from_target_infos


def info(target, opener=None):
    result = {"targetId": target, "type": "page", "url": "about:blank"}
    if opener:
        result["openerId"] = opener
    return result


def snapshot(targets):
    return _snapshot_from_target_infos({"targetInfos": list(targets.values())})


def prepared():
    driver = BrowserDriver("existing_chrome")
    driver._state = "open"
    driver._endpoint = SimpleNamespace()
    driver.owned_target_id = "root"
    driver._owned_target_ids = {"root"}
    targets = {"foreign": info("foreign"), "root": info("root")}
    driver.foreign_before = snapshot({"foreign": targets["foreign"]})
    driver._browser = SimpleNamespace(close=AsyncMock(side_effect=AssertionError("foreign browser close")))
    driver._context = SimpleNamespace(close=AsyncMock(side_effect=AssertionError("foreign context close")))
    closed = []

    async def close_target(method, params):
        assert method == "Target.closeTarget"
        closed.append(params["targetId"])
        targets.pop(params["targetId"], None)
        return {"success": True}

    session = SimpleNamespace(send=AsyncMock(side_effect=close_target), detach=AsyncMock())
    transport = SimpleNamespace(stop=AsyncMock())
    driver._ownership_session = session
    driver._playwright = transport

    async def inventory():
        for item in list(targets.values()):
            driver._record_target_created({"targetInfo": item})
        return snapshot(targets)

    driver._cdp_inventory = inventory
    return driver, targets, closed, session, transport


def event(driver, targets, target, opener):
    targets[target] = info(target, opener)
    driver._record_target_created({"targetInfo": targets[target]})


async def test_wave3_opener_chain_only_excludes_baseline_and_unrelated_targets():
    driver, targets, closed, session, transport = prepared()
    event(driver, targets, "grandchild", "popup")
    assert driver._owned_target_ids == {"root"}
    event(driver, targets, "popup", "root")
    event(driver, targets, "foreign", "root")  # baseline identity always wins
    event(driver, targets, "unrelated", "foreign")
    assert driver._owned_target_ids == {"root", "popup", "grandchild"}
    await driver.close()
    assert set(closed) == {"root", "popup", "grandchild"}
    assert set(targets) == {"foreign", "unrelated"}
    assert driver.residual_owned is False and not driver.cleanup_pending
    assert driver.foreign_mismatch is True  # unrelated user target remains visible
    session.detach.assert_awaited_once()
    transport.stop.assert_awaited_once()


async def test_wave3_inventory_opener_proof_added_during_final_sweep_is_closed():
    driver, targets, closed, _, _ = prepared()
    targets["popup"] = info("popup", "root")  # discovery event deliberately delayed
    assert "popup" not in driver._owned_target_ids
    await driver.close()
    assert set(closed) == {"root", "popup"}
    assert set(targets) == {"foreign"}
    assert driver.foreign_mismatch is False and driver.residual_owned is False


async def test_wave3_popup_close_cancel_retains_session_and_does_not_replay_acked_root():
    driver, targets, closed, session, transport = prepared()
    event(driver, targets, "popup", "root")
    original_send = session.send.side_effect
    attempted_popup = False

    async def cancel_popup(method, params):
        nonlocal attempted_popup
        if params["targetId"] == "popup" and not attempted_popup:
            attempted_popup = True
            raise asyncio.CancelledError
        return await original_send(method, params)

    session.send.side_effect = cancel_popup
    with pytest.raises(asyncio.CancelledError):
        await driver.close()
    assert driver.cleanup_pending and driver._ownership_session is session
    assert driver._playwright is transport
    assert driver._closed_target_ids == {"root"} and driver.residual_owned is True
    transport.stop.assert_not_awaited()
    session.detach.assert_not_awaited()
    await driver.close()
    assert closed.count("root") == closed.count("popup") == 1
    assert driver._state == "closed" and driver.residual_owned is False
    assert driver.foreign_mismatch is False


async def test_wave3_stop_only_retry_never_reopens_completed_target_cleanup():
    driver, _, closed, session, transport = prepared()
    transport.stop.side_effect = [asyncio.CancelledError(), None]
    with pytest.raises(asyncio.CancelledError):
        await driver.close()
    assert driver.cleanup_pending and driver._cleanup_mode_done
    assert driver._ownership_session is None and closed == ["root"]
    driver._cdp_inventory = AsyncMock(side_effect=AssertionError("finished inventory replay"))
    session.send.side_effect = AssertionError("finished close replay")
    await driver.close()
    assert driver._state == "closed" and closed == ["root"]
    assert transport.stop.await_count == 2


async def test_wave3_unacknowledged_creation_inventory_delta_is_never_owned():
    driver, targets, closed, _, _ = prepared()
    driver.owned_target_id = None
    driver._owned_target_ids.clear()
    await driver._cleanup_existing(starting_failure=True)
    assert closed == [] and "root" in targets
    assert driver.residual_owned is True and driver.foreign_mismatch is True


async def test_wave3_close_false_ack_keeps_exact_target_and_transport_for_retry():
    driver, _, closed, session, transport = prepared()
    original_send = session.send.side_effect
    session.send.side_effect = None
    session.send.return_value = {"success": False}
    await driver.close()
    assert driver.cleanup_pending and driver.residual_owned is True
    assert driver._ownership_session is session and driver._playwright is transport
    transport.stop.assert_not_awaited()
    session.send.side_effect = original_send
    await driver.close()
    assert closed == ["root"] and driver._state == "closed"


async def test_wave3_discovery_overflow_is_unknown_retained_work(monkeypatch):
    from decision_browser import browser

    driver, targets, closed, session, transport = prepared()
    monkeypatch.setattr(browser, "_INVENTORY_TARGET_CAP", 1)
    event(driver, targets, "unrelated", "foreign")
    event(driver, targets, "popup", "root")
    await driver.close()
    assert driver.cleanup_pending and driver.residual_owned is True
    assert driver._ownership_session is session and driver._playwright is transport
    assert closed == ["root"] and "popup" in targets  # no guessed ownership after overflow
    assert driver.cleanup_errors.count("ownership_unverified") == 1
    transport.stop.assert_not_awaited()


async def test_wave3_detach_cancel_retries_without_replaying_target_closes():
    driver, _, closed, session, transport = prepared()
    session.detach.side_effect = [asyncio.CancelledError(), None]
    with pytest.raises(asyncio.CancelledError):
        await driver.close()
    assert driver.cleanup_pending and driver._ownership_session is session
    assert closed == ["root"]
    transport.stop.assert_not_awaited()
    await driver.close()
    assert driver._state == "closed" and closed == ["root"]
    assert session.detach.await_count == 2
    transport.stop.assert_awaited_once()


async def test_wave3_live_inventory_overrides_close_acknowledgement():
    driver, _, closed, session, transport = prepared()
    original_send = session.send.side_effect
    session.send.side_effect = None
    session.send.return_value = {"success": True}  # accepted request, target still visible
    await driver.close()
    assert driver.cleanup_pending and driver.residual_owned is True
    assert driver._owned_target_ids == {"root"} and not driver._closed_target_ids
    assert driver._ownership_session is session
    transport.stop.assert_not_awaited()
    session.send.side_effect = original_send
    await driver.close()
    assert driver._state == "closed" and closed == ["root"]


async def test_review_detach_late_target_retains_same_browser_and_recreates_session():
    driver, targets, closed, session, transport = prepared()
    recreated = SimpleNamespace(send=AsyncMock(side_effect=session.send.side_effect), detach=AsyncMock(), on=lambda *_: None)
    original_send = recreated.send.side_effect

    async def send(method, params):
        if method == "Target.setDiscoverTargets":
            return {}
        return await original_send(method, params)

    recreated.send.side_effect = send
    driver._browser.new_browser_cdp_session = AsyncMock(return_value=recreated)

    async def detach_late():
        event(driver, targets, "late-popup", "root")

    session.detach.side_effect = detach_late
    browser, context = driver._browser, driver._context
    await driver.close()
    assert driver.cleanup_pending and driver._state == "cleanup_pending"
    assert driver.residual_owned is True
    assert driver._browser is browser and driver._context is context
    assert driver._playwright is transport and driver._ownership_session is None
    assert driver._closed_target_ids == {"root"} and closed == ["root"]
    assert set(targets) == {"foreign", "late-popup"}
    transport.stop.assert_not_awaited()
    await driver.close()
    assert closed == ["root", "late-popup"]  # no replay of completed root closure
    assert set(targets) == {"foreign"}
    browser.new_browser_cdp_session.assert_awaited_once()
    assert driver._state == "closed" and driver.residual_owned is False
    assert driver.foreign_mismatch is False
    browser.close.assert_not_awaited()
    context.close.assert_not_awaited()
    transport.stop.assert_awaited_once()


async def test_review_final_inventory_late_event_is_not_absence_proof():
    driver, targets, closed, session, transport = prepared()
    baseline_inventory = driver._cdp_inventory
    first = True

    async def stale_snapshot_with_late_event():
        nonlocal first
        result = await baseline_inventory()
        if first:
            first = False
            event(driver, targets, "late-popup", "root")
        return result

    driver._cdp_inventory = stale_snapshot_with_late_event
    original_send = session.send.side_effect

    async def defer_new_pending(method, params):
        if params["targetId"] == "late-popup":
            return {"success": False}
        return await original_send(method, params)

    session.send.side_effect = defer_new_pending
    await driver.close()
    assert driver.cleanup_pending and driver.residual_owned is True
    assert driver._closed_target_ids == {"root"} and closed == ["root"]
    assert driver._ownership_session is session and driver._playwright is transport
    transport.stop.assert_not_awaited()
    session.send.side_effect = original_send
    await driver.close()
    assert closed == ["root", "late-popup"]
    assert set(targets) == {"foreign"}
    assert driver._state == "closed" and not driver.cleanup_pending
