# Public-release checklist

This branch prepares a release; it does not publish one. Review the exact commit
and built artifacts that will be distributed. A draft PR is not release approval.

The [public-release delivery plan](public-release-plan.md) tracks the simplified
user journey and remaining implementation stages. Guided provider setup is an
implementation milestone, not a replacement for the native/live gates below.

## Preparation status — 2026-11-21

- [x] Prepare a separate publication repository from the reviewed tracked files.
  The private development repository used `9e746206f66056702b5db51ff498420a0bcd87e6`
  as its baseline. The publication snapshot starts its own Git history.
- [x] Restore the owner's selected MIT license, package SPDX expression and
  license files. Preserve separate upstream MIT notices for distributed excerpts.
- [x] Provide project-only installation for Codex, Claude Code or both, with a
  persistent wheel runtime, full Chromium and bundled skills. Keep credentials,
  project trust and native admission outside setup.
- [x] Document updates, removal, conflicts, local checks, support and limitations.
- [x] Align the Python distribution, lockfile and npm launcher at `0.1.1`.
  The npm publisher chose the personal scope `@haffa` for the public release.
- [x] Preserve historical benchmark numbers, images and revision identifiers.
  They are not fresh results for this candidate.

## Candidate evidence

The preceding local candidate `e9a2905` passed 2,350 tests plus five subtests on
macOS/Python 3.12, including real owned Chromium fixtures. Installer tests and
isolated client configuration/stdio checks passed. Native human approval and
live provider tasks were **NOT_RUN**. These results must not be relabeled as
verification of a later commit.

For the final PR revision, attach or link concrete evidence for each item:

- [ ] Review the complete intended commit history and tree for secrets, personal
  data, accidental local paths and unexpected artifacts. Record the owner's
  decision about existing author and committer email addresses. To keep the
  development history private, start a separate publication repository with
  the reviewed tracked files and qualify its initial commit. Use a verified
  noreply identity for new commits.
- [ ] Build wheel/sdist, inspect license metadata, linked docs, skills and notices,
  and install the exact wheel in a clean environment.
- [ ] Exercise installer lifecycle and README examples in isolated projects;
  record the actual Python, uv, client and browser versions.
- [ ] Run the full verifier on the final tree. Report failures, BLOCKED and
  NOT_RUN checks explicitly, including native/live coverage.
- [ ] Verify hosted Python 3.11–3.13 browser CI for the exact final commit.
  Review the PR head SHA and any merge commit used by PR CI. A passing run for
  an earlier commit does not close a failed run for the candidate.

The PR description and attached verification results are the evidence record;
unchecked boxes here are release gates, not claims that a check failed.

## Before publishing

- [x] Owner confirms rights to code, benchmark source records, screenshots/GIFs
  and other assets. Included licenses do not settle all material ownership.
- [ ] Establish and test a private vulnerability reporting channel and define
  maintenance expectations. GitHub's private reporting feature is available for
  public repositories. If using it, [enable private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
  and verify the reporting entry point before announcing the release. Use a
  separate private channel if reports need to be accepted before the repository
  opens.
- [ ] Complete the [native acceptance procedure](../tests/native/README.md) in
  both Codex and Claude Code with explicitly authorized provider credentials.
  Record installation, the actual form schema, human accept/decline/cancel,
  a bounded task with independent checks and cleanup, explicit cancellation,
  and direct/implicit skill execution for the final candidate.
- [x] Choose `0.1.0`, move its notes to the dated `2026-11-20` changelog entry,
  and pass `scripts/check_release.py --tag v0.1.0` at `d367eb2`. Repeat this
  check on the final candidate before creating its tag.
- [x] The owner approved public visibility, the GitHub release and publication
  of the npm package as `@haffa/decision-browser`.
- [ ] After release approval, tag the reviewed commit and require the full tag
  matrix plus release-candidate gate. Publish only the tested artifact hashes.

See [release procedure](releasing.md), [security policy](../SECURITY.md) and
[verification boundaries](verification.md). No current automated check proves
that publication is safe for all private information or that arbitrary websites
will work.
