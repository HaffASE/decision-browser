"""Shared budgets and pricing for the long browser benchmark."""
from decimal import Decimal

MAX_STEPS = 24
MAX_CALLS = 24
TIMEOUT_S = 180
MAX_BYTES = 512 * 1024
MODELS = {"responses": "gpt-6.1-terra", "openai": "gpt-6-luna", "opencode": "jev-1.13"}


def make_request(case, url):
    import compare_browser_libraries as bench
    import long_scenarios

    request = bench.make_request(case, url, long_scenarios)
    request.update(max_steps=MAX_STEPS, max_calls=MAX_CALLS, max_provider_calls=MAX_CALLS,
                   timeout_s=TIMEOUT_S, max_input_bytes=MAX_BYTES, max_cost_usd=.25)
    return request


def charge(call, model):
    """Same standard USD token rates as the published short benchmark."""
    if not call.get("usage_reported") or call.get("status") != 200:
        return None
    n = call.get("input_tokens")
    if type(n) is not int or n < 0:
        return None
    endpoint = call["endpoint"].rsplit("/", 1)[-1]
    if endpoint == "systemone":
        return Decimal(".042") * n / 1_000_000
    if endpoint not in {"responses", "decisions"}:
        raise ValueError("unrecognized provider endpoint")
    cached, written, output = (call.get(k) for k in
                               ("cached_tokens", "cache_write_tokens", "output_tokens"))
    if any(type(v) is not int or v < 0 for v in (cached, written, output)) or cached + written > n:
        return None
    if model == MODELS["responses"]:
        if n > 272_000:
            return None
        amount = n - cached - written + Decimal(".05") * cached + Decimal("1.25") * written + 5 * output
    else:
        amount = (Decimal(".10") * (n - cached - written) + Decimal(".01") * cached
                  + Decimal(".125") * written) * (2 if n > 272_000 else 1)
        if endpoint == "responses":
            amount += Decimal(".50") * output * (Decimal("1.5") if n > 272_000 else 1)
    return amount / 1_000_000
