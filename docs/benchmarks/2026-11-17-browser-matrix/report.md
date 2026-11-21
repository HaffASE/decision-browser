# Browser matrix: Jev, Decision API and Terra 6.1 medium

Fresh completed capture: 21 cases × 3 repetitions × 3 configurations = 189 attempts. Every failure and early stop remains in the denominator.

## Configuration and totals

| Configuration | Confirmed | Page + oracle | Calls | Input tokens | Output tokens | Cached | Cache write | Reasoning | Median process seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| jev | 13/63 | 13/63 | 530 | 1810309 | 161088 | unknown (reported 0; 530 calls missing; 0 runs unknown) | unknown (reported 0; 530 calls missing; 0 runs unknown) | unknown (reported 0; 530 calls missing; 0 runs unknown) | 7.064 |
| decision_api | 52/63 | 53/63 | 293 | unknown (reported 738574; 1 calls missing; 0 runs unknown) | unknown (reported 0; 1 calls missing; 0 runs unknown) | unknown (reported 4060; 1 calls missing; 0 runs unknown) | unknown (reported 184441; 1 calls missing; 0 runs unknown) | unknown (reported 0; 1 calls missing; 0 runs unknown) | 4.766 |
| terra | 57/63 | 57/63 | 384 | 390682 | 9744 | 7828 | 127220 | 1970 | 10.781 |

Jev uses direct TypeSafe System One (`jev-latest`); it is a different route and model series from the archived OpenCode Jev 1.13 results. Decision Browser uses its frozen current Core providers and runner. Standalone Terra uses `gpt-6.1-terra`, `reasoning.effort=medium`, and the Responses API.

Requested and observed model identities:

- **jev** requested `jev-latest`; observed `{"jev-1.14.0": 530}`.
- **decision_api** requested `gpt-6-luna`; observed `{"gpt-6-luna": 292}`.
- **terra** requested `gpt-6.1-terra`; observed `{"gpt-6.1-terra": 384}`.

## Confirmed repetitions by scenario

| Scenario | Jev | Decision API | Terra medium |
| --- | ---: | ---: | ---: |
| `click` | 3/3 | 3/3 | 3/3 |
| `search` | 2/3 | 3/3 | 3/3 |
| `select` | 1/3 | 3/3 | 3/3 |
| `navigate` | 1/3 | 3/3 | 3/3 |
| `counter` | 0/3 | 3/3 | 3/3 |
| `public_about` | 1/3 | 2/3 | 3/3 |
| `hotel` | 0/3 | 2/3 | 3/3 |
| `profile_form` | 1/3 | 2/3 | 3/3 |
| `autocomplete` | 0/3 | 3/3 | 3/3 |
| `calendar` | 0/3 | 3/3 | 3/3 |
| `styled_filter` | 1/3 | 3/3 | 3/3 |
| `consent` | 1/3 | 3/3 | 3/3 |
| `delayed_menu` | 0/3 | 2/3 | 3/3 |
| `loading_overlay` | 2/3 | 3/3 | 3/3 |
| `below_fold` | 0/3 | 3/3 | 3/3 |
| `article` | 0/3 | 3/3 | 3/3 |
| `rail_itinerary` | 0/3 | 3/3 | 0/3 |
| `library_research` | 0/3 | 1/3 | 3/3 |
| `equipment_pickup` | 0/3 | 2/3 | 3/3 |
| `support_handoff` | 0/3 | 2/3 | 3/3 |
| `exhibit_planner` | 0/3 | 0/3 | 0/3 |

## Scenario token usage

Each cell is input / output tokens across all three attempts, including failed attempts.

| Scenario | Jev input / output | Decision API input / output | Terra input / output |
| --- | ---: | ---: | ---: |
| `click` | 21898 / 2043 | 4578 / 0 | 3699 / 139 |
| `search` | 32476 / 3092 | 14499 / 0 | 6231 / 203 |
| `select` | 24635 / 2398 | 11853 / 0 | 6138 / 222 |
| `navigate` | 13145 / 1132 | 5356 / 0 | 3783 / 135 |
| `counter` | 7009 / 675 | 14496 / 0 | 7717 / 320 |
| `public_about` | 17451 / 1872 | 12388 / 0 | 47175 / 127 |
| `hotel` | 92894 / 9721 | 35218 / 0 | 14133 / 409 |
| `profile_form` | 78025 / 8044 | 32065 / 0 | 12012 / 348 |
| `autocomplete` | 104353 / 10042 | 11277 / 0 | 8091 / 281 |
| `calendar` | 20401 / 2034 | 15262 / 0 | 8229 / 268 |
| `styled_filter` | 15674 / 1586 | 4662 / 0 | 3972 / 132 |
| `consent` | 88211 / 8588 | 16017 / 0 | 8055 / 378 |
| `delayed_menu` | 98006 / 8386 | unknown (reported 11073; 1 calls missing; 0 runs unknown) / unknown (reported 0; 1 calls missing; 0 runs unknown) | 7380 / 317 |
| `loading_overlay` | 94275 / 8827 | 22336 / 0 | 9957 / 396 |
| `below_fold` | 83990 / 6764 | 12673 / 0 | 6156 / 220 |
| `article` | 80381 / 7915 | 18848 / 0 | 8451 / 266 |
| `rail_itinerary` | 43886 / 4396 | 90494 / 0 | 21891 / 658 |
| `library_research` | 216682 / 17725 | 78036 / 0 | 39663 / 870 |
| `equipment_pickup` | 273298 / 22868 | 97941 / 0 | 47346 / 1042 |
| `support_handoff` | 286310 / 22780 | 148437 / 0 | 56358 / 1147 |
| `exhibit_planner` | 117309 / 10200 | 81065 / 0 | 64245 / 1866 |

## Unconfirmed attempts

Generic native reasons and exception classes are retained as observed; they do not establish a root cause. A native completion with an unsuccessful oracle is an oracle mismatch, not a confirmed task.

| Arm | Scenario · repetition | Native status / reason | Diagnostics / failed dimensions | Attempt |
| --- | --- | --- | --- | --- |
| jev | equipment_pickup · 1 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/equipment_pickup/1.json) |
| jev | autocomplete · 1 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/autocomplete/1.json) |
| jev | delayed_menu · 1 | limit_exceeded / budget_limit | text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/delayed_menu/1.json) |
| jev | calendar · 1 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/calendar/1.json) |
| jev | counter · 1 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/counter/1.json) |
| jev | rail_itinerary · 1 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/rail_itinerary/1.json) |
| terra | rail_itinerary · 1 | error / Error | native_failure; page_check_failure; oracle_mismatch | [JSON](runs/terra/rail_itinerary/1.json) |
| jev | article · 1 | failed / failed | provider:rejected_sentinel; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/article/1.json) |
| jev | below_fold · 1 | limit_exceeded / budget_limit | text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/below_fold/1.json) |
| jev | library_research · 1 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/library_research/1.json) |
| decision_api | profile_form · 1 | blocked / no_progress | text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/profile_form/1.json) |
| jev | support_handoff · 1 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/support_handoff/1.json) |
| jev | styled_filter · 1 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/styled_filter/1.json) |
| jev | hotel · 1 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/hotel/1.json) |
| jev | exhibit_planner · 1 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/exhibit_planner/1.json) |
| decision_api | exhibit_planner · 1 | verification_failed / verification_failed | model:unable; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/exhibit_planner/1.json) |
| terra | exhibit_planner · 2 | error / Error | native_failure; page_check_failure; oracle_mismatch | [JSON](runs/terra/exhibit_planner/2.json) |
| jev | exhibit_planner · 2 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/exhibit_planner/2.json) |
| decision_api | exhibit_planner · 2 | verification_failed / verification_failed | model:unable; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/exhibit_planner/2.json) |
| terra | exhibit_planner · 1 | done / goal_achieved | oracle_mismatch | [JSON](runs/terra/exhibit_planner/1.json) |
| jev | equipment_pickup · 2 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/equipment_pickup/2.json) |
| jev | search · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/search/2.json) |
| jev | support_handoff · 2 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/support_handoff/2.json) |
| jev | navigate · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/navigate/2.json) |
| jev | profile_form · 2 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/profile_form/2.json) |
| jev | styled_filter · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/styled_filter/2.json) |
| jev | consent · 2 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/consent/2.json) |
| jev | select · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/select/2.json) |
| jev | delayed_menu · 2 | limit_exceeded / budget_limit | text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/delayed_menu/2.json) |
| jev | calendar · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/calendar/2.json) |
| jev | counter · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/counter/2.json) |
| decision_api | library_research · 2 | verification_failed / verification_failed | model:unable; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/library_research/2.json) |
| jev | loading_overlay · 2 | limit_exceeded / budget_limit | text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/loading_overlay/2.json) |
| jev | library_research · 2 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/library_research/2.json) |
| jev | public_about · 2 | failed / failed | provider:rejected_sentinel; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/public_about/2.json) |
| jev | below_fold · 2 | limit_exceeded / budget_limit | text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/below_fold/2.json) |
| jev | autocomplete · 2 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/autocomplete/2.json) |
| jev | hotel · 2 | failed / failed | provider:rejected_sentinel; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/hotel/2.json) |
| decision_api | hotel · 2 | verification_failed / verification_failed | model:unable; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/hotel/2.json) |
| jev | rail_itinerary · 2 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/rail_itinerary/2.json) |
| terra | rail_itinerary · 2 | error / Error | native_failure; page_check_failure; oracle_mismatch | [JSON](runs/terra/rail_itinerary/2.json) |
| jev | article · 2 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/article/2.json) |
| jev | public_about · 3 | failed / failed | provider:rejected_sentinel; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/public_about/3.json) |
| decision_api | public_about · 3 | outcome_unknown / outcome_unknown | route:mismatch; native_failure | [JSON](runs/decision_api/public_about/3.json) |
| jev | profile_form · 3 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/profile_form/3.json) |
| jev | autocomplete · 3 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/autocomplete/3.json) |
| jev | navigate · 3 | failed / failed | provider:rejected_sentinel; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/navigate/3.json) |
| jev | exhibit_planner · 3 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/exhibit_planner/3.json) |
| decision_api | exhibit_planner · 3 | verification_failed / verification_failed | model:unable; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/exhibit_planner/3.json) |
| jev | counter · 3 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/counter/3.json) |
| terra | exhibit_planner · 3 | done / goal_achieved | oracle_mismatch | [JSON](runs/terra/exhibit_planner/3.json) |
| jev | support_handoff · 3 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/support_handoff/3.json) |
| decision_api | support_handoff · 3 | verified / verified | route:ok; text:ok; oracle_mismatch | [JSON](runs/decision_api/support_handoff/3.json) |
| decision_api | library_research · 3 | verification_failed / verification_failed | model:unable; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/library_research/3.json) |
| jev | library_research · 3 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/library_research/3.json) |
| jev | article · 3 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/article/3.json) |
| jev | select · 3 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/select/3.json) |
| jev | consent · 3 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/consent/3.json) |
| jev | hotel · 3 | limit_exceeded / limit_exceeded | budget:input_bytes; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/hotel/3.json) |
| jev | calendar · 3 | failed / failed | provider:rejected_sentinel; text:mismatch; route:ok; value:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/calendar/3.json) |
| decision_api | delayed_menu · 3 | failed / failed | provider:http_error; http_status:server_error; text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch; lifecycle_failure | [JSON](runs/decision_api/delayed_menu/3.json) |
| jev | delayed_menu · 3 | limit_exceeded / budget_limit | text:mismatch; route:mismatch; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/delayed_menu/3.json) |
| jev | rail_itinerary · 3 | failed / failed | provider:rejected_sentinel; route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/rail_itinerary/3.json) |
| decision_api | equipment_pickup · 3 | blocked / no_progress | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/decision_api/equipment_pickup/3.json) |
| terra | rail_itinerary · 3 | error / Error | native_failure; page_check_failure; oracle_mismatch | [JSON](runs/terra/rail_itinerary/3.json) |
| jev | below_fold · 3 | limit_exceeded / budget_limit | text:mismatch; route:ok; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/below_fold/3.json) |
| jev | equipment_pickup · 3 | limit_exceeded / budget_limit | route:mismatch; text:playwright_error; native_failure; page_check_failure; oracle_mismatch | [JSON](runs/jev/equipment_pickup/3.json) |

## Method and evidence

All configurations receive the same case goals, supplied values, independent page checks, fixture oracles and case budgets. There are three repetitions, no retries and no fallback routes. Sixteen short cases use 8 baseline / 24 expanded steps, 12 API calls, 75 seconds and 128 KiB. Five long cases use 24 steps, 24 calls, 180 seconds and 512 KiB. The standalone Terra agent retains its historical estimated spend guard of $0.25 per attempt; this is not an invoice or a new cost estimate.

Twenty scenarios use synthetic owned fixtures; `public_about` is a read-only visit to Python.org. Browsers use fresh isolated Chromium contexts. Core uses task_scope development admission; these results do not prove native human admission or general-site acceptance. Confirmation requires native completion, independent page and oracle success, complete reported input/output usage and successful cleanup. Medians include unsuccessful attempts and startup/cleanup, with missing timing explicitly retained in JSON.

Tokens come from API usage for each physical request. Missing values remain unknown; partial reported sums and missing counts remain in JSON/CSV. Cache and reasoning are token subsets and must not be added to input/output totals. These are API-reported tokens, not a USD invoice.

Capture source: `98a0103c1ba9c81c415eaad38f4a9379e8509cb56adcbe70d4bac97f4e6a38cb` (67 verified files). Browser/runtime: `{"chromium": "Google Chrome for Testing 156.0.8078.4", "chromium_sha256": "c8509aee32abd6977968e9999769980b7a420dea05402d90042d9e5b41db15ef", "parent_python": "3.13.5 (main, Jun 12 2025, 12:22:43) [Clang 20.1.4 ]", "playwright": "1.64.1", "worker_python_executable": "/private/tmp/decision-browser-wave6-20261117/venv/bin/python"}`.

### Reporting correction

The capture launcher aliased remaining to its plan. Removing completed cells therefore shrank progress denominators and summary domains. All futures had already been queued; immutable reconstruction and the per-attempt records restore reporting without changing outcomes or making paid reruns.

Raw [results](results.json), [summary](summary.json) and [CSV](runs.csv) remain byte-for-byte unchanged. Use the [audited results](audited-results.json), [audited summary](audited-summary.json), [all-attempt CSV](audited-runs.csv), [full error evidence](errors.json), and [audit manifest](audit.json).
