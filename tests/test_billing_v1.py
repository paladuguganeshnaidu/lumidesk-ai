"""Stripe Checkout and webhook regression tests. No real Stripe request is made."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import billing
from app.db import Base, PaymentOrder


def test_billing_is_unavailable_until_configured(monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert billing.configuration()["ready"] is False
    from app.main import app
    with TestClient(app) as client:
        page = client.get("/billing")
        assert page.status_code == 200
        assert "Optional payment" in page.text
        config = client.get("/api/billing/config")
        assert config.status_code == 200
        assert config.json()["ready"] is False
        assert "STRIPE_SECRET_KEY" not in json.dumps(config.json())
        assert client.post("/api/billing/checkout").status_code == 403
        assert client.post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "bad"}).status_code == 503


def test_checkout_and_signed_webhook_are_persisted_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured-remote")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example_for_mock")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_example_for_mock")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_example_for_mock")
    monkeypatch.setenv("STRIPE_CHECKOUT_MODE", "payment")
    monkeypatch.setenv("STRIPE_LIVE_ENABLED", "false")
    engine = create_engine("sqlite:///" + str(tmp_path / "billing-test.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(billing, "SessionLocal", factory)

    requests = []
    class FakePrice:
        @staticmethod
        def retrieve(price_id):
            assert price_id == "price_example_for_mock"
            return {"active": True, "livemode": False, "recurring": None, "unit_amount": 1200, "currency": "inr"}
    class FakeSession:
        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return {"id": "cs_test_example", "url": "https://checkout.stripe.com/c/pay/cs_test_example"}
    class FakeWebhook:
        @staticmethod
        def construct_event(payload, signature, secret):
            assert secret == "whsec_example_for_mock"
            if signature != "signed-by-stripe":
                raise ValueError("signature mismatch")
            return json.loads(payload)
    class SignatureVerificationError(Exception): pass
    fake = SimpleNamespace(Price=FakePrice, checkout=SimpleNamespace(Session=FakeSession),
                           Webhook=FakeWebhook, error=SimpleNamespace(SignatureVerificationError=SignatureVerificationError))
    monkeypatch.setattr(billing, "_stripe", lambda: fake)

    assert billing.price_info()["price_display"] == "INR 12.00"
    created = billing.create_checkout({"id": 123, "email": "test@example.com"})
    assert created["test_mode"] is True
    assert requests[0]["line_items"] == [{"price": "price_example_for_mock", "quantity": 1}]
    assert requests[0]["mode"] == "payment"
    assert requests[0]["client_reference_id"] == "123"
    assert requests[0]["success_url"] == "https://lumidesk-ai.onrender.com/billing?checkout=returned"
    assert len(billing.history(123)) == 1
    assert billing.history(234) == []

    event = {"id": "evt_example", "livemode": False, "type": "checkout.session.completed",
             "data": {"object": {"id": "cs_test_example", "customer": "cus_fake",
                                  "payment_status": "paid", "payment_intent": "pi_fake"}}}
    payload = json.dumps(event).encode()
    with pytest.raises(HTTPException) as bad:
        billing.handle_webhook(payload, "unsigned")
    assert bad.value.status_code == 400
    assert billing.history(123)[0]["status"] == "pending"
    assert billing.handle_webhook(payload, "signed-by-stripe") == {"received": True}
    assert billing.history(123)[0]["status"] == "paid"
    assert billing.handle_webhook(payload, "signed-by-stripe")["duplicate"] is True
    with factory() as db:
        order = db.scalar(select(PaymentOrder).where(PaymentOrder.stripe_session_id == "cs_test_example"))
        assert order.stripe_customer_id == "cus_fake"

    refund = {"id": "evt_refund", "livemode": False, "type": "charge.refunded",
              "data": {"object": {"payment_intent": "pi_fake", "refunded": True}}}
    billing.handle_webhook(json.dumps(refund).encode(), "signed-by-stripe")
    assert billing.history(123)[0]["status"] == "refunded"


def test_stripe_price_mode_mismatch_refuses_checkout(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example_for_mock")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_example")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_example")
    monkeypatch.setenv("STRIPE_CHECKOUT_MODE", "subscription")
    fake = SimpleNamespace(Price=SimpleNamespace(retrieve=lambda _: {"active": True, "livemode": False,
                "recurring": None, "unit_amount": 1500, "currency": "inr"}))
    monkeypatch.setattr(billing, "_stripe", lambda: fake)
    with pytest.raises(HTTPException) as err:
        billing.price_info()
    assert err.value.status_code == 503


def test_live_mode_requires_explicit_switch(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_fake")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setenv("STRIPE_CHECKOUT_MODE", "payment")
    monkeypatch.delenv("STRIPE_LIVE_ENABLED", raising=False)
    assert billing.configuration()["ready"] is False
