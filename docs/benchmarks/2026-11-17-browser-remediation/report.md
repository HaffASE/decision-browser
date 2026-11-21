# Browser matrix follow-up

The rerun replays the 67 originally unconfirmed arm, case and repetition combinations. Each cell ran once with the same goal and limits. The baseline also contains 122 confirmed cells.

| Configuration | Baseline confirmed | Recovered in rerun | Retained + recovered | Matched median, before → after (s) | Rerun API calls | Rerun input tokens | Rerun output tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TypeSafe Jev | 13/63 | 9/50 | 22/63 | 7.32 → 7.27 | 527 | 1,971,516 | 154,677 |
| Decision API | 52/63 | 7/11 | 59/63 | 13.18 → 14.26 | 107 | 318,066 | 0 |
| Terra 6.1 medium | 57/63 | 6/6 | 63/63 | 20.60 → 38.12 | 90 | 141,978 | 1,890 |

The retained + recovered column combines the baseline successes and the recoveries. Both medians use the same originally unconfirmed cells. The baseline Terra runs stopped early, and all six reruns finished the workflow. The token totals cover every rerun, including failures.

## Results by case

| Case | Jev baseline + recovered / rerun | Decision API baseline + recovered / rerun | Terra baseline + recovered / rerun |
| --- | ---: | ---: | ---: |
| `click` | 3/3 + — | 3/3 + — | 3/3 + — |
| `search` | 2/3 + 0/1 | 3/3 + — | 3/3 + — |
| `select` | 1/3 + 1/2 | 3/3 + — | 3/3 + — |
| `navigate` | 1/3 + 0/2 | 3/3 + — | 3/3 + — |
| `counter` | 0/3 + 3/3 | 3/3 + — | 3/3 + — |
| `public_about` | 1/3 + 2/2 | 2/3 + 1/1 | 3/3 + — |
| `hotel` | 0/3 + 0/3 | 2/3 + 0/1 | 3/3 + — |
| `profile_form` | 1/3 + 0/2 | 2/3 + 1/1 | 3/3 + — |
| `autocomplete` | 0/3 + 0/3 | 3/3 + — | 3/3 + — |
| `calendar` | 0/3 + 0/3 | 3/3 + — | 3/3 + — |
| `styled_filter` | 1/3 + 2/2 | 3/3 + — | 3/3 + — |
| `consent` | 1/3 + 0/2 | 3/3 + — | 3/3 + — |
| `delayed_menu` | 0/3 + 0/3 | 2/3 + 0/1 | 3/3 + — |
| `loading_overlay` | 2/3 + 1/1 | 3/3 + — | 3/3 + — |
| `below_fold` | 0/3 + 0/3 | 3/3 + — | 3/3 + — |
| `article` | 0/3 + 0/3 | 3/3 + — | 3/3 + — |
| `rail_itinerary` | 0/3 + 0/3 | 3/3 + — | 0/3 + 3/3 |
| `library_research` | 0/3 + 0/3 | 1/3 + 2/2 | 3/3 + — |
| `equipment_pickup` | 0/3 + 0/3 | 2/3 + 1/1 | 3/3 + — |
| `support_handoff` | 0/3 + 0/3 | 2/3 + 1/1 | 3/3 + — |
| `exhibit_planner` | 0/3 + 0/3 | 0/3 + 1/3 | 0/3 + 3/3 |

## Reproduced issues and changes

- Terra sent `Alt+Left` on stage 3 of `rail_itinerary`. The live diagnostic captured `Locator.press: Unknown key: "Left"`. The agent validates supported element keys before dispatch, returns a fixed diagnostic to the next turn, and keeps a bounded record of the last three page snapshots with accessibility references stripped.
- Jev chose the padded reject option in 26 baseline runs and often used up its budget on waits. The updated request describes what the singleton option does, explains when to reject it, and gives explicit progress guidance for waiting and scrolling. A reject still stops safely.
- The baseline `support_handoff` page check accepted the generic receipt title with the wrong toggle state. Long workflows now check the complete visible receipt as well as the URL. The independent event oracle is unchanged. Scripted Chromium tests also checked the available controls and prior-page clues in the long Decision API workflows. The API 503, native unconfirmed navigation and model stops remain distinct outcomes.

## Still unconfirmed

Jev still hit 30 limits and selected the reject option 11 times. It requested
398 waits on these cells, up from 343 in their baseline attempts. Decision API
returned `UNABLE` twice on `exhibit_planner` and once on `delayed_menu`.
It also stopped without progress once on `hotel`. The rerun had no HTTP errors.

| Route | Case | Previous result | Rerun result | Diagnostics |
| --- | --- | --- | --- | --- |
| Decision API | `delayed_menu` #3 | failed / failed | verification_failed / verification_failed | model:unable, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/decision_api/delayed_menu/3.json) |
| Decision API | `exhibit_planner` #1 | verification_failed / verification_failed | verification_failed / verification_failed | model:unable, route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/decision_api/exhibit_planner/1.json) |
| Decision API | `exhibit_planner` #2 | verification_failed / verification_failed | verification_failed / verification_failed | model:unable, route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/decision_api/exhibit_planner/2.json) |
| Decision API | `hotel` #2 | verification_failed / verification_failed | blocked / no_progress | text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/decision_api/hotel/2.json) |
| TypeSafe Jev | `article` #1 | failed / failed | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/article/1.json) |
| TypeSafe Jev | `article` #2 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/article/2.json) |
| TypeSafe Jev | `article` #3 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/article/3.json) |
| TypeSafe Jev | `autocomplete` #1 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/autocomplete/1.json) |
| TypeSafe Jev | `autocomplete` #2 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/autocomplete/2.json) |
| TypeSafe Jev | `autocomplete` #3 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/autocomplete/3.json) |
| TypeSafe Jev | `below_fold` #1 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/below_fold/1.json) |
| TypeSafe Jev | `below_fold` #2 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/below_fold/2.json) |
| TypeSafe Jev | `below_fold` #3 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/below_fold/3.json) |
| TypeSafe Jev | `calendar` #1 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/calendar/1.json) |
| TypeSafe Jev | `calendar` #2 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/calendar/2.json) |
| TypeSafe Jev | `calendar` #3 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/calendar/3.json) |
| TypeSafe Jev | `consent` #2 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/consent/2.json) |
| TypeSafe Jev | `consent` #3 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/consent/3.json) |
| TypeSafe Jev | `delayed_menu` #1 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/delayed_menu/1.json) |
| TypeSafe Jev | `delayed_menu` #2 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/delayed_menu/2.json) |
| TypeSafe Jev | `delayed_menu` #3 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/delayed_menu/3.json) |
| TypeSafe Jev | `equipment_pickup` #1 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/equipment_pickup/1.json) |
| TypeSafe Jev | `equipment_pickup` #2 | failed / failed | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/equipment_pickup/2.json) |
| TypeSafe Jev | `equipment_pickup` #3 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/equipment_pickup/3.json) |
| TypeSafe Jev | `exhibit_planner` #1 | failed / failed | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/exhibit_planner/1.json) |
| TypeSafe Jev | `exhibit_planner` #2 | failed / failed | failed / failed | provider:rejected_sentinel, route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/exhibit_planner/2.json) |
| TypeSafe Jev | `exhibit_planner` #3 | failed / failed | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/exhibit_planner/3.json) |
| TypeSafe Jev | `hotel` #1 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/hotel/1.json) |
| TypeSafe Jev | `hotel` #2 | failed / failed | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/hotel/2.json) |
| TypeSafe Jev | `hotel` #3 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/hotel/3.json) |
| TypeSafe Jev | `library_research` #1 | failed / failed | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/library_research/1.json) |
| TypeSafe Jev | `library_research` #2 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/library_research/2.json) |
| TypeSafe Jev | `library_research` #3 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/library_research/3.json) |
| TypeSafe Jev | `navigate` #2 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/navigate/2.json) |
| TypeSafe Jev | `navigate` #3 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/navigate/3.json) |
| TypeSafe Jev | `profile_form` #2 | limit_exceeded / limit_exceeded | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/profile_form/2.json) |
| TypeSafe Jev | `profile_form` #3 | failed / failed | limit_exceeded / limit_exceeded | budget:input_bytes, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/profile_form/3.json) |
| TypeSafe Jev | `rail_itinerary` #1 | failed / failed | failed / failed | provider:rejected_sentinel, route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/rail_itinerary/1.json) |
| TypeSafe Jev | `rail_itinerary` #2 | failed / failed | failed / failed | provider:rejected_sentinel, route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/rail_itinerary/2.json) |
| TypeSafe Jev | `rail_itinerary` #3 | failed / failed | failed / failed | provider:rejected_sentinel, route:mismatch, text:playwright_error, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/rail_itinerary/3.json) |
| TypeSafe Jev | `search` #2 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/search/2.json) |
| TypeSafe Jev | `select` #2 | failed / failed | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/select/2.json) |
| TypeSafe Jev | `support_handoff` #1 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/support_handoff/1.json) |
| TypeSafe Jev | `support_handoff` #2 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/support_handoff/2.json) |
| TypeSafe Jev | `support_handoff` #3 | limit_exceeded / budget_limit | limit_exceeded / budget_limit | route:mismatch, text:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/jev/support_handoff/3.json) |

The [audited CSV](audited-runs.csv) records each attempt, including time and API usage. The [raw results](results.json) include all physical calls. The [error records](errors.json) include native diagnostics and the independent oracle.

Capture source SHA-256 `62f8c2bee0c63516f58d32a035560f1719f959be200374793c6e938031656777`. The [manifest](source-manifest.json) covers the changed sources in `capture-delta/` and the identical files in the [baseline capture](../2026-11-17-browser-matrix/report.md).
