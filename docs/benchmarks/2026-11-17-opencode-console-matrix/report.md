# OpenCode Console browser matrix

Both models ran on the paid OpenCode Console System One endpoint. Each of the 21 cases ran three times with the original goals and limits. Each browser context was isolated. Twenty cases use loopback fixtures and one opens the public About page on Python.org.

| Model | Confirmed | Short | Long | Median | Calls | Input tokens | Output tokens | Estimated USD from captured input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Jev 1.13 | 58/63 | 43/48 | 15/15 | 4.12 s | 343 | 1,370,208 | 110,282 | $0.0575 |
| Gemini Decision 1 | 63/63 | 48/48 | 15/15 | 5.04 s | 335 | 1,217,843 | 0 | $0.1218 |

Medians include process startup, browser actions, verification and cleanup for every attempt. The estimate uses the published rates of $0.042 and $0.10 per million input tokens. Output is listed as free for these models. Probes and any requests without captured usage are excluded.
[OpenCode rates](https://opencode.ai/docs/console/models/#pricing).

The earlier direct TypeSafe matrix used Jev 1.14.0. Console uses Jev 1.13. The source snapshots also differ, so the runs measure two configurations and cannot isolate the effect of the route.

## Cases

| Case | Jev confirmed | Jev median | Gemini confirmed | Gemini median |
| --- | ---: | ---: | ---: | ---: |
| `click` | 3/3 | 2.33 s | 3/3 | 2.72 s |
| `search` | 3/3 | 3.18 s | 3/3 | 3.80 s |
| `select` | 3/3 | 3.21 s | 3/3 | 3.77 s |
| `navigate` | 3/3 | 2.63 s | 3/3 | 2.69 s |
| `counter` | 3/3 | 3.85 s | 3/3 | 4.55 s |
| `public_about` | 3/3 | 6.35 s | 3/3 | 6.18 s |
| `hotel` | 3/3 | 5.13 s | 3/3 | 6.29 s |
| `profile_form` | 3/3 | 5.42 s | 3/3 | 6.13 s |
| `autocomplete` | 3/3 | 3.54 s | 3/3 | 3.65 s |
| `calendar` | 0/3 | 2.42 s | 3/3 | 5.71 s |
| `styled_filter` | 3/3 | 2.58 s | 3/3 | 2.98 s |
| `consent` | 3/3 | 3.87 s | 3/3 | 4.95 s |
| `delayed_menu` | 3/3 | 4.05 s | 3/3 | 4.89 s |
| `loading_overlay` | 3/3 | 4.93 s | 3/3 | 5.52 s |
| `below_fold` | 1/3 | 3.63 s | 3/3 | 3.85 s |
| `article` | 3/3 | 4.31 s | 3/3 | 4.65 s |
| `rail_itinerary` | 3/3 | 10.73 s | 3/3 | 12.97 s |
| `library_research` | 3/3 | 11.78 s | 3/3 | 14.85 s |
| `equipment_pickup` | 3/3 | 15.57 s | 3/3 | 17.62 s |
| `support_handoff` | 3/3 | 15.16 s | 3/3 | 21.98 s |
| `exhibit_planner` | 3/3 | 19.04 s | 3/3 | 19.88 s |

## Unconfirmed attempts

| Model | Case | Result | Diagnostics |
| --- | --- | --- | --- |
| Jev 1.13 | `below_fold` #2 | verification_failed / verification_failed | model:unable, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/opencode_jev/below_fold/2.json) |
| Jev 1.13 | `below_fold` #3 | verification_failed / verification_failed | model:unable, text:mismatch, route:ok, native_failure, page_check_failure, oracle_mismatch [record](runs/opencode_jev/below_fold/3.json) |
| Jev 1.13 | `calendar` #1 | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/opencode_jev/calendar/1.json) |
| Jev 1.13 | `calendar` #2 | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/opencode_jev/calendar/2.json) |
| Jev 1.13 | `calendar` #3 | failed / failed | provider:rejected_sentinel, text:mismatch, route:ok, value:mismatch, native_failure, page_check_failure, oracle_mismatch [record](runs/opencode_jev/calendar/3.json) |

The [audited CSV](audited-runs.csv) lists all attempts. [Errors](errors.json) include the per-call HTTP statuses and fixture oracle. [Full capture](results.json) and [source manifest](source-manifest.json) record the source and limits.
