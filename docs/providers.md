# Model providers

Choose a provider with `decision-browser configure` (also offered by `setup`).
Hosted presets ask for one API key through hidden input; endpoint/model settings
use their preset defaults on first setup. Reconfiguring the same provider keeps
its saved endpoint, model and choice limit while rotating the key. Local services must already be running and ask for
their endpoint/model. Configuration does not contact a provider or test a key.

On macOS/Linux, the selected profile is stored in
`~/.config/decision-browser/provider.json`, outside projects, with a `0700`
directory and `0600` file. Credentials are plaintext in that private file, not
encrypted. The profile is shared by your projects and survives project uninstall.
Reconfiguring replaces the selected profile, including its old key. To remove
the saved credential entirely, delete that profile file. Windows credential-file
storage is refused until its access controls have a validated implementation;
use the existing environment route there.

Setup checks the destination's private path, permissions and write access before
requesting a key, and checks the destination again when saving atomically. The
write probe contains no credential; it may create the private profile directory.
Configuration and Unicode failures use fixed diagnostics without echoing input.

Advanced users can set `DECISION_BROWSER_CONFIG_FILE` to an absolute profile
path. Explicit `DECISION_BROWSER_PROVIDER` **or** `DECISION_BROWSER_ENDPOINT`
bypasses the entire saved profile, including its key, even for the same provider.
Otherwise explicit environment keys take precedence: generic key first, then
the selected provider's named key on its preset origin, then its saved key. An empty explicit key
does not fall back. Other environment settings override saved values. No key is
reused across provider/endpoint overrides. A malformed or insecure profile
blocks configuration rather than silently selecting another provider.

Provider selection is operator configuration; the six MCP tools and `TaskRequest` do not accept model,
endpoint, or credential fields. No provider is inferred from another key or a
model name, and failed calls are not retried or sent to another provider.

## Decision routes

| `DECISION_BROWSER_PROVIDER` | Default endpoint | Default model | Key variable | Choices per question |
| --- | --- | --- | --- | ---: |
| `openai` (default) | `https://api.openai.com/v1/decisions` | `gpt-6-luna` | `OPENAI_API_KEY` | 255 |
| `typesafe` (`jev`) | `https://api.typesafe.ai/v1/systemone` | `jev-latest` | `TYPESAFE_API_KEY` | 255 |
| `openrouter` | `https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` | `OPENROUTER_API_KEY` | 255 |
| `opencode` | `https://opencode.ai/inference/systemone/v1/systemone` | `jev-1.13` | `OPENCODE_API_KEY` | 255 |
| `gemini` | `https://opencode.ai/inference/systemone/v1/systemone` | `gemini-decision-1` | `OPENCODE_API_KEY` | 255 |
| `local` (`nimble`) | `http://127.0.0.1:8000/v1/systemone` | `nimble-latest` | None required | 26 |

The original preset contracts were checked against the official
[OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions),
[TypeSafe API](https://docs.typesafe.ai/api/system-one),
[OpenRouter Jev guide](https://openrouter.ai/blog/insights/what-is-jev/) and
[OpenCode Console models](https://opencode.ai/docs/console/models/#jev) on
2026-11-13. This verifies documented routes, not account access or live execution.

For alternatives, `DECISION_BROWSER_ENDPOINT` and `DECISION_BROWSER_MODEL`
override the defaults. `DECISION_BROWSER_API_KEY` explicitly overrides the
selected route's key; otherwise only the key variable in that row is read.
Preset-named keys are bound to the default HTTPS origin (scheme, host and port;
an omitted HTTPS port means 443). A custom path on that origin may use the named
key. A different host or port requires `DECISION_BROWSER_API_KEY` as an explicit
credential choice for the configured route. Combining a custom host with only
a preset-named key returns `credential_route_mismatch`; that key is not sent.
This also blocks a named-key environment override of a saved custom-host
profile. Remove the unbound override or explicitly choose the intended generic
key. A saved profile's own key remains associated with its exact endpoint, and
an endpoint environment override never inherits that saved credential.
The local route never borrows a cloud key. An explicitly empty override does
not fall back to another credential. OpenAI's endpoint, model and choice limit
remain fixed; incompatible overrides are rejected.

An explicit generic key is supported for authenticated loopback services.
Reconfiguring a saved local service keeps its key only when its endpoint is
unchanged; changing the endpoint clears the old credential rather than silently
moving it. Local setup does not request a new key interactively.

Remote decision endpoints require HTTPS. The local route accepts only
`localhost` or literal loopback IP addresses, over HTTP or HTTPS. URLs with
credentials, query strings, fragments or whitespace are rejected. The server
reads only its own saved profile and explicit environment settings; it does not
read jev-ra's configuration files.

With the selected cloud key already exported, launch one route:

```sh
DECISION_BROWSER_PROVIDER=typesafe /path/to/venv/bin/python -m decision_browser.cli serve
DECISION_BROWSER_PROVIDER=openrouter /path/to/venv/bin/python -m decision_browser.cli serve
DECISION_BROWSER_PROVIDER=opencode /path/to/venv/bin/python -m decision_browser.cli serve
DECISION_BROWSER_PROVIDER=gemini /path/to/venv/bin/python -m decision_browser.cli serve
DECISION_BROWSER_PROVIDER=local /path/to/venv/bin/python -m decision_browser.cli serve
```

For Codex registration, copy and enable one block from
[codex.providers.toml](../examples/codex.providers.toml), replacing its absolute
paths. Keys are inherited by name, not stored in the example. The existing
[Keychain launcher](../src/decision_browser/keychain_entry.py) loads only the
`OPENAI_API_KEY` service and is intended for the default OpenAI route.

### OpenCode Console and Go

The `opencode` route uses the documented Console API (formerly Zen), with a
Console API key. It does not launch OpenCode or read its credential stores.
The default `jev-1.13` is a paid Console model. To select the documented
limited-time free offer explicitly, set `DECISION_BROWSER_MODEL=jev-1.13-free`.
An access or quota error never switches to the paid model or another provider.

OpenCode Go is a separate subscription. Its published model list and Go API
routes currently omit Jev/System One, so this preset does not establish Go
coverage. It sends no fabricated OpenCode client or session headers. Account
entitlement and model availability require a real request with your own key.
See [OpenCode's Jev API](https://opencode.ai/v2/docs/console/models/#jev),
[Go models and routes](https://opencode.ai/v2/docs/console/go/) and the
[official adapter](https://github.com/anomalyco/opencode/blob/67139d6827af3a337c80472c33f9cb7c6c2ce500/packages/ai/src/providers/opencode.ts).

### Gemini Decision 1 through OpenCode

Run `decision-browser configure gemini` and enter an OpenCode Console API key,
or export `DECISION_BROWSER_PROVIDER=gemini` and `OPENCODE_API_KEY`. The preset
selects `gemini-decision-1` and uses the same System One endpoint and bounded
adapter as Jev. Google API keys are not used by this route. Existing OpenCode
configurations can instead keep `DECISION_BROWSER_PROVIDER=opencode` and set
`DECISION_BROWSER_MODEL=gemini-decision-1` explicitly.

The [official OpenCode model guide](https://opencode.ai/docs/console/models/#gpt-6-luna-decision-and-gemini-decision-1)
documents the shared `model`, `state`, `questions` request contract (checked on
2026-11-14). This adapter sends text state and Choice questions; Gemini's other
multimodal inputs are not exposed here. Visual diagnostics remain unsupported,
and unbound field text still needs the optional text helper. Access and quota
errors stop the request without switching models or providers. Documentation
and mock-transport tests do not establish live account access.

## Local development and choice limits

Run a System One-compatible service separately, then select `local`. Its request
contract is `model`, `state` and a named `questions` map with Choice `criteria`;
responses contain named `answers`, `model` and `usage`. An ordinary chat endpoint
such as Ollama's `/v1/chat/completions` is not a decision endpoint.

The local default follows Nimble's current System One server, which advertises
26 candidates and 64 questions, even though the latest model README describes
255-choice support. Set `DECISION_BROWSER_MAX_CHOICES` to an integer from 2 to
255 only when the selected server supports that limit.

If a question exceeds that limit, the adapter chooses a group and narrows it in
additional calls. All original choices remain reachable. Up to 64 questions are
asked together; larger bundles ask the operation first and then its selected
target. Each physical request consumes the same task call, byte and time budget.
A smaller local limit can therefore require a larger caller-supplied call budget.
No browser action is dispatched until the final original choice is validated.

### Ollama

Ollama 0.35.0 and later provide the System One endpoint natively. Start Ollama,
download a local decision model with `ollama pull nimble:latest`, and configure:

```sh
export DECISION_BROWSER_PROVIDER=local
export DECISION_BROWSER_ENDPOINT=http://127.0.0.1:11434/v1/systemone
export DECISION_BROWSER_MODEL=nimble:latest
export DECISION_BROWSER_MAX_CHOICES=26
```

This uses the existing System One adapter without an API key or chat conversion.
The model must be installed locally; a cloud model tag is a different service.
The model's advertised context limit is not necessarily its loaded context.
Check `ollama ps` and configure an adequate context in Ollama if needed. Local
System One requests have a 64 KiB limit for text and schema; inputs are not
silently truncated. See [Ollama decisions](https://docs.ollama.com/capabilities/decision)
and the [System One API](https://docs.ollama.com/api/systemone).

Local comparison on 2026-11-02: Ollama 0.40.13 selected `nimble:latest` as a 9B
MXFP8/MLX model (registered digest `13ae0a506102...`) on an M4 Pro with 24 GiB
memory and an 8192-token loaded context. It verified **12/18** runs of the six
browser cases repeated three times. Search and sorting failed in all repeats;
click, counter and both navigation cases passed. The earlier OpenAI reference
verified 18/18. This local model is functional, but has lower observed accuracy
on that comparison. No cloud inference was used for the local series.

## Optional text helper

System One models choose supplied options; they do not write arbitrary field
text. `TaskRequest.values` still supplies bound field values without a helper.
For unbound text generation, configure an independent OpenAI-compatible chat
service using both variables below:

```sh
export DECISION_BROWSER_TEXT_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
export DECISION_BROWSER_TEXT_MODEL=your-installed-text-model
```

This is a protocol example; it does not install a model or launch a server.
For a remote HTTPS helper, also provide `DECISION_BROWSER_TEXT_API_KEY`.
Loopback helpers may omit it. The helper never inherits the decision provider's
key or `OPENAI_API_KEY`; its full endpoint and model must be explicit. Partial
helper configuration is rejected. OpenAI uses its existing native Responses
helper and rejects these alternative-helper settings.

Without a configured helper, unbound generation raises
`text_provider_not_configured` before a helper request. Helper calls share the
decision calls' original privacy gate, deadline and byte/call budgets. Only one
complete assistant text is accepted; refused, truncated or tool-call responses
are not used as field values.

## Readiness and verification boundary

`doctor` and `capabilities` report the canonical `provider`, selected-key
`api_key_present` boolean, `provider_ready`, a safe `provider_error`,
`text_generation` and `visual`. These checks open no sockets and print no key,
endpoint or model values. Keyless local configuration can be ready without a
running server; readiness does not prove entitlement or a successful request.

TypeSafe, OpenRouter, OpenCode, Gemini and local decisions currently return
`visual_provider_not_supported` as an incomplete visual diagnostic and transmit
no image to another service. Functional verification remains separate. Mock and
owned-loopback tests validate the adapters, not live Jev/OpenRouter/OpenCode acceptance
or an installed Nimble model. General-site and native-human claims remain
outside that evidence.

Protocol references: [TypeSafe System One](https://docs.typesafe.ai/api/system-one),
[Choice](https://docs.typesafe.ai/primitives/choice),
[OpenRouter Jev](https://openrouter.ai/blog/tutorials/how-to-use-jev/), and
[Nimble serving limits](https://github.com/bespokelabsai/nimble/blob/4edb0703a2e75d0170c3a45645a1961f4d1a2867/nimble/serving/server.py).


## Page evidence and URL privacy

Observed page text and URL fields are provider input, not trusted instructions or
admission grants. The exact-literal privacy gate, artifact URL scrubbing and
consent presentation are different boundaries. See [model evidence and URL
presentation policy](context-evidence-privacy.md) for the bounded contracts,
percent-encoded representations and singleton preview metadata.
