# Process-local admission contracts

`AdmissionStore` is a bounded, in-memory Python API. Development desks are test
capabilities, not native-human proof or a public MCP grant route. Existing
object-identity, digest, live-record, deadline and replay checks stay mandatory.

## Provider and clock boundaries

A custom trusted `ProviderPort` must return an offered operation and target.
`DONE` is unavailable while the runner has pending completion obligations;
unoffered `DONE` is rejected as `helper_schema`. Offered `DONE` and `UNABLE`
still stop without dispatch, and the independent final verifier decides the
result. These defensive checks do not establish a remote authorization exploit.

The clock callback must return a finite non-boolean number. Callback exceptions
and invalid readings raise `ValueError("clock_error")` without callback text.
Failure supplies no substitute timestamp, updates no last-good time and extends
no deadline. Admission reads fail closed until the clock works again. Existing
`cancel`, `revoke` and process-memory `restart` do not need the clock; time-based
cleanup still requires a valid reading. Clock recovery cannot resurrect an
expired record, including after a backward reading.

## Unknown run revocation

`revoke(run_id)` validates the ID and is a no-op when no proposal has registered
that run. It allocates no tombstone. A later proposal may use that previously
unknown ID. Callers that require cancellation must own their run lifecycle;
the runner arms admission and retains its separate cancellation event before
proposal creation. This low-level no-op is not a claim about an immediate-cancel
MCP bypass. No supported workload requires a new pre-registration revocation
API, so this change adds no unbounded caller-ID cache.

## Retained proposal capacity

`max_proposals` counts every retained proposal object, including cancelled,
consumed and revoked proposals. Terminal objects cannot grant or replay. Cancel
or an unsupported scripted development response hides and terminalizes its own
proposal, but does not free a retained-object quota slot; cancel also does not
revoke sibling proposals. The explicit `cancel` response to the development desk
revokes the whole run instead. Run-deadline plus bounded cleanup-grace expiry
removes the retained evidence and frees capacity. Restart discards process-local
memory and never revives old proposal IDs.

The supported runner is already bounded by its action/artifact budgets. No
supported greater-than-256-proposal journey has established a need to split
live capacity from bounded terminal evidence. The existing finite backpressure
and retention contracts therefore remain in place. Increasing capacity or
adding tombstones is a separate API change, not a side effect of cancellation.
