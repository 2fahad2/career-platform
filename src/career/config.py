"""Application settings, loaded from the environment.

Secrets come from an untracked .env (never committed — §15.15). Staging and
production load DISTINCT files with DISTINCT credentials; the two environments
share nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The longest a price-test window may be opened for, in days. The window
#: suppresses the founder price lock (see `career.promises.price_lock`), so
#: while it is open no new customer records the price they bought at — a real
#: cost, paid deliberately for a few days and never for a season. The wiring
#: tool refuses a longer window and the boot check escalates one it finds.
PRICE_TEST_MAX_DAYS = 14


class PriceTestState(StrEnum):
    """What ``SALLA_PRICE_TEST_UNTIL`` says about today.

    Four states rather than a boolean, and the reason is the same one
    :class:`career.engine.cli.TokenState` was split for: «no window» and «a
    window nobody can read» are different facts with different instructions,
    and collapsing them means printing one of the two answers for both.
    """

    #: No window was ever opened. THE DEFAULT — see :func:`price_test_window`.
    ABSENT = "absent"
    #: Today is on or before the last day of the window.
    OPEN = "open"
    #: A window was opened and its last day has passed. Identical in effect to
    #: ABSENT (locks are captured again); kept distinct because the boot check
    #: has something to say about an expired window over cheap products.
    EXPIRED = "expired"
    #: Somebody typed something that is not a date. Treated as OPEN — see
    #: :func:`price_test_window` for why that direction and not the other.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class PriceTestWindow:
    """The answer to «are the store's products at a test price right now».

    It cannot be derived from anything the code can observe: during the test
    the environment's pricing map matches what Salla charges exactly, so every
    automatic check agrees the configuration is correct — which it is. The
    only thing that knows a 1.00 SAR sale is a payment-gateway rehearsal
    rather than a sale is the operator, so this is his declaration and nothing
    else.
    """

    state: PriceTestState
    #: The last day the window covers, inclusive. None unless it parsed.
    last_day: date | None = None
    #: Exactly what the environment held, for an operator-facing report.
    raw: str = ""

    @property
    def open(self) -> bool:
        return self.state is PriceTestState.OPEN

    @property
    def suppresses_capture(self) -> bool:
        """May a founder price lock be captured today?

        UNREADABLE counts as open, and that is the whole of the fail-safe
        choice. The two ways to be wrong are not symmetric: a lock NOT captured
        costs a customer nothing today (his renewal at the real price captures
        it then, and a lock only ever matters after a price RISE), while a lock
        captured at a test price is a standing authorisation to buy the real
        product for one riyal AND the thing that makes his next real payment
        fail closed and TERMINAL. So an unparseable date suppresses, and the
        boot check reports it every morning until it is fixed or cleared.
        """
        return self.state in (PriceTestState.OPEN, PriceTestState.UNREADABLE)

    def days_remaining(self, today: date) -> int | None:
        return None if self.last_day is None else (self.last_day - today).days


def price_test_window(raw: str | None, today: date) -> PriceTestWindow:
    """Read ``SALLA_PRICE_TEST_UNTIL`` against a day.

    A DATE and not a flag, deliberately. A boolean switch has to be turned off
    by the same hand that turned it on, at a moment when the interesting part
    is already over — and «no new customer records the price he paid» is
    exactly the kind of quiet that survives a forgotten switch for months. A
    date closes itself: the day after it passes, capture is armed again with
    no human step. What that self-closing must NOT do is silently re-arm over
    products that are still cheap, which is why it has a second half in
    `engine.cli.verify_environment` — an expired window over a below-approved
    price escalates every morning.

    An EMPTY or missing value is ABSENT, i.e. no window at all. That is the
    default in `Settings` and the default here, so a host that has never heard
    of this variable behaves exactly as it did before it existed.
    """
    text = (raw or "").strip()
    if not text:
        return PriceTestWindow(PriceTestState.ABSENT)
    try:
        last_day = date.fromisoformat(text[:10])
    except ValueError:
        return PriceTestWindow(PriceTestState.UNREADABLE, raw=text)
    state = (PriceTestState.OPEN if today <= last_day
             else PriceTestState.EXPIRED)
    return PriceTestWindow(state, last_day=last_day, raw=text)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["staging", "production"] = Field(default="staging", alias="CAREER_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Postgres — application role (NOT a superuser; RLS applies to it).
    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(default="career", alias="DB_NAME")
    db_user: str = Field(default="career_app", alias="DB_USER")
    db_password: str = Field(default="", alias="DB_PASSWORD")

    # Postgres — owner/migration role (runs Alembic; owns tables).
    db_owner_user: str = Field(default="career_owner", alias="DB_OWNER_USER")
    db_owner_password: str = Field(default="", alias="DB_OWNER_PASSWORD")

    # Redis
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")

    # Object storage (filesystem-backed StorageAdapter for now).
    storage_root: str = Field(default="./data", alias="STORAGE_ROOT")

    # Malware scanning of customer uploads — §11 step 3, the control that was
    # an interface with nothing behind it for the whole live period (the runner
    # injected a stand-in whose scan() returned None, i.e. «clean», for every
    # file). An EMPTY socket path is a real, declared posture and not a
    # misconfiguration: career.onboarding.upload.build_scanner returns the
    # UnconfiguredScanner, the declared ScanPolicy accepts the file and stamps
    # the row `unscanned` rather than `clean`, and the watchtower shows the
    # missing engine for as long as it stays missing.
    #
    # These live here rather than being read out of os.environ inside
    # build_scanner so that the runners pass them in explicitly: a
    # CV_SCAN_TIMEOUT_S that is not a number then fails at boot with the rest
    # of the environment, instead of raising ValueError inside a customer's
    # first upload; and the path itself is proven at boot by the health line
    # the worker logs beside its environment report.
    cv_scan_clamd_socket: str = Field(default="", alias="CV_SCAN_CLAMD_SOCKET")
    cv_scan_timeout_s: float = Field(default=8.0, alias="CV_SCAN_TIMEOUT_S")

    # Anthropic — the only LLM provider (locked). Unused until C7.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # SearchAPI.io — Google Jobs discovery source (C6, deviation D13).
    searchapi_api_key: str = Field(default="", alias="SEARCHAPI_API_KEY")

    # §14 cost metering — provider list prices the operator must be able to
    # correct without a deploy (§06: «قابلة للضبط في الإعدادات لا في الكود»).
    # SearchAPI.io: USD per google_jobs search credit.
    searchapi_usd_per_search: float = Field(
        default=0.004, alias="SEARCHAPI_USD_PER_SEARCH"
    )
    # Meta prices a template message per CATEGORY and per market (Saudi
    # Arabia); marketing costs multiples of utility, so they are separate.
    whatsapp_usd_per_utility_message: float = Field(
        default=0.0157, alias="WHATSAPP_USD_PER_UTILITY_MESSAGE"
    )
    whatsapp_usd_per_marketing_message: float = Field(
        default=0.0384, alias="WHATSAPP_USD_PER_MARKETING_MESSAGE"
    )

    # Engine caps (§06: «قابلة للضبط في الإعدادات لا في الكود»).
    engine_max_per_query: int = Field(default=5, alias="ENGINE_MAX_PER_QUERY")
    engine_retrieval_cap: int = Field(default=50, alias="ENGINE_RETRIEVAL_CAP")
    engine_enrich_cap: int = Field(default=30, alias="ENGINE_ENRICH_CAP")

    # Salla — billing/webhooks (C3). Filled when the Partner App is created.
    salla_webhook_secret: str = Field(default="", alias="SALLA_WEBHOOK_SECRET")
    salla_api_key: str = Field(default="", alias="SALLA_API_KEY")
    # product id → plan code JSON map — decides funnel vs subscription
    # (audit fix: lived only as a raw env read in the worker script).
    salla_product_catalog: str = Field(default="{}", alias="SALLA_PRODUCT_CATALOG")
    # product id → [amount, currency] JSON — the §09 triple-match prices
    salla_product_pricing: str = Field(default="{}", alias="SALLA_PRODUCT_PRICING")
    # captured access-token expiry (ISO date) — manual until auto-refresh lands
    salla_token_expires_at: str = Field(default="", alias="SALLA_TOKEN_EXPIRES_AT")
    # The 1-riyal payment-gateway proof: the LAST DAY (ISO, Riyadh) on which
    # the three real products are knowingly selling at a test price. Empty —
    # the default — means «no window», which is the posture of every host that
    # is not mid-test. While it is open no founder price lock is captured
    # (`career.promises.price_lock.capture`), because a lock recorded from a
    # test price would authorise buying the real product for that price
    # forever and would fail the founder's next real payment closed.
    salla_price_test_until: str = Field(default="", alias="SALLA_PRICE_TEST_UNTIL")
    # the storefront the customer renews from (§16). Empty until the store is
    # published — every message that would carry it degrades to no link
    # rather than printing a broken one.
    salla_store_url: str = Field(default="", alias="SALLA_STORE_URL")

    # WhatsApp Cloud API (C4). Filled when the Meta app + WABA are set up.
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    whatsapp_verify_token: str = Field(default="", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_number_e164: str = Field(default="", alias="WHATSAPP_NUMBER_E164")
    whatsapp_waba_id: str = Field(default="", alias="WHATSAPP_WABA_ID")

    # Telegram admin channel (C4). Bot token must be rotated before live use.
    telegram_admin_bot_token: str = Field(default="", alias="TELEGRAM_ADMIN_BOT_TOKEN")
    telegram_admin_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_CHAT_ID")

    # Canary phase: the operator's own WhatsApp number (evening window nudge).
    canary_test_phone: str = Field(default="", alias="CANARY_TEST_PHONE")

    # Loopback port Caddy proxies to — the watchtower asks the API what code
    # it is actually running through it.
    api_publish_port: int = Field(default=8000, alias="API_PUBLISH_PORT")

    def _dsn(self, user: str, password: str) -> str:
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def app_database_url(self) -> str:
        """DSN for the application role — subject to RLS."""
        return self._dsn(self.db_user, self.db_password)

    @property
    def owner_database_url(self) -> str:
        """DSN for the owner/migration role — used by Alembic only."""
        return self._dsn(self.db_owner_user, self.db_owner_password)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # audit fix: the exact-literal log scrubber existed but was never armed —
    # register every real secret so even pattern-missed echoes get redacted.
    from career.logging_filters import register_secret

    for value in (
        settings.db_password, settings.db_owner_password,
        settings.redis_password, settings.salla_webhook_secret,
        settings.salla_api_key, settings.anthropic_api_key,
        settings.searchapi_api_key, settings.whatsapp_access_token,
        settings.whatsapp_app_secret, settings.whatsapp_verify_token,
        settings.telegram_admin_bot_token,
    ):
        register_secret(value)
    return settings
