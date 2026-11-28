# Admission modes

The server operator selects an admission mode at startup. MCP task arguments
cannot change that policy.

```sh
decision-browser serve --admission standard_form
decision-browser serve --admission task_scope
```

## Standard forms

`standard_form` is the default. Start a task once and poll its retained run.
When `pending_proposal` is present, call `present_admission` with its exact
identifiers. The coding client asks the user to review the scope. The agent
must never synthesize the host response or its `reviewed` value.

The accepted form is bound to the live proposal and expiry. Decline, cancel,
expiry and unsupported form clients stop with a specific reason. Protocol
acceptance alone provides no evidence of a native user click.

## Task scope

`task_scope` applies to isolated tasks within their exact initial origin,
operations, budgets and deadline. Its returned `admission_class` identifies the
operator-selected policy. It grants no standard-form record. Consequential,
foreign-origin or unsupported actions stop according to their classification.

Read the connected server's `capabilities` for available modes and operations.
Existing Chrome requires standard-form admission because it shares a selected
account context. Cross-origin document navigation and forms need a separately
agreed task scope.

## Cancellation and uncertainty

Use `cancel_task` for the retained run and poll while cleanup is pending.
An already dispatched effect can have happened before cancellation. Inspect
`terminal`, `reason`, `recovery` and evidence together. Reconcile unknown outcomes
before deciding whether another run is appropriate.
