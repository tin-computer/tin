"""Trusted fal usage, separate from video artifacts and Codex token accounting."""

from decimal import Decimal, InvalidOperation

CARD = {
    "id": "fal-studio-reported-cost-2026-09-16-v1",
    "provider": "fal",
    "endpoints": ["fal-ai/gemini-3.1-flash-tts", "fal-ai/whisper"],
    "basis": "provider_reported_cost",
    "source": "https://fal.ai/docs/platform-apis/v1/models/billing-events",
    # A liability ceiling, not an invented supplier rate. Excess is Tin's cost.
    "request_maximum_nanos": 500_000_000,
}
# Image-library drawings (image_library.py). The model is a per-project setting, so this card
# names the step rather than a fixed endpoint list. brand.image runs are Tin-funded today, so
# the observation is recorded on the receipt and no customer charge follows from it.
IMAGE_CARD = {
    "id": "fal-image-reported-cost-2026-11-19-v1",
    "provider": "fal",
    "step": "image",
    "basis": "provider_reported_cost",
    "source": "https://fal.ai/docs/platform-apis/v1/models/billing-events",
    "request_maximum_nanos": 2_000_000_000,
}


def card_for(record):
    """The rate card for one usage record: the image card for drawings, else Studio voice's."""
    return IMAGE_CARD if record.get("step") == "image" else CARD


def price(card, record):
    amount = record.get("reported_cost_nanos")
    endpoint_ok = (
        record.get("step") == "image"
        if card == IMAGE_CARD
        else record.get("endpoint") in CARD["endpoints"]
    )
    if (
        card not in (CARD, IMAGE_CARD)
        or record.get("provider") != "fal"
        or not endpoint_ok
        or record.get("outcome") != "cost_received"
        or not record.get("provider_request_id")
        or type(amount) is not int
        or amount < 0
    ):
        return None
    return amount, {
        "rate_card": card["id"],
        "basis": card["basis"],
        "provider": "fal",
        "endpoint": record["endpoint"],
        "provider_request_id": record["provider_request_id"],
    }


def billing_event(payload, *, request_id, endpoint):
    """Only one exact request/endpoint match; missing or inconsistent cost is unknown."""
    events = payload.get("billing_events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or len(events) != 1 or payload.get("has_more"):
        return None
    event = events[0]
    if not isinstance(event, dict) or (
        event.get("request_id") != request_id or event.get("endpoint_id") != endpoint
    ):
        return None
    nanos = event.get("cost_estimate_nano_usd")
    if type(nanos) is not int or nanos < 0:
        return None
    try:
        total = Decimal(str(event["cost_total"]))
        if not total.is_finite() or total < 0 or abs(total * 1_000_000_000 - nanos) > 1:
            return None
    except (KeyError, InvalidOperation, ValueError):
        return None
    return nanos
