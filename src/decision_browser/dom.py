"""Top-frame DOM observe, guard, and dispatch.

Document identity is the live Document object. Guard and the native call are
not atomic: transport and in-page script can still run between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

_QUERY = "a,button,input,textarea,select,summary,form,iframe,canvas,[role],[tabindex]"
# Newlines keep a bundled trailing line comment inside its source boundary.
_SOURCE = "\n" + Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8") + "\n"
_NULL = "() => (" + _SOURCE + ")(null, null)"
_COLLECTOR = "\n" + Path(__file__).with_name("collector.js").read_text(encoding="utf-8") + "\n"
_COLLECT = "sel => (" + _COLLECTOR + ")(sel, (" + _SOURCE + "))"
_TEXT = Path(__file__).with_name("viewport_text.js").read_text(encoding="utf-8")
_SHADOW = "() => { const snapshot = (" + _SOURCE + "); let n = 0; for (const el of document.querySelectorAll('*')) if (Reflect.get(Object.getPrototypeOf(el), 'shadowRoot', el) && snapshot(el, 'click').geometry.visible) n++; return n; }"
_OWNER = "el => { const read=(node,key)=>Reflect.get(Object.getPrototypeOf(node),key,node); return read(el,'tagName')==='FORM' ? el : read(el,'form') || Reflect.apply(read(el,'closest'),el,['form']); }"
_SCROLL = "(dir) => scrollBy(0, dir === 'down' ? innerHeight : -innerHeight)"
_SUBMIT = "el => HTMLFormElement.prototype.requestSubmit.call(el)"
_MAX, _CAP, _TEXT_MAX = 64 * 1024, 80, 4096
_NO_FILL = frozenset(
    {
        "file",
        "password",
        "hidden",
        "checkbox",
        "radio",
        "button",
        "submit",
        "reset",
        "image",
        "range",
    }
)
_UNSUP = {
    "iframe": "iframe",
    "canvas": "canvas",
    "shadowroot": "shadow",
    "file": "file",
    "password": "password",
    "download": "download",
    "targetblank": "newwindow",
    "namedtarget": "namedtarget",
}
_ELEMENT = frozenset({"click", "fill", "select", "enter", "submit"})
_DOC = frozenset({"scroll", "wait", "screenshot", "navigate"})


def _canon(obj: Any) -> Any:
    return json.loads(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def _same_semantic(actual: Any, expected: Any, *, focused_fill: bool = False) -> bool:
    actual, expected = _canon(actual), _canon(expected)
    if focused_fill and expected.get("operation") == "fill":
        node, prior = actual.get("node", {}), expected.get("node", {})
        aria = node.get("state", {}).get("aria", {})
        old_aria = prior.get("state", {}).get("aria", {})
        if (node.get("role") == prior.get("role") == "combobox"
                and old_aria.get("expanded") == "false" and aria.get("expanded") == "true"):
            aria["expanded"] = "false"
    return actual == expected


def _fp(
    token: str | None, document_id: str, destination: str | None, semantic: Any
) -> str:
    body = {
        "token": token,
        "document_id": document_id,
        "destination": destination,
        "semantic": semantic,
    }
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Candidate:
    token: str
    operations: tuple[str, ...]
    semantic_by_operation: Mapping[str, Any]
    geometry: Mapping[str, Any] | None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    origin: str
    document_id: str
    text: str
    candidates: Mapping[str, Candidate]
    unsupported: tuple[str, ...]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Action:
    operation: str
    token: str | None
    value: str | int | None
    destination: str | None
    observation_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    status: str
    reason: str
    geometry: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    status: str
    reason: str
    data: Any = None


class _CDPHandle:
    _APPLY = "function(source,arg,hasArg){var fn=(0,eval)('('+source+')');return hasArg?fn.call(this,this,arg):fn.call(this,this);}"

    def __init__(
        self,
        world: _CDPWorld,
        object_id: str | None,
        context_id: int,
        backend_id: int | None,
        subtype: str | None,
        type_name: str | None,
        value: Any,
        unserializable: str | None,
    ) -> None:
        self._world = world
        self.object_id = object_id
        self.backend_id = backend_id
        self.context_id = context_id
        self.point: tuple[int, int] | None = None
        self._subtype = subtype
        self._type = type_name
        self._value = value
        self._unserializable = unserializable

    def as_element(self) -> _CDPHandle | None:
        if self._subtype == "node":
            return self
        return None

    def _bind(self, world: _CDPWorld) -> None:
        if self._world is not world:
            raise PlaywrightError("foreign handle")
        if self.context_id != world._context_id:
            raise PlaywrightError("execution context mismatch")
        if self._subtype == "null":
            return
        if self._subtype == "node" and not self.object_id:
            raise PlaywrightError("disposed")

    def _call_argument(self, world: _CDPWorld) -> dict[str, Any]:
        self._bind(world)
        if self._subtype == "null":
            return {"value": None}
        if self._type == "undefined":
            return {"unserializableValue": "undefined"}
        if self._type in ("string", "number", "boolean"):
            if self._unserializable is not None:
                return {"unserializableValue": self._unserializable}
            return {"value": self._value}
        if self._type == "bigint" and self._unserializable is not None:
            return {"unserializableValue": self._unserializable}
        if not self.object_id:
            raise PlaywrightError("missing objectId")
        return {"objectId": self.object_id}

    async def evaluate(self, function_string: str, arg: Any = None) -> Any:
        remote = await self._call(function_string, arg, True)
        return _CDPWorld._decode_value(remote)

    async def evaluate_handle(
        self, function_string: str, arg: Any = None
    ) -> _CDPHandle:
        remote = await self._call(function_string, arg, False)
        return await self._world._adopt(remote, True)

    async def _call(
        self, function_string: str, arg: Any, by_value: bool
    ) -> dict[str, Any]:
        self._bind(self._world)
        if not self.object_id:
            raise PlaywrightError("missing objectId")
        temps: list[str] = []
        has_arg = arg is not None
        arguments: list[dict[str, Any]] = [
            {"value": function_string},
            {"value": None},
            {"value": False},
        ]
        params: dict[str, Any] = {
            "functionDeclaration": _CDPHandle._APPLY,
            "objectId": self.object_id,
            "arguments": arguments,
            "returnByValue": by_value,
            "awaitPromise": True,
        }
        try:
            if has_arg:
                arguments[1] = await self._world._argument(arg, temps)
                arguments[2] = {"value": True}
            result = await self._world._send("Runtime.callFunctionOn", params)
        finally:
            await self._world._release_ids(temps)
        remote = result.get("result")
        if not isinstance(remote, dict):
            raise PlaywrightError("missing result")
        return remote

    async def get_properties(self) -> dict[str, _CDPHandle]:
        self._bind(self._world)
        if not self.object_id:
            raise PlaywrightError("missing objectId")
        result = await self._world._send(
            "Runtime.getProperties", {"objectId": self.object_id, "ownProperties": True}
        )
        values = [(desc["name"], desc["value"]) for desc in result.get("result") or []
                  if isinstance(desc.get("name"), str) and isinstance(desc.get("value"), dict)]
        props: dict[str, _CDPHandle] = {}
        index = -1
        try:
            for index, (name, value) in enumerate(values):
                props[name] = await self._world._adopt(value, False)
            return props
        except BaseException:
            # _adopt releases the rejected value. We still own earlier handles
            # and every property value not yet passed to _adopt.
            try:
                await _dispose_handles(list(props.values()))
            finally:
                await self._world._release_ids([
                    value["objectId"] for _, value in values[index + 1:]
                    if isinstance(value.get("objectId"), str)
                ])
            raise

    async def dispose(self) -> None:
        object_id = self.object_id
        self.object_id = None
        if not object_id:
            return
        with contextlib.suppress(PlaywrightError):
            await self._world._send("Runtime.releaseObject", {"objectId": object_id})


class _CDPWorld:
    _ARRAY = "function(){return Array.prototype.slice.call(arguments);}"

    def __init__(self, page: Page, *, world_name: str | None = None) -> None:
        self._page = page
        self._cdp: Any = None
        self._context_id: int | None = None
        self._frame_id: str | None = None
        self._runtime_enabled = False
        self._world_name = str(uuid.uuid4()) if world_name is None else world_name

    async def _ensure_session(self) -> Any:
        if self._cdp is None:
            self._cdp = cast(Any, await self._page.context.new_cdp_session(self._page))
            self._runtime_enabled = False
        if not self._runtime_enabled:
            await self._send("Runtime.enable")
            self._runtime_enabled = True
        return self._cdp

    async def _send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = self._cdp
        if session is None:
            raise PlaywrightError("missing CDP session")
        try:
            result = await session.send(method, params or {})
        except PlaywrightError:
            raise
        except Exception as exc:
            raise PlaywrightError(str(exc)) from exc
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise PlaywrightError("missing result")
        details = result.get("exceptionDetails")
        if details:
            raise PlaywrightError(self._exception_text(details))
        return result

    @staticmethod
    def _exception_text(details: Any) -> str:
        if isinstance(details, dict):
            exception = details.get("exception")
            if isinstance(exception, dict) and exception.get("description"):
                return str(exception["description"])
            if details.get("text"):
                return str(details["text"])
        return "evaluation failed"

    @staticmethod
    def _decode_value(remote: dict[str, Any]) -> Any:
        if remote.get("type") == "undefined":
            return None
        token = remote.get("unserializableValue")
        if token == "NaN":
            return float("nan")
        if token == "Infinity":
            return float("inf")
        if token == "-Infinity":
            return float("-inf")
        if token == "-0":
            return -0.0
        if token is not None:
            return token
        return remote.get("value")

    def _require_context(self) -> int:
        if self._context_id is None:
            raise PlaywrightError("missing execution context")
        return self._context_id

    async def refresh(self) -> None:
        await self._ensure_session()
        tree = await self._send("Page.getFrameTree")
        frame = (tree.get("frameTree") or {}).get("frame") or {}
        frame_id = frame.get("id")
        if not frame_id:
            raise PlaywrightError("missing main frame")
        # Protocol spells this grantUniveralAccess.
        created = await self._send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": self._world_name,
                "grantUniveralAccess": False,
            },
        )
        context_id = created.get("executionContextId")
        if context_id is None:
            raise PlaywrightError("missing execution context")
        self._frame_id = str(frame_id)
        self._context_id = int(context_id)

    async def _argument(self, arg: Any, temps: list[str]) -> dict[str, Any]:
        if isinstance(arg, _CDPHandle):
            return arg._call_argument(self)
        if isinstance(arg, list):
            return await self._array_argument(arg, temps)
        return {"value": arg}

    async def _array_argument(
        self, items: list[Any], temps: list[str]
    ) -> dict[str, Any]:
        arguments = [await self._argument(item, temps) for item in items]
        remote = await self._call_in_world(self._ARRAY, arguments, False)
        object_id = remote.get("objectId")
        if not object_id:
            raise PlaywrightError("missing objectId")
        temps.append(str(object_id))
        return {"objectId": object_id}

    async def _call_in_world(
        self,
        function_string: str,
        arguments: list[dict[str, Any]] | None,
        by_value: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "functionDeclaration": function_string,
            "executionContextId": self._require_context(),
            "returnByValue": by_value,
            "awaitPromise": True,
        }
        if arguments is not None:
            params["arguments"] = arguments
        result = await self._send("Runtime.callFunctionOn", params)
        remote = result.get("result")
        if not isinstance(remote, dict):
            raise PlaywrightError("missing result")
        return remote

    async def _evaluate_document(self, by_value: bool) -> dict[str, Any]:
        result = await self._send(
            "Runtime.evaluate",
            {
                "expression": "document",
                "contextId": self._require_context(),
                "returnByValue": by_value,
            },
        )
        remote = result.get("result")
        if not isinstance(remote, dict):
            raise PlaywrightError("missing result")
        return remote

    async def _release_ids(self, object_ids: list[str]) -> None:
        failure: BaseException | None = None
        for object_id in object_ids:
            try:
                with contextlib.suppress(PlaywrightError):
                    await self._send("Runtime.releaseObject", {"objectId": object_id})
            except BaseException as error:  # noqa: BLE001 - finish releasing this acquisition batch
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    async def evaluate(self, function_string: str, arg: Any = None) -> Any:
        temps: list[str] = []
        try:
            if function_string == "document":
                remote = await self._evaluate_document(True)
            else:
                arguments = None if arg is None else [await self._argument(arg, temps)]
                remote = await self._call_in_world(function_string, arguments, True)
        finally:
            await self._release_ids(temps)
        return self._decode_value(remote)

    async def evaluate_handle(
        self, function_string: str, arg: Any = None
    ) -> _CDPHandle:
        temps: list[str] = []
        try:
            if function_string == "document":
                remote = await self._evaluate_document(False)
            else:
                arguments = None if arg is None else [await self._argument(arg, temps)]
                remote = await self._call_in_world(function_string, arguments, False)
            handle = await self._adopt(remote, True)
        finally:
            await self._release_ids(temps)
        return handle

    async def _adopt(self, remote: dict[str, Any], require_object: bool) -> _CDPHandle:
        object_id = remote.get("objectId")
        subtype = (
            remote.get("subtype") if isinstance(remote.get("subtype"), str) else None
        )
        type_name = remote.get("type") if isinstance(remote.get("type"), str) else None
        primitive = subtype == "null" or type_name in (
            "string",
            "number",
            "boolean",
            "undefined",
            "bigint",
        )
        if object_id is not None and not isinstance(object_id, str):
            raise PlaywrightError("missing objectId")
        if not object_id and ((require_object and subtype != "null") or not primitive):
            raise PlaywrightError("missing objectId")
        try:
            backend_id = None
            if subtype == "node":
                if not object_id:
                    raise PlaywrightError("unknown node")
                described = await self._send("DOM.describeNode", {"objectId": object_id})
                node = described.get("node") or {}
                backend_id = node.get("backendNodeId")
                if backend_id is None:
                    raise PlaywrightError("unknown node")
                doc = await self._evaluate_document(False)
                doc_id = doc.get("objectId")
                if not isinstance(doc_id, str):
                    raise PlaywrightError("foreign frame")
                try:
                    owned = await self._send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": "function(top){return this===top||Reflect.get(Object.getPrototypeOf(this),'ownerDocument',this)===top;}",
                            "objectId": object_id,
                            "arguments": [{"objectId": doc_id}],
                            "returnByValue": True,
                        },
                    )
                finally:
                    await self._release_ids([doc_id])
                if (owned.get("result") or {}).get("value") is not True:
                    raise PlaywrightError("foreign frame")
            return _CDPHandle(
                self,
                object_id,
                self._require_context(),
                int(backend_id) if backend_id is not None else None,
                subtype,
                type_name,
                remote.get("value") if "value" in remote else None,
                remote.get("unserializableValue")
                if isinstance(remote.get("unserializableValue"), str)
                else None,
            )
        except BaseException:
            if object_id:
                await self._release_ids([object_id])
            raise

    async def close(self) -> None:
        session = self._cdp
        self._cdp = None
        self._context_id = None
        self._frame_id = None
        self._runtime_enabled = False
        if session is None:
            return
        try:
            await session.detach()
        except PlaywrightError:
            raise
        except Exception as exc:
            raise PlaywrightError(str(exc)) from exc


class DOMSession:
    """Accepts a Playwright Page. close() drops handles only."""

    def __init__(
        self,
        page: Page,
        *,
        note_navigation: Callable[[], None] | None = None,
        navigation_remaining: Callable[[], float] | None = None,
    ) -> None:
        self._page = page
        self._world = _CDPWorld(page)
        self._note_navigation = note_navigation
        self._navigation_remaining = navigation_remaining
        self._doc: _CDPHandle | None = None
        self._doc_id = ""
        self._base: dict[str, Any] = {}
        self._url = ""
        self._obs: Observation | None = None
        self._handles: dict[str, _CDPHandle] = {}
        self._owners: dict[str, _CDPHandle] = {}
        self._popups = 0
        self._foreign_baseline = tuple(
            page for page in self._page.context.pages if page is not self._page
        )
        self._prepared: dict[int, tuple[Action, dict[str, Any]]] = {}

    @property
    def current_observation(self) -> Observation | None:
        """Last published observation by identity; successful close clears it.

        Failed cleanup may retain it without granting freshness or dispatch.
        """
        return self._obs

    def attach_owned_note(self, note: Callable[[], None]) -> None:
        """Connect the synchronous owned-navigation note once."""
        if not callable(note):
            raise TypeError("note")
        self._note_navigation = note

    async def close(self) -> None:
        try:
            try:
                await self._drop()
            finally:
                if self._doc is not None:
                    doc, self._doc = self._doc, None
                    await doc.dispose()
                self._obs = None
        finally:
            await self._world.close()

    async def observe(self) -> Observation:
        await self._drop()
        await self._bind_document()
        base = await self._world.evaluate(_NULL)
        self._base = dict(base["semantic"])
        self._url = str(self._base.get("url") or "")
        text = str(await self._world.evaluate(_TEXT))
        raw: _CDPHandle | None = None
        pending: list[_CDPHandle] = []
        overflow = False
        prefix, found, reasons, hosts = uuid.uuid4().hex, [], [], 0
        try:
            raw = await self._world.evaluate_handle(_COLLECT, _QUERY)
            diagnostics = await raw.evaluate("items => items.diagnostics")
            props = await raw.get_properties()
            pending = list(props.values())
            elements: list[_CDPHandle] = []
            for key, handle in props.items():
                el = handle.as_element() if key.isdigit() else None
                if el is None or len(elements) >= _CAP:
                    if el is not None:
                        overflow = True
                    pending.remove(handle)
                    await handle.dispose()
                else:
                    elements.append(el)
            released, raw = raw, None
            await released.dispose()
            for index, el in enumerate(elements):
                token = f"{prefix}-{index}"
                pending.remove(el)
                self._handles[token] = el
                self._owners[token] = await el.evaluate_handle(_OWNER)
                cand, reason, host = await self._candidate(token, el)
                found.append(cand)
                hosts += host
                if reason:
                    reasons.append(reason)
            if int(await self._world.evaluate(_SHADOW)) > hosts:
                reasons.append("shadow")
            self._popups = self._popup_count()
            if self._popups:
                reasons.append(f"popup:{self._popups}")
            if isinstance(diagnostics, dict):
                for reason in diagnostics.get("reasons") or []:
                    if reason not in reasons:
                        reasons.append(reason)
            if overflow:
                reasons.append("capacity")
            before_budget = len(found)
            found, reasons = _budget(text, found, reasons)
            if isinstance(diagnostics, dict):
                diagnostics["budget_dropped"] = before_budget - len(found)
            mapping = {item.token: item for item in found}
            for token in list(self._handles):
                if token not in mapping:
                    await self._handles.pop(token).dispose()
                    removed = self._owners.pop(token, None)
                    if removed is not None:
                        await removed.dispose()
            obs = Observation(
                uuid.uuid4().hex,
                str(self._base.get("origin") or ""),
                self._doc_id,
                text,
                mapping,
                tuple(reasons),
                diagnostics if isinstance(diagnostics, dict) else {},
                self._url,
            )
        except BaseException:
            try:
                await self._drop()
            finally:
                try:
                    await _dispose_handles(pending)
                finally:
                    if raw is not None:
                        await raw.dispose()
            raise
        self._obs = obs
        return obs

    def _target_identity(self, token: str | None) -> tuple[str, int] | None:
        """Current observed node identity only; never resolves or adopts a node."""
        obs = self._obs
        if token is None or obs is None or token not in obs.candidates or obs.document_id != self._doc_id:
            return None
        handle = self._handles.get(token)
        if handle is None or type(handle.backend_id) is not int:
            return None
        return self._doc_id, handle.backend_id

    async def prepare(
        self,
        op: str,
        token: str | None = None,
        *,
        value: Any = None,
        destination: str | None = None,
    ) -> Action:
        obs = self._obs
        if obs is None:
            raise ValueError("observe")
        if op == "navigate" and token is not None:
            candidate = obs.candidates.get(token)
            if candidate is None or "click" not in candidate.operations:
                raise ValueError("target")
            source = candidate.semantic_by_operation["click"]
            destination, value = _doc_args(op, value, destination, self._url)
            destination = await self._world.evaluate(
                "([destination,base]) => new URL(destination,base).href",
                [destination, self._url],
            )
            if destination != (source.get("node") or {}).get("href"):
                raise ValueError("navigate")
            semantic = {**source, "operation": op}
            result = Action(op, token, value, destination, obs.id,
                            _fp(token, obs.document_id, destination, semantic))
            stored = _stored(result)
            assert stored is not None
            self._prepared[id(result)] = (result, stored)
            return result
        if op in _DOC:
            if token is not None:
                raise ValueError("target")
            destination, value = _doc_args(op, value, destination, self._url)
            if op == "navigate":
                destination = await self._world.evaluate(
                    "([destination,base]) => new URL(destination,base).href",
                    [destination, self._url],
                )
            sem = {
                **self._base,
                "operation": op,
                "unsupported": None,
                "node": None,
                "form": None,
            }
            result = Action(
                op,
                None,
                value,
                destination,
                obs.id,
                _fp(None, obs.document_id, destination, sem),
            )
            stored = _stored(result)
            assert stored is not None
            self._prepared[id(result)] = (result, stored)
            return result
        cand = obs.candidates.get(token or "")
        if (
            token is None
            or op not in _ELEMENT
            or cand is None
            or op not in cand.operations
            or destination is not None
        ):
            raise ValueError("operation")
        value = _el_value(op, value, cand.semantic_by_operation[op])
        fp = _fp(token, obs.document_id, None, cand.semantic_by_operation[op])
        result = Action(op, token, value, None, obs.id, fp)
        stored = _stored(result)
        assert stored is not None
        self._prepared[id(result)] = (result, stored)
        return result

    async def guard(self, action: Action) -> GuardResult:
        try:
            return await self._guard(action)
        except PlaywrightError:
            return GuardResult("blocked", "context")

    async def _guard(self, action: Action, *, focused_fill: bool = False) -> GuardResult:
        registered = self._prepared.get(id(action))
        if registered is None or registered[0] is not action:
            return GuardResult("stale", "observation")
        obs, stored = self._obs, registered[1]
        if (
            obs is None
            or stored is None
            or action.observation_id != obs.id
            or self._doc is None
        ):
            return GuardResult("stale", "observation")
        try:
            same_document = await self._world.evaluate(
                "(old) => old === document", self._doc
            )
        except PlaywrightError:
            return GuardResult("stale", "document")
        if not same_document or stored.get("document_id") != obs.document_id:
            return GuardResult("stale", "document")
        if self._popup_count() != self._popups:
            return GuardResult("blocked", "popup")
        if action.operation in _DOC and action.token is None:
            base = await self._world.evaluate(_NULL)
            wanted = {
                key: stored["semantic"][key] for key in ("origin", "url", "timeOrigin")
            }
            if _canon(base["semantic"]) != _canon(wanted):
                return GuardResult("stale", "document")
            return GuardResult("pass", "")
        source_op = "click" if action.operation == "navigate" else action.operation
        el = self._handles.get(action.token or "")
        if el is None or source_op not in _ELEMENT:
            return GuardResult("stale", "target")
        try:
            if not await el.evaluate("el => Reflect.get(Object.getPrototypeOf(el),'isConnected',el)"):
                return GuardResult("stale", "detached")
            sem, raw = await self._shot(el, source_op)
            sem = {**sem, "operation": action.operation}
        except PlaywrightError:
            return GuardResult("stale", "detached")
        if not _same_semantic(sem, stored["semantic"], focused_fill=focused_fill):
            return GuardResult("stale", "semantic")
        fresh = await el.evaluate_handle(_OWNER)
        try:
            owner = self._owners.get(action.token or "")
            if owner is None or not await self._world.evaluate(
                "([a, b]) => a === b", [owner, fresh]
            ):
                return GuardResult("stale", "form")
        finally:
            await fresh.dispose()
        if action.operation == "fill":
            desc = await el.evaluate(
                "(el, value) => {\n"
                "  const raw = value == null ? '' : String(value);\n"
                "  const tag = String(el.nodeName || '').toLowerCase();\n"
                "  const typeable = {'': 1, email: 1, number: 1, search: 1, tel: 1, text: 1, url: 1};\n"
                "  const specials = {color: 1, date: 1, time: 1, 'datetime-local': 1, month: 1, week: 1};\n"
                "  if (tag === 'textarea') return {valid: true, value: raw, special: false};\n"
                "  if (tag !== 'input') return {valid: false, value: raw, special: false};\n"
                "  const type = String(el.type || '').toLowerCase();\n"
                "  if (type === 'password' || type === 'file' || type === 'range') return {valid: false, value: raw, special: false};\n"
                "  if (typeable[type]) {\n"
                "    let next = raw;\n"
                "    if (type === 'number') {\n"
                "      next = raw.trim();\n"
                "      if (isNaN(Number(next))) return {valid: false, value: next, special: false};\n"
                "    }\n"
                "    return {valid: true, value: next, special: false};\n"
                "  }\n"
                "  if (!specials[type]) return {valid: false, value: raw, special: false};\n"
                "  let next = type === 'color' ? raw.toLowerCase() : raw;\n"
                "  next = next.trim();\n"
                "  const doc = Reflect.get(Object.getPrototypeOf(el),'ownerDocument',el).implementation.createHTMLDocument('');\n"
                "  const probe = doc.createElement('input');\n"
                "  probe.type = type;\n"
                "  probe.value = next;\n"
                "  if (probe.value !== next) return {valid: false, value: next, special: true};\n"
                "  return {valid: true, value: next, special: true};\n"
                "}",
                action.value,
            )
            if (
                not isinstance(desc, dict)
                or not desc.get("valid")
                or not isinstance(desc.get("value"), str)
            ):
                return GuardResult("blocked", "value", None)
            stored["_fill"] = {
                "valid": True,
                "value": desc["value"],
                "special": bool(desc.get("special")),
            }
        geo = raw.get("geometry")
        blocked = await self._geometry(geo, el)
        if blocked is not None:
            return blocked
        owner = self._owners.get(action.token or "")
        if owner is None:
            return GuardResult("stale", "form")
        op_lit = "'" + source_op + "'"
        script = (
            "(element, storedOwner) => {"
            "const snap = (" + _SOURCE + ")(element, " + op_lit + ");"
            "const current = (" + _OWNER + ")(element);"
            "const sameOwner = current === storedOwner;"
            "const geo = snap && snap.geometry;"
            "const pt = geo && geo.point;"
            "const doc = (element && Reflect.get(Object.getPrototypeOf(element),'ownerDocument',element)) || document;"
            "const hit = pt && pt.length === 2 ? doc.elementFromPoint(pt[0], pt[1]) : null;"
            "const op = " + op_lit + ";"
            "const native = ((el, hit) => {if(!el||!hit)return 0;const read=(node,key)=>Reflect.get(Object.getPrototypeOf(node),key,node);const invoke=(node,key,...args)=>Reflect.apply(read(node,key),node,args);const parent=e=>read(e,'parentElement');const n=read(hit,'nodeType')===1?hit:parent(hit);if(!n||(n!==el&&!invoke(el,'contains',n)))return 0;if(String(read(el,'tagName')||'').toUpperCase()==='FORM')return op==='submit'?1:0;const nat=e=>{const t=String(read(e,'tagName')||'').toUpperCase();return t==='A'?(invoke(e,'hasAttribute','href')||(read(e,'namespaceURI')==='http://www.w3.org/2000/svg'&&invoke(e,'hasAttributeNS','http://www.w3.org/1999/xlink','href'))):t==='BUTTON'||t==='INPUT'||t==='SELECT'||t==='TEXTAREA';};for(let c=n,depth=0;c;c=parent(c)){if(++depth>10000)return 0;if(String(read(c,'tagName')||'').toUpperCase()==='LABEL'&&(c===el||!nat(el)||(read(c,'control')&&read(c,'control')!==el)))return 0;if(nat(c))return c===el?1:0;}return 1;})(element, hit);"
            "return {snap, sameOwner, native};"
            "}"
        )
        try:
            verdict = await el.evaluate(script, owner)
        except PlaywrightError:
            return GuardResult("stale", "detached")
        snap = verdict.get("snap") if isinstance(verdict, dict) else None
        if not isinstance(snap, dict):
            return GuardResult("stale", "detached")
        if not verdict.get("sameOwner"):
            return GuardResult("stale", "form")
        sem = _decorate(snap, None, action.operation, "")
        if not _same_semantic(sem, stored["semantic"], focused_fill=focused_fill):
            return GuardResult("stale", "semantic")
        geo = snap.get("geometry")
        pt = geo.get("point") if isinstance(geo, dict) else None
        if not (
            isinstance(geo, dict)
            and geo.get("visible") is True
            and isinstance(pt, list)
            and len(pt) == 2
        ):
            return GuardResult("blocked", "offscreen", geo)
        if geo.get("occluded") or verdict.get("native") != 1:
            return GuardResult("blocked", "covered", geo)
        el.point = (int(pt[0]), int(pt[1]))
        return GuardResult("pass", "", geo)

    async def dispatch(
        self, action: Action, before_dispatch: Callable[[Action], None] | None = None
    ) -> DispatchResult:
        """Guard, run the sync callback, then one native call. The gap is not atomic."""
        checked = await self.guard(action)
        if checked.status != "pass":
            return DispatchResult(checked.status, checked.reason)
        if before_dispatch is not None:
            before_dispatch(action)
        return DispatchResult("pass", "", await self._native(action))

    async def _native(self, action: Action) -> Any:
        op, el = action.operation, self._handles.get(action.token or "")
        if op == "scroll":
            await self._world.evaluate(_SCROLL, action.value)
        elif op == "wait":
            await self._page.wait_for_timeout(cast(float, action.value))
        elif op == "screenshot":
            return await self._page.screenshot(type="png")
        elif op == "navigate":
            note = self._note_navigation
            if note is not None:
                note()
            remaining = self._navigation_remaining
            timeout = 30_000.0 if remaining is None else max(0.0, remaining() * 1000)
            if not math.isfinite(timeout) or timeout <= 0:
                raise TimeoutError("navigation deadline")
            await self._page.goto(
                cast(str, action.destination), timeout=timeout, wait_until="load"
            )
            return self._page.url
        elif el is None:
            return None
        elif op == "click":
            el._bind(self._world)
            pt = el.point
            if pt is None:
                return None
            x, y = pt
            await self._world._send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            # Hover can synchronously replace or reroute the reviewed target.
            # The attempt has begun, so a failed recheck must never be retried.
            checked = await self.guard(action)
            if checked.status != "pass" or el.point != (x, y):
                raise RuntimeError("click recipient changed after hover")
            await self._world._send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            await self._world._send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
        elif op == "fill":
            registered = self._prepared.get(id(action))
            if not (
                isinstance(registered, tuple)
                and registered[0] is action
                and isinstance(registered[1], dict)
            ):
                raise RuntimeError("closed")
            desc = registered[1].get("_fill")
            if (
                not isinstance(desc, dict)
                or not isinstance(desc.get("value"), str)
                or "special" not in desc
            ):
                raise RuntimeError("closed")
            el._bind(self._world)
            bid = el.backend_id
            if bid is None:
                return None
            await self._world._send("DOM.focus", {"backendNodeId": bid})
            await self._verify_focused_recipient(action, el)
            if desc["special"]:
                ok = await el.evaluate(
                    "(el, value) => {\n"
                    "  el.value = value;\n"
                    "  if (el.value !== value) return false;\n"
                    "  el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));\n"
                    "  el.dispatchEvent(new Event('change', {bubbles: true}));\n"
                    "  return true;\n"
                    "}",
                    desc["value"],
                )
                if ok is not True:
                    raise RuntimeError("closed")
            else:
                await el.evaluate(
                    "(el) => {\n"
                    "  const tag = String(el.nodeName || '').toLowerCase();\n"
                    "  if (tag === 'input') el.select();\n"
                    "  else if (tag === 'textarea') {\n"
                    "    const n = el.value == null ? 0 : String(el.value).length;\n"
                    "    el.selectionStart = 0;\n"
                    "    el.selectionEnd = n;\n"
                    "  }\n"
                    "}",
                    None,
                )
                await self._verify_focused_recipient(action, el)
                await self._world._send("Input.insertText", {"text": desc["value"]})
        elif op == "select":
            el._bind(self._world)
            select_snapshot = self._prepared[id(action)][1]
            options = select_snapshot["semantic"]["node"]["options"]
            await el.evaluate(
                "(el, wanted) => {"
                "const snap = (" + _SOURCE + ")(el, 'select');"
                "const options = snap.semantic.node.options;"
                "if(!Reflect.get(Object.getPrototypeOf(el),'isConnected',el) || snap.semantic.node.state.disabled || "
                "JSON.stringify(options)!==JSON.stringify(wanted.options)) throw new Error('select changed');"
                "const matches = options.map((o,i)=>[o,i]).filter(([o])=>o[0]===wanted.value);"
                "if(matches.length!==1 || matches[0][0][2]) throw new Error('select ambiguous');"
                "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex').set.call(el, matches[0][1]);"
                "el.dispatchEvent(new Event('input', {bubbles:true}));"
                "el.dispatchEvent(new Event('change', {bubbles:true}));}",
                {"value": action.value, "options": options},
            )
        elif op == "enter":
            el._bind(self._world)
            await self._world._send("DOM.focus", {"backendNodeId": el.backend_id})
            await self._verify_focused_recipient(action, el)
            await self._world._send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "text": "\r",
                },
            )
            await self._world._send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "text": "\r",
                },
            )
        elif op == "submit":
            el._bind(self._world)
            await el.evaluate(_SUBMIT, None)
        return None

    async def settle(self, action: Action) -> None:
        """Bounded render opportunity after acknowledgement, never network-idle."""
        if action.operation not in {"click", "fill", "select", "enter", "submit"}:
            return
        # A native action can replace the Document and destroy its CDP context.
        # Keep this cancellable pause outside that context; observe() then binds
        # the current Document and the runner checks the actual destination.
        await asyncio.sleep(0.2)

    async def _verify_focused_recipient(self, action: Action, el: _CDPHandle) -> None:
        # Focus/select can synchronously run page handlers. Dispatch has already
        # begun: failure here is an ambiguous/partial attempt, never stale retry.
        checked = await self._guard(action, focused_fill=action.operation == "fill")
        if checked.status != "pass":
            raise RuntimeError("focused recipient changed after focus: " + checked.reason)
        focused = await el.evaluate(
            "el => Reflect.get(Object.getPrototypeOf(el),'isConnected',el) && Reflect.get(Object.getPrototypeOf(el),'ownerDocument',el) === document && document.activeElement === el"
        )
        if focused is not True:
            raise RuntimeError("focused recipient lost focus")

    async def _shot(
        self, el: _CDPHandle, operation: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = await el.evaluate(_SOURCE, operation)
        return _decorate(raw, None, operation, ""), raw

    async def _candidate(
        self, token: str, el: _CDPHandle
    ) -> tuple[Candidate, str | None, int]:
        sem, raw = await self._shot(el, "click")
        reason = (
            sem.get("unsupported") if isinstance(sem.get("unsupported"), str) else None
        )
        host = 1 if reason == "shadow" else 0
        if reason:
            return (
                Candidate(token, (), {"click": sem}, raw.get("geometry")),
                reason,
                host,
            )
        ops, by_op, geometry = _operations(sem), {}, raw.get("geometry")
        available = []
        for op in ops:
            if op == "click":
                by_op[op] = sem
                available.append(op)
                continue
            shot_sem, shot = await self._shot(el, op)
            if shot_sem.get("unsupported"):
                if shot_sem["unsupported"] == "formserialization":
                    # An unsupported future submission must not remove a safe
                    # fill/select operation on this same control.
                    by_op[op] = shot_sem
                    reason = "formserialization"
                    continue
                return (
                    Candidate(token, (), {op: shot_sem}, shot.get("geometry")),
                    str(shot_sem["unsupported"]),
                    host,
                )
            by_op[op] = shot_sem
            available.append(op)
            geometry = shot.get("geometry")
        return Candidate(token, tuple(available), by_op, geometry, raw.get("context") or {}), reason, host

    async def _geometry(self, geometry: Any, el: _CDPHandle) -> GuardResult | None:
        if not isinstance(geometry, dict):
            return GuardResult("blocked", "offscreen", None)
        rect = geometry.get("rect") or {}
        metrics = await self._world._send("Page.getLayoutMetrics", {})
        view = metrics.get("cssLayoutViewport") or {}
        vw = float(view.get("clientWidth", 0))
        vh = float(view.get("clientHeight", 0))
        pt = geometry.get("point")
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            px, py = int(pt[0]), int(pt[1])
        else:
            x = float(rect.get("x", 0))
            y = float(rect.get("y", 0))
            w = float(rect.get("width", 0))
            h = float(rect.get("height", 0))
            px, py = int((x + w / 2) // 1), int((y + h / 2) // 1)
        if (
            vw <= 0
            or vh <= 0
            or not geometry.get("visible")
            or not (0 <= px < vw and 0 <= py < vh)
        ):
            return GuardResult("blocked", "offscreen", geometry)
        if geometry.get("occluded"):
            return GuardResult("blocked", "covered", geometry)
        # CDP DOM hit testing takes document CSS coordinates. Snapshot points
        # and native mouse input remain relative to the main-frame viewport.
        hit = await self._world._send("DOM.getNodeForLocation", {
            "x": math.floor(px + float(view.get("pageX", 0))),
            "y": math.floor(py + float(view.get("pageY", 0))),
        })
        if not isinstance(hit, dict) or hit.get("frameId") != self._world._frame_id:
            return GuardResult("blocked", "covered", geometry)
        bid = hit.get("backendNodeId")
        if not isinstance(bid, int):
            return GuardResult("blocked", "covered", geometry)
        try:
            ctx_id = self._world._require_context()
        except PlaywrightError:
            return GuardResult("blocked", "covered", geometry)
        if not isinstance(ctx_id, int):
            return GuardResult("blocked", "covered", geometry)
        resolved = await self._world._send(
            "DOM.resolveNode", {"backendNodeId": bid, "executionContextId": ctx_id}
        )
        remote = resolved.get("object") if isinstance(resolved, dict) else None
        if not isinstance(remote, dict) or not remote.get("objectId"):
            return GuardResult("blocked", "covered", geometry)
        handle = await self._world._adopt(remote, True)
        try:
            verdict = await el.evaluate(
                "(el, hit) => {if(!el||!hit)return 0;const read=(node,key)=>Reflect.get(Object.getPrototypeOf(node),key,node);const invoke=(node,key,...args)=>Reflect.apply(read(node,key),node,args);const parent=e=>read(e,'parentElement');const n=read(hit,'nodeType')===1?hit:parent(hit);if(!n||(n!==el&&!invoke(el,'contains',n)))return 0;if(read(el,'tagName')==='FORM')return 1;const nat=e=>{const t=String(read(e,'tagName')||'').toUpperCase();return t==='A'?(invoke(e,'hasAttribute','href')||(read(e,'namespaceURI')==='http://www.w3.org/2000/svg'&&invoke(e,'hasAttributeNS','http://www.w3.org/1999/xlink','href'))):t==='BUTTON'||t==='INPUT'||t==='SELECT'||t==='TEXTAREA';};for(let c=n,depth=0;c;c=parent(c)){if(++depth>10000)return 0;if(String(read(c,'tagName')||'').toUpperCase()==='LABEL'&&(c===el||!nat(el)||(read(c,'control')&&read(c,'control')!==el)))return 0;if(nat(c))return c===el?1:0;}return 1;}",
                handle,
            )
        finally:
            await handle.dispose()
        if verdict != 1:
            return GuardResult("blocked", "covered", geometry)
        geometry["point"] = [px, py]
        return None

    async def _bind_document(self) -> None:
        await self._world.refresh()
        fresh = await self._world.evaluate_handle("document")
        try:
            if self._doc is None:
                self._doc, self._doc_id = fresh, uuid.uuid4().hex
                return
            try:
                same = await self._world.evaluate("([a, b]) => a === b", [self._doc, fresh])
            except PlaywrightError:
                same = False
                try:
                    await self._doc.dispose()
                except PlaywrightError:
                    pass
            else:
                await self._doc.dispose()
            self._doc = fresh
            if not same:
                self._doc_id = uuid.uuid4().hex
        except BaseException:
            await fresh.dispose()
            raise

    def _popup_count(self) -> int:
        foreign_baseline = self._foreign_baseline
        return sum(
            1
            for page in self._page.context.pages
            if page is not self._page
            and all(page is not foreign for foreign in foreign_baseline)
        )

    async def _drop(self) -> None:
        self._prepared.clear()
        handles = [*self._handles.values(), *self._owners.values()]
        self._handles.clear()
        self._owners.clear()
        await _dispose_handles(handles)


async def _dispose_handles(handles: list[_CDPHandle]) -> None:
    failure: BaseException | None = None
    for handle in handles:
        try:
            await handle.dispose()
        except BaseException as error:  # noqa: BLE001 - release the remaining acquired handles before raising
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


def _decorate(
    raw: dict[str, Any], role: str | None, operation: str, target: str
) -> dict[str, Any]:
    sem, node = (
        dict(raw.get("semantic") or {}),
        dict((raw.get("semantic") or {}).get("node") or {}),
    )
    if role:
        node["role"] = role
    sem["node"], sem["operation"] = node, operation
    code = sem.get("unsupported")
    reason = _UNSUP.get(code) if isinstance(code, str) else None
    if target not in {"", "_self", "_top"}:
        reason = "newwindow" if target == "_blank" else "namedtarget"
    if _oversized(node):
        node.pop("value", None), node.pop("options", None), node.pop("selected", None)
        sem["node"], sem["unsupported"] = node, "oversized"
        return sem
    if reason:
        sem["unsupported"] = reason
    return sem


def _oversized(node: Mapping[str, Any]) -> bool:
    value, options = node.get("value"), node.get("options")
    if isinstance(value, str) and len(value.encode()) > _TEXT_MAX:
        return True
    return options is not None and len(json.dumps(options).encode()) > _TEXT_MAX


def _operations(sem: Mapping[str, Any]) -> tuple[str, ...]:
    node, state = sem.get("node") or {}, (sem.get("node") or {}).get("state") or {}
    tag, kind = state.get("tag"), state.get("type") or ""
    if state.get("disabled"):
        return ()
    if tag == "form":
        return ("submit",)
    ops: list[str] = []
    if kind != "hidden":
        ops.append("click")
    if (
        tag == "textarea" or (tag == "input" and kind not in _NO_FILL)
    ) and not state.get("readonly"):
        ops.extend(("fill", "enter"))
    if tag == "select" and node.get("role") != "listbox":
        ops.append("select")
    return tuple(ops)


def _doc_args(
    op: str, value: Any, destination: str | None, url: str
) -> tuple[str | None, str | int | None]:
    if op in ("scroll", "wait") and destination is not None:
        raise ValueError(op)
    if op == "scroll":
        if value not in {"up", "down"}:
            raise ValueError("scroll")
        return None, value
    if op == "wait":
        amount = 100 if value is None else value
        if (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or not 0 <= amount <= 1000
        ):
            raise ValueError("wait")
        return None, amount
    if op == "screenshot":
        if value is not None or destination is not None:
            raise ValueError("screenshot")
        return None, None
    if not isinstance(destination, str):
        raise TypeError("navigate")
    if value is not None:
        raise ValueError("navigate")
    return _http(destination, url), None


def _http(destination: str, base: str) -> str:
    raw = destination.strip()
    if raw.lower().startswith("javascript:"):
        raise ValueError("navigate")
    parts = urlsplit(raw)
    if parts.scheme == "":
        raw, parts = urljoin(base, raw), urlsplit(urljoin(base, raw))
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("navigate")
    return parts.geturl()


def _el_value(op: str, value: Any, sem: Mapping[str, Any]) -> str | None:
    if op in {"click", "enter", "submit"}:
        if value is not None:
            raise ValueError(op)
        return None
    if op == "fill":
        if not isinstance(value, str) or len(value.encode()) > _TEXT_MAX:
            raise ValueError("fill")
        return value
    options = (sem.get("node") or {}).get("options") or []
    matches = [item for item in options
               if isinstance(item, list) and len(item) >= 3 and item[0] == value]
    if len(matches) > 1:
        raise ValueError("select_ambiguous")
    if not isinstance(value, str) or len(matches) != 1 or matches[0][2]:
        raise ValueError("select")
    return value


def _stored(action: Action) -> dict[str, Any] | None:
    try:
        data = json.loads(action.fingerprint)
    except json.JSONDecodeError:
        return None
    sem = data.get("semantic") if isinstance(data, dict) else None
    if not isinstance(sem, dict) or sem.get("operation") != action.operation:
        return None
    if (
        data.get("token") != action.token
        or data.get("destination") != action.destination
    ):
        return None
    return data


def _budget(
    text: str, found: list[Candidate], reasons: list[str]
) -> tuple[list[Candidate], list[str]]:
    def size(items: list[Candidate]) -> int:
        payload = {
            "text": text,
            "candidates": [item.semantic_by_operation for item in items],
            "unsupported": reasons,
        }
        return len(json.dumps(payload).encode())

    dropped = False
    while found and size(found) > _MAX:
        found.pop()
        dropped = True
    if dropped and "capacity" not in reasons:
        reasons.append("capacity")
    return found, reasons
