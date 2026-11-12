"""The merchant payment command (python -m app.payments). No PayPal calls: review lists from the
database, and reconcile refuses to run without a complete Sandbox configuration."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app import payments as cli
from app.db import create_db_engine, session_factory
from app.domain.orders.models import Order
from tests.conftest import make_settings
from tests.fake_paypal import MERCHANT_ID


def test_review_lists_orders_needing_attention(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings(migrated_database_url))
    assert cli.main(["review"]) == 0
    assert "nothing needs attention" in capsys.readouterr().out


def test_review_shows_a_flagged_order(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.clock import FixedClock
    from tests.factories import V, make_product
    from tests.integration.test_order_service import cart_with, order_for  # reuse the order builders
    from tests.order_support import add_shipping
    from tests.reservation_builders import NOW

    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        make_product(session, "shell", variants=[V(sku="A-S", size="S", on_hand=5)])
        add_shipping(session)
        session.commit()
        clock = FixedClock(NOW)
        ref = order_for(session, cart_with(session, clock, {"A-S": 1}), clock).order.public_ref
        session.execute(
            update(Order)
            .where(Order.public_ref == ref)
            .values(status="needs_review", paypal_order_id="PP-1", review_reason="amount_mismatch")
        )
        session.commit()
    engine.dispose()

    monkeypatch.setattr(cli, "get_settings", lambda: make_settings(migrated_database_url))
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert ref in out
    assert "needs_review" in out
    assert "amount_mismatch" in out


def test_reconcile_refuses_without_the_sandbox_configuration(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(migrated_database_url, paypal_mode="live", paypal_merchant_id=MERCHANT_ID)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["reconcile", "a" * 32]) == 2
    assert "PAYPAL_MODE" in capsys.readouterr().err
