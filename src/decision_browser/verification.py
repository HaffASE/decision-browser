"""Allowlisted functional checks, deterministic geometry, and a visual-model seam.

Limitations
-----------
``verify_functional`` runs only the declared allowlist. It never reads model
prose, a DONE token, or a screenshot. ``terminal=\"verified\"`` means every
declared check matched in one final synchronous isolated-world observation.
It does not mean the product is correct or that the page stays unchanged after
return. Empty input is unverified. Text/value readiness waits at most 500 ms
for attachment per selector; it never retries a mismatching expected value.
Route/visible/count are immediate in the final observation.

``inspect_geometry`` resizes the page to the requested viewport and always
tries to restore the previous size. The probe is one fixed script owned by
this module and runs in a code-owned isolated world. It considers visible
``button``, ``input``, ``select``,
``textarea``, ``label``, ``p``, and ``a`` elements, at most 256 of them.
Clipping is reported only when an ancestor with ``overflow`` ``hidden`` or
``clip`` actually cuts the element box. Overlap is reported only for sibling
interactive pairs (button, input, select, textarea, link). Label/control
pairs and ancestor/descendant pairs are excluded, including ``label for`` and
wrapping labels. Responsive means an element box crosses the requested
viewport horizontally. Document scroll width is not used. At most 128
findings are returned. ``diagnostic=\"complete\"`` means that deterministic
pass finished, including when findings exist. It is not a clean bill of health.

``data-visual-ignore=\"true\"`` on the element or an ancestor omits that
subtree. The marker is a trusted fixture the caller owns. Overlays, modals,
and fixed layers are not suppressed unless they carry it.

``validate_visual_response`` checks a caller-supplied object against a closed
schema. It does not call a provider, take a screenshot, or transmit pixels.
Empty findings use reason ``no_model_findings_not_proof_of_cleanliness``.
The model report has no functional, terminal, or verified field. An unsafe
capture is incomplete before any other outcome. Reasons are stable codes and
do not echo page data, expected values, or transport bodies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from decision_browser.check_validation import (
    CHECK_KINDS,
    MAX_CHECKS,
    FixtureScalar,
    check_problem,
    clean_fixture_state,
    shown_target,
)
from decision_browser.contracts import Check, CheckKind, Viewport
from decision_browser.dom import _CDPWorld

_TIMEOUT_MS = 500
_MAX_CHECKS = MAX_CHECKS
_MAX_NODES = 256
_MAX_FINDINGS = 128
_STRING_CAP = 1024
_BOX_SLOP = 0.5

_TAGS = frozenset({"button", "input", "select", "textarea", "label", "p", "a"})
_INTERACTIVE = frozenset({"button", "input", "select", "textarea", "a"})

EvidenceReason = Literal[
    "ok",
    "type_mismatch",
    "target_invalid",
    "target_oversized",
    "expected_oversized",
    "selector_rejected",
    "empty_target",
    "mismatch",
    "playwright_error",
    "missing_fixture_state",
    "fixture_state_invalid",
    "too_many_checks",
]

# No awaits or page-world callbacks inside this observation. Results contain
# only booleans and closed codes, never arbitrary actual page strings.
_FUNCTIONAL_PROBE = """function(checks) {
  function visible(el) {
    const style = getComputedStyle(el);
    if (style.display === 'contents') {
      for (const child of el.childNodes) {
        if (child.nodeType === 1 && visible(child)) return true;
        if (child.nodeType === 3) {
          const range = document.createRange();
          range.selectNode(child);
          const rect = range.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) return true;
        }
      }
      return false;
    }
    if (!el.checkVisibility() || style.visibility !== 'visible') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function result(passed) {
    return {passed, reason: passed ? 'ok' : 'mismatch'};
  }
  return checks.map(check => {
    try {
      if (check.kind === 'route') return result(location.href === check.expected);
      const nodes = document.querySelectorAll(check.target);
      if (check.kind === 'count') return result(nodes.length === check.expected);
      if (nodes.length === 0 && check.kind === 'visible') return result(check.expected === false);
      if (nodes.length !== 1) throw new Error('strict selector');
      let el = nodes[0];
      if (check.kind === 'text') return result(el.textContent === check.expected);
      if (check.kind === 'visible') return result(visible(el) === check.expected);
      if (!el.matches('input,textarea,select') && !el.isContentEditable)
        el = el.closest('button,[role=button],[role=checkbox],[role=radio]') || el;
      if (!el.matches('a,input,textarea,button,select,[role=link],[role=button],[role=checkbox],[role=radio]') && !el.isContentEditable) {
        const label = el.closest('label');
        if (label && label.control) el = label.control;
      }
      if (!['INPUT','TEXTAREA','SELECT'].includes(el.nodeName)) throw new Error('not an input');
      return result(el.value === check.expected);
    } catch (_) {
      return {passed:false, reason:'playwright_error'};
    }
  });
}"""

# Fixed probe. No interpolation and no caller-supplied script.
_GEOMETRY_PROBE = """() => {
  const cap = 256;
  const selector = "button, input, select, textarea, label, p, a";
  const interactive = { button: true, input: true, select: true, textarea: true, a: true };
  function ignored(el) {
    let node = el;
    while (node && node.nodeType === 1) {
      if (node.getAttribute("data-visual-ignore") === "true") return true;
      node = node.parentElement;
    }
    return false;
  }
  function shown(el) {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") {
      return false;
    }
    for (let node = el; node; node = node.parentElement) {
      const opacity = Number(window.getComputedStyle(node).opacity);
      if (!Number.isFinite(opacity) || opacity === 0) return false;
    }
    if (el.getClientRects().length === 0) return false;
    const rect = el.getBoundingClientRect();
    if (!Number.isFinite(rect.x) || !Number.isFinite(rect.y)) return false;
    if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height)) return false;
    return rect.width > 0 || rect.height > 0;
  }
  function clipped(el) {
    const rect = el.getBoundingClientRect();
    let node = el.parentElement;
    while (node) {
      const style = window.getComputedStyle(node);
      const clipX = style.overflowX === "hidden" || style.overflowX === "clip";
      const clipY = style.overflowY === "hidden" || style.overflowY === "clip";
      if (clipX || clipY) {
        const host = node.getBoundingClientRect();
        const outsideX = rect.left < host.left - 0.5 || rect.right > host.right + 0.5;
        const outsideY = rect.top < host.top - 0.5 || rect.bottom > host.bottom + 0.5;
        if ((clipX && outsideX) || (clipY && outsideY)) return true;
      }
      node = node.parentElement;
    }
    return false;
  }
  function bounded(value) {
    const text = value || "";
    let out = "";
    let bytes = 0;
    let seen = 0;
    for (let i = 0; i < text.length && seen < 200; ) {
      const code = text.charCodeAt(i);
      let units = 1;
      let size = 0;
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = text.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          units = 2;
          size = 4;
        } else {
          seen += 1;
          i += 1;
          continue;
        }
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        seen += 1;
        i += 1;
        continue;
      } else if (code <= 0x7f) {
        size = 1;
      } else if (code <= 0x7ff) {
        size = 2;
      } else {
        size = 3;
      }
      if (seen + units > 200 || bytes + size > 200) break;
      out += text.slice(i, i + units);
      bytes += size;
      seen += units;
      i += units;
    }
    return out;
  }
  const elements = [];
  const found = document.querySelectorAll(selector);
  for (let i = 0; i < found.length && elements.length < cap; i += 1) {
    const el = found[i];
    if (!ignored(el) && shown(el)) elements.push(el);
  }
  const parents = new Map();
  let nextParent = 1;
  const nodes = [];
  for (let i = 0; i < elements.length; i += 1) {
    const el = elements[i];
    const rect = el.getBoundingClientRect();
    const ancestors = [];
    for (let j = 0; j < elements.length; j += 1) {
      if (i !== j && elements[j].contains(el)) ancestors.push(j);
    }
    let parentKey = -1;
    const parent = el.parentElement;
    if (parent) {
      const known = parents.get(parent);
      if (known === undefined) {
        parentKey = nextParent;
        nextParent += 1;
        parents.set(parent, parentKey);
      } else {
        parentKey = known;
      }
    }
    const tag = el.tagName.toLowerCase();
    nodes.push({
      tag: tag,
      htmlId: bounded(el.id || ""),
      forId: bounded(el.getAttribute("for") || ""),
      labelledBy: bounded(el.getAttribute("aria-labelledby") || ""),
      interactive: interactive[tag] === true,
      parent: parentKey,
      ancestors: ancestors,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      clipped: clipped(el)
    });
  }
  return { nodes: nodes };
}"""


class CheckEvidence(BaseModel):
    """One allowlisted check outcome. Values from the page are not stored."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: CheckKind
    target: str
    passed: bool
    reason: EvidenceReason


class FunctionalReport(BaseModel):
    """Terminal and functional status for an independent batch of checks."""

    model_config = ConfigDict(extra="forbid", strict=True)

    terminal: Literal["verified", "completed_unverified", "verification_failed"]
    functional: Literal["verified", "unverified", "failed"]
    evidence: list[CheckEvidence]


class GeometryRegion(BaseModel):
    """Finite box in viewport coordinates. Width and height may be zero."""

    model_config = ConfigDict(extra="forbid", strict=True)

    x: float
    y: float
    width: float
    height: float

    @field_validator("x", "y", "width", "height")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value

    @field_validator("width", "height")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("negative")
        return value


class GeometryFinding(BaseModel):
    """One deterministic geometry defect. Origin is always geometry."""

    model_config = ConfigDict(extra="forbid", strict=True)

    origin: Literal["geometry"]
    kind: Literal["clipping", "overlap", "responsive"]
    viewport: Viewport
    region: GeometryRegion
    element: str = Field(min_length=1, max_length=_STRING_CAP)
    evidence: str = Field(min_length=1, max_length=_STRING_CAP)
    severity: Literal["low", "medium", "high"]
    uncertainty: float = Field(ge=0.0, le=1.0)

    @field_validator("uncertainty")
    @classmethod
    def _uncertainty(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value

    @field_validator("element", "evidence")
    @classmethod
    def _byte_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _STRING_CAP:
            raise ValueError("bound")
        return value


class GeometryReport(BaseModel):
    """Complete means the deterministic pass finished, not that the UI is clean."""

    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic: Literal["complete", "failed"]
    findings: list[GeometryFinding]
    reason: str = Field(min_length=1, max_length=_STRING_CAP)


class ModelRegion(BaseModel):
    """Closed region. Integers normalize to finite floats. Booleans do not."""

    model_config = ConfigDict(extra="forbid", strict=False)

    x: float
    y: float
    width: float
    height: float

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def _number(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("coord")  # noqa: TRY004 — Pydantic converts ValueError to schema failure
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("coord") from exc
        if not math.isfinite(number):
            raise ValueError("coord")
        return number

    @field_validator("width", "height")
    @classmethod
    def _positive(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("size")
        return value


class ModelViewport(BaseModel):
    """Strict integer viewport. Floats and booleans are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    width: int = Field(ge=1, le=20000)
    height: int = Field(ge=1, le=20000)


class ModelFinding(BaseModel):
    """Model note; text is at most 1024 Unicode characters/4096 UTF-8 bytes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    origin: Literal["model"]
    evidence: str
    viewport: ModelViewport
    region: ModelRegion
    element: str
    severity: Literal["low", "medium", "high"]
    uncertainty: float = Field(ge=0.0, le=1.0)

    @field_validator("uncertainty", mode="before")
    @classmethod
    def _strict_unit(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("uncertainty")  # noqa: TRY004 - Pydantic schema failure
        if not 0 <= value <= 1:
            raise ValueError("uncertainty")
        return float(value)

    @field_validator("evidence", "element")
    @classmethod
    def _text(cls, value: str) -> str:
        if value.strip() == "" or len(value) > _STRING_CAP:
            raise ValueError("bound")
        # A Unicode scalar is at most four UTF-8 bytes. Encoding also rejects
        # unpaired surrogates, which are not valid UTF-8 text.
        value.encode("utf-8")
        return value


class _ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    findings: list[ModelFinding]

    @field_validator("findings")
    @classmethod
    def _cap(cls, value: list[ModelFinding]) -> list[ModelFinding]:
        if len(value) > _MAX_FINDINGS:
            raise ValueError("cap")
        return value


class ModelVisualReport(BaseModel):
    """Schema verdict only. No functional, terminal, or verified status."""

    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic: Literal["complete", "failed", "incomplete"]
    reason: str = Field(min_length=1, max_length=_STRING_CAP)
    findings: list[ModelFinding]


@dataclass(frozen=True)
class _Node:
    index: int
    tag: str
    element: str
    html_id: str
    for_id: str
    labelled_by: str
    interactive: bool
    parent: int
    ancestors: frozenset[int]
    x: float
    y: float
    width: float
    height: float
    clipped: bool


def _nbytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _clip_text(value: str, limit: int = _STRING_CAP) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    trimmed = raw[:limit].decode("utf-8", "ignore")
    return trimmed or "element"


def _evidence(
    kind: CheckKind, target: str, passed: bool, reason: EvidenceReason
) -> CheckEvidence:
    safe_target = shown_target(target)
    return CheckEvidence(kind=kind, target=safe_target, passed=passed, reason=reason)


async def _functional_batch(page: Page, checks: Sequence[Check]) -> list[tuple[bool, EvidenceReason]]:
    payload: list[dict[str, object]] = []
    for check in checks:
        expected: object = check.expected
        # A DOM collection cannot have more than 2**32-1 entries. Keep larger
        # Python integers out of the JavaScript number transport entirely.
        if check.kind == "count" and type(expected) is int and expected > 0xFFFFFFFF:
            expected = None
        payload.append({"kind": check.kind, "target": check.target, "expected": expected})
    # Reuse this stateless realm on the same Document; each temporary CDP
    # session still detaches below. DOMSession keeps its unique default realm.
    world = _CDPWorld(page, world_name="decision-browser-functional-verification-v1")
    try:
        await world.refresh()
        raw = await world.evaluate(_FUNCTIONAL_PROBE, payload)
    finally:
        await world.close()
    if type(raw) is not list or len(raw) != len(checks):
        raise PlaywrightError("invalid verification observation")
    cleaned: list[tuple[bool, EvidenceReason]] = []
    for row in raw:
        if (
            type(row) is not dict or type(row.get("passed")) is not bool
            or row.get("reason") not in {"ok", "mismatch", "playwright_error"}
        ):
            raise PlaywrightError("invalid verification observation")
        reason: EvidenceReason = (
            "ok" if row["reason"] == "ok" else
            "mismatch" if row["reason"] == "mismatch" else "playwright_error"
        )
        cleaned.append((row["passed"] is True, reason))
    return cleaned


async def verify_functional(
    page: Page,
    checks: Sequence[Check],
    *,
    fixture_state: Mapping[str, FixtureScalar] | None = None,
) -> FunctionalReport:
    """Prepare attachment readiness, then measure one final check conjunction.

    Direct fixture maps are bounded snapshots supplied by a trusted adapter.
    Public TaskRequest rejects fixture_state because it has no such adapter.
    """
    if len(checks) == 0:
        return FunctionalReport(
            terminal="completed_unverified", functional="unverified", evidence=[]
        )
    if len(checks) > _MAX_CHECKS:
        return FunctionalReport(
            terminal="verification_failed", functional="failed",
            evidence=[_evidence("count", "", False, "too_many_checks")],
        )
    state = clean_fixture_state(fixture_state) if fixture_state is not None else None
    evidence: list[CheckEvidence | None] = [None] * len(checks)
    browser_checks: list[Check] = []
    indices: list[int] = []
    for index, check in enumerate(checks):
        kind: CheckKind = check.kind if type(check.kind) is str and check.kind in CHECK_KINDS else "count"
        target = shown_target(check.target)
        problem = check_problem(check.kind, check.target, check.expected)
        if problem is not None:
            evidence[index] = _evidence(kind, target, False, problem)
        elif check.kind == "fixture_state":
            if fixture_state is None:
                reason: EvidenceReason = "missing_fixture_state"
                passed = False
            elif state is None:
                reason, passed = "fixture_state_invalid", False
            else:
                actual = state.get(check.target)
                passed = type(actual) is type(check.expected) and actual == check.expected
                reason = "ok" if passed else "mismatch"
            evidence[index] = _evidence(kind, target, passed, reason)
        else:
            browser_checks.append(check)
            indices.append(index)

    readiness_failed: set[int] = set()
    for index, check in zip(indices, browser_checks, strict=True):
        if check.kind not in {"text", "value"}:
            continue
        try:
            await page.locator(check.target).wait_for(state="attached", timeout=_TIMEOUT_MS)
        except PlaywrightError:
            readiness_failed.add(index)

    if browser_checks:
        results: list[tuple[bool, EvidenceReason]]
        if all(check.kind == "route" for check in browser_checks):
            current_url = page.url
            results = [(current_url == check.expected,
                        "ok" if current_url == check.expected else "mismatch")
                       for check in browser_checks]
        else:
            try:
                results = await _functional_batch(page, browser_checks)
            except PlaywrightError:
                results = [(False, "playwright_error") for _ in browser_checks]
        for index, check, (passed, reason) in zip(indices, browser_checks, results, strict=True):
            if index in readiness_failed:
                evidence[index] = _evidence(check.kind, check.target, False, "playwright_error")
            else:
                evidence[index] = _evidence(check.kind, check.target, passed, reason)
    assert all(row is not None for row in evidence)
    complete = [row for row in evidence if row is not None]
    passed = all(row.passed for row in complete)
    return FunctionalReport(
        terminal="verified" if passed else "verification_failed",
        functional="verified" if passed else "failed", evidence=complete,
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _field_text(node: dict[str, object], key: str, limit: int) -> str | None:
    value = node.get(key)
    if type(value) is not str or _nbytes(value) > limit:
        return None
    return value


def _parse_node(raw: object, index: int) -> _Node | None:
    if type(raw) is not dict:
        return None
    tag = raw.get("tag")
    if type(tag) is not str or tag not in _TAGS:
        return None
    html_id = _field_text(raw, "htmlId", 200)
    for_id = _field_text(raw, "forId", 200)
    labelled_by = _field_text(raw, "labelledBy", 200)
    if html_id is None or for_id is None or labelled_by is None:
        return None
    interactive = raw.get("interactive")
    clipped = raw.get("clipped")
    if type(interactive) is not bool or type(clipped) is not bool:
        return None
    if interactive != (tag in _INTERACTIVE):
        return None
    parent = _as_int(raw.get("parent"))
    if parent is None:
        return None
    ancestors_raw = raw.get("ancestors")
    if type(ancestors_raw) is not list or len(ancestors_raw) > _MAX_NODES:
        return None
    ancestors: set[int] = set()
    for item in ancestors_raw:
        parsed = _as_int(item)
        if parsed is None or parsed < 0 or parsed >= _MAX_NODES:
            return None
        ancestors.add(parsed)
    x = _as_float(raw.get("x"))
    y = _as_float(raw.get("y"))
    width = _as_float(raw.get("width"))
    height = _as_float(raw.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    if width < 0 or height < 0 or (width == 0 and height == 0):
        return None
    element = _clip_text(f"{tag}#{html_id}" if html_id else tag)
    if element == "":
        return None
    return _Node(
        index=index,
        tag=tag,
        element=element,
        html_id=html_id,
        for_id=for_id,
        labelled_by=labelled_by,
        interactive=interactive,
        parent=parent,
        ancestors=frozenset(ancestors),
        x=x,
        y=y,
        width=width,
        height=height,
        clipped=clipped,
    )


def _region(x: float, y: float, width: float, height: float) -> GeometryRegion | None:
    if not all(math.isfinite(number) for number in (x, y, width, height)):
        return None
    if width < 0 or height < 0:
        return None
    return GeometryRegion(x=x, y=y, width=width, height=height)


def _overlaps(left: _Node, right: _Node) -> tuple[float, float, float, float] | None:
    ix = max(left.x, right.x)
    iy = max(left.y, right.y)
    right_edge = min(left.x + left.width, right.x + right.width)
    bottom = min(left.y + left.height, right.y + right.height)
    width = right_edge - ix
    height = bottom - iy
    if width <= _BOX_SLOP or height <= _BOX_SLOP:
        return None
    return ix, iy, width, height


def _append(
    findings: list[GeometryFinding],
    *,
    kind: Literal["clipping", "overlap", "responsive"],
    viewport: Viewport,
    region: GeometryRegion | None,
    element: str,
    evidence: str,
    severity: Literal["low", "medium", "high"],
    uncertainty: float,
) -> None:
    if (
        region is None
        or len(findings) >= _MAX_FINDINGS
        or element == ""
        or evidence == ""
    ):
        return
    findings.append(
        GeometryFinding(
            origin="geometry",
            kind=kind,
            viewport=viewport,
            region=region,
            element=_clip_text(element),
            evidence=evidence,
            severity=severity,
            uncertainty=uncertainty,
        )
    )


def _report_from_probe(raw: object, viewport: Viewport) -> GeometryReport:
    if type(raw) is not dict:
        return GeometryReport(
            diagnostic="failed", findings=[], reason="probe_payload_invalid"
        )
    nodes_raw = raw.get("nodes")
    if type(nodes_raw) is not list:
        return GeometryReport(
            diagnostic="failed", findings=[], reason="probe_payload_invalid"
        )
    if len(raw) != 1:
        return GeometryReport(
            diagnostic="failed", findings=[], reason="probe_payload_invalid"
        )
    slots: list[_Node | None] = []
    for index, item in enumerate(nodes_raw[:_MAX_NODES]):
        slots.append(_parse_node(item, index))
    findings: list[GeometryFinding] = []
    nodes = [node for node in slots if node is not None]
    for node in nodes:
        if node.clipped:
            _append(
                findings,
                kind="clipping",
                viewport=viewport,
                region=_region(node.x, node.y, node.width, node.height),
                element=node.element,
                evidence="clipped by an ancestor with overflow hidden or clip",
                severity="medium",
                uncertainty=0.1,
            )
        past_left = node.x < -_BOX_SLOP
        past_right = node.x + node.width > float(viewport.width) + _BOX_SLOP
        if past_left or past_right:
            _append(
                findings,
                kind="responsive",
                viewport=viewport,
                region=_region(node.x, node.y, node.width, node.height),
                element=node.element,
                evidence="element bounds extend past the horizontal viewport",
                severity="medium",
                uncertainty=0.05,
            )
    for left_index, left in enumerate(nodes):
        if len(findings) >= _MAX_FINDINGS:
            break
        for right in nodes[left_index + 1 :]:
            if len(findings) >= _MAX_FINDINGS:
                break
            if not left.interactive or not right.interactive:
                continue
            if left.parent < 0 or left.parent != right.parent:
                continue
            if left.index in right.ancestors or right.index in left.ancestors:
                continue
            box = _overlaps(left, right)
            if box is None:
                continue
            _append(
                findings,
                kind="overlap",
                viewport=viewport,
                region=_region(box[0], box[1], box[2], box[3]),
                element=_clip_text(f"{left.element}+{right.element}"),
                evidence="sibling interactive elements overlap",
                severity="high",
                uncertainty=0.15,
            )
    return GeometryReport(
        diagnostic="complete", findings=findings, reason="geometry_pass_completed"
    )


def _viewport_ok(viewport: Viewport) -> bool:
    width = viewport.width
    height = viewport.height
    if type(width) is not int or type(height) is not int:
        return False
    return 1 <= width <= 8192 and 1 <= height <= 8192


async def _restore_viewport(page: Page, original: object) -> bool:
    if not isinstance(original, dict):
        return False
    width = original.get("width")
    height = original.get("height")
    if type(width) is not int or type(height) is not int:
        return False
    if width < 1 or height < 1:
        return False
    try:
        await page.set_viewport_size({"width": width, "height": height})
    except PlaywrightError:
        return False
    restored = page.viewport_size
    if not isinstance(restored, dict):
        return False
    return restored.get("width") == width and restored.get("height") == height


async def inspect_geometry(page: Page, viewport: Viewport) -> GeometryReport:
    """Resize locally, run the fixed probe, then restore the prior viewport."""
    if not _viewport_ok(viewport):
        return GeometryReport(
            diagnostic="failed", findings=[], reason="viewport_invalid"
        )
    original = page.viewport_size
    original_width = original.get("width") if isinstance(original, dict) else None
    original_height = original.get("height") if isinstance(original, dict) else None
    if (
        not isinstance(original, dict)
        or type(original_width) is not int
        or type(original_height) is not int
        or original_width < 1
        or original_height < 1
    ):
        return GeometryReport(
            diagnostic="failed", findings=[], reason="viewport_original_invalid"
        )
    report = GeometryReport(diagnostic="failed", findings=[], reason="playwright_error")
    try:
        await page.set_viewport_size(
            {"width": viewport.width, "height": viewport.height}
        )
        world = _CDPWorld(page, world_name="decision-browser-geometry-verification-v1")
        try:
            await world.refresh()
            raw = await world.evaluate(_GEOMETRY_PROBE)
        finally:
            await world.close()
        report = _report_from_probe(raw, viewport)
    except PlaywrightError:
        report = GeometryReport(
            diagnostic="failed", findings=[], reason="playwright_error"
        )
    finally:
        if not await _restore_viewport(page, original):
            report = GeometryReport(
                diagnostic="failed",
                findings=report.findings,
                reason="viewport_restore_failed",
            )
    return report


def _model(
    diagnostic: Literal["complete", "failed", "incomplete"], reason: str
) -> ModelVisualReport:
    return ModelVisualReport(diagnostic=diagnostic, reason=reason, findings=[])


def _refusal(payload: dict[str, object]) -> bool:
    markers = {"refusal", "refused", "refusal_reason"}
    if markers.intersection(payload):
        return True
    # Tuple membership compares by equality. A list or dict token must not be hashed.
    kind = payload.get("type")
    status = payload.get("status")
    return kind in ("refusal", "refused") or status in ("refusal", "refused")


def validate_visual_response(
    payload: object,
    *,
    safe_capture: bool,
    transport_error: str | None = None,
) -> ModelVisualReport:
    """Validate a visual payload. This function never contacts a model provider."""
    if type(safe_capture) is not bool or not safe_capture:
        return _model("incomplete", "unsafe_capture")
    if transport_error is not None:
        return _model("failed", "transport_error")
    if type(payload) is not dict:
        return _model("failed", "malformed_payload")
    for key in ("type", "status"):
        if key in payload and type(payload[key]) is not str:
            return _model("failed", "malformed_payload")
    if _refusal(payload):
        return _model("incomplete", "model_refusal")
    if payload.get("incomplete") is True or payload.get("status") == "incomplete":
        return _model("incomplete", "incomplete_payload")
    if "findings" not in payload:
        return _model("incomplete", "incomplete_payload")
    if set(payload) != {"findings"}:
        return _model("failed", "malformed_payload")
    try:
        parsed = _ModelPayload.model_validate(payload)
    except ValidationError:
        return _model("failed", "malformed_payload")
    if len(parsed.findings) == 0:
        return ModelVisualReport(
            diagnostic="complete",
            reason="no_model_findings_not_proof_of_cleanliness",
            findings=[],
        )
    return ModelVisualReport(
        diagnostic="complete",
        reason="model_findings_recorded",
        findings=parsed.findings,
    )
