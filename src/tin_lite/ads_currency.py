"""Money in a Google Ads account's own currency.

Tin's campaign figures start in US dollars: the assessment's keyword bids come from a USD
account and the launch inputs are in USD. Google reads every amount in the account's
currency. A launch converts once, at the European Central Bank's reference rate for that day,
rounds to Google's minimum unit for the currency, and pins the rate on the plan the founder
approves. Nothing converts again afterwards: the monitor compares the account's own costs with
the figures pinned at launch, so rate movements never shift a target silently.

Only currencies both Google Ads bills in and the ECB publishes are supported; any other is a
named refusal, never a guessed rate.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_SOURCE = "ECB euro foreign exchange reference rates"
# Google Ads' minimum unit per currency in micros (developers.google.com/google-ads/api/data/
# codes-formats#currency-codes), limited to the currencies the ECB publishes a rate for.
MINIMUM_UNIT_MICROS = {
    "AUD": 10_000,
    "BRL": 10_000,
    "CAD": 10_000,
    "CHF": 10_000,
    "CNY": 10_000,
    "CZK": 10_000,
    "DKK": 10_000,
    "EUR": 10_000,
    "GBP": 10_000,
    "HKD": 10_000,
    "HUF": 10_000,
    "IDR": 1_000_000,
    "ILS": 10_000,
    "INR": 10_000,
    "JPY": 1_000_000,
    "KRW": 1_000_000,
    "MXN": 10_000,
    "MYR": 10_000,
    "NOK": 10_000,
    "NZD": 10_000,
    "PHP": 10_000,
    "PLN": 10_000,
    "RON": 10_000,
    "SEK": 10_000,
    "SGD": 10_000,
    "THB": 10_000,
    "TRY": 10_000,
    "USD": 10_000,
    "ZAR": 10_000,
}
SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "INR": "₹",
    "KRW": "₩",
    "ILS": "₪",
    "AUD": "A$",
    "CAD": "CA$",
    "NZD": "NZ$",
    "HKD": "HK$",
    "SGD": "S$",
    "MXN": "MX$",
    "BRL": "R$",
}
USD_FX = {"currency_code": "USD", "usd_rate": "1", "source": "identity", "date": None}
_MICROS = Decimal(1_000_000)
_CUBE = re.compile(r"<Cube\s+currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9.]+)['\"]\s*/>")
_TIME = re.compile(r"<Cube\s+time=['\"](\d{4}-\d{2}-\d{2})['\"]")


class RateUnavailable(Exception):
    """The ECB rate could not be read; the code names why, never the provider's text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def supported(code) -> bool:
    return isinstance(code, str) and code in MINIMUM_UNIT_MICROS


def unit(code: str) -> Decimal:
    return Decimal(MINIMUM_UNIT_MICROS[code]) / _MICROS


def unit_micros(code: str) -> int:
    return MINIMUM_UNIT_MICROS[code]


def code_of(fx: dict | None) -> str:
    return (fx or USD_FX)["currency_code"]


def parse_ecb(text: str) -> dict:
    """The day's euro rates, with EUR itself at one."""
    day = _TIME.search(text or "")
    rates = {code: rate for code, rate in _CUBE.findall(text or "")}
    if not day or "USD" not in rates:
        raise RateUnavailable("ecb_feed_unreadable")
    try:
        parsed = {code: Decimal(rate) for code, rate in rates.items()}
    except InvalidOperation:
        raise RateUnavailable("ecb_feed_unreadable") from None
    if any(not value.is_finite() or value <= 0 for value in parsed.values()):
        raise RateUnavailable("ecb_feed_unreadable")
    return {"date": day.group(1), "rates": {**rates, "EUR": "1"}}


def fx_for(code: str, feed: dict) -> dict:
    """How many units of `code` one US dollar buys, from one ECB feed (cross rate via EUR)."""
    if code == "USD":
        return dict(USD_FX)
    if not supported(code) or code not in feed["rates"]:
        raise RateUnavailable("currency_not_published")
    rate = Decimal(feed["rates"][code]) / Decimal(feed["rates"]["USD"])
    return {
        "currency_code": code,
        "usd_rate": str(rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)),
        "source": ECB_SOURCE,
        "date": feed["date"],
    }


async def read_ecb() -> str:
    """One fixed public read; the caller receipts the parsed rate."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(ECB_URL)
    except httpx.HTTPError:
        raise RateUnavailable("ecb_unreachable") from None
    if response.status_code != 200 or len(response.content) > 200_000:
        raise RateUnavailable(f"ecb_http_{response.status_code}")
    return response.text


def convert(usd, fx: dict | None, *, floor_usd=None) -> float:
    """A USD figure in the account's currency, on Google's minimum unit and never below
    `floor_usd`'s equivalent or one unit."""
    fx = fx or USD_FX
    code = fx["currency_code"]
    rate, step = Decimal(fx["usd_rate"]), unit(code)
    value = (Decimal(str(usd)) * rate).quantize(step, rounding=ROUND_HALF_UP)
    if floor_usd is not None:
        floor = (Decimal(str(floor_usd)) * rate).quantize(step, rounding=ROUND_CEILING)
        value = max(value, floor, step)
    return float(value)


def round_to_unit(amount, code: str) -> float:
    return float(Decimal(str(amount)).quantize(unit(code), rounding=ROUND_HALF_UP))


def in_usd(amount, fx: dict | None) -> float:
    fx = fx or USD_FX
    return float(Decimal(str(amount)) / Decimal(fx["usd_rate"]))


def money(amount, code: str | None, *, cents: bool | None = None) -> str:
    """`$20.00`, `€1,250`, `¥3,000`, `TRY 650.00`; whole units for large or unit-less amounts."""
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return "unknown"
    if number != number:
        return "unknown"
    code = code if supported(code) else None
    whole = (code is not None and unit(code) >= 1) or (cents is not True and abs(number) >= 100)
    text = f"{number:,.0f}" if whole else f"{number:,.2f}"
    if code is None:
        return f"{text} (account currency)"
    symbol = SYMBOLS.get(code)
    return f"{symbol}{text}" if symbol else f"{code} {text}"


def with_usd(amount, fx: dict | None) -> str:
    """The account amount, followed by its dollar equivalent when the account is not in USD."""
    code = code_of(fx)
    shown = money(amount, code)
    if code == "USD":
        return shown
    return f"{shown} (about {money(in_usd(amount, fx), 'USD')})"


def rate_line(fx: dict | None) -> str:
    fx = fx or USD_FX
    if fx["currency_code"] == "USD":
        return "Amounts are in US dollars, the account's currency."
    return (
        f"Amounts are in {fx['currency_code']}, the account's currency, converted once from the "
        f"assessment's US dollar figures at {fx['usd_rate']} {fx['currency_code']} per dollar "
        f"({fx['source']}, {fx['date']}). Tin never converts them again."
    )
