# Admission and retained runs

The native server holds one run in memory. Its scope, pending proposal, worker,
form response, cancellation flag and artifacts belong to that run. A second
start returns `busy` while the run remains retained. Restarting the process
loses its in-memory state.

The standard-form channel binds the current run ID, proposal ID, canonical
digest, exact form ID and deadline. The server checks strict `reviewed: true`
content after an `accept` response. It rechecks expiry and cancellation before
allowing the worker to continue. Responses for another request, stale proposals,
replay and URL-only elicitation provide no admission.

Each mutating input revalidates its observed target and semantic destination.
Cancellation prevents later dispatch and asks owned browser resources to close.
A cleanup failure is returned as `outcome_unknown` with `cleanup_unknown`.
Read that result and reconcile effects before another logical task.

`task_scope` is an explicit startup policy for isolated exact-origin tasks.
Its capability and admission class identify that policy. The default remains
`standard_form`. [Mode guide](task-scoped-admission.md)

The native implementation and regression tests are in
[product_mcp.rs](../src/product_mcp.rs) and [stdio tests](../tests/product_mcp.rs).
The pre-migration design is preserved in
[the pinned source](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/admission-store.md).
