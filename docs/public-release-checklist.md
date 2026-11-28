# Native release checklist

Apply this checklist to the final commit and the exact artifacts intended for
publication. Keep evidence beside each completed item.

- [ ] The tracked tree passes `scripts/check-python-free.sh`.
- [ ] Cargo.toml, Cargo.lock, the executable version and dated changelog agree.
- [ ] Rustfmt, Clippy and all native tests pass, including Chrome tests.
- [ ] The native archive passes checksum, six-tool discovery, install, upgrade,
  recovery preview/apply and uninstall acceptance.
- [ ] Installation succeeds with Python, uv and Node.js unavailable.
- [ ] Ordinary authorized sites and exact-origin policy have native coverage.
- [ ] Existing Chrome preserves unrelated tabs and records cleanup uncertainty.
- [ ] Each supported platform has native browser and installer evidence.
- [ ] All authorized live model scenarios are attempted, with elapsed time,
  failed rows and reruns preserved.
- [ ] The coding client completes the MCP confirmation and artifact lifecycle.
- [ ] Sol and Astra reviews have no unresolved release blockers.
- [ ] Hosted checks pass on the exact reviewed commit.
- [ ] The distributed license, notices, data and source ownership are reviewed.
- [ ] Publication authorization applies to the reviewed release and assets.
- [ ] Published assets match the tested SHA-256 values and immutable tag.

Native protocol acceptance, user-click admission, provider access and task
success have separate evidence. Fill the checklist from observed results.
[Verification](verification.md) · [Release procedure](releasing.md)

Historical public-release evidence remains in
[the migration base](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/public-release-checklist.md).
