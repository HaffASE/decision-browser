# Browser control through CDP

The native server uses Chrome DevTools Protocol for observation, target checks
and browser input. `DECISION_BROWSER_CHROME` selects an absolute executable
when the browser is outside supported discovery paths. Keep its sandbox enabled.

## Isolated browser

An isolated task creates a fresh browser context and owns its task resources.
The task's start URL, origin, operation set and finite limits define its scope.
The runner rechecks observed controls and evaluates independent completion checks.

Forms, redirects, document navigation and network requests are constrained by the
exact-origin scope. External resources can be prevented by this policy. Sites
that require an unsupported interaction return a reason within the same limits.

## Existing Chrome

Select a Chrome debugging session through its trusted local `DevToolsActivePort`
file. `DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT` accepts its absolute path. The
server checks the file and loopback debugging endpoint before attaching.

The runner creates an acknowledged task tab in the selected context. Existing
cookies and account access are shared. Other tabs belong to the user, and task
cleanup closes its acknowledged resources. Ambiguous cleanup returns
`outcome_unknown` with a cleanup reason.

With an explicit `DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT` selection, the
`task_scope` policy admits ordinary actions in the task tab without a form.
An acceptance run with a disposable Chrome profile can verify tab inventory and
cleanup. Record account-profile acceptance separately if a real account is
intentionally tested.

## Actions and evidence

Read `capabilities.ops` for the connected build's supported actions. The native
runner discovers controls, builds choices, binds model selections to those
choices and rechecks target semantics before input. Field values, selected
options, destination URLs and form metadata contribute to effect checks.

Supported completion checks query the resulting page independently of a model's
chosen action. Artifacts are accessed through MCP IDs. Geometry diagnostics
remain distinct from functional verification.

```sh
cargo test --locked --lib browser -- --include-ignored --test-threads=1
cargo test --locked --lib task_runner -- --include-ignored --test-threads=1
```

[Request example](../examples/browser-control-task.json) ·
[Verification](verification.md) · [Security](../SECURITY.md)
