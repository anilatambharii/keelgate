"""The Stripe client: test mode only, optionally pointed at stripe-mock.

The client is typed ``Any`` on purpose. The SDK's request parameters are ``TypedDict``s with
hundreds of optional keys; building them as plain dicts keeps this code readable, and every
request shape is checked against Stripe's OpenAPI spec by the stripe-mock tests instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import stripe

from keelgate_cloud.config import TEST_MODE_KEY_PREFIX

if TYPE_CHECKING:
    from keelgate_cloud.config import Settings
    from keelgate_cloud.state import AppState

# Tests (and only tests) put a replacement client here: ``state.extras["stripe_client"]``.
EXTRAS_KEY = "stripe_client"


def make_client(settings: Settings) -> Any | None:
    """A Stripe client for the settings, or ``None`` when Stripe is not configured.

    Refuses a non-test key unless the owner opted in to live mode (``Settings`` enforces the
    same rule at start-up; this repeats it because a client is the thing that moves money).
    """
    key = settings.stripe_api_key
    if not key:
        return None
    if not key.startswith(TEST_MODE_KEY_PREFIX) and not settings.allow_live_stripe:
        raise ValueError("refusing to build a Stripe client with a non-test key")
    base = {"api": settings.stripe_api_base} if settings.stripe_api_base else None
    return stripe.StripeClient(key, base_addresses=base, max_network_retries=2)  # type: ignore[arg-type]  # SDK types base_addresses as a TypedDict


def client_for(state: AppState) -> Any | None:
    """The app's Stripe client (built once), or ``None`` when Stripe is not configured."""
    if EXTRAS_KEY not in state.extras:
        state.extras[EXTRAS_KEY] = make_client(state.settings)
    return state.extras[EXTRAS_KEY]
