# Why Luna dropped from 14/15 to 11/15

Three of the 15 previously matched attempts changed from confirmed to unconfirmed. No attempt changed in the opposite direction. Each row below corresponds to the same task and repetition under the same budgets. These were separate live runs, so the browser sessions and API responses were independent.

| Task and repetition | Intermediate Core | Following Core revision |
| --- | --- | --- |
| Exhibit 1 | Confirmed | Model refusal on call 3, before the first section was saved |
| Exhibit 2 | Confirmed | Rejected the offered choice on call 15, after six correct sections |
| Rail 1 | Confirmed | Model refusal on call 1 |
| Exhibit 3 | Rejected the offered choice on call 17 | Rejected the offered choice on call 15 |

The remaining 11 attempts passed on both revisions. A refusal is an explicit model response. A rejected choice means a selected question returned the additional no-action option used when the underlying API requires at least two choices. The runner stops in either case. The old records include actions, checks and stop codes but omit the full request and chosen question, so they do not identify which target the model rejected. Recorded comparison (historical revision `75807ce56fbc2fac571f67f7a8dd08a7ca538585`)

## What changed in the requests

The later revision added a sentence telling the model when to reject a sole target, and feedback after two consecutive waits with no visible change. Luna made **zero waits** in the 30 archived attempts across the two revisions. A parser change for callers who define their own operation named `UNABLE` also had no effect on these native runs. That leaves the singleton-target sentence as a change to the Luna request at the same browser state. The runners used the same model, fixtures, checks and budgets. Exact code change (historical revisions `a5c22bb8d58aedcedefff29e6073b98bd346a823` → `0ccf42c65baf609369124bf721a9311406fecc97`)

## Matched replay

We reconstructed two browser states with the native Core runner and Chromium. One was the first rail page. The other was the exhibit route editor after the name and template were filled, at the point of the historical call-15 stops. Both historical implementations compiled the same captured context. Request bodies differed only by the singleton-target sentence in two rail questions and four exhibit questions. At each state, the two requests were sent to GPT-6 Luna three times each, alternating which version went first.

| Browser state | Earlier wording | Later wording | Expected next action |
| --- | --- | --- | --- |
| Rail search | Fill “From station”, 3/3 | Fill “From station”, 3/3 | Fill a station |
| Exhibit route editor | Select empty placeholder, 3/3 | Select empty placeholder, 3/3 | Advance the completed form via “Preview route” |

The replay did **not** reproduce a refusal or a rejected choice. It therefore does not establish that the sentence caused the entire historical score drop. It did expose a separate planning error: on all six exhibit requests Luna chose an option that would clear the existing “Two stops” selection. The replay logged each decision without dispatching it. These diagnostic requests do not count toward completion, timing or cost in the browser matrix. All 12 returned HTTP 200, at an estimated total of $0.00312865. [Paired observations and request hashes](luna-wording-probe.json)

## Distinguishing form operations

A second comparison used the same frozen exhibit editor and earlier singleton wording. We changed just the descriptions of two **operation** choices. The `select` description explains that selecting an empty option clears the field. The `submit` description explains when to advance a form whose values already match the goal. The descriptions contain no exhibit, field or button names. The page state, available choices, target instructions and stop options stayed identical across each pair.

In three alternated pairs, the baseline chose the empty option three times. With the descriptions, Luna chose **click → “Preview route”** three times. All six requests returned HTTP 200, with an estimated total cost of $0.00135702. This establishes a difference on the reconstructed decision. The full browser runs measure whether it carries through to completed workflows. [Per-call results and exact descriptions](luna-form-probe.json)

## What Core changed

The first trial removed the singleton rejection sentence only from Luna. It repeated **11/15 on Luna and 15/15 on Jev**, with the same five tasks and three repetitions. This time Luna finished one museum attempt, but incorrectly saved a companion reference in one library attempt and a note tag in one support attempt. The identical aggregate score masked different failures. Wording-only source (historical revision `ff2cf08138427fb82c990f81335612d0dc20aa01`)

The next change gives Luna the tested operation descriptions when a native form offers both a select control with a nonempty selected value and an available submit action for that form. It preserves the offered targets, explicit clearing of a selection, the no-action choice, wait feedback and the existing dispatch and completion checks. Jev uses its previous question format and singleton instructions. The actual request bodies captured on the reconstructed rail and exhibit pages matched the previous Jev version byte for byte. On the exhibit page, the Luna request matched the tested form variant. Form-guidance source (historical revision `dd7ea91c51879a8afcb9c8d4a89c94bd18e68d69`)

The [long-suite report](report.md#core-rerun) keeps full browser runs separate from the isolated provider decisions. An improvement on a single reconstructed page does not identify the cause of every earlier failure.

One additional unscored live museum attempt on the measured revision completed all nine sections in 18 HTTP 200 responses ($0.00643190 estimated). At call 7, Luna clicked “Add first stop”; a refusal on an unused select question did not stop the run. At call 15 it clicked “Preview route”. This trace demonstrates that the same stage can yield different decisions across fresh runs. It is excluded from the 15 scored Luna attempts. [Sanitized diagnostic trajectory](luna-trace-probe.json)

## Asking for an explicit action and target

The form-guidance version completed 11/15 on Luna and 15/15 on Jev. Three Luna attempts stopped on a rejected choice (two museum attempts and one support attempt). We therefore tested a change to how native Luna questions represent a single fully described target. Previously the model could select an operation such as `submit` and separately select the no-action alternative in its target question. The new request can offer one named choice for submitting that particular observed form. The selected choice must identify both the operation and the target. A bare operation, an unsolicited answer or a refusal does not authorize a browser action. Multi-target operations still require a separate explicit target. The browser guard runs before dispatch.

We compared the two representations twice at each of two fixed museum states. All eight decisions chose an action that advanced the scripted workflow. Both versions clicked “Add first stop”. In the editor the earlier version clicked “Preview route”; the new one explicitly chose to submit that same form. This check verified that the selected option decoded to the correct observed target. It did not show a success-rate difference at those two states. All eight requests returned HTTP 200 ($0.00171795 estimated) and are excluded from the browser matrix. [Paired decisions and action bindings](luna-bound-choice-probe.json)

## Full rerun

On five tasks repeated three times, the explicit-choice version confirmed **14/15 on Luna** and **15/15 on Jev 1.13** with the original 24-call and 24-step limits. Luna passed the museum and rail tasks 3/3 each. Its one unconfirmed equipment attempt stopped before saving the first section after three clicks produced no observable progress. Every run that Core declared successful also passed the independent server check. The scored runs used 205 requests on Luna and 210 on Jev. The harness retried one interrupted Luna library attempt after HTTP 503, at the same URL with the same request and reset fixture state; the interruption is recorded separately.

Across the three new versions the scores were **11 → 11 → 14** for Luna and **15 → 15 → 15** for Jev. The explicit-choice version recovered Luna's earlier total on a fresh run. Three repetitions per task are enough to identify the specific stops in this series, but not to assign all three historical losses to one wording change or guarantee the future success rate. Measured explicit-choice source (historical revision `f3ba534d0540bb00f3229b792636c85f8772f898`) · [Results and cost per model](report.md#core-measurement-history)
