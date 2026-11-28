# Compact Decisions context

Historical design record from the pre-migration implementation. Current native
commands and acceptance requirements are in [verification](verification.md).
The [original source and procedure](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/context-optimization.md)
remain available at the pinned revision.


## Scope and behavior lock

Optimize only the ordinary runner's provider-bound representation. Keep its local
bindings, original DOM tokens, semantic fingerprints, action guards, admission,
privacy, budgets, selected-head validation and independent checks intact. Preserve
the earlier fixes already present in the working tree.

Baseline: the six-case, three-repeat OpenAI comparison passed 18/18 with 30
Decisions calls, 76,542 input tokens and estimated cost $0.008391475. Evidence is
in `.cache/ultrafast-comparison-2026-11-02/`.

## Plan before editing

1. Lock current behavior with the binding/domain/provider/privacy tests and the
   seven native comparison regressions. Add tests for lossless choice mapping,
   state/default-value preservation, grouped selects, shared forms, malformed
   context and privacy rejection before HTTP.
2. Add an explicit internal context marker. At the OpenAI provider boundary,
   factor repeated descriptions into indexed element/form tables and use short,
   observation-local wire choice IDs. Decode only codec-validated choices back
   to the original local IDs before returning to the runner.
3. Preserve all offered targets and option values. Keep meaningful false states,
   empty field values, selected/expanded state, captions, destinations, form
   actions, focus, nearby context, task text and acknowledged action history.
   Omit only duplicated descriptions and internal collector diagnostics from
   the model representation; retain those diagnostics locally.
4. Keep opaque direct-provider input and the existing Flights projection on their
   current paths. These are explicit supported input contracts, not recovery
   paths for malformed marked contexts. Malformed marked context fails before
   HTTP; do not fall back to uncompressed input to hide errors.
5. Run targeted regressions, Ruff, Mypy, the full pytest suite, wheel/sdist build
   and installed-wheel checks. Repeat the same six live tasks three times using
   the existing OpenAI credential, synthetic local pages and public python.org,
   unchanged limits and independent oracle. Keep every attempted live run.
6. Compare actual HTTP usage, input tokens and estimated cache-aware cost with
   the baseline. Require all tests and 18/18 confirmed runs before completion.

## Duplication and fallback inventory

- Repeated node/form descriptions and opaque DOM IDs are wire duplication.
- Ordinary runner inputs, arbitrary direct-provider text and specialized Flights
  inputs are separate existing contracts; keep their behavior explicit.
- Do not add retries, substitute model choices, prune target domains, weaken
  guards, or turn a missing/unknown outcome into success.
- Do not add dependencies or site-specific rules. Review the representation and
  mapping independently after implementation.

## Results

Fresh paired comparison on 2026-11-02: six cases, three repeats per version,
alternating order, one Chromium version, viewport 1280x720 and disposable profiles.
The baseline source was extracted from the previously verified wheel; the live
runners assert their imported source paths. All 36 attempts are retained.

| Metric | Before | After |
| --- | ---: | ---: |
| Confirmed outcomes | 18/18 | 18/18 |
| Actual HTTP Decisions calls | 30 | 32 |
| Input tokens | 76,456 | 69,258 |
| Request body bytes | 245,743 | 232,411 |
| Estimated cost of 18 runs | $0.008384075 | $0.006936850 |
| Median cold process time | 2.668 s | 3.0345 s |

Total input tokens decreased 9.4%, request bytes 5.4%, and estimated cost 17.3%.
Average input per HTTP call decreased 15.1%. One optimized search needed four
Decisions instead of two; every other task retained its call count. Neither
version called Responses. All six cases passed all three repetitions. The small
sample does not establish a general latency improvement: the measured median
was slower after optimization.

Cost uses actual returned usage and the standard GPT-6 Luna/Decisions rates,
including cache reads and writes; it is not a billing statement. Cache reads
were 0 before and 3,240 tokens after; cache writes were 29,539 before and 12,106
after. Part of the cost reduction therefore reflects cache behavior in this
series as well as smaller requests.

Verification: 142 targeted tests, the full 1,703-test suite without failures or
skips, Ruff, Mypy, wheel/sdist build, installed-wheel imports and dependency
checks passed. Independent review found one malformed-marker classification
issue; it was reproduced, fixed and re-reviewed before the final full suite.
The stopped preliminary run is not counted as a full pass.

The original DOM, runner, policy, admission, privacy, budget, decision codec and
Flights provider files remain byte-identical to the baseline. The provider-bound
context adapter is the only representation change; original target IDs are
restored after selected-head codec validation.

The representative 24-field/three-operation regression fixture reduces the
compiled payload from 42,740 to 14,448 bytes (66.2%). That synthetic reduction is
separate from the measured six-case totals above.

Detailed local evidence, paired raw results, source hashes and reproduction
scripts are retained under `.cache/context-optimization-2026-11-02/`.

Sources: [Decisions pricing](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability)
and [GPT-6 Luna pricing](https://developers.openai.com/api/docs/models/gpt-6-luna).
