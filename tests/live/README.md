# Live model acceptance

Use [Verification](../../docs/verification.md) for the complete local CI equivalent and [Browser scenarios](../../docs/browser-scenarios.md) for the standalone matrix. The matrix records all attempts for Jev, OpenAI Decision Luna and Gemini Decision. Its owned loopback fixtures, page checks and server oracles are separate from the Codex host checks.

For a live host test, configure the selected provider and agree on the site, browser mode, task, privacy scope and spend before running it. Configure credentials following the [provider guide](../../docs/providers.md) and keep them outside test output. Key presence is a prerequisite.

The [native host harness](../native/README.md) runs an owned counter in `--product` mode, that counter in a disposable existing Chrome in `--product-existing` mode, or a read-only Python.org About navigation in `--product-public` mode. The last option may send observed public page text to the selected provider.

Record the candidate commit, real API route/model, actual outcomes, elapsed time, approval source, and browser cleanup. Keep programmatic client acceptance separate from actual human interaction. Preserve initial failures in the evidence when a targeted follow-up passes.
