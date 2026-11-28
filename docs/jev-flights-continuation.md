# Approved Flights state continuation

Historical design record from the pre-migration implementation. Current native
commands and acceptance requirements are in [verification](verification.md).
The [original source and procedure](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/jev-flights-continuation.md)
remain available at the pinned revision.


This is a run-local authorization of independently checked state, not causal
ownership of a URL change. The user approved implementation on 2026-10-10.
No spend window, credentials, browser session or external publication is granted.
Only the pilot entry point opts in; generic/default/standard-form recovery stays
unchanged. Initial construction may wrap the existing provider for projection;
recovery never replaces the run, Alpha, provider, budget, ledger or deadline.

## Request and URL contract

The trusted pilot request is exactly the existing public JFK to LAX task:
one way, November 10 2026, one adult, economy including Basic, isolated mode,
visual disabled, the two existing bound airport values, no extra checks.
The original task limits must remain within 40 calls, 40 steps, 512 KiB and
600 seconds. A page/model/caller request field cannot opt into this policy.

Accept only HTTPS www.google.com at effective port 443, with no credentials,
fragment, whitespace/control characters, path escapes or prefix aliases.
Exact paths are /travel/flights and /travel/flights/search. The search path
requires a fully completed acknowledged Search and all committed query controls.
Require hl=en; allow at most one each of tfs and tfu, nonempty URL-safe base64
shape, each <=4096 characters; total URL <=4096 characters. Unknown, duplicate,
encoded key names, empty/malformed fields and redirect keys stop. Opaque values
are local, untrusted state. They neither prove flight query nor grant authority.

## Local DOM contract

Capture the existing bounded evidence in the isolated world between native
document-action guards. Capture URL, cached observation URL and live page URL
must agree; every candidate's document URL must agree. Replacement Document,
popup/context change, missing/ambiguous required controls, truncation, CAPTCHA,
visible password/login form, unknown modal or conflicting destinations stop.

There must be one observable origin field, destination field and departure
field, with the existing label prefixes; a recognized airport picker may expose
one additional expanded field. Trip is exactly Round trip or One way; cabin is
Economy including Basic and passenger count is one using existing oracle labels.
Recognized modals contain the actual bounded origin/destination input or calendar
grid; unknown dialogs stop. Their untrusted text is never outgoing context.

Successful completed One way, exact JFK/LAX suggestion and exact requested
calendar Done commit those settings. Later state must independently retain
committed airport identities (the existing oracle's collapsed label/value rule),
trip and date. Search requires both airports and acknowledged exact date Done.
Results state requires completed Search; priced success still belongs solely to
the unchanged independent oracle, with no Search replay.

Continuation is narrower than ordinary planning: only a closed One way form or
post-Search results may continue. URL drift while a picker/calendar is open
stops. Default uncommitted fields are not forwarded as free text or grants.
An unrelated timer producing the same permitted state is deliberately accepted
under this explicit policy, without an ownership claim. Hidden application
effects remain the existing residual risk; this is not a network firewall.

## Offered actions and provider projection

Filter bindings before compiling finite questions, retaining only canonical
flight controls: trip selection, the two airport fields and exact suggestions,
departure/calendar navigation, the exact 2026-11-10 date and exact date Done,
Search after query commitment, bounded scroll and wait. No screenshot, arbitrary
navigate, enter, purchase/flight selection, sign-in, tracking, CAPTCHA or
unbound fill. Recognize only appropriate native roles; ambiguity among required
retained controls stops. In results only wait/DONE is offered.

The closed binding table uses case-insensitive, whitespace-normalized full
matches; no suffix/prefix matching. Empty or selected field labels are exactly
`Where from?`, `Where from? New York JFK`, `Where from? John F. Kennedy
International Airport JFK`, and the analogous Los Angeles/LAX destination.

| Phase | Role | Operation | Exact names / bound values |
| --- | --- | --- | --- |
| Closed form | combobox, button | click | Round trip, One way (trip picker) |
| Trip picker | option, menuitemradio | click | One way |
| Closed form or corresponding airport picker | combobox, textbox | click/fill | Canonical origin/destination field; fill only JFK/LAX; filled label canonicalized to the original request key |
| Origin/destination picker | option | click | John F. Kennedy International Airport (JFK) / Los Angeles International Airport (LAX) |
| Closed form | textbox, combobox, button | click | Departure, Departure date |
| Calendar | button | click | Next month, Previous month; bare Next/Previous only in the calendar phase |
| Calendar | button, gridcell | click | November 10, 2026 or Nov 10, 2026, optionally prefixed Tuesday or Tue and a comma; no other words |
| Calendar | button | click | Done. Search for one-way flights departing on November 10, 2026 (the live label's comma after "flights" is accepted and canonicalized) |
| Closed committed form | button | click/submit | Search, Search for flights |
| Any permitted form phase | document | scroll/wait | up/down; fixed 100 ms wait |
| Completed Search/results | document | wait | fixed 100 ms wait; DONE is still independently verified |

Exactly one trip control, one passenger control and one cabin control must be
present in bounded evidence. Multiple identical controls are ambiguous too.
The user approved one narrow intermediate-menu exception on 2026-10-11:
after a fully returned acknowledged Round trip from a validated closed form,
Google may hide the entire underlying form. Bind that permission to the same
isolated Document, exact URL, navigation generation and completed attempt.
Require one visible listbox/menu with exactly Round trip, One way and Multi-city
options in its corresponding native roles, no visible inputs/grid/modal, and
no associated label, aria-labelledby override, or aria-label that disagrees with
option text, and
one usable native One way binding. Offer only that click; do not read or project
hidden form values as newly observed. Clear this permission after the next UI
action. The next decision must fully revalidate the restored form, including
committed One way, before provider input. No exception applies to missing or
partial controls outside this expected menu, Search/results, or a continuation.
At most one visible modal is allowed. An airport picker has an expanded matching
field (one canonical collapsed field and at most one bare expanded field), and
its modal contains only that airport input, not password/form submission fields.
A calendar modal contains one grid and no airport/password input. A trip picker
has the exact One way option and no airport/calendar modal. An unknown modal,
multiple picker types, or multiple distinct tokens with the same canonical
name/operation stop. Only the expanded airport field is offered when present.
No fallback recognition based on a modal's arbitrary text is allowed.

### Pickers that hide the form (approved 2026-10-11)

A no-API probe of live Google Flights on 2026-10-11 showed that every picker
hides the underlying form, as the trip menu does. The user approved this
exception and the matching label corrections:

- A picker is bound only by an acknowledged click on the closed, validated
  form's Where from?, Where to? or Departure field, in the same Document. The
  binding uses that dispatch's own post-observation URL (Google may rewrite the
  opaque query during the click), the navigation generation and the completed
  attempt. Actions inside the picker renew it; the exact JFK/LAX suggestion or
  exact Done ends it. Any further URL or generation drift stops, and a hidden
  picker is never a continuation, Search or results state.
- While bound, exactly one modal of the bound kind is required, with no
  visible cabin, passenger or results evidence. An airport picker shows only
  its own single expanded input: `Where from?`/`Where to?`, or Google's
  `Where else?` (shown when the origin is pre-filled from location), which only
  the binding attributes to origin or destination. A calendar shows one grid,
  at most the Departure input and at most a committed trip control. Only that
  picker's controls are offered; hidden values are never read or projected.
  The full form is revalidated, including committed values, after it closes.
- Dialog-kind labels are whitespace-normalized (the live destination input is
  `Where to? ` with a trailing space). A calendar dialog may contain exactly the
  Departure text input.
- A calendar day button is named by its number. In the calendar phase only, the
  host reads, for that bound token, the full date of its single role-less
  labelled descendant whose text is the same day number; disagreement leaves
  the day unrecognized, and two matches stop as ambiguous. Fingerprints and
  native guards are unchanged.
- After an acknowledged committing choice (One way, JFK, LAX, Done), the pilot
  waits up to 1.5 s for Google's query rewrite (observed 0.3-0.5 s later) so it
  lands on the closed form and the ordinary continuation validates it. The
  pause makes no provider call and spends no step; the deadline still applies.

### Results evidence on the live page (approved 2026-10-11)

The same no-API probe reached Search, and the acceptance oracle failed on the
live results page. The user approved these oracle corrections:

- The loading hazard counts the phrase "Loading results", "Loading flights" or
  "Searching for flights" only when a smallest element containing it is painted
  (not display:none, visibility:hidden or opacity:0, and with a nonzero box).
  ARIA hiding does not exempt painted text. Google keeps an idle opacity:0
  "Loading results" bar in the page, and `innerText` includes it. Painted
  `aria-busy`/progressbar elements still count, and readyState is unchanged.
- Google's result row link is an empty `pointer-events:none` overlay, so the
  hit test lands in the row's painted content beside it. For such an overlay
  only, a hit inside the overlay's own parent counts, provided that parent
  holds no other result link. Any other hit, a hit-testable overlay that is
  covered, and every other viewport rule are unchanged.
- `Russian rubles` maps to RUB. Google prices in the location's currency, and
  the probe's Moscow IP gave roubles. Nonempty row text in RUB must still match
  the closed `RUB 123`/`123 RUB` forms; the empty overlay text is accepted as
  accessibility evidence only, as for every currency.
- After an acknowledged Search, the post-observation waits up to 8 s,
  read-only, for visible rows, no loading hazard and two identical consecutive
  evidence captures 0.3 s apart. Google takes about 1.2-1.4 s to replace the
  form and fill rows, and the 200 ms render opportunity otherwise caught the old
  form half torn down. The wait records `results_settle` with
  `settled`/`timeout`/`unreadable` and the elapsed time. It makes no provider
  call and spends no step, and every proof and the oracle still follow.

A date
helper that searches a date inside arbitrary prose is insufficient: binding
recognition and committed-date proof both use the full-match grammar above
(the closed departure value may omit the year only after exact Done).

Validate both href and form destination for every retained binding, independently
of the current document route, then preserve ordinary per-action admission and
native guards with full internal fingerprints/destinations. Unrelated controls
may be excluded; a required control with an unsafe destination stops before
model input.

Construct outgoing context afresh: original public goal/values, canonical
control names/roles, booleans for selection/expansion/request match, validated
phase and bounded canonical action history. Query/fragment is absent from every
outgoing URL field, including nested href/form/context/history. Arbitrary
viewport text, nearby context, raw uncommitted values, noncanonical labels and
opaque parameters never enter provider bytes. Questions retain exactly matching
filtered binding keys. The real provider/model/endpoint and privacy/usage checks
stay unchanged. New exports must not echo opaque query or raw fingerprint state.

## Continuation transaction

Require a live original Alpha, same isolated Document, queued=False, cancellation
and original deadline checks, active cumulative ledger and remaining original
quotas at read/model/effect boundaries. A completed action means _dispatch_one
returned successfully after post-observation, not its earlier journal ack.
Only successful non-wait UI operations issue capacity.

Consume one continuation for that completed action ID synchronously before the
first await, including failed validation. At most four continuations per run.
Wait, screenshot, bare ack, failed/partial/unknown action or drift during the new
decision without another completed UI action cannot create more capacity.
Discard the stale decision and all old prepared handles; one fresh bounded
observation must pass URL/Document/state/destination proof before a new model
decision. No stale action is reused, no spend/attempt is refunded.

A previously admitted exact state URL may repair an old loop observation without
another grant, but a further URL change cannot. Pure verification may use the
same gate before an oracle snapshot, consumes the same capacity and preserves
Search acknowledgement. Proof mismatch during reading stops; generic navigation
exceptions and acknowledged/unknown effects are never caught as retry requests.

Repair means returning the same already-validated Observation object, with the
same document and navigation generation, after checking the live URL and budget.
It does not observe/capture/revalidate or issue capacity. The pilot registers a
main-frame `framenavigated` event counter before its first decision. Each proof
binds that generation before/after asynchronous reads; each fully returned
dispatch records the then-current generation. A generation change with the same
URL is rejected (including observed leave-and-return), never treated as a free
repair. Any further drift while validating a continuation stops. This event
counter is bounded run-local bookkeeping, not page-injected code or a causal
claim; it cannot retrospectively detect navigation before registration.

The independent URL contract is implemented without the oracle's permissive
`_google_flights` helper. Sanitization covers the complete opt-in report,
including result, actions, diagnostic/journal copies and oracle raw snapshots,
after original oracle/digest validation. Internal fingerprints stay full; their
export copies contain only hashes. Exported raw snapshots are explicitly marked
as projected and their original internal digests are not recomputed as though
they authenticated the projection. A full serialized report canary test is
required, alongside the real provider's serialized mock-transport request test.

## Required adversarial acceptance

Native sandboxed intercepted fixtures and pure tests must cover:
allowed delayed trip/airport state; unrelated allowed timer with no ownership
claim; old decision/token never dispatched; original objects/deadlines/counters
unchanged; capacity spent before await/failure; one per completed action and
four total; wait/bare acknowledgement/unknown cannot replenish; repeated drift
stops; pure verification does not replay Search.

Deny before provider: payment/search/collector routes with neutral labels,
prefix/dot/encoded aliases, hostile href/form destinations, wrong/duplicate/
unknown query, new Document, origin expansion, popup, active login/CAPTCHA,
ambiguous/missing state and changed committed airports/date/trip.
Put a test-only canary in every query, nested URL, value, text, context and
history location; inspect serialized provider bytes and new exports for absence.
Retain native focus/replacement/partial-effect and all generic delayed-SPA
negative tests. Revoke/deadline/quota exhaustion never resets or refunds.
Fixtures and full verifier are not live Flights acceptance; no priced PASS is
promised and four continuations can be exhausted before a late results transition.
