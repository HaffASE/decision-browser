# Local interfaces and packaged experimental utilities

These boundaries describe the existing Python/package contracts. They add no
browser capability, provider route, consent authority, or human admission claim.

## Provider values and profiles

`provider_config` remains the public facade for `ProviderConfigError`,
`ProviderSettings`, `TextSettings`, validators, route names, settings loading and
prerequisite status. Its value classes retain their historical public pickle
module identity. `provider_values` owns pure explicit-mapping validation and
frozen values; it reads neither profiles nor environment variables.

`user_config` validates profile values through that pure layer, rather than
calling back into the profile-aware facade. The dependency direction is now
`provider_config → user_config → provider_values`, with the facade also using
`provider_values`. Explicit environment mappings still bypass profile reads;
saved-route credential matching, exact class checks and sanitized errors stay
unchanged. This removes mutual runtime imports without a provider framework.

## Admission and observation reads

`AdmissionStore.standard_form_pending` owns the production form adapter's
existing predicate: the same stored object and exact type, pending state,
non-development metadata, no grant record, a nonempty string digest, unrevoked
run and freshness. It describes a pending proposal; it does not grant one.
`StandardFormDesk` still separately requires the supported form capability,
accepted response and strictly reviewed boolean before recording admission.
Development and task-scope transitions remain distinct.

`DOMSession.current_observation` returns the last observation by identity and is
read-only; successful close clears it. Failed cleanup may retain that observation
without granting freshness or dispatch authority. Flights uses this seam for its existing fallback
when the manager has no current observation. The seam does not adopt a new
document, reset a budget, bypass generation checks or expose script execution.
Other narrow code-owned DOM/owned navigation hooks remain internal. A shared
plugin/state-transition framework is intentionally not introduced.

The factory's missing-key refusal is an explicit coded check that survives
Python `-O`. Runner cleanup also uses an explicit owned-object check while
retaining the owned-close flag needed by its uncertainty bookkeeping. Neither
removed assert was evidence of a public authorization bypass.

## Keep the distinct Fetch protocols

| Path | Contract that remains separate |
| --- | --- |
| `BrowserDriver.read_owned_document_body` | Explicit bounded body reading without the runner's owned initial navigation transaction. |
| Owned initial document helpers | Bound page/context/budget/owner/resource identity, arm-before-navigation, pause ordering, fulfillment and owned evidence lifecycle. |
| `capture_committed_document_navigation` | Reference-page capture and committed-navigation proof with its own cancellation/release obligations. |

Common Fetch/IO calls do not make these ownership and commit contracts
equivalent. No blanket reader merge or shared state machine is made. A future
pure I/O extraction needs an actual change, equivalent caps/decoding and
separate release/cancellation controls for every caller.

## Retain declared package entrypoints

The owned body reader, owned binding helpers, fixed-frame PNG module,
`capture_committed_document_navigation`, Counter and Flights remain packaged with their
existing import paths and entrypoints. They are explicit local/experimental
utilities with their existing tests, rather than new normal-browse capabilities.
Lack of a normal-runner caller is not a reason to remove a declared method or
module. No compatibility relocation is needed because no API is moved out of
the package. Geometry remains diagnostic; the PNG utility keeps its fixed size
and refusal caps; current Flights query freshness and opt-in limits remain in
force. Native human admission and live provider acceptance remain separately
unqualified.

## Private cleanup decisions

`dom._SUBMIT` is retained: the current submit dispatch uses it. `_clean_map` is
absent from the accepted source, so no fictional replacement is added. The
geometry label-only helper is removed because parsed label nodes are explicitly
noninteractive and cannot enter the interactive overlap pair branch. Label
exclusion and real control overlap retain their tests. The owned server derives
its frozen content-ID set from the existing immutable catalog and keeps exact
membership, resolver identity, type, hash and CSP checks; only the tautological
UTF-8 decode-after-encode equality is removed.
