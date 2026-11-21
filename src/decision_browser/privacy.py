"""Gate outgoing bytes before any socket. This module does not send.

CaptureEvidence is the capture pipeline's own assessment of sanitization and
secrets. It is not a TaskRequest field, an admission token, or a caller bool.
A secret rendered into pixels cannot be deduced from base64 or raw bytes.
Without that assessment, image bytes stay on the machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from .contracts import PrivacyPolicy


class PrivacyError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CaptureEvidence:
    capture_sha256: str
    sanitized: bool
    sensitive_content_detected: bool

    def __post_init__(self) -> None:
        if type(self.capture_sha256) is not str:
            raise TypeError("capture_sha256")
        if type(self.sanitized) is not bool or type(self.sensitive_content_detected) is not bool:
            raise TypeError("capture_flags")


class PrivacyGate:
    def __init__(self, policy: PrivacyPolicy) -> None:
        self._policy = policy

    def prepare_image(self, image: bytes, evidence: CaptureEvidence) -> str:
        self._allow_image(evidence)
        self._reject_raw(image)
        if _digest(image) != evidence.capture_sha256:
            raise PrivacyError("evidence_mismatch")
        payload = base64.b64encode(image).decode("ascii")
        return f"data:image/png;base64,{payload}"

    def check_payload(
        self,
        body: bytes,
        capture: CaptureEvidence | None = None,
        contains_image: bool = False,
    ) -> None:
        self._reject_raw(body)
        try:
            parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PrivacyError("malformed_json") from None
        strings = _walk(parsed)
        self._reject_text(strings)
        images = [item for item in strings if _data_image(item)]
        if not images and not contains_image:
            return
        if len(images) != 1 or not contains_image:
            raise PrivacyError("image_count" if len(images) > 1 else "image_flag")
        if capture is None:
            raise PrivacyError("evidence_missing")
        self._allow_image(capture)
        self._bind(images[0], capture)

    def _allow_image(self, evidence: CaptureEvidence) -> None:
        if not self._policy.send_images:
            raise PrivacyError("images_disabled")
        if not evidence.sanitized or evidence.sensitive_content_detected:
            raise PrivacyError("evidence_unsafe")

    def _literals(self) -> list[str]:
        return [item for item in self._policy.blocked_literals if item]

    def _reject_raw(self, blob: bytes) -> None:
        for literal in self._literals():
            if literal.encode("utf-8") in blob:
                raise PrivacyError("blocked_literal")

    def _reject_text(self, strings: list[str]) -> None:
        for literal in self._literals():
            if any(literal in text for text in strings):
                raise PrivacyError("blocked_literal")

    def _bind(self, url: str, evidence: CaptureEvidence) -> None:
        prefix = "data:image/png;base64,"
        if not url.startswith(prefix):
            raise PrivacyError("image_undecodable")
        payload = url[len(prefix) :]
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            raise PrivacyError("image_undecodable") from None
        self._reject_raw(raw)
        if _digest(raw) != evidence.capture_sha256:
            raise PrivacyError("evidence_mismatch")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _data_image(value: str) -> bool:
    return "data:image" in value.lower()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise PrivacyError("malformed_json")
        seen.add(key)
        obj[key] = value
    return obj


def _walk(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            found.extend(_walk(key))
            found.extend(_walk(value))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(_walk(item))
        return found
    return []
