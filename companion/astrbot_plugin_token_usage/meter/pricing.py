"""Explicit user-supplied per-million prices; no remote price lookup."""
from decimal import Decimal, InvalidOperation


def rate(value):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= 10**6 else None
    except (InvalidOperation, ValueError):
        return None


def estimate(usage, billing, provider, model, prices):
    matches = [p for p in prices if isinstance(p, dict) and p.get('model') == model
               and p.get('provider', '') in ('', provider)]
    matches.sort(key=lambda p: p.get('provider') == provider, reverse=True)
    price = matches[0] if matches else {}
    # An explicit external subscription classification can never be priced as API.
    kind = billing if billing in {'subscription', 'local'} else price.get('billing', billing)
    if kind not in {'api', 'subscription', 'local'}:
        kind = 'unknown'
    result = {'billing': kind, 'currency': '', 'cost': None, 'saving': None}
    if kind != 'api' or usage.input is None or usage.output is None:
        return result
    currency = price.get('currency', '')
    if currency not in {'CNY', 'USD', 'EUR'}:
        return result
    ordinary, cached, output = (rate(price.get(k)) for k in ('input', 'cached', 'output'))
    if ordinary is None or output is None:
        return result
    if usage.cached is None:
        if cached != ordinary:
            return result
        count = 0  # equal rates allow a price without pretending cache usage is known
    else:
        count = usage.cached
        if count and cached is None:
            return result
    writes = usage.written or 0
    write_rate = rate(price.get('write')) if writes else ordinary
    if write_rate is None:
        return result
    cost = (Decimal(usage.input - count - writes) * ordinary + Decimal(count) * (cached or 0)
            + Decimal(writes) * write_rate + Decimal(usage.output) * output) / Decimal(10**6)
    saving = Decimal(count) * (ordinary - (cached or 0)) / Decimal(10**6) if usage.cached is not None else None
    return dict(result, currency=currency, cost=str(cost), saving=str(saving) if saving is not None else None)
