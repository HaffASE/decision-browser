# Bootstrap origin validation

Historical design record from the pre-migration implementation. Current native
commands and acceptance requirements are in [verification](verification.md).
The [original source and procedure](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/bootstrap-origin-validation.md)
remain available at the pinned revision.


## Cause and repair plan

The first Google Flights navigation was acknowledged, but the runner stopped
before the first provider decision. Bootstrap compared the browser's serialized
origin directly with the admission layer's canonical origin. A browser omits
default ports (`https://www.google.com`), while `canonical_origin` includes the
effective port (`https://www.google.com:443`). These strings never match on a
default-port HTTP or HTTPS document. Existing loopback tests used ephemeral,
nondefault ports and therefore missed this case.

1. Reproduce the failure on owned native Chromium pages at default HTTP/HTTPS
   ports, using controlled responses and scripted admission only.
2. Compare canonical origins on both sides. Preserve the exact canonical URL
   check and all original admission, deadline, popup, and no-replay boundaries.
3. Retain bounded diagnostic evidence for bootstrap validation failures, without
   exposing page text, exception messages, query strings, or cookies.
4. Run regressions and static checks, then retry Google Flights through the
   connected MCP with its normal standard-form admission.

An acknowledged native navigation is not task completion. Unknown outcomes
remain unknown; diagnostics never authorize retry or origin expansion. A changed
final URL, a foreign origin, a popup, an invalid destination, or failed observation
still stops bootstrap. Local controlled responses and scripted admission do not
establish live Google Flights or provider success.
