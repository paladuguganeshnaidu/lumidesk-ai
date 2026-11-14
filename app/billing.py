"""Stripe-hosted optional payment integration.

No browser-controlled prices, no charges without account configuration, and no
fulfillment from a success redirect. Only signed webhooks update records.
Billing does not grant additional AI allowances until an explicit product policy exists.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .db import PaymentOrder, StripeEvent, SessionLocal

LOG = logging.getLogger(__name__)
PRODUCTION_URL = "https://lumidesk-ai.onrender.com"
ZERO_DECIMAL = frozenset("bif clp djf gnf jpy kmf krw mgn pyg rwf ugx vnd vuv xaf xof xpf".split())


def _stripe():
    try:
        import stripe
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Stripe integration is not installed.") from exc
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key or not key.startswith(("sk_test_", "sk_live_")):
        raise HTTPException(status_code=503, detail="Stripe has not been configured by the site owner.")
    if key.startswith("sk_live_") and os.getenv("STRIPE_LIVE_ENABLED", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Live billing has not been enabled by the site owner.")
    stripe.api_key = key
    return stripe


def configuration() -> dict:
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    price = os.getenv("STRIPE_PRICE_ID", "").strip()
    webhook = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    database = os.getenv("DATABASE_URL", "").strip()
    mode = os.getenv("STRIPE_CHECKOUT_MODE", "payment").strip().lower()
    test = key.startswith("sk_test_")
    eligible = (key.startswith(("sk_live_", "sk_test_")) and price.startswith("price_")
                and webhook.startswith("whsec_") and database.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))
                and mode in ("payment", "subscription")
                and (test or os.getenv("STRIPE_LIVE_ENABLED", "").lower() == "true"))
    return {
        "ready": bool(eligible), "mode": mode if mode in ("payment", "subscription") else "payment",
        "environment": "test" if test else "live" if key.startswith("sk_live_") else "unconfigured",
        "purpose": "Optional supporter checkout; no additional AI access or academic claims are included.",
        "requires_sign_in": True,
    }


def price_info() -> dict:
    state = configuration()
    if not state["ready"]:
        return state
    stripe = _stripe()
    try:
        price = stripe.Price.retrieve(os.environ["STRIPE_PRICE_ID"])
    except Exception:
        LOG.exception("Could not retrieve the configured Stripe price")
        raise HTTPException(status_code=503, detail="Billing price is temporarily unavailable.")
    expected_live = state["environment"] == "live"
    recurring = state["mode"] == "subscription"
    if (not price.get("active") or bool(price.get("livemode")) != expected_live
            or bool(price.get("recurring")) != recurring
            or price.get("unit_amount") is None):
        raise HTTPException(status_code=503, detail="The configured Stripe Price does not match this billing mode.")
    amount_minor = int(price["unit_amount"])
    currency = str(price["currency"]).upper()
    divisor = 1 if currency.lower() in ZERO_DECIMAL else 100
    amount = f"{amount_minor / divisor:,.0f}" if divisor == 1 else f"{amount_minor / divisor:,.2f}"
    return {**state, "currency": currency, "amount_minor": amount_minor, "price_display": f"{currency} {amount}",
            "interval": (price.get("recurring") or {}).get("interval") if recurring else None}


def _base_url() -> str:
    # Never construct Checkout redirects from untrusted Host / X-Forwarded-Host.
    value = os.getenv("PUBLIC_BASE_URL", PRODUCTION_URL).strip().rstrip("/")
    if not value.startswith("https://") or "@" in value or "#" in value or "?" in value:
        raise HTTPException(status_code=503, detail="PUBLIC_BASE_URL must be an HTTPS URL.")
    return value


def create_checkout(user: dict) -> dict:
    plan = price_info()
    if not plan["ready"]:
        raise HTTPException(status_code=503, detail="Stripe checkout is not yet enabled.")
    stripe = _stripe()
    customer_email = str(user.get("email") or "")
    if not customer_email or not user.get("id"):
        raise HTTPException(status_code=401, detail="Sign in with Google to continue.")
    mode = plan["mode"]
    uid = int(user["id"])
    base = _base_url()
    opts = {
        "mode": mode, "line_items": [{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
        "customer_email": customer_email,
        "client_reference_id": str(uid),
        "metadata": {"lumidesk_user_id": str(uid)},
        "success_url": base + "/billing?checkout=returned",
        "cancel_url": base + "/billing?checkout=cancelled",
    }
    if mode == "subscription":
        opts["subscription_data"] = {"metadata": {"lumidesk_user_id": str(uid)}}
    try:
        session = stripe.checkout.Session.create(**opts)
        checkout_url = str(session.get("url") or "")
        if not checkout_url.startswith("https://checkout.stripe.com/"):
            raise ValueError("Stripe did not return a secure Checkout URL")
        with SessionLocal() as db:
            order = PaymentOrder(
                user_id=uid, stripe_session_id=str(session["id"]),
                mode=mode, status="pending", amount_minor=plan["amount_minor"],
                currency=plan["currency"], price_id=os.environ["STRIPE_PRICE_ID"],
            )
            db.add(order)
            db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("Stripe Checkout creation failed")
        raise HTTPException(status_code=502, detail="Checkout is unavailable. No charge was confirmed.") from exc
    return {"url": checkout_url, "test_mode": plan["environment"] == "test"}


def history(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        entries = db.scalars(select(PaymentOrder).where(PaymentOrder.user_id == user_id)
                             .order_by(PaymentOrder.created_at.desc()).limit(15)).all()
        return [{"status": o.status, "mode": o.mode, "currency": o.currency,
                 "amount_minor": o.amount_minor, "created_at": o.created_at.isoformat()} for o in entries]


def handle_webhook(payload: bytes, signature: str) -> dict:
    if len(payload) > 128 * 1024:
        raise HTTPException(status_code=413, detail="Webhook body too large.")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret.startswith("whsec_"):
        raise HTTPException(status_code=503, detail="Billing webhooks are not configured.")
    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe event.") from exc
    expect_live = os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_live_")
    if bool(event.get("livemode")) != expect_live:
        raise HTTPException(status_code=400, detail="Stripe event mode mismatch.")
    event_id = str(event.get("id") or "")
    if not event_id.startswith("evt_"):
        raise HTTPException(status_code=400, detail="Missing Stripe event ID.")
    kind = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object") or {})
    with SessionLocal() as db:
        if db.get(StripeEvent, event_id):
            return {"received": True, "duplicate": True}
        order = None
        if kind.startswith("checkout.session."):
            order = db.scalar(select(PaymentOrder).where(PaymentOrder.stripe_session_id == obj.get("id")))
            if order is None:
                raise HTTPException(status_code=503, detail="Checkout order not yet recorded. Retry webhook.")
            if kind == "checkout.session.completed":
                order.stripe_customer_id = str(obj.get("customer") or "") or order.stripe_customer_id
                order.stripe_subscription_id = str(obj.get("subscription") or "") or order.stripe_subscription_id
                order.stripe_payment_intent = str(obj.get("payment_intent") or "") or order.stripe_payment_intent
                if order.mode == "payment":
                    order.status = "paid" if obj.get("payment_status") == "paid" else "processing"
                else:
                    order.status = "processing"
            elif kind == "checkout.session.async_payment_succeeded":
                order.status = "paid"
            elif kind == "checkout.session.async_payment_failed":
                order.status = "failed"
        elif kind.startswith("customer.subscription."):
            subscription_id = obj.get("id")
            order = db.scalar(select(PaymentOrder).where(PaymentOrder.stripe_subscription_id == subscription_id))
            if order is None:
                # Subscription events can arrive before checkout.session.completed.
                # Do not acknowledge an unlinked event; Stripe retries it.
                raise HTTPException(status_code=503, detail="Subscription not yet linked. Retry webhook.")
            if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
                order.status = str(obj.get("status") or ("canceled" if kind.endswith(".deleted") else "processing"))
        elif kind == "charge.refunded":
            intent = obj.get("payment_intent")
            if intent:
                order = db.scalar(select(PaymentOrder).where(PaymentOrder.stripe_payment_intent == intent))
                if order is not None and obj.get("refunded"):
                    order.status = "refunded"
        db.add(StripeEvent(id=event_id, kind=kind))
        try:
            db.commit()
        except Exception:
            db.rollback()
            if db.get(StripeEvent, event_id):
                return {"received": True, "duplicate": True}
            raise
    return {"received": True}
