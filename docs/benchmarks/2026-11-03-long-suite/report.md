# Five multi-step browser workflows

Each agent ran the five workflows three times in a fresh browser. Terra used ordinary Playwright actions and GPT-6.1 Terra at medium reasoning. The other three libraries each ran on GPT-6 Luna through OpenAI Decisions and Jev 1.13 through OpenCode. These are 105 scored attempts across seven configurations.

| Agent | Model | Confirmed | Independently observed | Median per attempt | Model calls | Estimated cost for 15 attempts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Standalone Terra | gpt-6.1-terra | 9/15 | 9/15 | 20.66 s | 184 | $0.22552 |
| Decision Browser Core | gpt-6-luna | 14/15 | 14/15 | 22.43 s | 205 | $0.06233 |
| Decision Browser Core | jev-1.13 | 15/15 | 15/15 | 20.38 s | 210 | $0.03341 |
| jev-ultrafast | gpt-6-luna | 0/15 | 0/15 | 15.85 s | 291 | $0.05480 |
| jev-ultrafast | jev-1.13 | 1/15 | 1/15 | 12.31 s | 159 | $0.01512 |
| jev-ra | gpt-6-luna | 5/15 | 5/15 | 7.51 s | 164 | $0.04130 |
| jev-ra | jev-1.13 | 4/15 | 4/15 | 8.46 s | 147 | $0.01637 |

The median includes unsuccessful attempts, even if the agent stopped on an early page. The timer covers worker and Chromium startup, agent work, independent page checks and cleanup. The parent process checks the saved fixture records immediately after the timed worker exits. Configurations ran concurrently on the same host, so timings include shared-host and provider variability. Estimated cost uses reported tokens for scored calls. Ultrafast used its GPT-6 Luna text helper on both decision-provider routes. Its requests and estimated charges are included. A ≥ sign marks the known subtotal when requests lack complete usage records.

## Core rerun

Core ran all 30 attempts again with the same goals, fixtures, scoring and budgets. The other 75 measurements come from the original series. The two series ran at different times. Measured Core source (historical revision `f3ba534d0540bb00f3229b792636c85f8772f898`) · Previous report (historical revision `75807ce56fbc2fac571f67f7a8dd08a7ca538585`)

| Model | Previous confirmations | New confirmations | Previous median | New median | Previous cost | New cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-6-luna | 11/15 | 14/15 | 19.72 s | 22.43 s | $0.06595 | $0.06233 |
| jev-1.13 | 15/15 | 15/15 | 21.20 s | 20.38 s | $0.03398 | $0.03341 |

Excluded HTTP 503 attempts added 12 requests. Usage was available for 11 of them, worth an estimated $0.003169400. Across both Core models, known estimated spend including retries is $0.098915613. The tables use scored costs only.

Unconfirmed attempts in the final Core run:

| Model | Task | Repetition | Stop | Correct sections | Model calls | Provider diagnostic |
| --- | --- | ---: | --- | ---: | ---: | --- |
| gpt-6-luna | equipment_pickup | 2 | `no_progress` | 0/7 | 4 | — |

## Core measurement history

Each row represents a separate run of the same 15 attempts per model. The chart uses the latest Core row. [Luna regression diagnosis](luna-diagnosis.md)

| Measured source | Luna confirmed | Luna median | Luna cost | Jev confirmed | Jev median | Jev cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bf83535a (historical revision `bf83535a10b3a9fb4cf3e58f3974a7f96d391406`) | 4/15 | 19.63 s | $0.06162 | 2/15 | 13.73 s | $0.01612 |
| a5c22bb8 (historical revision `a5c22bb8d58aedcedefff29e6073b98bd346a823`) | 14/15 | 20.83 s | $0.07666 | 14/15 | 24.48 s | $0.03435 |
| 0ccf42c6 (historical revision `0ccf42c65baf609369124bf721a9311406fecc97`) | 11/15 | 19.72 s | $0.06595 | 15/15 | 21.20 s | $0.03398 |
| ff2cf081 (historical revision `ff2cf08138427fb82c990f81335612d0dc20aa01`) | 11/15 | 20.41 s | $0.06927 | 15/15 | 20.89 s | $0.03359 |
| dd7ea91c (historical revision `dd7ea91c51879a8afcb9c8d4a89c94bd18e68d69`) | 11/15 | 20.81 s | $0.06174 | 15/15 | 19.86 s | $0.03397 |
| f3ba534d (historical revision `f3ba534d0540bb00f3229b792636c85f8772f898`) | 14/15 | 22.43 s | $0.06233 | 15/15 | 20.38 s | $0.03341 |

The audit includes source identities, links to earlier publications and per-attempt records for the intermediate runs. The 75 measurements from the other agents contribute once.

## Workflows

| Task | Minimum browser actions | Sections | Goal |
| --- | ---: | ---: | --- |
| `rail_itinerary` | 10 | 5 | Save a rail itinerary from York to Durham. Pick the earliest direct train after 09:00, choose a Window seat in the Quiet coach, and use that train's listed platform. |
| `library_research` | 12 | 7 | Find the Glacier Atlas field guide in the Earth Science collection. Follow its appendix reference to the matching companion work, choose the peer-reviewed edition, and save both works in a reading list named Ice Notes tagged Fieldwork. |
| `equipment_pickup` | 14 | 7 | Prepare a local equipment pick list for the Boreal weatherproof camera, with a Telephoto lens and Protective case. Choose the depot with both items in stock, Friday Afternoon, for collector Lina Cruz, and save the list. |
| `support_handoff` | 16 | 8 | Prepare a handoff draft for ticket R-214 in the Network queue. Inspect the activity log and classify the most recent fault as High priority. Route it to Network Operations and include the On-call rotation. Add note 'Compare edge counters' tagged Diagnosis and save the draft. |
| `exhibit_planner` | 18 | 9 | Plan a Bristol museum visit in Spring. Start with Tidal Maps, turn on English captions and Audio guide, then follow its linked wing to the Fresco Study. Choose the earliest slot after noon with Step-free access. Name the route Harbour Walk, use the Two stops template, set owner Mira Lane and save the plan. |

A minimum action is a field entry, selection, checkbox change or section submission on the successful path. Initial navigation and a terminal completion signal are outside that count. The fixture records submitted choices and values on every section. Confirmation requires the agent’s native completion signal, the independent final page checks and an exact match to the saved sequence.

## Confirmations by workflow

| Agent | Model | Rail | Library | Equipment | Support | Exhibit |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Standalone Terra | gpt-6.1-terra | 0/3 | 3/3 | 3/3 | 3/3 | 0/3 |
| Decision Browser Core | gpt-6-luna | 3/3 | 3/3 | 2/3 | 3/3 | 3/3 |
| Decision Browser Core | jev-1.13 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| jev-ultrafast | gpt-6-luna | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |
| jev-ultrafast | jev-1.13 | 0/3 | 1/3 | 0/3 | 0/3 | 0/3 |
| jev-ra | gpt-6-luna | 0/3 | 3/3 | 0/3 | 2/3 | 0/3 |
| jev-ra | jev-1.13 | 0/3 | 1/3 | 0/3 | 3/3 | 0/3 |

## Time and completion

| Agent | Model | Median among confirmed attempts | Recorded stops on unconfirmed attempts |
| --- | --- | ---: | --- |
| Standalone Terra | gpt-6.1-terra | 27.50 s | `Error` × 6 |
| Decision Browser Core | gpt-6-luna | 22.52 s | `no_progress` × 1 |
| Decision Browser Core | jev-1.13 | 20.38 s | — |
| jev-ultrafast | gpt-6-luna | — | `ValueError` × 3, `agent_blocked` × 2, `call_limit` × 10 |
| jev-ultrafast | jev-1.13 | 14.77 s | `agent_blocked` × 12, `native_done_checks_failed` × 2 |
| jev-ra | gpt-6-luna | 10.68 s | `blocked` × 1, `invalid_decision` × 3, `native_done_checks_failed` × 1, `needs_value` × 1, `stuck_loop` × 4 |
| jev-ra | jev-1.13 | 11.41 s | `blocked` × 3, `needs_value` × 2, `stuck_loop` × 6 |

## Protocol and reproduction

All agents used a 1280 × 720 Chromium viewport with up to 24 native steps, 24 physical model requests, 180 seconds and 512 KiB of cumulative request data per attempt. A text-helper request counts toward the same 24-call limit. Reaching a local request limit is a scored stop. The native stop reason can reflect the agent’s browser controls, guardrails or model output. The CSV reports the length of the correct prefix of submitted sections separately from final completion.

The protocol retries HTTP 503 on the same request after resetting the fixture. This run excluded 1 such attempts from scored timing and cost.

An earlier preflight issued 75 attempts. Forms redirected away from their declared destinations, and the initial Terra adapter rejected valid Playwright references after navigation. The fixture now submits to its declared destination, and Terra accepts document-scoped references present in its current snapshot. All seven configurations ran again. The preflight recorded 498 requests and at least $0.101409323 in estimated usage. Billing for the preflight is incomplete. The full comparison excludes those trials.

The combined GIF opens with the two long-workflow provider views, then shows the historical 16-task suite. Each cohort has a separate time scale and a separate cost denominator. The same Terra measurements appear once in each provider view of their cohort.

The original fixture, common runner and benchmark workers used source revision `bf83535a10b3` (historical revision `bf83535a10b3a9fb4cf3e58f3974a7f96d391406`). The audit includes the exact worker hashes and library source identities.

[Combined GIF](../../assets/browser-four-agents.gif) · [Per-attempt CSV](runs.csv) · [Actions, page checks, saved sections and request usage](evidence.jsonl) · [Summary](summary.json) · [Sources, preflight and retries](audit.json)

[Fixture definitions](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/scripts/benchmarks/long_scenarios.py) · [Shared limits and pricing](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/scripts/benchmarks/long_protocol.py) · [Live runner](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/scripts/benchmarks/long_run.py)

The installation command below has been updated for the current bundled full Chromium
headless channel. This documentation correction does not regenerate or change the
historical measurements, source revisions or images in this report.

To exercise the five reference paths and their independent checks with local Chromium, install the locked development environment and run the fixture test. It covers the required action counts, wrong submitted values and direct attempts to open a receipt before finishing.

```sh
uv sync --frozen --group dev
. .venv/bin/activate
python -m playwright install chromium
python -m pytest -q tests/integration/test_long_benchmark_scenarios.py
```

To run one live Terra attempt, set `CHROMIUM` to the Chromium executable, `PLAYWRIGHT_BROWSERS_PATH` to its browser installation directory and `OPENAI_API_KEY` to a valid Responses credential. On macOS, `--keychain-service SERVICE` can supply the credential instead.

```sh
run_dir=$(mktemp -d)
python scripts/benchmarks/long_run.py \
  --engine terra --route responses --worker scripts/benchmarks/long_terra.py \
  --source "$PWD" --python "$VIRTUAL_ENV/bin/python" \
  --chromium "$CHROMIUM" --browsers "$PLAYWRIGHT_BROWSERS_PATH" \
  --case rail_itinerary --repetition 1 --output "$run_dir"
```

Omit `--case` and `--repetition` to run all 15 attempts in that configuration. Core uses `--engine decision-browser`, `--worker scripts/benchmarks/long_core.py` and the project checkout for `--source`. Jev-ra and Ultrafast use `--engine jev-ra` or `--engine jev-ultrafast` with `--worker scripts/benchmarks/long_external.py`. For each external engine, point `--source` to its prepared source checkout and `--python` to its environment. Both library routes accept `--route openai` or `--route opencode`. OpenCode uses its stored Console credential or `OPENCODE_API_KEY`; Ultrafast also needs the OpenAI credential for its text helper.

To rebuild the report and GIF from the published CSV, run the following command in the project environment with `ffmpeg`, `ffprobe` and `rsvg-convert` on PATH.

```sh
python scripts/benchmarks/publish_long_suite.py
```

To import this series, supply the raw Core run directories and pinned revisions.

```sh
python scripts/benchmarks/publish_long_suite.py \
  --raw-dir "$FINAL_RUN" \
  --intermediate-core-run "$INTERMEDIATE_RUN_1" \
  --intermediate-core-run "$INTERMEDIATE_RUN_2" \
  --source-revision f3ba534d0540bb00f3229b792636c85f8772f898 \
  --previous-publication 75807ce56fbc2fac571f67f7a8dd08a7ca538585
```
