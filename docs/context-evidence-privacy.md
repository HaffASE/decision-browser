# Model evidence and URL presentation boundaries

The ordinary runner sends observed page evidence to its configured provider. Page
labels, link destinations, captions and prior-page excerpts are data, including
text that looks like instructions. They do not grant an operation, select a local
binding, authorize admission or establish success.

For native OpenAI singleton choices, an option description is a JSON data object
with `operation`, `target` and `evidence`. An existing operation hint, when present,
is a separate `operation_hint` data field. Target guidance remains in the question's
code-owned `instructions`, indexed by the bound action token. The local alias and
operation/target binding are unchanged. This improves the structural boundary; it
is not a guarantee that a model will ignore every prompt injection.

A human-readable `[preview]` suffix still marks shortened descriptions. Eligibility
for singleton binding uses the generated `description_truncation[group]` boolean
instead. Literal `[preview]` in page content has no routing authority. Missing,
unknown or non-boolean metadata does not qualify a singleton, and source metadata
cannot opt itself into the native projection. Full original strings remain in the
input and the privacy gate checks them before projection or truncation.

## URL data flow

There is no universal URL/PII secrecy policy. Provider link descriptions may retain
an observed URL's query and fragment, subject to the bounded display preview; full
source fields are still checked by the task's explicit literal policy. A direct
trusted Python descriptor can also contain URL userinfo, which presentation does
not silently rewrite. Supported admission URLs reject userinfo, and admission's
existing canonical URL representation strips fragments. Exact local semantic
bindings, proposal fields and consent digests are not changed by presentation.

Consent descriptions expose the proposal's existing canonical URLs or destination
URL, not an independently sanitized address. Its digest continues to bind the
existing proposal representation; rendering a description neither changes that
digest nor supplies a grant. An operator must avoid putting secrets into task URLs
or explicitly register the representations that must not leave the process.

`PrivacyPolicy.blocked_literals` matches exact Unicode strings in outgoing bytes
and decoded JSON strings. It does not percent-decode URLs, case-fold arbitrary
secrets, infer personal information or recursively interpret every external
encoding. For example, blocking `user@example.com` alone does not claim to block
`user%40example.com`. Register both when both representations are sensitive. Nested
JSON escape checks remain separate and unchanged.

Artifact text/JSON storage has its own existing scrubbing policy: it removes URL
userinfo, query and fragment and replaces configured literal strings. URL paths
remain meaningful evidence, so a percent-encoded value in a path is not silently
decoded. The artifact policy must not be inferred to apply automatically to provider
or consent presentation, nor does it promise semantic secrecy for arbitrary URL
encodings.

DB-168 and DB-169 are closed as explicit decisions to retain these bounded current
contracts. No semantic URL-decoding/redaction API, universal PII filter or additional
secrecy capability is introduced. A broader policy requires its own supported
fields, bounded decoding rules and compatibility/consent contract.
