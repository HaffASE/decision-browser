# Action classification

## Change plan

1. Add regressions for safe navigation and controls when the task mentions a
   consequential action, including an explicit prohibition. Require the normal
   admission proposal before any browser or provider activity.
2. Remove task-goal text from bootstrap and per-action classifier signals. Keep
   the original goal in model requests. Classify the selected control, its form
   and default submitter, and the effective destination URL.
3. Return `consequential_action_blocked` for a known consequential action. Keep
   `no_supported_human_grant_channel` for an unavailable admission channel.
4. Verify the focused regressions, existing admission and action guards, lint,
   typing, and the available local suite. Report live browser proof separately.

## Contract

The task is model context, not evidence that each individual browser action has
an external consequence. For example, "find flights; do not purchase or book"
must not prevent opening the flight-search page or clicking its Search button.

The classifier uses code-owned signals from the selected action's DOM snapshot
and destination path/query. A Purchase button, a consequential default submitter
on Enter, or a checkout route remains blocked even if the task says not to buy.
Removing task text from these signals does not grant admission. Normal origin,
freshness, budget, and record-backed admission checks still apply.

The complete DOM labels `Sort order` and `Apply order` describe ordering controls,
so their `order` token alone is not classified as a purchase. This narrow wording
exception applies only to the whole label (ignoring case and whitespace). It
does not exempt `Place order`, compound labels, another form/default-submitter
signal, destination paths or query parameters, or conflicting origin signals.

This is a narrow repair of signal provenance, not natural-language understanding
of arbitrary controls or URLs. The action classifier still uses a conservative
lexicon: form labels and route/query text can cause false positives, and an
unlabelled JavaScript side effect may be missed. The model's completion decision
and local tests do not establish successful execution on Google Flights.
