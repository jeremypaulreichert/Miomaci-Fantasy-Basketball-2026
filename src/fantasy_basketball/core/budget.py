"""Conservative cash commitments; usage estimates never become invoices."""
from decimal import Decimal, ROUND_CEILING, localcontext
from .config import integer


def charge(limits, input_tokens, output_tokens, reasoning_tokens):
    for value in (input_tokens, output_tokens, reasoning_tokens):
        integer(value)
    # USD per million tokens is numerically microUSD per token.
    with localcontext() as context:
        context.prec = 60
        value = (Decimal(limits.input_usd_per_million) * input_tokens
                 + Decimal(limits.output_usd_per_million) * output_tokens)
        if limits.reasoning_mode == "separate":
            value += Decimal(limits.reasoning_usd_per_million) * reasoning_tokens
        return int(value.to_integral_value(rounding=ROUND_CEILING)) + limits.fixed_micro_usd + limits.safety_micro_usd


def request_bound(limits):
    return charge(limits, limits.input_tokens, limits.output_tokens, limits.separate_reasoning_tokens)


def totals(accounts):
    return {
        "estimated_charge_micro_usd": sum(a["estimate"] or 0 for a in accounts.values()),
        "invoice_confirmed_micro_usd": sum(a["invoice"] or 0 for a in accounts.values()),
        "unresolved_exposure_micro_usd": sum(max(a["bound"], a["estimate"] or 0) for a in accounts.values() if a["invoice"] is None),
        "committed_micro_usd": sum(max(a["bound"], a["estimate"] or 0) if a["invoice"] is None else a["invoice"] for a in accounts.values()),
    }


def reserve(accounts, key, limits, config, protected=0):
    integer(protected)
    if key in accounts:
        raise ValueError("duplicate reservation")
    bound = request_bound(limits)
    if totals(accounts)["committed_micro_usd"] + bound + protected > config.budget_micro_usd:
        raise ValueError("budget cannot cover request and protected work")
    accounts[key] = {"manager": limits.manager, "bound": bound, "estimate": None, "invoice": None}


def record_estimate(accounts, key, value):
    if type(value) is not int or value < 0:
        raise ValueError("nonnegative integer estimate required")
    if accounts[key]["estimate"] is not None:
        raise ValueError("duplicate estimate")
    accounts[key]["estimate"] = value
    return value <= accounts[key]["bound"]
