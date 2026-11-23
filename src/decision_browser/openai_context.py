"""Bounded observed affordances for native OpenAI choices."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .decision_codec import ChoiceQuestion

_FORM_RULE = (
    "Before submitting, compare these observed fields with the goal. Complete missing required "
    "fields using fill or select. Check requested checkbox states; unchecked may be correct. "
    "A checkbox state requested by the user must be satisfied even if the HTML field is optional. "
    "If its observed state differs from the requested state, choose the offered checkbox click "
    "before submitting. If its state already matches the request, leave it unchanged. "
    "Submission uses the current checkbox states shown here and does not toggle those controls."
)
_SCROLL_RULE = (
    "Scroll only toward content that can be revealed in that direction. "
    "If neither direction can move, use an offered visible control to advance the goal."
)


def _bounded(value: object, limit: int = 2048) -> str | None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded.encode()) <= limit else None


def _owner(row: dict[str, Any]) -> tuple[str, str, str] | None:
    form = row["form"]
    return (row["origin"], row["url"], form["path"]) if form else None


def _submits(row: dict[str, Any]) -> bool:
    node = row["node"]
    operation = row["operation"]
    if operation == "submit":
        return True
    if operation not in {"click", "enter"}:
        return False
    return row["form"].get("submits") is True or (
        row["form"].get("submits") is not False and operation == "click"
        and node.get("state", {}).get("type") in {"submit", "image"}
    )


def action_evidence(
    questions: tuple[ChoiceQuestion, ...], source: dict[str, Any],
    aliases: Mapping[str, Mapping[str, str]], *, operation_head: str,
    target_heads: Mapping[str, str],
) -> tuple[ChoiceQuestion, ...]:
    """Supplement validated native choices without truncating any evidence group."""
    fields: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    visible: dict[str, dict[str, None]] = {}
    for group, rows in source["candidates"].items():
        for row in rows.values():
            operation, node = row["operation"], row["node"]
            state = node.get("state", {})
            if (not state.get("disabled") and operation == "click"
                    and row["name"]):
                visible.setdefault(group, {})[row["name"]] = None
            owner = _owner(row)
            if owner is None:
                continue
            item = {"name": row["name"]}
            if node.get("valueMissing") is True:
                item["valueMissing"] = True
            if state.get("type") in {"checkbox", "radio"} or node.get("role") in {"checkbox", "radio", "switch"}:
                item["checked"] = state["checked"]
            if state.get("tag") == "select":
                item["value"] = node.get("value")
                if state.get("type") == "select-multiple" and "selected" in node:
                    item["selected"] = node["selected"]
            if len(item) > 1:
                fields.setdefault(owner, {})[json.dumps(item, sort_keys=True)] = item
    summaries = {owner: _bounded(list(items.values())) for owner, items in fields.items()}
    scroll = source.get("scroll")
    scrolling = _bounded({"scroll": scroll}) if scroll is not None else None
    clicking = {group: _bounded(list(names)) for group, names in visible.items()}
    scroll_rule = _SCROLL_RULE
    if scroll == {"up": False, "down": False}:
        scroll_rule += (
            " Neither direction can move the observed page. Scrolling again leaves this viewport unchanged. "
            "Compare the offered visible controls with the next unfinished requirement in the goal."
        )
    groups = {head: group for group, head in target_heads.items()}
    rewritten = []
    for question in questions:
        if question.name == operation_head:
            descriptions = list(question.descriptions or (None,) * len(question.choices))
            for index, choice in enumerate(question.choices):
                controls = clicking.get(str(choice))
                if choice in aliases and str(choice).split("_group", 1)[0] == "click" and controls:
                    descriptions[index] = " ".join(filter(None, (descriptions[index],
                        "Compare these observed clickable controls with the next unfinished requirement in the goal.",
                        "Observed click controls: " + controls)))
                if choice in aliases and str(choice).split("_group", 1)[0] == "scroll" and scrolling:
                    descriptions[index] = " ".join(filter(None, (descriptions[index], scroll_rule,
                                                                  "Observed evidence: " + scrolling)))
            rewritten.append(replace(question, descriptions=tuple(descriptions)))
            continue
        group = groups[question.name]
        descriptions = []
        for choice, description in zip(question.choices, question.descriptions, strict=True):
            row = source["candidates"][group][aliases[group][str(choice)]]
            state = row["node"].get("state", {})
            if (row["operation"] == "click" and state.get("tag") == "input"
                    and state.get("type") == "checkbox" and not state.get("disabled")):
                effect = "off" if state["checked"] else "on"
                current = "checked" if state["checked"] else "unchecked"
                description = ((description or "") + f" Click toggles this currently {current} checkbox {effect}."
                               " Compare this effect with the goal before choosing it.")
            owner = _owner(row)
            evidence = summaries.get(owner) if owner is not None else None
            if _submits(row) and evidence:
                description = (description or "") + " " + _FORM_RULE + " Observed form fields: " + evidence
            if row["operation"] == "scroll" and scrolling:
                description = (description or "") + " " + scroll_rule + " Observed evidence: " + scrolling
            descriptions.append(description)
        rewritten.append(replace(question, descriptions=tuple(descriptions)))
    return tuple(rewritten)
