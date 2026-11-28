# Action classification

The native policy classifies an observed action using its exact destination,
control label, form/submitter signals and semantic context. Task prose and a
provider's suggested action do not establish permission.

Recognized purchase, payment, send, publish, delete, transfer, reservation and
booking signals are consequential and blocked. The classifier handles decoded
URL routes and identifier word boundaries. A conflicting or ambiguous signal
remains unresolved. Ordinary controls still require the active task's scope and
admission policy.

Before input, the runner verifies the target remains connected, visible,
unobscured and semantically consistent with the observed choice. Changes to
form action, method, target or link destination invalidate the prior binding.
Unknown post-dispatch outcomes require reconciliation before retrying.

The task's exact origin constrains navigation and network effects. An ordinary
label can still have site-specific side effects, including autosave. Use sites
and accounts authorized for the requested task.

[Native policy](../src/native_policy.rs) · [Runner](../src/task_runner.rs)
· [Admission](task-scoped-admission.md)
