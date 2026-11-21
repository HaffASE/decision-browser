"""Clock-driven cleanup must not break alpha_allows iteration."""

from decision_browser.admission import AdmissionStore


def test_alpha_allows_continues_after_cleanup_drops_current_record():
    # propose/accept consume six clock reads. alpha_allows reads once, then
    # is_live -> _fresh reads again. Advance on that eighth read, mid-scan.
    reads = {"n": 0}

    def clock() -> float:
        reads["n"] += 1
        if reads["n"] >= 8:
            return 15.0
        return 0.0

    store = AdmissionStore(clock=clock, cleanup_grace=5.0)
    current = store.propose_alpha(
        "run-old",
        ["https://example.com/old"],
        ["click"],
        {"steps": 1},
        deadline=10.0,
        development_only=True,
    )
    later = store.propose_alpha(
        "run-new",
        ["https://example.com/new"],
        ["click"],
        {"steps": 1},
        deadline=100.0,
        development_only=True,
    )
    desk = store.development_desk()
    assert desk.accept(current.proposal_id, current.digest)
    assert desk.accept(later.proposal_id, later.digest)

    allowed = store.alpha_allows(
        "run-old",
        "click",
        "https://example.com/old",
        "https://example.com/old",
    )

    assert allowed is False
    assert current.proposal_id not in store._proposals
    assert "run-old" not in store._run_deadlines
    assert later.proposal_id in store._proposals
    assert store.alpha_allows(
        "run-new",
        "click",
        "https://example.com/new",
        "https://example.com/new",
    )


def test_expiry_revocation_beta_and_canonical_urls_stay_fail_closed():
    instant = {"t": 0.0}
    store = AdmissionStore(clock=lambda: instant["t"], cleanup_grace=5.0)
    alpha = store.propose_alpha(
        "run",
        ["https://example.com/doc"],
        ["navigate"],
        {"steps": 1},
        deadline=60.0,
        expires_at=55.0,
        development_only=True,
    )
    beta = store.propose_beta(
        "run",
        "finger",
        "navigate",
        "https://example.com/doc",
        "https://example.com/doc",
        "https://example.com/doc#frag",
        deadline=60.0,
        development_only=True,
    )
    desk = store.development_desk()
    assert desk.accept(alpha.proposal_id, alpha.digest)
    assert desk.accept(beta.proposal_id, beta.digest)
    assert alpha.canonical_urls == ("https://example.com/doc",)
    assert beta.destination_url == "https://example.com/doc"
    assert store.beta_allows(
        "run",
        beta.proposal_id,
        beta.digest,
        "finger",
        "navigate",
        "https://example.com/doc",
        "https://example.com/doc",
        "https://example.com/doc#frag",
    )

    instant["t"] = 55.0
    assert (
        store.alpha_allows(
            "run",
            "navigate",
            "https://example.com/doc",
            "https://example.com/doc",
        )
        is False
    )
    assert (
        store.beta_allows(
            "run",
            beta.proposal_id,
            beta.digest,
            "finger",
            "navigate",
            "https://example.com/doc",
            "https://example.com/doc",
            "https://example.com/doc",
        )
        is False
    )

    instant["t"] = 56.0
    kept = store.propose_alpha(
        "other",
        ["https://example.com/other"],
        ["click"],
        {"steps": 1},
        deadline=80.0,
        development_only=True,
    )
    assert desk.accept(kept.proposal_id, kept.digest)
    store.revoke("other")
    assert (
        store.alpha_allows(
            "other",
            "click",
            "https://example.com/other",
            "https://example.com/other",
        )
        is False
    )
