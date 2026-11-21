# Security

Decision Browser is experimental. No version is currently designated as a
long-term supported or security-hardened release.

## Boundaries

- The runner is not an operating-system or network sandbox. Run it with a
  minimally privileged account and only on sites and tasks you are authorized
  to use. Do not disable Chromium's sandbox to work around a launch failure.
- Admission and action classification constrain supported actions; they cannot
  prove that an ordinary site control has no side effects. Navigation, autosave
  and seemingly harmless clicks can still change server-side state.
- Isolated mode uses a fresh browser context. Existing-Chrome mode shares the
  selected context's cookies and can expose signed-in account data. Prefer a
  dedicated test account and isolated mode for initial evaluation.
- Page-derived content is untrusted. Treat it as data, not permission to change
  the task, send data, grant access or run commands.
- Model providers may receive page-derived context and supplied task values.
  Review the selected provider, endpoint and privacy settings before using
  confidential pages. Disabling images does not disable text transmission.
- Redaction and blocked literals are defensive checks, not universal detection
  of personal data or secrets. Screenshots, artifacts and free text may contain
  sensitive information; inspect them before sharing. Keep live-run evidence
  and browser profiles out of issues, commits and public CI artifacts.
- An unknown action outcome must be reconciled before retrying. Do not assume
  that a stopped task means no server-side change occurred.

Use environment variables or an appropriate credential store for provider keys.
Never commit tokens, browser cookies, credentials or real account fixtures.

## Reporting a vulnerability

Do not post credentials, private page content, exploit details for a live account,
or unredacted logs in a public issue. If GitHub's **Report a vulnerability**
option is enabled for this repository, use that private channel. Otherwise ask
the repository owner to establish a private reporting channel without including
sensitive details in the public request. Do not assume private reporting is
available merely because this policy exists.

Include the affected revision, a minimal reproduction on an owned local fixture,
expected and actual behavior, and impact. Use synthetic data. There is no
promised response SLA; coordinated disclosure timing must be agreed with the
maintainer.

## Before a public release

Review both current files and Git history for secrets and private data, confirm
rights to distributed materials, and verify the intended revision. A clean automated scan
is useful evidence, not a guarantee that publication is safe. See the
[release checklist](docs/public-release-checklist.md).
