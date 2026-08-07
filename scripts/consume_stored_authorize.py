"""Consume an ``app.store.authorize`` webhook that is still sitting in the DB.

WHY THIS SCRIPT EXISTS
----------------------
The worker consumes these events by itself (``provisioning._apply_authorize``).
Two situations leave one behind anyway, and both are live history:

1. Before CHANGELOG §29 the event was not handled at all — it fell to the
   ``else`` that marks a row ``ignored``. The 2026-07-15 delivery is still
   there, unread.
2. Since 2026-08-07 a credential belonging to a merchant OTHER than the one we
   hold — or to a merchant we cannot prove is the one we hold — is refused by
   ``tokens.store_credentials`` and the row is left ``received``. That refusal
   is deliberate: this platform holds exactly one credential, and repointing it
   at another store on the strength of one webhook is an outage nobody would
   see until real paid orders started failing. The decision belongs to a human,
   and this script is where the human takes it.

WHAT IT DOES NOT DO
-------------------
It does not re-implement the consumption. ``_apply_authorize`` owns the whole
sequence — read the signature verdict first, pull the fields off the shape
Salla actually sends, store, mark the row, alert — and a second copy of that
here would drift from the first and be wrong in exactly the way that costs a
credential. The only thing this script adds is the operator's confirmation,
injected as ``allow_store_change=True`` on the one call underneath it, and only
when ``--allow-store-change`` was typed.

**And it does not re-implement the PREDICTION either — that is the same rule
applied to the dry run, and it had to be learned twice.** This file used to
read the store id out of the payload with its own ``payload.get("merchant")``
while ``provisioning._offered_store_id`` had grown four more paths, a merchant
OBJECT, a deliberate refusal to read ``data.id`` (the APP id, identical on
every delivery), and a strict scalar pattern. Two readers, one payload, two
answers: the dry run could print «store (none in payload)» for a payload the
worker attributes perfectly, and — worse — its ``--allow-store-change`` gate
could open or close on a verdict the writer did not share, so the operator's
confirmation would be about a different decision from the one that executed.
**A dry run is a PROMISE about what the apply will do.** So every value the
promise rests on is imported from the module that will act on it:

* the store id — ``provisioning._offered_store_id``;
* the signature verdict — ``provisioning._verified``;
* «has a human already signed for this?» — ``provisioning._operator_confirmed``,
  asked about the very ``functools.partial`` that ``--apply`` installs;
* the expiry — ``tokens.parse_expiry``;
* the identity verdict — ``tokens.store_identity``, against the credential read
  the way the WRITER reads it (the file alone, never the process environment);
* and the outcome itself — :func:`_predict` runs ``tokens.store_credentials``,
  the real one, with its three write seams neutralised (see
  :func:`_writes_disabled`). The refusal, the STALE ordering guard, the
  UNCHANGED comparison and the resulting credential are the writer's own, not a
  second opinion about them.

It also does not restart anything. A running unit baked the old token into an
Authorization header at boot; the restart is the operator's, and the alert text
printed at the end says so.

SAFETY
------
Dry run is the default and prints exactly what would happen, including which
store the platform would end up serving. ``--apply`` is the only thing that
writes. No token, and no part of one, is printed by any path here: the
prediction handles the real values in memory and reports only booleans, a store
number and a date.

Run (from the host, where the staging database is published on 5433):

    .venv/bin/python scripts/consume_stored_authorize.py \
        --db-host 127.0.0.1 --db-port 5433                    # dry run
    .venv/bin/python scripts/consume_stored_authorize.py \
        --db-host 127.0.0.1 --db-port 5433 --apply --allow-store-change
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from typing import Any, NamedTuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import inspect, select  # noqa: E402
from sqlalchemy.orm import Session, load_only  # noqa: E402

from career.db.models import WebhookEvent  # noqa: E402
from career.db.session import engine_for  # noqa: E402
from career.logging_filters import install_secret_redaction  # noqa: E402
from career.salla import provisioning, tokens  # noqa: E402
from career.webhooks import intake  # noqa: E402

#: The status the worker itself treats as «waiting to be consumed» — the one
#: ``process_pending_webhooks`` selects on and the one
#: ``intake.credential_is_still_consumable`` keeps a body alive for. A row that
#: is ``ignored`` or ``failed`` is a decision something already took, so it is
#: not swept up by accident here either — name it with ``--event-id`` if you
#: really mean it. (Asserted against the worker's real sweep by
#: tests/test_consume_authorize.py rather than trusted as a matching literal.)
PENDING_STATUS = "received"

#: Every ``webhook_events`` column this script or ``_apply_authorize`` touches,
#: and NOTHING else.
#:
#: Not tidiness — necessity. The ORM model has run ahead of the staging
#: database: ``WebhookEvent`` declares ``next_attempt_at``, ``failure_kind``,
#: ``failure_detail``, ``subject_tenant_id`` and ``payload_redacted_at``, none
#: of which exist in ``career_staging`` yet, so the default SELECT — which asks
#: for every mapped column — dies on ``UndefinedColumn`` before it can read the
#: credential. Loading exactly what is used makes this tool work on both sides
#: of that gap, and it will keep working when the migrations land. (The drift
#: itself is not this script's to fix and no migration is run from here.)
NEEDED_COLUMNS = (
    "provider", "event_type", "signature_valid", "payload",
    "processing_status", "attempt_count", "received_at", "processed_at",
)

#: The three functions inside ``career.salla.tokens`` through which a credential
#: write reaches the world, and the ONLY reason :func:`_predict` may run the
#: real writer: with these replaced, ``store_credentials`` still takes every
#: decision it would really take and cannot touch the disk or this process.
#:
#: * ``_merged_body`` builds the file body from the ``wanted`` mapping — which
#:   IS the resulting credential, so capturing it is how the dry run reports the
#:   after-state without guessing at the inheritance rules;
#: * ``_atomic_write`` is the write itself, and the ONLY caller of
#:   ``_rotate_backup``/``_shred``, so stopping it stops the backup churn too;
#: * ``_publish_to_process_env`` mutates ``os.environ`` and clears the Settings
#:   cache — invisible on disk and still a change a dry run must not make.
#:
#: If any of them is renamed, :func:`_writes_disabled` refuses to run at all
#: rather than run the writer with a hole in the harness. A test asserts they
#: still exist, so the rename fails in CI and not on the operator's terminal.
_WRITE_SEAMS = ("_merged_body", "_atomic_write", "_publish_to_process_env")


class PrintingAdmin:
    """Show the operator alert instead of sending it.

    ``_apply_authorize`` builds the message that tells Fahad what landed and
    which two units must be restarted before anything uses it. That text is the
    most useful output this script has — but a manual run should not fire the
    admin channel, so it is printed where the operator already is. The alerts
    contain store ids and dates and never a credential; that is asserted by the
    zero-leakage tests, not assumed here.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_admin(self, text: str) -> str:
        self.messages.append(text)
        return "printed"


def _load_env(env_file: pathlib.Path) -> None:
    """Make the secrets file the process environment, without overriding it.

    ``career.config.Settings`` reads ``.env`` and the environment; a manual run
    on the host has neither, because ``.env.staging`` is a systemd
    ``EnvironmentFile`` and cannot be ``source``d by a shell (its values are
    not shell-quoted). Anything already exported wins, so running this inside a
    unit's environment behaves identically.
    """
    for key, value in dotenv_values(env_file).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def _held(env_file: pathlib.Path) -> tokens.SallaCredentials:
    """What the WRITER will compare against — the file, and only the file.

    Deliberately not ``tokens.current()``. That function falls back to the
    process environment when the file names no access token, and
    ``store_credentials`` explicitly does not (see its «THE FILE ONLY» comment):
    a run inside a unit whose environment still carries July's values would
    otherwise be told it holds a credential the writer is about to treat as
    absent — ``NO_INCUMBENT`` against ``SAME``, an identity verdict inverted in
    the report the operator is reading before he types ``--apply``.
    """
    return tokens._credentials_from(tokens._read_env_file(env_file))


def fingerprint(
    creds: tokens.SallaCredentials,
) -> tuple[str, str, datetime | None, str | None]:
    """Two credentials, comparable without either being rendered.

    The token values ARE compared — that is the point, a prediction that got
    the store right and the token wrong is still a lie — but only the equality
    survives into anything printable. Public because the tests compare a
    prediction with an apply the same way.
    """
    return (
        creds.access_token, creds.refresh_token, creds.expires_at,
        creds.store_id,
    )


def _describe(creds: tokens.SallaCredentials) -> str:
    """A credential rendered for a human. ``SallaCredentials.__repr__`` already
    refuses to show the values; this only adds the expiry in words."""
    if not creds.present:
        return "nothing stored"
    return (
        f"store {creds.store_id or '(never recorded)'}, expires "
        f"{creds.expires_at.isoformat() if creds.expires_at else '(unknown)'}"
    )


def _missing_columns(engine: Any) -> list[str]:
    present = {c["name"] for c in inspect(engine).get_columns("webhook_events")}
    return [name for name in NEEDED_COLUMNS if name not in present]


def _select(session: Session, event_id: str | None) -> WebhookEvent | None:
    stmt = select(WebhookEvent).options(
        load_only(*(getattr(WebhookEvent, name) for name in NEEDED_COLUMNS)),
    ).where(
        WebhookEvent.provider == provisioning.SALLA_PROVIDER,
        WebhookEvent.event_type == provisioning._AUTHORIZE_EVENT,
    )
    if event_id:
        stmt = stmt.where(WebhookEvent.id == uuid.UUID(event_id))
    else:
        stmt = stmt.where(
            WebhookEvent.signature_valid.is_(True),
            WebhookEvent.processing_status == PENDING_STATUS,
        )
    stmt = stmt.order_by(WebhookEvent.received_at.desc()).limit(1)
    return session.execute(stmt).scalars().first()


class Offered(NamedTuple):
    """The four values ``_apply_authorize`` pulls off the payload, pulled the
    same way and normalised the same way (a non-string access token becomes
    ``""``, a non-string refresh token becomes ``None`` — «inherit», which is
    not the same as ``""`` — «clear»).

    ``access`` is carried because the identity ladder uses it as proof of
    sameness, and because the writer needs it to answer at all.

    A ``NamedTuple`` and not a ``@dataclass`` for a reason that looks like
    trivia and is not: this file is loaded BY PATH, and ``@dataclass`` resolves
    its annotations through ``sys.modules[cls.__module__]`` — so in a loader
    that does not register the module first, a dataclass here raises «'NoneType'
    object has no attribute '__dict__'» before the first assertion. All three
    loaders in the suite now register before executing, so the trap is one
    forgotten line away rather than currently sprung; that is exactly why
    ``TestTheScriptStaysLoadable`` exercises the unregistered path on purpose.
    Staying a ``NamedTuple`` is what keeps that test cheap to keep.

    The generated ``__repr__`` is replaced, for the same reason
    ``SallaCredentials`` has none: a tuple that renders itself puts a live
    access token into every traceback frame and every pytest diff (constant 13).

    Both hiding methods are written out as ``def``. ``__str__ = __repr__`` —
    which is what ``SallaCredentials`` may legitimately write, because it is a
    ``@dataclass`` — is NOT safe here: inside a ``NamedTuple`` body an
    assignment is the syntax for «field with a default», so a type checker
    reads that line as a fifth field named ``__str__`` and stops seeing the
    class's rendering as methods at all. It happens to bind at runtime (the
    metaclass ``setattr``s every non-field name onto the generated class), so
    it was armour that worked while being invisible to the only tool that could
    tell us it had stopped working. Written as ``def``, the redaction is
    checkable — and TestNoContainerHereCanRenderItsSecret proves it binds.
    """

    access: str
    refresh: str | None
    expires_at: datetime | None
    store_id: str | None

    def __repr__(self) -> str:
        return (
            "Offered("
            f"access={'set' if self.access else 'unset'}, "
            f"refresh={'set' if self.refresh else ('none' if self.refresh is None else 'empty')}, "
            f"expires_at={self.expires_at.isoformat() if self.expires_at else None}, "
            f"store_id={self.store_id!r})"
        )

    def __str__(self) -> str:
        # Redundant at runtime — `object.__str__` already delegates to
        # `__repr__` — and kept because it is not redundant to a reader or to
        # `str()`-shaped call sites, and because it costs one line to be
        # explicit about which of the two rendering protocols is redacted.
        return self.__repr__()


def _offered(event: WebhookEvent) -> Offered:
    """Read the payload with the worker's own readers.

    The two keys named here (``data`` and its three fields) are the only thing
    left that is spelled out twice, and the drift test in
    tests/test_consume_authorize.py closes that gap by asserting these values
    equal the ones ``_apply_authorize`` actually hands to the writer.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    return Offered(
        access=access if isinstance(access, str) else "",
        refresh=refresh if isinstance(refresh, str) else None,
        expires_at=tokens.parse_expiry(data.get("expires")),
        # THE SINGLE READER. See the module docstring: this line is the whole
        # reason this file was rewritten.
        store_id=provisioning._offered_store_id(payload),
    )


@contextlib.contextmanager
def _writes_disabled() -> Iterator[list[dict[str, str]]]:
    """Run the real ``store_credentials`` with its hands tied.

    Yields the list of ``wanted`` mappings it tried to write — empty when it
    decided not to write at all, which is itself the answer to «would this
    change anything?».

    Refuses rather than degrades. If a seam has been renamed, the harness has a
    hole in it and the only safe move is to not run the writer: a dry run that
    quietly wrote the live credential file would be the worst possible failure
    of this entire script.
    """
    captured: list[dict[str, str]] = []
    originals: dict[str, Callable[..., Any]] = {}
    for name in _WRITE_SEAMS:
        original = getattr(tokens, name, None)
        if not callable(original):
            raise RuntimeError(
                f"career.salla.tokens.{name} is missing — this script can no "
                "longer prove a dry run writes nothing, so it refuses to run "
                "the credential writer at all. Update _WRITE_SEAMS."
            )
        originals[name] = original

    # The one seam whose RESULT is used, bound with its real signature instead
    # of read back out of the erased `originals` mapping. Not cosmetic: this is
    # the line that fails type checking if `_merged_body` ever stops being
    # «(existing, wanted) -> str», which is the shape `captured` depends on.
    real_merged_body: Callable[[list[str], dict[str, str]], str] = (
        tokens._merged_body
    )

    def _merged_body(existing: list[str], wanted: dict[str, str]) -> str:
        captured.append(dict(wanted))
        # The real body, so nothing downstream behaves differently — it is
        # simply handed to a `_atomic_write` that drops it.
        return real_merged_body(existing, wanted)

    def _atomic_write(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _publish_to_process_env(*_args: Any, **_kwargs: Any) -> None:
        return None

    # No `type: ignore` on these three. Each replacement is signature-compatible
    # with the seam it stands in for, and that is worth keeping true: an ignore
    # here would also silence the day one of them stops matching, which is the
    # day the harness has a hole in it and the dry run starts writing.
    tokens._merged_body = _merged_body
    tokens._atomic_write = _atomic_write
    tokens._publish_to_process_env = _publish_to_process_env
    try:
        yield captured
    finally:
        for name, original in originals.items():
            setattr(tokens, name, original)


class Prediction(NamedTuple):
    """What ``_apply_authorize`` will do, computed by the code that will do it.

    Every field is something the apply produces, so every field is assertable
    against the apply — which is exactly what the tests do. (``NamedTuple`` for
    the loader reason given on :class:`Offered`; every field here is already
    safe to render, and ``after`` renders through
    ``SallaCredentials.__repr__``.)
    """

    #: ``processing_status`` the row would be left in.
    row_status: str
    #: The writer's own verdict when it accepted the credential.
    outcome: tokens.StoreOutcome | None
    #: Which refusal, if any: ``unsigned``, ``no_store_id``, ``no_credential``,
    #: ``foreign_store``, ``write_error``.
    refusal: str | None
    #: Would the secrets file actually be rewritten?
    writes: bool
    #: The credential that would be held afterwards — built from the mapping
    #: the writer itself assembled, so the inheritance rules (refresh kept,
    #: cleared across a store change; expiry cleared when unknown) are not
    #: restated here.
    after: tokens.SallaCredentials


def _predict(
    event: WebhookEvent, offered: Offered, held: tokens.SallaCredentials,
    writer: Callable[..., tokens.StoreOutcome],
) -> Prediction:
    """Mirror ``_apply_authorize``'s branch order, using its own decisions.

    ``writer`` is the very ``functools.partial`` that ``--apply`` installs on
    ``tokens.store_credentials``, so the operator's confirmation is part of the
    prediction in exactly the way it will be part of the action — including the
    ``_operator_confirmed`` guard, which reads the binding off that object.
    """
    if not provisioning._verified(event):
        # `_apply_authorize` marks this `failed` without reading the payload.
        return Prediction("failed", None, "unsigned", False, held)
    if offered.store_id is None and not provisioning._operator_confirmed(writer):
        return Prediction("failed", None, "no_store_id", False, held)
    with _writes_disabled() as captured:
        try:
            outcome = writer(
                offered.access,
                refresh_token=offered.refresh,
                expires_at=offered.expires_at,
                store_id=offered.store_id,
            )
        except tokens.CredentialError:
            return Prediction("failed", None, "no_credential", False, held)
        except tokens.ForeignStoreCredential:
            return Prediction(PENDING_STATUS, None, "foreign_store", False, held)
        except Exception:  # noqa: BLE001 — a write failure defers, never fails
            return Prediction(PENDING_STATUS, None, "write_error", False, held)
    after = tokens._credentials_from(captured[-1]) if captured else held
    return Prediction("processed", outcome, None, bool(captured), after)


def _refresh_line(held: tokens.SallaCredentials, after: tokens.SallaCredentials) -> str:
    """What happens to the RENEWAL PATH — booleans only, never a value.

    Worth its own line because the rule is not obvious and the cost of it is
    silent: across an accepted store change the held refresh token is cleared
    rather than inherited (it buys the OLD merchant's next rotation), so a
    payload carrying none leaves the new credential with no way to renew and
    nothing else would say so until it expired.
    """
    if after.refresh_token and after.refresh_token == held.refresh_token:
        return "kept (the one already on disk)"
    if after.refresh_token:
        return "replaced by the one in this payload"
    if held.refresh_token:
        return "⚠️  CLEARED — the new credential will have NO renewal path"
    return "⚠️  none, before or after — nothing to renew with"


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """``session_factory`` is the test seam, and it exists for one reason: a
    script that can only be exercised by running it against the live database
    is a script nobody runs twice. It returns a context manager yielding a
    Session; the default builds one on the application engine exactly as
    before."""
    parser = argparse.ArgumentParser(
        description="Store the credential from a pending Salla authorize "
                    "webhook. Dry run unless --apply.",
    )
    parser.add_argument("--event-id", default=None,
                        help="consume this row instead of the newest pending "
                             "one (any status; you are naming it deliberately)")
    parser.add_argument("--env-file", default=str(tokens.ENV_FILE),
                        help=f"secrets file to read and write "
                             f"(default: {tokens.ENV_FILE})")
    parser.add_argument("--db-host", default=None,
                        help="override DB_HOST — use 127.0.0.1 from the host, "
                             "where the compose network name does not resolve")
    parser.add_argument("--db-port", default=None,
                        help="override DB_PORT — 5433 for staging on the host")
    parser.add_argument("--allow-store-change", action="store_true",
                        help="confirm that a credential for a DIFFERENT store "
                             "(or one whose store we cannot verify, or a "
                             "payload that names no store at all) may replace "
                             "what is held")
    parser.add_argument("--apply", action="store_true",
                        help="actually store and mark the row processed; "
                             "without it nothing is written anywhere")
    args = parser.parse_args(argv)

    install_secret_redaction()
    env_file = pathlib.Path(args.env_file)
    # `_apply_authorize` -> `store_credentials` writes `tokens.ENV_FILE`, not
    # anything this script hands it. Pointing the module at the chosen file is
    # what stops --env-file from being a flag that changes the report and not
    # the write. It is the default value, so this is a no-op unless asked.
    tokens.ENV_FILE = env_file
    _load_env(env_file)
    if args.db_host:
        os.environ["DB_HOST"] = args.db_host
    if args.db_port:
        os.environ["DB_PORT"] = str(args.db_port)

    from career.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    print(f"consume_stored_authorize — {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"  database   : {settings.db_name} @ {settings.db_host}:"
          f"{settings.db_port}")
    print(f"  env file   : {env_file}")

    held = _held(env_file)
    print(f"  held now   : {_describe(held)}")

    # THE ONE ADDITION, built here because BOTH halves need the same object:
    # the dry run predicts with it and --apply installs it. `_apply_authorize`
    # looks `store_credentials` up on the module at call time, and asks
    # `_operator_confirmed` about the binding — so a prediction made with any
    # other object would be answering a different question from the one the
    # apply asks.
    writer = partial(
        tokens.store_credentials, allow_store_change=args.allow_store_change,
    )
    # …and the same call WITHOUT the confirmation, which is how «does this need
    # --allow-store-change?» is answered by the writer instead of by a copy of
    # its rules living here.
    unconfirmed = partial(tokens.store_credentials, allow_store_change=False)

    if session_factory is None:
        # THE APPLICATION ROLE, through the factory. Three constraints agree on
        # it: `webhook_events` is the one system-level table with RLS off and
        # `career_app` holds SELECT and UPDATE on it, so the owner buys nothing
        # here; `db.session`'s role guard refuses a privileged role to any
        # process not named in its escape lists, and this script is not one of
        # them — adding it would be claiming a privilege it does not need; and
        # `engine_for` is the only sanctioned way to build an engine at all
        # (tests/test_rls_runtime_role.py enforces both halves of that).
        engine = engine_for(settings.app_database_url, future=True)
        session_factory = partial(Session, engine)

    with session_factory() as session:
        absent = _missing_columns(session.get_bind())
        if absent:
            print(f"\nREFUSING: webhook_events is missing {', '.join(absent)} — "
                  "this database is older than the code needs. Migrate it "
                  "first.")
            return 4

        event = _select(session, args.event_id)
        if event is None:
            print("\nnothing to do: no signature-valid app.store.authorize row "
                  f"is waiting in '{PENDING_STATUS}'.")
            return 0

        offered = _offered(event)
        identity = tokens.store_identity(held, offered.store_id, offered.access)
        prediction = _predict(event, offered, held, writer)
        baseline = _predict(event, offered, held, unconfirmed)

        print(f"\n  event      : {event.id}")
        print(f"  received   : {event.received_at.isoformat()}")
        print(f"  status     : {event.processing_status} "
              f"(attempts {event.attempt_count})")
        print(f"  signature  : {'valid' if event.signature_valid else 'INVALID'}")
        print(f"  offers     : store "
              f"{offered.store_id or '(none in payload)'}, expires "
              f"{offered.expires_at.isoformat() if offered.expires_at else '(unknown)'}, "
              f"refresh token {'yes' if offered.refresh else 'NO'}")
        print(f"  identity   : {identity.value}")
        # The retention promise `ForeignStoreCredential` makes about this row,
        # read from the module that keeps it rather than restated here.
        consumable = intake.credential_is_still_consumable(
            event, now=datetime.now(UTC)
        )
        print("  still live : " + (
            "yes — the body is exempt from the retention sweep while this "
            "credential can still be used"
            if consumable else
            "NO — this row is past its credential's expiry, or its decision "
            "was already taken"
        ))

        if prediction.refusal == "unsigned":
            # _apply_authorize would mark this `failed` and never read the
            # payload. Say so rather than doing it from a manual run.
            print("\nREFUSING: the row is not signature-valid. Its payload was "
                  "not read. Reinstall the app to get a signed delivery.")
            return 2

        # THE GATE, and it is the writer's own. `baseline` is this very payload
        # offered to `store_credentials` with the confirmation withheld: if that
        # is refused and the confirmed call is not, then --allow-store-change is
        # precisely what stands between the two, which is the definition of
        # «required». No second copy of the identity rules decides this.
        needs_confirmation = baseline.refusal in ("foreign_store", "no_store_id")
        if needs_confirmation and not args.allow_store_change:
            if baseline.refusal == "no_store_id":
                print(
                    "\nREFUSING: this payload names no store at all. The "
                    "signature proves Salla sent it and proves nothing about "
                    "WHOSE credential is inside it, and the store id is the "
                    "only discriminator there is.\nApplying without "
                    "confirmation would mark this row 'failed' — terminal — "
                    "and store nothing. Nothing is lost by stopping.\nRe-run "
                    "with --allow-store-change only if you are certain this "
                    "credential is your store's."
                )
            else:
                print(
                    "\nREFUSING: this credential does not belong to the store "
                    "we hold" + (
                        " — it is a different merchant."
                        if identity is tokens.StoreIdentity.DIFFERENT else
                        ", and the stored credential never recorded which "
                        "store it is for, so nothing here can prove otherwise."
                    ) + "\nStoring it repoints EVERY salla call at "
                    f"store {offered.store_id}. Nothing is lost by stopping: "
                    "the credential stays in this row.\nRe-run with "
                    "--allow-store-change if that is what you mean."
                )
            return 3
        if needs_confirmation:
            print(f"\n  ⚠️  store change confirmed: "
                  f"{held.store_id or '(unrecorded)'} → "
                  f"{offered.store_id or '(none in payload)'}")

        print(f"\n  would      : row → '{prediction.row_status}'"
              + (f", credential {prediction.outcome.value}"
                 if prediction.outcome is not None else "")
              + (f", refused ({prediction.refusal})"
                 if prediction.refusal else ""))
        print("  writes     : " + (
            "the env file is rewritten (0600, one backup first, no credential "
            "in it)" if prediction.writes else "NOTHING is written"
        ))
        print(f"  after      : {_describe(prediction.after)}")
        print(f"  refresh    : {_refresh_line(held, prediction.after)}")

        if not args.apply:
            print(f"\n       then mark the row '{prediction.row_status}' and "
                  "tell the operator to restart:")
            print(f"       {provisioning._RESTART_HINT}")
            print("\nnothing was written. Re-run with --apply to do it.")
            return 0

        admin = PrintingAdmin()
        # `_apply_authorize` looks `store_credentials` up on the module at call
        # time, so binding the operator's confirmation onto it here is enough —
        # the rest of the sequence (marking the row, building the alert,
        # deferring on a write failure) runs untouched and unduplicated.
        # Restored immediately afterwards so nothing in this process is left
        # carrying a standing permission.
        original = tokens.store_credentials
        tokens.store_credentials = writer
        try:
            provisioning._apply_authorize(session, event, admin)
        finally:
            tokens.store_credentials = original

        # Only this column — a full refresh would ask for the ones staging does
        # not have yet (see NEEDED_COLUMNS).
        session.refresh(event, attribute_names=["processing_status"])
        print(f"\n  row status : {event.processing_status}")
        for message in admin.messages:
            print("\n--- operator alert (printed, not sent) ---")
            print(message)
        after = _held(env_file)
        print(f"\n  held now   : {_describe(after)}")
        # The promise, checked. If these ever disagree the dry run lied to the
        # operator, and he must hear it from the tool and not from a 401 three
        # days later.
        if (event.processing_status != prediction.row_status
                or fingerprint(after) != fingerprint(prediction.after)):
            print("\n🔴 THE DRY RUN AND THE APPLY DISAGREED — predicted "
                  f"row '{prediction.row_status}' / {_describe(prediction.after)}."
                  "\nSomething in career.salla has drifted from what this "
                  "script predicts. Report it before trusting another dry run.")
            return 5
        if event.processing_status != "processed":
            print("\nthe credential was NOT stored — the row was kept for "
                  "retry. See the alert above.")
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
