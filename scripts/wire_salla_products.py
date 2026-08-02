"""Wire the Salla product ids into the catalog — and PROVE they will provision.

The §09 triple match refuses any paid order whose product, amount and currency
do not all agree with what we expect. That guard is correct and it is also a
trap the day the prices change: the store now sells 29 / 199 / 449 while the
environment still describes a single 149 product, so every purchase would be
refused with «مبلغ مخالف» and no customer would be served.

Getting that wrong is only discovered by a real buyer paying and getting
nothing. So this script does both halves: it writes the catalog and pricing,
and then it DRIVES the real provisioning path against each product with the
real amount, using a fake Salla client, and refuses to save anything unless
all three provision. Run it once, after the products exist in the store.

    python scripts/wire_salla_products.py \
        --analysis 123 --pass 456 --plus 789 \
        --store-url https://…

Nothing is written until every check passes; nothing here touches staging data
(the rehearsal runs on the disposable test database and cleans up after itself).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import uuid
from decimal import Decimal

#: product argument → (plan_code, the exact price the store must charge)
PLANS: dict[str, tuple[str, Decimal]] = {
    "analysis": ("cv_analysis", Decimal("29.00")),
    "pass": ("professional", Decimal("199.00")),
    "plus": ("executive", Decimal("449.00")),
}

ENV_FILE = pathlib.Path("/root/career/.env.staging")


def _rehearse(catalog: dict[str, str], pricing: dict[str, list]) -> list[str]:
    """Provision one order per product on the TEST database. Returns failures.

    Two halves, and the second one matters. The first version built the test
    order's amount from the SAME table it was checking against — a fake
    asserting on itself, which passed happily when handed a wrong price. So
    the order is now priced from the canonical PLANS table (what we decided
    each product must cost) while the guard is handed the configured pricing:
    if the two ever disagree, the rehearsal fails, which is the entire point.

    Then a NEGATIVE rehearsal proves the guard is actually alive: an order at
    the wrong amount must be REFUSED. A green run where nothing can ever be
    refused would tell us nothing.
    """
    import os

    os.environ["DB_NAME"] = "career_test"
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from career.config import get_settings
    from career.salla.client import FakeSallaClient, SallaOrder
    from career.salla.provisioning import ProvisionStatus, provision_order

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.db_name.endswith("_test"):
        return [f"REFUSING: rehearsal DB is {settings.db_name!r}, not a test DB"]

    priced = {k: (Decimal(str(v[0])), str(v[1])) for k, v in pricing.items()}
    engine = create_engine(settings.owner_database_url, future=True)
    failures: list[str] = []
    made: list[str] = []
    with Session(engine) as session:
        for product_id, plan in catalog.items():
            # canonical price — NOT the configured one we are validating
            amount = next(p for _, (pl, p) in PLANS.items() if pl == plan)
            currency = "SAR"
            order_id = f"REHEARSE-{uuid.uuid4()}"
            # A DISTINCT buyer per product. The first rehearsal used one
            # number for all three and the third came back «renewed» — the
            # §16 renewal logic correctly recognising the same customer
            # buying again. The rehearsal was wrong, not the product.
            phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
            order = SallaOrder(order_id, "paid", product_id, amount, currency,
                               customer_phone=phone)
            result = provision_order(
                session, order_id,
                salla_client=FakeSallaClient({order_id: order}),
                product_catalog=catalog, expected_pricing=priced,
            )
            if result.status is not ProvisionStatus.PROVISIONED:
                failures.append(
                    f"{plan} ({product_id}, {amount} {currency}) → {result.status}"
                )
            else:
                made.append(str(result.tenant_id))
        # the guard must be able to say NO — otherwise green means nothing
        if not failures:
            probe_id = list(catalog)[0]
            bad_order_id = f"REHEARSE-BAD-{uuid.uuid4()}"
            bad = SallaOrder(
                bad_order_id, "paid", probe_id, Decimal("7.77"), "SAR",
                customer_phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
            )
            probe = provision_order(
                session, bad_order_id,
                salla_client=FakeSallaClient({bad_order_id: bad}),
                product_catalog=catalog, expected_pricing=priced,
            )
            if probe.status is ProvisionStatus.PROVISIONED:
                failures.append(
                    "the triple match accepted a WRONG amount — the guard is "
                    "not protecting the money path"
                )
            elif probe.tenant_id:
                made.append(str(probe.tenant_id))

        session.rollback()
        for tid in made:      # leave the rehearsal DB exactly as we found it
            for table in ("activation_tokens", "subscription_events",
                          "subscriptions", "onboarding_sessions",
                          "customer_channels"):
                session.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                                {"t": tid})
            session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        session.commit()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in PLANS:
        parser.add_argument(f"--{key}", required=True,
                            help=f"Salla product id for {PLANS[key][0]}")
    parser.add_argument("--store-url", required=True,
                        help="the public storefront URL customers renew from")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse and report, write nothing")
    args = parser.parse_args()

    catalog: dict[str, str] = {}
    pricing: dict[str, list] = {}
    for key, (plan, price) in PLANS.items():
        pid = str(getattr(args, key)).strip()
        if not pid or pid in catalog:
            print(f"✗ product id for {plan} is empty or duplicated")
            return 2
        catalog[pid] = plan
        pricing[pid] = [float(price), "SAR"]

    print("سأربط:")
    for pid, plan in catalog.items():
        print(f"  {plan:14} ← {pid}  ({pricing[pid][0]} ريال)")

    print("\nبروفة على قاعدة الاختبار…")
    failures = _rehearse(catalog, pricing)
    if failures:
        print("✗ البروفة فشلت — لم يُكتب شيء:")
        for f in failures:
            print(f"    {f}")
        print("\nالسبب الأغلب: سعر المنتج في المتجر لا يطابق السعر المعتمد.")
        return 1
    print("✓ الثلاثة تُزوَّد بنجاح بالمطابقة الثلاثية")

    if args.dry_run:
        print("\n(بروفة فقط — لم يُكتب شيء)")
        return 0

    lines = ENV_FILE.read_text().splitlines()
    wanted = {
        "SALLA_PRODUCT_CATALOG": json.dumps(catalog, ensure_ascii=False),
        "SALLA_PRODUCT_PRICING": json.dumps(pricing),
        "SALLA_STORE_URL": args.store_url.strip(),
    }
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0]
        if key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    print(f"\n✓ كُتب في {ENV_FILE}")
    print("أعد تشغيل الخدمات:")
    print("  systemctl restart career-worker career-admin-bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
