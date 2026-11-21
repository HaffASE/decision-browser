"""Pinned owned-fixture documents. Resolve is exact content-id equality."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from types import MappingProxyType

from decision_browser.policy import ActionScope, classify

_ORIGIN = "http://owned-fixture.invalid"
_FINGERPRINT = "owned-fixture-v1"
_ERROR = "owned fixture catalog mismatch"
_PINS = {
    "clean": "02ba761051ac15bd4b5c1762266efe1fba9fb40d8cef50b9871a112765ff172c",
    "clipping": "2014082eb93e8f11ffb8b51a0086ef2446df740c51a781003466db01ddc63ff6",
    "overlap": "f6c992423ebfc1f34cbff1693c64ddea1d59d7c01883274fefb1cae0df7dc47f",
    "responsive": "adf76193d6ebd2c5e10095f8b78e104528b4ecc13ffd11c109cb0f5737be7648",
}
_IDS = {
    "clean": f"{_ORIGIN}/v1/clean",
    "clipping": f"{_ORIGIN}/v1/clipping",
    "overlap": f"{_ORIGIN}/v1/overlap",
    "responsive": f"{_ORIGIN}/v1/responsive",
}
_CLEAN = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Clean control</title>
  <style>
    body {
      margin: 0;
      font-family: system-ui, sans-serif;
      background: #fff;
      color: #444;
    }
    main {
      max-width: 36rem;
      margin: 0 auto;
      padding: 1rem 1.25rem;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    label { color: #555; }
    input, button {
      font: inherit;
      color: #333;
      background: #f4f4f4;
      border: 1px solid #ccc;
      padding: 0.5rem 0.75rem;
      max-width: 100%;
    }
  </style>
</head>
<body>
  <main>
    <form action=\"#\">
      <label for=\"query\">Search</label>
      <input id=\"query\" name=\"query\" type=\"text\" value=\"atlas\">
      <button id=\"submit\" type=\"submit\">Search</button>
    </form>
  </main>
</body>
</html>
"""
_CLIPPING = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Clipping</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #fff; color: #444; }
    /* Defect class: clipping. The 80px overflow:hidden ancestor clips a 160px button. Both sit inside every owned viewport. */
    #clip-box {
      width: 80px;
      height: 48px;
      overflow: hidden;
      border: 1px solid #ccc;
      background: #f4f4f4;
    }
    #clipped {
      width: 160px;
      height: 32px;
      margin: 8px;
    }
  </style>
</head>
<body>
  <div id=\"clip-box\">
    <button id=\"clipped\" type=\"button\">Clipped action</button>
  </div>
</body>
</html>
"""
_OVERLAP = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Overlap</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #fff; color: #444; }
    /* Defect class: overlap. Absolute buttons share 30px (left 20px width 100px, and left 90px). */
    #stage { position: relative; height: 80px; }
    #real-a, #real-b {
      position: absolute;
      top: 16px;
      width: 100px;
      height: 36px;
      border: 1px solid #ccc;
      background: #f4f4f4;
    }
    #real-a { left: 20px; }
    #real-b { left: 90px; }
    /* Trusted owned fixture annotation. data-visual-ignore=true suppresses this overlapping subtree only. */
    #ignored {
      position: relative;
      height: 70px;
      margin-top: 12px;
    }
    #ignored button {
      position: absolute;
      top: 8px;
      width: 100px;
      height: 36px;
    }
    #ignored .one { left: 20px; }
    #ignored .two { left: 50px; }
  </style>
</head>
<body>
  <div id=\"stage\">
    <button id=\"real-a\" type=\"button\">Alpha</button>
    <button id=\"real-b\" type=\"button\">Beta</button>
  </div>
  <div id=\"ignored\" data-visual-ignore=\"true\">
    <button class=\"one\" type=\"button\">Ignore A</button>
    <button class=\"two\" type=\"button\">Ignore B</button>
  </div>
</body>
</html>
"""
_RESPONSIVE = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Responsive</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #fff; color: #444; }
    /* Defect class: responsive. 1700px button overflows 390, 768, and 1440. */
    #overflow-button {
      width: 1700px;
      height: 40px;
      border: 1px solid #ccc;
      background: #f4f4f4;
    }
  </style>
</head>
<body>
  <button id=\"overflow-button\" type=\"button\">Overflows every owned viewport</button>
</body>
</html>
"""
_BODIES = {
    "clean": _CLEAN,
    "clipping": _CLIPPING,
    "overlap": _OVERLAP,
    "responsive": _RESPONSIVE,
}


def validate_literals(records: Mapping[str, str] | None = None) -> None:
    """Reject a missing, empty, or non-pinned literal set."""
    bodies = _BODIES if records is None else records
    if set(bodies) != set(_PINS):
        raise RuntimeError(_ERROR)
    for key, pin in _PINS.items():
        body = bodies[key]
        if not isinstance(body, str) or not body:
            raise RuntimeError(_ERROR)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest != pin:
            raise RuntimeError(_ERROR)


validate_literals()


@dataclass(frozen=True, slots=True)
class OwnedDocument:
    content_id: str
    body: str
    signals: tuple[str, ...]


class _Signals(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.skip += tag in {"style", "script"}
        if tag != "input":
            return
        value = next((item for name, item in attrs if name == "value" and item and item.strip()), None)
        if value:
            self.out.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        self.skip -= tag in {"style", "script"} and self.skip > 0

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self.skip:
            self.out.append(text)


def _signals(body: str) -> tuple[str, ...]:
    parser = _Signals()
    parser.feed(body)
    parser.close()
    return tuple(parser.out)


def resolve(content_id: str) -> OwnedDocument | None:
    """Return one pinned document, or None unless the content id matches exactly."""
    if not isinstance(content_id, str):
        return None
    key = next((name for name, url in _IDS.items() if url == content_id), None)
    if key is None:
        return None
    validate_literals(_BODIES)
    body = _BODIES[key]
    signals = _signals(body)
    scope = ActionScope(
        run_id="owned-fixture",
        operation="navigate",
        document_origin=_ORIGIN,
        destination_origin=_ORIGIN,
        semantic_fingerprint=_FINGERPRINT,
        signals=signals,
        destination_url=content_id,
    )
    if classify(scope) != "clear_non_match":
        raise RuntimeError(_ERROR)
    return OwnedDocument(content_id, body, signals)


PINS: Mapping[str, str] = MappingProxyType(_PINS)
CONTENT_IDS: Mapping[str, str] = MappingProxyType(_IDS)
DOCUMENTS: Mapping[str, str] = MappingProxyType(_BODIES)
