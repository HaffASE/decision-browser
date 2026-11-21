# Browser tasks on OpenAI Decisions and OpenCode Jev

On 3 November 2026, three browser libraries each ran the same 16 tasks three times with each decision provider. The comparison scored 288 attempts, 48 for each library/provider combination.

One additional browser attempt received HTTP 503 on its first OpenAI Decisions request. We reran that same Core profile-form task with the original goal, limits and frozen worker. The retry passed the page and event checks after four HTTP 200 responses. The original attempt is excluded from all scores, timings and cost estimates. All 1,069 model requests in the scored attempts returned HTTP 200. [Original and retry records](retry-audit.json)

## Completion and speed

| Library | Decision provider | Confirmed | Page and event checks passed | Median process time | Decision requests | Text requests |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Decision Browser Core | OpenAI Decisions | 42/48 | 42/48 | 3.232 s | 159 | 0 |
| Decision Browser Core | OpenCode Jev | 38/48 | 38/48 | 3.418 s | 138 | 0 |
| jev-ultrafast | OpenAI Decisions | 34/48 | 34/48 | 3.800 s | 193 | 24 |
| jev-ultrafast | OpenCode Jev | 38/48 | 38/48 | 3.669 s | 166 | 21 |
| jev-ra | OpenAI Decisions | 34/48 | 41/48 | 3.263 s | 192 | 0 |
| jev-ra | OpenCode Jev | 32/48 | 35/48 | 3.614 s | 176 | 0 |

Times include Python and browser startup, model calls, browser checks and cleanup, ending when the worker exits. For each row the median is the midpoint of the 24th and 25th durations after sorting all 48 attempts. A fast failure contributes its actual duration.

## Within-library comparison

Each pair is one task and repetition tried on both providers. A confirmed result requires both native library success and the independently verified outcome.

An unconfirmed result describes the browser task as a whole. For example, two jev-ultrafast sorting attempts on OpenAI ended in a library `ValueError`. The available trace does not establish whether the model or the adapter caused it. These are recorded as unconfirmed library runs, with no model-error attribution.

| Library | Both confirmed | OpenAI only | Jev only | Neither |
| --- | ---: | ---: | ---: | ---: |
| Decision Browser Core | 33 | 9 | 5 | 1 |
| jev-ultrafast | 26 | 8 | 12 | 2 |
| jev-ra | 25 | 9 | 7 | 7 |

## Confirmed attempts by task

Each cell is the number of confirmed attempts out of three.

| Task | Core · OpenAI | Core · Jev | Ultrafast · OpenAI | Ultrafast · Jev | jev-ra · OpenAI | jev-ra · Jev |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `click` | 3 | 3 | 3 | 3 | 3 | 3 |
| `search` | 3 | 2 | 3 | 3 | 3 | 0 |
| `select` | 3 | 3 | 1 | 3 | 3 | 3 |
| `navigate` | 3 | 3 | 3 | 3 | 3 | 3 |
| `counter` | 3 | 3 | 3 | 3 | 0 | 0 |
| `public_about` | 3 | 3 | 3 | 3 | 0 | 3 |
| `hotel` | 0 | 2 | 0 | 3 | 1 | 2 |
| `profile_form` | 3 | 3 | 2 | 3 | 0 | 3 |
| `autocomplete` | 3 | 2 | 3 | 0 | 3 | 3 |
| `calendar` | 3 | 0 | 0 | 2 | 3 | 3 |
| `styled_filter` | 3 | 3 | 3 | 3 | 3 | 3 |
| `consent` | 3 | 3 | 3 | 0 | 0 | 0 |
| `delayed_menu` | 3 | 3 | 3 | 3 | 3 | 0 |
| `loading_overlay` | 0 | 3 | 1 | 3 | 3 | 3 |
| `below_fold` | 3 | 0 | 2 | 0 | 3 | 3 |
| `article` | 3 | 2 | 1 | 3 | 3 | 0 |

## Estimated model cost

| Library | OpenAI Decisions arm | OpenCode Jev arm |
| --- | ---: | ---: |
| Decision Browser Core | $0.02972 | $0.01518 |
| jev-ultrafast | $0.01882 | $0.01203 |
| jev-ra | $0.02840 | $0.01180 |

Each arm estimate covers all 48 attempts, including text generation. An incomplete arm has no estimate. The CSV leaves an attempt’s cost blank if any request failed without billable usage, lacked the required token counts, or had incomplete accounting. Other attempts keep their estimates.

Standard USD rates per million tokens were $0.10 for OpenAI uncached input, $0.01 for cache reads, $0.125 for cache writes and $0.50 for Responses output. Decisions output was free. OpenCode Jev was $0.042 for input with free output. Estimates exclude browser operation, account-specific discounts, regional premiums and the access probe. [OpenAI Decisions pricing](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability) · [GPT-6 Luna pricing](https://developers.openai.com/api/docs/models/gpt-6-luna) · [OpenCode Console pricing](https://opencode.ai/v2/docs/console/models/#pricing)

## Protocol and scope

- OpenAI Decisions used `gpt-6-luna`. The OpenCode Console System One route used `jev-1.13`. When jev-ultrafast needed to generate text for a field, both arms used the same OpenAI Responses helper. Text requests count separately from decision requests. [OpenCode Jev route](https://opencode.ai/v2/docs/console/models/#jev)
- Goal text, supplied values, browser size (1280 × 720) and checks were held constant across arms. Each attempt used a fresh browser process. Fifteen tasks used local synthetic pages. The remaining task, `public_about`, visited the public Python website.
- The fixture recorded expected events and independent page checks. On Python.org the oracle checked the final URL. The page-and-event column counts attempts passing both independent checks, even if the library reported failure.
- Tasks had a 75-second limit, up to 12 physical model requests and 128 KiB of request input. The six baseline tasks allowed eight steps. The ten expanded tasks allowed 24. The three repetitions used seeded case and arm orders. The first pair of Core click attempts served as launch checks and remained in the matrix. A separate synthetic model-access request is excluded.
- The six arms use their library’s own observation representation, available operations and stopping conditions. Timing measures these complete integrations, including network and service variability. Three repeats per task support descriptive comparisons for this corpus.

[All 288 attempts (CSV)](runs.csv) · [Aggregate, paired outcomes and source identities (JSON)](summary.json)
