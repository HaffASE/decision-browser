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
identifiers. The coding client asks the user to review the goal, origin,
operations and limits. For an isolated task, the accepted form covers ordinary
actions at that exact origin until the task deadline. Controls with unresolved
effects and actions in existing Chrome are reviewed individually. The agent
must never synthesize a host response or its `reviewed` value.

In a managed Codex registration, `start_task` queues the request directly and
`present_admission` opens the form. That form controls when provider and
browser work can begin.

The accepted form is bound to the live proposal and expiry. Decline, cancel,
expiry and unsupported form clients stop with a specific reason. Protocol
acceptance alone provides no evidence of a native user click.

## Task scope

`task_scope` admits ordinary actions within the task's exact initial origin,
operations, budgets and deadline. Its returned `admission_class` identifies the
operator-selected policy. It grants no standard-form record. Consequential,
foreign-origin or unsupported actions stop according to their classification.
Opaque scripted controls with an unresolved effect require a standard form and
stop under `task_scope`.

`isolated` uses a fresh browser context. To enable `existing_chrome`, set
`DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT` in the server environment to the
absolute path of the selected Chrome session's `DevToolsActivePort` file. The
task shares that profile's account access and opens a separate tab which it
closes at the end. See the [Chrome setup](installation.md#existing-chrome).

Read the connected server's `capabilities` for available modes and operations.
To work on a different origin, start a separately agreed task there with its
own finite limits and admission.

## Cancellation and uncertainty

Use `cancel_task` for the retained run and poll while cleanup is pending.
An already dispatched effect can have happened before cancellation. Inspect
`terminal`, `reason`, `recovery` and evidence together. Reconcile unknown outcomes
before deciding whether another run is appropriate.
