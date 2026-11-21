# Ordinary tasks without admission forms

## Plan

The user requested ordinary browser tests without repeated, incomprehensible
manual admission dialogs. Add an explicit server startup policy, `task_scope`,
separate from `standard_form`. The default standard-form behavior remains
unchanged until this policy is selected in the server configuration.

1. Add a distinct code-owned task-scope admission path and class. Do not call
   `accept_standard_form`, fabricate `reviewed`, or report a human click.
2. Restrict the new mode to isolated browsers and the current request's single
   initial origin, operations, budgets, and deadline. Preserve expiry, revoke,
   cleanup, and the no-replay boundary.
3. Keep consequential actions blocked. Stop on origin expansion or uncertain
   action scope instead of silently granting a beta proposal or presenting an
   unexpected form.
4. Expose the selected policy in capabilities, add an explicit CLI option, and
   carry it through the existing Keychain entry without changing credential
   retrieval. Update the packaged skill and preserve standard-form tests.
5. Verify the code before enabling the option in the project-local MCP config.
   Retest Google Flights in a fresh native connection and retain its evidence.

Task-scoped admission is an automated server policy, not standard-form or
native-human proof. It is scoped to one run; it is not a persistent site
allowlist. The existing lexical consequence classifier and native DOM guards
remain in force. Ordinary controls can have hidden application effects, so this
mode does not certify that a site is read-only or that arbitrary consequences
are detected.

The CLI accepts `serve --admission task_scope`. When the host retains old
launcher arguments, CLI startup also resolves this saved option from the current
project's `.codex/config.toml`, only if the interpreter, working directory, and
Keychain launcher match the active process. Explicit CLI options take precedence.
Missing/nonmatching project settings keep standard-form defaults; malformed
matching policy fails before the server starts. This resolves a startup policy,
not a per-request permission flag or a human grant.
