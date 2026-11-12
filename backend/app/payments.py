"""Merchant payment commands (M3.2, ADR-026). Sandbox only.

    python -m app.payments review            # orders whose payment needs attention
    python -m app.payments reconcile REF     # ask PayPal about one order and apply what it says

``reconcile`` works on an order in any status that has a PayPal order, so a verified payment that
appears after the order ended is found and flagged (``capture_after_end``). It never releases
stock or fulfils an order by hand: it applies only what PayPal's own records prove, through the
same finalization service as checkout. Output never includes payer data or credentials.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import or_, select

import app.models  # noqa: F401  (register every table)
from app.clock import SystemClock
from app.config import get_settings
from app.db import create_db_engine, session_factory, unit_of_work
from app.domain.orders.models import Order
from app.domain.payments.paypal import SandboxPayPalClient
from app.domain.payments.service import reconcile_by_reference

ATTENTION = ("capturing", "payment_pending", "needs_review")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.payments", description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("review", help="list orders whose payment needs attention")
    reconcile = commands.add_parser("reconcile", help="reconcile one order with PayPal")
    reconcile.add_argument("ref", help="the order reference (32 hex characters)")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        with session_factory(engine)() as session:
            if args.command == "review":
                with unit_of_work(session):
                    rows = session.execute(
                        select(Order.public_ref, Order.status, Order.review_reason, Order.status_changed_at)
                        .where(or_(Order.status.in_(ATTENTION), Order.review_reason.is_not(None)))
                        .order_by(Order.status_changed_at)
                    ).all()
                for row in rows:
                    changed = f"{row.status_changed_at:%Y-%m-%d %H:%M}"
                    print(f"{row.public_ref}  {row.status:<16} {row.review_reason or '-':<22} {changed}")
                if not rows:
                    print("nothing needs attention")
                return 0

            problem = settings.paypal_problem
            if problem is not None:
                print(f"payments are disabled: {problem}", file=sys.stderr)
                return 2
            gateway = SandboxPayPalClient(
                settings.paypal_client_id.strip(), settings.paypal_client_secret.get_secret_value().strip()
            )
            try:
                result = reconcile_by_reference(
                    session,
                    args.ref,
                    gateway=gateway,
                    merchant_id=settings.paypal_merchant_id.strip(),
                    clock=SystemClock(),
                )
            except LookupError:
                print("no order with that reference", file=sys.stderr)
                return 1
            finally:
                gateway.close()
            print(f"{args.ref}  status={result.order.status}  result={result.result}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
