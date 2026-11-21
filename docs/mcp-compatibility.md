# MCP compatibility boundary

The application pins MCP 2.3.0. `mcp_compat.close_tool_arguments` is the only
application adapter that touches its private tool registry. The high-level SDK
does not expose a public argument-model configuration parameter: its generated
outer models otherwise ignore unknown properties and pre-parse JSON-looking
strings. The adapter subclasses each generated model with strict/extra-forbid
configuration and replaces only that tool's metadata with literal argument
handling. It does not mutate the SDK's original model configuration. This is a
pinned compatibility seam, not a stable public SDK extension point.

The wire input schemas and runtime validation are tested together. Supplied
arguments must use their advertised JSON types; a JSON-encoded request object
inside a string is not a second supported input format. An omitted or JSON-null
`get_task.run_id` retains current-run recovery. A string `"null"` retains its
identity and returns the same unknown-run code as any other absent run.

Public `CallToolResult` supplies tool-origin domain errors with `isError: true`
and the existing fixed code in both text JSON and structured content. Framework
argument errors retain the SDK's validation-error path. The server's package
version is supplied as standard `serverInfo.version`; supported browser
operations and admission classes are derived from code-owned declarations.

Form admission uses `ServerSession.client_capabilities` to inspect the actual
form subcapability. The pinned generic capability check only tests whether
elicitation exists. Per protocol 2025-11-25, empty `elicitation: {}` remains a
form declaration; URL-only elicitation does not. The form requests an exact
`reviewed` boolean with `additionalProperties: false`, and runtime validation
continues to reject extra fields and coercion. Digest, freshness, revocation and
non-development record binding remain mandatory. A scripted MCP host response
is not proof of a native human click.

Artifact compatibility is explicit: the capped legacy channel retains both
original small-artifact envelopes, including bounded base64 duplication. It
rejects partial/oversized whole reads. The explicit range-v1 structured/resource
channels encode each at-most-65,536-byte chunk once and retain whole/chunk hashes
and run ownership. This does not lower or bypass the independent store's byte
and count quotas. Ordinary screenshot traffic reaching the store's 100 MiB
maximum is not claimed by these tests; synthetic producers exercise pagination.

For SDK upgrades, rerun `test_server.py`, `test_standard_form_protocol.py`,
`test_wave4_mcp_contracts.py`, `test_mcp_protocol.py` and
`test_wave4_mcp_protocol.py` with the new pin. The actual stdio clients verify
initialization/version, domain-error signals, closed schemas, literal IDs,
default/explicit artifact channels, URL-only rejection and legacy form support.
Owned Chromium cases retain the real RunManager, Policy, Budget, Journal,
independent verifier and cleanup; only the provider and external consent
response are synthetic. Live providers and native-human acceptance remain
separate qualification.
