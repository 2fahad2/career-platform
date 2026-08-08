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

THE TEST PRICE. To prove the payment gateway with real money the three REAL
products are dropped to a token price for a few days, real people buy, and the
prices are restored. The pricing map MUST follow the store — a §09 triple
match against 199 while Salla charges 1.00 refuses every one of those orders —
so the same mandatory rehearsal runs at the test price:

    python scripts/wire_salla_products.py \
        --analysis 123 --pass 456 --plus 789 --store-url https://… \
        --test-price 1.00 --test-until 2026-08-12

`--test-price` and `--test-until` are ONE flag in two halves and neither is
accepted alone. The date is written to `SALLA_PRICE_TEST_UNTIL` in the SAME
atomic write as the cheap pricing map, and running without `--test-price`
CLEARS it in the same way — so «the store is at a test price» and «the founder
price lock is suspended» are one fact with one writer and can never drift
apart. That matters more than it looks: a 1.00 SAR purchase captured as a
customer's founding price is a standing authorisation to buy the real pass for
one riyal, and it fails that customer's next real payment closed and TERMINAL.
See `career.promises.price_lock.capture`.

Nothing is written until every check passes; nothing here touches staging data
(the rehearsal runs on the disposable test database and cleans up after itself).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

#: product argument → (plan_code, the exact price the store must charge)
#:
#: THE CANONICAL TABLE, and a test price never touches it. `career.engine.cli
#: .canonical_sale_plans` imports this module and reads exactly this dict to
#: answer «what does a pass cost» when it checks the environment against
#: `plan_entitlements`; rewriting it for the test window would make the two
#: authorities agree on 1.00 SAR and the boot check would go silent on the one
#: configuration it exists to shout about.
PLANS: dict[str, tuple[str, Decimal]] = {
    "analysis": ("cv_analysis", Decimal("29.00")),
    "pass": ("professional", Decimal("199.00")),
    "plus": ("executive", Decimal("449.00")),
}

ENV_FILE = pathlib.Path("/root/career/.env.staging")

#: The window may not be opened for longer than this. Imported from the same
#: place the boot check reads it, so there is one number.
try:                                       # the script also runs on a host
    from career.config import PRICE_TEST_MAX_DAYS  # where src/ is installed
except Exception:                          # noqa: BLE001 — a bound, not a dependency
    PRICE_TEST_MAX_DAYS = 14


def _rehearse(
    catalog: dict[str, str], pricing: dict[str, list[object]],
    plan_prices: dict[str, Decimal],
) -> list[str]:
    """Provision one order per product on the TEST database. Returns failures.

    Two halves, and the second one matters. The first version built the test
    order's amount from the SAME table it was checking against — a fake
    asserting on itself, which passed happily when handed a wrong price. So
    the order is now priced from ``plan_prices`` (what we decided each product
    must cost — the canonical table, or the test price when one is declared)
    while the guard is handed the configured pricing: if the two ever
    disagree, the rehearsal fails, which is the entire point.

    ``plan_prices`` is a PARAMETER rather than a read of the module global for
    exactly that reason: the test-price run must rehearse the price the store
    is actually charging, and it must do it through the same proof, not
    through a shortcut that writes without proving.

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
            # decided price — NOT the configured one we are validating
            amount = plan_prices[plan]
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
            # DERIVED from the price under test, never a fixed 7.77: at a test
            # price of 7.77 the negative rehearsal would be an order at the
            # correct amount, would provision, and would report the guard as
            # broken — or, one refactor later, report nothing at all.
            wrong = plan_prices[catalog[probe_id]] + Decimal("7.77")
            bad = SallaOrder(
                bad_order_id, "paid", probe_id, wrong, "SAR",
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


def prices_for_window(
    test_price: Decimal | None, until: date | None, today: date
) -> tuple[dict[str, Decimal], list[str]]:
    """The prices to wire, and every reason this is not allowed to be wired.

    The refusals are the point. This is the one tool that writes a below-real
    price into the environment, so it is the one place that can guarantee the
    cheap map and the open window are never separated — a cheap map with NO
    window is the trap itself (every buyer's founding price recorded as the
    test price, permanently), and it is the state a hand-edited file falls
    into by default.
    """
    canonical = {plan: price for plan, price in PLANS.values()}
    problems: list[str] = []
    if test_price is None and until is None:
        return canonical, problems
    if (test_price is None) != (until is None):
        problems.append(
            "--test-price and --test-until are one flag in two halves: a test "
            "price with no closing date suspends the founder price lock "
            "indefinitely, and a date with no test price means nothing"
        )
        return canonical, problems
    if test_price is None or until is None:      # narrowed above
        return canonical, problems
    if test_price <= Decimal("0"):
        problems.append("--test-price must be a positive amount")
    for plan, price in sorted(canonical.items()):
        if test_price >= price:
            problems.append(
                f"--test-price {test_price} is not below the real price of "
                f"{plan} ({price}) — that is a price CHANGE, not a test, and "
                "it belongs in PLANS with the whitepaper edited first"
            )
    if until < today:
        problems.append(
            f"--test-until {until} is in the past: the window would be closed "
            "before the products were cheap, and every purchase would record "
            "the test price as its buyer's founding price"
        )
    elif (until - today).days > PRICE_TEST_MAX_DAYS:
        problems.append(
            f"--test-until {until} is {(until - today).days} days away; the "
            f"maximum is {PRICE_TEST_MAX_DAYS}. While the window is open NO "
            "customer records the price they bought at, and a season of that "
            "is a broken promise rather than a rehearsal"
        )
    if problems:
        return canonical, problems
    return {plan: test_price for plan in canonical}, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in PLANS:
        parser.add_argument(f"--{key}", required=True,
                            help=f"Salla product id for {PLANS[key][0]}")
    parser.add_argument("--store-url", required=True,
                        help="the public storefront URL customers renew from")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse and report, write nothing")
    parser.add_argument(
        "--test-price", default=None,
        help="the token price the three REAL products are temporarily on, to "
             "prove the payment gateway with real money (e.g. 1.00). Requires "
             "--test-until.",
    )
    parser.add_argument(
        "--test-until", default=None,
        help="ISO date (Riyadh), inclusive, of the LAST day of that window. "
             "Written to SALLA_PRICE_TEST_UNTIL, which suspends founder price "
             "lock capture; omitting --test-price CLEARS it.",
    )
    args = parser.parse_args()

    today = date.today()
    try:
        test_price = (None if args.test_price is None
                      else Decimal(str(args.test_price)))
    except InvalidOperation:
        print(f"✗ --test-price {args.test_price!r} is not an amount")
        return 2
    try:
        until = (None if args.test_until is None
                 else date.fromisoformat(str(args.test_until)))
    except ValueError:
        print(f"✗ --test-until {args.test_until!r} is not an ISO date")
        return 2

    plan_prices, refusals = prices_for_window(test_price, until, today)
    if refusals:
        print("✗ لن أكتب شيئًا:")
        for line in refusals:
            print(f"    {line}")
        return 2

    catalog: dict[str, str] = {}
    pricing: dict[str, list[object]] = {}
    for key, (plan, _canonical) in PLANS.items():
        pid = str(getattr(args, key)).strip()
        if not pid or pid in catalog:
            print(f"✗ product id for {plan} is empty or duplicated")
            return 2
        catalog[pid] = plan
        pricing[pid] = [float(plan_prices[plan]), "SAR"]

    print("سأربط:")
    for pid, plan in catalog.items():
        print(f"  {plan:14} ← {pid}  ({pricing[pid][0]} ريال)")
    if until is not None:
        print("\n⚠️  سعر اختبار — قفل سعر المؤسسين معطّل حتى:")
        print(f"    {until}")
        print("    تأكد أن أسعار المنتجات في سلة هي نفسها قبل التشغيل.")
    else:
        print("\nالأسعار الحقيقية — ونافذة سعر الاختبار ستُغلق بهذه الكتابة.")

    print("\nبروفة على قاعدة الاختبار…")
    failures = _rehearse(catalog, pricing, plan_prices)
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
        # ONE write for both halves. An empty value is a closed window, so a
        # restoring run does not merely stop writing the date — it clears it,
        # and there is no ordering in which the environment holds a cheap price
        # with the lock armed.
        "SALLA_PRICE_TEST_UNTIL": "" if until is None else until.isoformat(),
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
    # Constant 7's atomic discipline, applied to the one file that has no
    # copy anywhere: .env.staging is gitignored AND deliberately excluded from
    # the backup, and it is the EnvironmentFile for all three units. A crash
    # midway through a plain truncating write would take every secret on the
    # server with it. Backup first, then temp → fsync → atomic replace.
    import os
    import tempfile

    backup = ENV_FILE.with_suffix(ENV_FILE.suffix + ".bak")
    backup.write_text(ENV_FILE.read_text())
    os.chmod(backup, 0o600)
    body = "\n".join(out) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(ENV_FILE.parent), prefix=".env.tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV_FILE)      # atomic within the filesystem
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    print(f"\n✓ كُتب في {ENV_FILE}")
    print(f"  نسخة الأمان:\n  {backup}")
    print("أعد تشغيل الخدمات:")
    print("  systemctl restart career-worker career-admin-bot")
    # The environment is read once, at process start, so nothing above has
    # happened yet from the customer's point of view. The order below is the
    # whole safety of the operation and it is printed HERE, where the operator
    # is standing, rather than in a document he would have to remember to open.
    if until is None:
        print("\nبعد إعادة التشغيل: أعد أسعار المنتجات في سلة إلى أسعارها"
              " الحقيقية.")
        print("لا تعدّلها قبل إعادة التشغيل: بين اللحظتين يكون المتجر أرخص من"
              " الإعدادات، وأسوأ ما قد يحدث رفض طلب بريال واحد — بينما العكس"
              " يرفض طلبًا حقيقيًا بمئتي ريال.")
    else:
        print("\nقبل إعادة التشغيل يجب أن تكون أسعار سلة قد نزلت فعلًا.")
        print("بعدها: اشترِ أنت أولًا قبل أن ترسل الرابط لأحد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
