# Providers

Choose the route with `decision-browser configure`, or set its explicit
environment configuration in the MCP registration. Provider settings are
operator configuration. The six MCP tools accept browser task arguments.

| Provider | Preset endpoint | Preset model | Key variable |
| --- | --- | --- | --- |
| `openai` / `luna` | `https://api.openai.com/v1/decisions` | `gpt-6-luna` | `OPENAI_API_KEY` |
| `typesafe` / `jev` | `https://api.typesafe.ai/v1/systemone` | `jev-latest` | `TYPESAFE_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` | `OPENROUTER_API_KEY` |
| `opencode` | `https://opencode.ai/inference/systemone/v1/systemone` | `jev-1.13` | `OPENCODE_API_KEY` |
| `gemini` | `https://opencode.ai/inference/systemone/v1/systemone` | `gemini-decision-1` | `OPENCODE_API_KEY` |
| `local` / `nimble` | `http://127.0.0.1:8000/v1/systemone` | `nimble-latest` | Optional |

A preset identifies the configured route and request contract. Model access,
quota and billing depend on the provider account. OpenCode Console and an
OpenCode Go subscription have separate entitlements. Gemini Decision uses the
configured OpenCode Console credential.

## Credentials

On supported Unix hosts, `configure` stores a private profile outside projects
at `~/.config/decision-browser/provider.json`. The directory is private and the
file contains the selected credential. Project uninstall preserves this profile.
Use an absolute `DECISION_BROWSER_CONFIG_FILE` to select another profile.

Explicit provider or endpoint selection prevents credentials from an unrelated
saved route from being reused. An explicit environment key takes precedence
according to its selected route. A malformed profile stops configuration with a
fixed diagnostic. Inspect `capabilities` for provider prerequisites and avoid
printing credentials.

The macOS native route can read a matching named Keychain item. The supported
services are `TYPESAFE_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and
`codex-opencode-console`. `capabilities` checks matching item metadata, and a
live request reads the selected credential when needed.

## Direct stdio launch

Export the chosen key before starting the server.

```sh
DECISION_BROWSER_PROVIDER=typesafe /absolute/path/decision-browser serve
DECISION_BROWSER_PROVIDER=gemini /absolute/path/decision-browser serve
DECISION_BROWSER_PROVIDER=local /absolute/path/decision-browser serve
```

[Codex examples](../examples/codex.providers.toml) contain disabled registration
blocks with credential variable names and absolute executable placeholders.
Set the selected block's path and enable that block in the project configuration.

## Endpoint and model configuration

Advanced environment settings include `DECISION_BROWSER_ENDPOINT`,
`DECISION_BROWSER_MODEL` and `DECISION_BROWSER_MAX_CHOICES`. Use the selected
provider's actual supported endpoint and model. Remote endpoints require HTTPS,
and local System One endpoints are restricted to loopback. Credentials remain
bound to the selected configuration. Unsupported or invalid settings produce a
configuration error before a browser task can use them.

System One services implement finite choice questions. A chat-completions
endpoint alone does not supply that contract. For a local service, start the
server separately and ensure its endpoint and choice limits match the model.

```sh
export DECISION_BROWSER_PROVIDER=local
export DECISION_BROWSER_ENDPOINT=http://127.0.0.1:11434/v1/systemone
export DECISION_BROWSER_MODEL=nimble:latest
export DECISION_BROWSER_MAX_CHOICES=26
```

## Field text and visual results

Provide known field values in `TaskRequest.values`. Text generation uses a
separately configured helper when advertised by `capabilities.text_generation`.
Inspect helper configuration and budget use before an unbound-field task.

`visual` and `visual_diagnostic` describe their own capability and result.
Geometry can be retained locally while pixel capture is unsafe. An incomplete
visual result provides insufficient evidence of visual correctness.

## Validation

`doctor` and `capabilities` report prerequisites without a paid request. Test
provider access with the bounded native counter acceptance, then the full
[verification matrix](verification.md). Preserve the response model, usage,
elapsed time and all failed attempts when reporting results.
