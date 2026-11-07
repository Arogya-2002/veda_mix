"""Payment gateway interface and the Razorpay implementation.

Services depend on `PaymentGateway`, never on Razorpay directly, so tests use a fake
and a second gateway later means one new class, not edits across the code."""

import hashlib
import hmac
from functools import lru_cache
from typing import Protocol

import httpx

from veda.config.configuration import get_settings
from veda.exception import VedaException
from veda.logging import get_logger

log = get_logger(__name__)


class PaymentGateway(Protocol):
    key_id: str  # public; the React checkout needs it

    async def create_order(self, amount_paise: int, currency: str, receipt: str) -> str:
        """Create an order at the gateway; returns its order id."""
        ...

    def verify_payment(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Checkout callback signature: proves this payment belongs to this order."""
        ...

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        """Webhook signature over the raw request body."""
        ...


def _hmac_matches(secret: str, message: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)  # constant time: no timing leaks


class RazorpayGateway:
    API = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self.key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret

    async def create_order(self, amount_paise: int, currency: str, receipt: str) -> str:
        if not (self.key_id and self._key_secret):
            raise VedaException("PAYMENTS_NOT_CONFIGURED", "Payments are not set up", 503)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self.API}/orders",
                    auth=(self.key_id, self._key_secret),
                    json={"amount": amount_paise, "currency": currency, "receipt": receipt},
                )
            r.raise_for_status()
            return r.json()["id"]
        except httpx.HTTPError as e:
            log.error("razorpay_order_failed", error=str(e))
            raise VedaException("PAYMENT_GATEWAY_ERROR", "Could not start payment", 502) from e

    def verify_payment(self, order_id: str, payment_id: str, signature: str) -> bool:
        return _hmac_matches(self._key_secret, f"{order_id}|{payment_id}".encode(), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        return _hmac_matches(self._webhook_secret, body, signature)


@lru_cache
def get_payment_gateway() -> PaymentGateway:
    """FastAPI dependency; tests override it with a fake."""
    s = get_settings()
    return RazorpayGateway(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret)
