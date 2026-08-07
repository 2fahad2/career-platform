"""The Salla merchant credential — where it lives, and how it is replaced.

CHANGELOG §29 (٧ أغسطس ٢٠٢٦). Salla's «Easy Mode» never shows the access token
to anyone: it is delivered once, inside the signed ``app.store.authorize``
webhook, at the moment the merchant approves the install. Refusing to consume
that payload was not caution — it was refusing the only road. The token
captured by hand on 2026-07-15 expired on 2026-07-29 with no way to renew it,
and every paid order since has been waiting on a credential nothing could
replace.

**The file, not a row.** The credential goes where every other credential in
this system already is: ``/root/career/.env.staging``, mode 0600, under the
same encrypted backup (constant 14). A ``salla_credentials`` table would have
been easier to write and would have widened the leak surface from «root on
this host» to «anybody who can read a row» — every operator query, every
export, every future join, every backup of the database rather than of the
secrets file. §29 settles it: the file.

**Never rendered.** ``SallaCredentials`` has no default repr. Constant 13 is
not a discipline problem — a dataclass with a generated repr puts the token in
every traceback frame, every ``logger.info("%s", creds)`` and every pytest
assertion diff, and no amount of care at the call sites takes it back. The
values are also handed to ``register_secret`` so that if one ever does escape
through a path nobody predicted, the redaction filter still catches the
literal.

Two callers by design:

* the provisioning worker, which consumes ``app.store.authorize`` and calls
  :func:`store_credentials` (see ``provisioning._apply_authorize``);
* the refresh timer, which reads :func:`current` / :func:`days_left` and calls
  :func:`store_credentials` again with the refreshed pair, before the expiry
  rather than after it — §29's fourth condition.

**One credential on disk, ever.** The file is replaced atomically and the
generation it displaces is kept for exactly one purpose — putting back the
lines this module does not own (``DB_PASSWORD``, the Anthropic key, the Meta
token) after a bad merge. It is NOT a spare copy of the credential: the two
token values are stripped out of every backup, because a superseded access
token stays live until its own expiry and a second file holding one is a
second thing to rotate that nothing rotates. The recoverable copy of a
credential is the signed ``app.store.authorize`` row in ``webhook_events``,
which :class:`ForeignStoreCredential` names and ``career.webhooks.intake``
keeps for as long as that credential can still be consumed. See
:func:`_atomic_write` and :func:`prune_credential_backups`.

Nothing here restarts anything. A live process took its ``SALLA_API_KEY`` from
its environment at boot and baked it into ``HttpSallaClient``'s Authorization
header, so a newly stored token does not reach it until the units restart;
that is the operator's action and the alert says so out loud rather than
letting a stored-but-unused credential look like a fixed sale path.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

logger = logging.getLogger("career.salla")

#: The one secrets file on this host. Gitignored (`.env.*`), 0600, and the
#: EnvironmentFile of all three units.
ENV_FILE = pathlib.Path("/root/career/.env.staging")

#: The access token keeps the name the environment already uses
#: (``career.config.Settings.salla_api_key``, ``HttpSallaClient``) — renaming
#: it would have meant editing config, compose and the units for cosmetics,
#: and a half-applied rename is a dead sale path.
KEY_ACCESS = "SALLA_API_KEY"
KEY_REFRESH = "SALLA_REFRESH_TOKEN"
KEY_EXPIRES = "SALLA_TOKEN_EXPIRES_AT"
KEY_STORE = "SALLA_STORE_ID"

#: What a credential is allowed to contain. Salla issues Ory tokens
#: (``ory_at_…``/``ory_rt_…``, 94 chars, printable ASCII) — but the reason for
#: the check is the FILE FORMAT, not the vendor: this value is about to become
#: a line in an EnvironmentFile that systemd, docker-compose and every future
#: reader parse as ``KEY=value`` one line at a time. A value carrying a newline
#: would not be a corrupt token, it would be an injected VARIABLE — a way for
#: whatever produced the payload to set ``DB_PASSWORD`` or ``ANTHROPIC_API_KEY``
#: on the next boot. The signature check upstream is what makes that
#: unreachable; this is the second lock on the same door, and it costs one
#: regex.
_TOKEN_RE = re.compile(r"\A[!-~]{8,4096}\Z")

#: Same reasoning, looser alphabet: the store id is a Salla merchant number and
#: the expiry is an ISO timestamp. Neither may carry a line break either.
_SCALAR_RE = re.compile(r"\A[ -~]{1,256}\Z")

#: How many superseded generations of the secrets file are kept: ONE, and
#: without its credential (see :func:`_atomic_write`). More generations would
#: only widen the question «which of these files still holds a live token»,
#: and the answer this module wants that question to have is «none of them».
BACKUP_GENERATIONS = 1

#: How long the one backup is kept. Fourteen days is Salla's longest
#: documented access-token life, and it is the right clock for a different
#: reason too: the backup answers «what did this file look like before the last
#: rotation», a question whose value ends with the rotation after it. Beyond
#: that it is a copy of the host's other secrets with no reader — so the next
#: write, and the daily refresh timer, shred it.
BACKUP_MAX_AGE_DAYS = 14

#: The values a backup must never carry. Everything else in the file is a
#: value the LIVE file still holds too — copying it does not create a second
#: credential to keep track of. These two do: after a rotation they are the
#: PREVIOUS grant, alive until its own expiry and owned by nobody.
_BACKUP_STRIPPED_KEYS = (KEY_ACCESS, KEY_REFRESH)


class CredentialError(ValueError):
    """The payload did not carry a credential we are willing to write.

    Terminal by nature: retrying an authorize event whose ``access_token`` is
    absent or malformed will produce the same answer forever, so the caller
    marks the webhook ``failed`` and shouts, rather than deferring it into a
    retry loop that can never succeed.
    """


class CredentialWriteError(RuntimeError):
    """We had a credential and could not persist it.

    The opposite verdict from :class:`CredentialError`, and the distinction is
    the whole of §29's fourth condition: the payload is fine and the disk is
    not, so the webhook row must stay ``received`` — retryable, unprocessed,
    and never marked as if it had worked.
    """


class ForeignStoreCredential(CredentialWriteError):
    """The credential is for a store other than the one we currently hold —
    or we cannot prove it is not. Nothing was written; the operator decides.

    **Why refuse rather than switch.** This platform holds ONE credential.
    Writing a different merchant's token repoints every re-verification, every
    provisioning call and every future refresh at another store, and nothing
    would say so: the first symptom is real paid orders 401-ing. The trigger
    for that outage would be a routine action on a store that is not even the
    live one — reinstalling the app on the demo store is enough. Refusing
    costs a delay and an alert, and costs nothing that was still worth having:
    the credential stays in the ``webhook_events`` row — the row itself is
    never deleted, it is the idempotency record — and
    ``career.webhooks.intake`` exempts that row's BODY from the thirty-day
    retention sweep for as long as the credential in it can still be used, so
    the operator can consume it deliberately the moment they have decided
    (``scripts/consume_stored_authorize.py``). Once the offered credential has
    passed its own expiry the body is redacted like any other, loudly, because
    at that point there is nothing left to consume. One failure is silent and
    irreversible; the other is loud and reversible while reversing it means
    anything.

    That sentence was false until 2026-08-07: the sweep redacted every body
    older than thirty days whatever its ``processing_status``, so the promise
    made here destroyed the thing it promised.

    **Why an exception, and why this parent.** A new ``StoreOutcome`` member
    would fall through ``provisioning._apply_authorize``'s outcome checks into
    its final branch — the GREEN «a new token landed, restart the services»
    alert — which is the one message that must never be sent when nothing was
    written. :class:`CredentialWriteError` already carries the exact contract
    this needs and that branch already exists: leave the row ``received``,
    count the attempt, tell the operator, retry later. It is literally true
    here — we had a credential and did not persist it — and the retry is not
    futile: it succeeds the moment the operator resolves the identity.
    """


class StoreOutcome(StrEnum):
    STORED = "stored"
    #: Byte-identical to what is already on disk. A redelivered authorize
    #: event lands here: nothing is written, nothing is backed up, and the
    #: caller may safely mark the webhook processed.
    UNCHANGED = "unchanged"
    #: What we already hold expires LATER than what was offered, AND both are
    #: grants on the same store. Refused, and this is not a corner case: Salla
    #: redelivers install events (it sent four on 2026-07-15), and the refresh
    #: timer writes a newer credential between deliveries. Letting the older
    #: one win would break a live sale path with a webhook from last week.
    #:
    #: The «same store» half of that sentence was missing until 2026-08-07 and
    #: it was not a detail — see :class:`StoreIdentity`.
    STALE = "stale"


class StoreIdentity(StrEnum):
    """Whose grant is being offered, compared with the one on disk.

    **This exists because expiry is not identity.** The ordering guard behind
    :attr:`StoreOutcome.STALE` compares two expiry dates and concludes «this
    delivery is old news». That conclusion is only available WITHIN one grant
    lineage — the same app on the same store, re-delivered or rotated. Across
    two stores the comparison has no meaning at all: two merchants authorised
    us at two unrelated moments, so which token expires first says nothing
    about which one we should be holding.

    Live proof, 2026-08-07: the file held the demo store's credential, freshly
    rotated to expire 09:37Z, and the real store's install had delivered a
    credential expiring 07:24Z — two hours and thirteen minutes earlier. Read
    on expiry alone the real store's token is «stale». Read on identity it is
    the only credential in the building that belongs to the store we are
    actually selling from.
    """

    #: No access token on disk. There is no incumbent to protect, so neither
    #: the identity question nor the ordering question has a subject.
    NO_INCUMBENT = "no_incumbent"
    #: The caller named no store. Not ignorance — the refresh path passes the
    #: store id it read back from the file, which is ``None`` until an
    #: authorize event records one, and a refresh is BY CONSTRUCTION a rotation
    #: of the grant we already hold. Treated as :attr:`SAME`.
    UNCLAIMED = "unclaimed"
    #: Same store — or the same access token, which is stronger proof than any
    #: label. The ordering guard applies, and only here.
    SAME = "same"
    #: A different merchant. A different grant, not a late redelivery.
    DIFFERENT = "different"
    #: A token is stored but its store was never recorded, so the question is
    #: unanswerable from the file. Handled as :attr:`DIFFERENT` on purpose:
    #: «I cannot tell whose this is» must not be resolved by guessing, in
    #: either direction. This is the first-run state of every credential
    #: predating ``SALLA_STORE_ID`` — including the one live on this host.
    UNKNOWN = "unknown"


@dataclass(frozen=True, repr=False)
class SallaCredentials:
    """What we hold right now. Both token fields are LIVE credentials.

    ``repr=False`` and the explicit ``__repr__`` below are load-bearing, not
    tidiness — see the module docstring.
    """

    access_token: str = ""
    refresh_token: str = ""
    expires_at: datetime | None = None
    store_id: str | None = None

    @property
    def present(self) -> bool:
        """Is there an access token at all? The question every caller actually
        asks, written once so nobody re-implements it as a truthiness test on
        the token itself and prints it by accident in the failing branch."""
        return bool(self.access_token)

    def __repr__(self) -> str:
        return (
            "SallaCredentials("
            f"access_token={'set' if self.access_token else 'unset'}, "
            f"refresh_token={'set' if self.refresh_token else 'unset'}, "
            f"expires_at={self.expires_at.isoformat() if self.expires_at else None}, "
            f"store_id={self.store_id!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class CredentialLife:
    """How much life the stored credential has — AND whether there is one.

    :func:`days_left` answers ``int | None`` and that ``None`` carries two
    unrelated facts:

    * there is no credential at all — no order can be provisioned, nothing can
      be refreshed, and only a reinstall on the store issues a new one;
    * there IS a credential and its expiry was never recorded — the sale path
      is working right now and the only thing missing is a date.

    The second is not a corner case. :func:`store_credentials` CREATES it on
    purpose: ``expires_at=None`` clears the recorded expiry rather than keeping
    a wrong date beside a new token, which is exactly what an authorize payload
    whose ``data.expires`` is absent or renamed produces.

    Collapsing the two into one ``None`` cost something real. The boot check in
    ``career.engine.cli`` documented ``days_left is None`` as «the store holds
    NO credential» and printed the reinstall instruction for it — and
    reinstalling fires ``app.uninstalled``, i.e. it takes down the live
    credential that was there all along. ``scripts/refresh_salla_token.py``
    read the SAME value as «we do not know». Two modules, one type, opposite
    meanings; that disagreement is the defect, so the distinction now lives in
    the type instead of in a caller's memory.
    """

    #: Is there an access token at all?
    present: bool
    #: Whole days until it expires; negative once it has. Meaningful only when
    #: :attr:`present`, and ``None`` there means one thing only: undated.
    days_left: int | None = None

    def __post_init__(self) -> None:
        if not self.present and self.days_left is not None:
            raise ValueError("a credential that is not present has no runway")

    @property
    def undated(self) -> bool:
        """We hold a credential and cannot say how long it has."""
        return self.present and self.days_left is None


def _register(*values: str | None) -> None:
    """Teach the log redaction filter these exact strings (constant 13).

    ``career.config`` registers ``SALLA_API_KEY`` at settings load, but nothing
    has ever registered the REFRESH token — it is not a Settings field. The
    shape rules in ``logging_filters`` already match ``ory_at_``/``ory_rt_``,
    which is why nothing leaked so far; registering the literals means the day
    Salla changes its token format we are still covered by the value itself
    rather than by a regex written for the old one.
    """
    try:
        from career.logging_filters import register_secret

        for value in values:
            register_secret(value)
    except Exception:  # noqa: BLE001 — registration is armour, never a gate
        logger.warning("could not register a salla credential with the log "
                       "redaction filter", exc_info=True)


def parse_expiry(value: object) -> datetime | None:
    """Read an expiry from anything Salla or our own file might carry.

    Three spellings, one meaning, and the middle one is the live-verified fact:

    * ``data.expires`` in ``app.store.authorize`` is an ABSOLUTE unix epoch in
      seconds — verified against the stored 2026-07-15 payload, where
      ``1785334959`` decodes to 2026-07-29T14:22:39Z, exactly the expiry the
      operator wrote down by hand that day. It is NOT ``expires_in``: reading
      it as a duration would date the token to 1970 and make every renewal
      look overdue.
    * an ISO-8601 string, which is what this module writes back.
    * a ``datetime``, for callers that already parsed it.

    Anything else is None — «unknown», which every reader treats as «say
    nothing», never as «expired».
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, bool):        # bool is an int; it is not an epoch
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_expiry(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # The file may hold a bare date (what the operator used to write
            # by hand, and what `_warn_if_token_expiring` still reads with a
            # [:10] slice). Accept it rather than calling a readable value
            # unknown.
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _read_env_file(env_file: pathlib.Path) -> dict[str, str]:
    """``KEY=value`` pairs, comments and blanks dropped. Never raises."""
    try:
        raw = env_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def current(env_file: pathlib.Path | None = None) -> SallaCredentials:
    """The credential as PERSISTED — the file wins, the process environment
    fills gaps.

    That order is deliberate. A long-running worker booted with the token that
    was current in July; the file is what the authorize event and the refresh
    timer update. If ``current()`` preferred ``os.environ`` the refresh timer
    would keep refreshing a credential that had already been replaced, and
    would report a dead token as healthy. The environment fallback is what
    makes this work in a container whose file was mounted elsewhere, and in
    tests.

    A credential is read as a SET, not as four independent values: if the file
    names an access token, the file is the whole answer and the environment is
    not consulted for the other three. Mixing them per key looks harmless and
    is not — it would pair a token from one source with an expiry from another
    and let the freshness guard in :func:`store_credentials` compare a new
    file against a stale environment.

    Never raises: it is called from alerting paths, and an unreadable file is
    an empty credential, not an exception in the middle of an incident.
    """
    path = env_file or ENV_FILE
    from_file = _read_env_file(path)
    return _credentials_from(
        from_file if from_file.get(KEY_ACCESS) else dict(os.environ)
    )


def _credentials_from(source: dict[str, str]) -> SallaCredentials:
    def pick(key: str) -> str:
        return (source.get(key) or "").strip()

    access, refresh = pick(KEY_ACCESS), pick(KEY_REFRESH)
    _register(access, refresh)
    store = pick(KEY_STORE)
    return SallaCredentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=parse_expiry(pick(KEY_EXPIRES)),
        store_id=store or None,
    )


def life(
    now: datetime | None = None, *, env_file: pathlib.Path | None = None
) -> CredentialLife:
    """The whole answer about the stored credential, in one read of the file.

    One read matters as much as the type does: asking «is there one?» and «how
    long has it?» as two calls reads the file twice, and the refresh timer can
    replace it between them — which is the same class of bug one layer up.
    """
    creds = current(env_file)
    if not creds.present:
        # An expiry with no token beside it is not a runway, it is a leftover
        # line. Reporting a number for it is how a dead store looks healthy.
        return CredentialLife(present=False)
    if creds.expires_at is None:
        return CredentialLife(present=True)
    return CredentialLife(
        present=True,
        days_left=(creds.expires_at - (now or datetime.now(UTC))).days,
    )


def days_left(
    now: datetime | None = None, *, env_file: pathlib.Path | None = None
) -> int | None:
    """Whole days until the stored credential expires; negative once it has.

    ``None`` means «we do not know», which is a third answer and not a zero:
    an unknown expiry must never be rendered as «expires today» to an operator
    who would then go looking for a problem that may not exist. Floor
    semantics — a token with eleven hours left has ``0`` days, because it does.

    A caller that has to tell «no credential» from «a credential we cannot
    date» must NOT infer it from this ``None`` — it cannot be inferred. Use
    :func:`life`, which answers both questions in one type (see
    :class:`CredentialLife`). This function stays because «how many days» is
    the whole of what the refresh timer asks, and it reads that ``None``
    correctly already: «لا نعرف كم بقي من عمر الاعتماد».
    """
    return life(now, env_file=env_file).days_left


def store_identity(
    stored: SallaCredentials,
    offered_store_id: str | None,
    offered_access_token: str | None = None,
) -> StoreIdentity:
    """Whose grant is this? Answered without writing anything.

    Public because the answer has to be available BEFORE the decision as well
    as inside it: ``scripts/consume_stored_authorize.py`` reports what would
    happen, and a dry run that re-implemented this ladder would eventually
    disagree with the writer it is supposed to predict.

    The order of the tests is the argument:

    1. **An identical access token is the same grant**, whatever the labels
       say. A shared 94-character secret is stronger evidence of identity than
       a merchant number that may simply never have been recorded, and this
       rung is what keeps a redelivery of the credential we already hold from
       being escalated into an operator decision.
    2. **Nothing stored** → nothing to protect.
    3. **No store claimed** → the refresh path; a rotation of what we hold.
    4. **Both known** → compare them, and that comparison is the whole fix.
    5. **Otherwise** the file cannot answer, and a guess is not an answer.
    """
    offered = (offered_store_id or "").strip()
    held = (stored.store_id or "").strip()
    if offered_access_token and offered_access_token == stored.access_token:
        return StoreIdentity.SAME
    if not stored.present:
        return StoreIdentity.NO_INCUMBENT
    if not offered:
        return StoreIdentity.UNCLAIMED
    if held:
        return StoreIdentity.SAME if held == offered else StoreIdentity.DIFFERENT
    return StoreIdentity.UNKNOWN


def _validate(access_token: str, refresh_token: str) -> None:
    if not access_token or not _TOKEN_RE.match(access_token):
        # No token value in the message, here or anywhere below: an exception
        # string is a log line waiting to happen (constant 13).
        raise CredentialError("salla authorize payload carries no usable "
                              "access token")
    if refresh_token and not _TOKEN_RE.match(refresh_token):
        raise CredentialError("salla authorize payload carries a malformed "
                              "refresh token")


def _backup_path(path: pathlib.Path) -> pathlib.Path:
    """The one recovery file. Same path the operator already knows from
    ``scripts/wire_salla_products.py`` — one recovery file, not two."""
    return path.with_name(path.name + ".bak")


def _shred(path: pathlib.Path) -> None:
    """Overwrite the bytes, then unlink. Never just unlink.

    ``unlink`` drops a name; the blocks keep whatever was in them until
    something else claims them, and what was in them here is a copy of every
    secret on the host. Overwriting first is not a guarantee — a
    copy-on-write or log-structured filesystem may have written the original
    elsewhere, and this is ext4 on a VPS whose disk we do not own — so it is
    stated as what it is: it removes the value from every path that can read
    the filesystem (a stray ``cat``, a later operator, a backup script that
    globs ``.env.*``), which is the exposure that actually exists. It never
    raises: failing to tidy up must not fail a credential write.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        logger.debug("could not overwrite %s before unlinking it", path,
                     exc_info=True)
    try:
        path.unlink()
    except OSError:
        logger.warning("could not remove the superseded secrets backup at %s",
                       path)


def _superseded_body(existing: list[str]) -> str:
    """The backup's body: the displaced file WITHOUT its credential.

    The backup exists to put back the lines this module does not own after a
    bad merge — the database password, the Anthropic key, the Meta token, the
    comments and the ordering. Those are values the live file still holds too,
    so copying them creates no new secret to keep track of.

    The two token values are different in kind: after a rotation they are the
    PREVIOUS grant, and a Salla access token stays valid until its own expiry
    whatever we do with our copy. Keeping them here would leave a second live
    credential in a second file that nothing rotates, prunes or knows about —
    which is what ``.env.staging.bak`` actually was on 2026-08-07. So they are
    written back as empty values: the KEY stays (a human reading the backup
    can see that a credential was there) and the value does not.
    """
    out = [
        "# superseded generation of the secrets file. The salla token values "
        "were removed on write — see career.salla.tokens._superseded_body.",
    ]
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in _BACKUP_STRIPPED_KEYS and not line.lstrip().startswith("#"):
            out.append(f"{key}=")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def prune_credential_backups(
    env_file: pathlib.Path | None = None, *, now: datetime | None = None,
    max_age_days: int = BACKUP_MAX_AGE_DAYS,
) -> bool:
    """Shred the backup once it is older than :data:`BACKUP_MAX_AGE_DAYS`.

    The other half of the lifecycle. :func:`_atomic_write` replaces the backup
    on every write, which bounds it to one generation — but a host that stops
    rotating (the exact state this project was in from 2026-07-29 to
    2026-08-07) would otherwise keep the last one forever. The daily refresh
    timer calls this whether or not it refreshes anything, so the file has a
    death date that does not depend on the next write ever happening.

    Returns True when something was shredded. Never raises.
    """
    backup = _backup_path(env_file or ENV_FILE)
    try:
        age = (now or datetime.now(UTC)) - datetime.fromtimestamp(
            backup.stat().st_mtime, UTC
        )
    except OSError:
        return False
    if age.days < max_age_days:
        return False
    logger.info(
        "shredding the superseded secrets backup — it is %d days old and the "
        "credential it was taken beside cannot still be live", age.days,
    )
    _shred(backup)
    return True


def _rotate_backup(path: pathlib.Path) -> None:
    """Keep :data:`BACKUP_GENERATIONS` generation of ``path``, credential-free.

    Shred FIRST, then write: replacing the backup atomically would drop the
    previous one's inode without overwriting it, which is the difference
    between «the old token is gone» and «the old token is unlinked».
    """
    backup = _backup_path(path)
    existing = path.read_text(encoding="utf-8").splitlines()
    if not existing:
        return                      # nothing to lose, nothing to back up
    _shred(backup)
    _atomic_write(backup, _superseded_body(existing), keep_generation=False)


def _atomic_write(
    path: pathlib.Path, body: str, *, keep_generation: bool = True
) -> None:
    """temp → fsync → chmod → replace, with the directory fsynced after — and
    the generation it displaces handled here rather than by whoever remembers.

    The technique is ``scripts/wire_salla_products.py``'s, deliberately reused
    rather than reinvented — that script had already reasoned it out for this
    exact file: a plain truncating write that dies halfway takes every secret
    on the server with it, and this file is excluded from the repository AND
    from the database backup.

    One thing is tightened. That script writes the ``.bak`` with
    ``write_text`` and chmods it afterwards, so for an instant a full copy of
    every secret exists at mode 0644. Here nothing is ever created with wider
    permissions than it will end with: the temp file is opened by ``mkstemp``
    (0600 from birth) and the mode is set before, not after, the rename.

    ``keep_generation`` is False for the backup's own write, which is the only
    call that must not recurse. Everything else gets the policy: one
    generation, no credential in it, and a death date
    (:func:`prune_credential_backups`).
    """
    if keep_generation and path.exists():
        _rotate_backup(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)          # atomic within the filesystem
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    # The rename is atomic; its DURABILITY is the directory's, not the file's.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:  # a filesystem that will not let us fsync a directory
        logger.debug("could not fsync the secrets directory", exc_info=True)


def _merged_body(existing: list[str], wanted: dict[str, str]) -> str:
    """Rewrite the values we own, in place, and leave every other line exactly
    as it was — comments, blank lines and ordering included. An env file is
    read by humans at 3am; reordering it on every token refresh would make
    ``diff`` useless on the one file that has no history."""
    out: list[str] = []
    seen: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in wanted and not line.lstrip().startswith("#"):
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _publish_to_process_env(wanted: dict[str, str]) -> None:
    """Make THIS process agree with the file it just wrote.

    Not a substitute for a restart — ``HttpSallaClient`` baked the old token
    into its Authorization header at construction and no amount of environment
    updating reaches it. What this fixes is the smaller lie: without it,
    ``_warn_if_token_expiring`` would keep reading July's expiry out of a
    cached Settings object and keep telling the operator the token is dead
    thirty seconds after a fresh one was stored.
    """
    try:
        from career.config import get_settings

        for key, value in wanted.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — never fail a stored credential over this
        logger.warning("stored credential could not be published to the "
                       "process environment", exc_info=True)


def store_credentials(
    access_token: str,
    *,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    store_id: str | None = None,
    env_file: pathlib.Path | None = None,
    allow_store_change: bool = False,
) -> StoreOutcome:
    """Persist the credential atomically, or refuse and say why.

    ``refresh_token``/``store_id`` left as ``None`` keep whatever is already
    stored — a refresh that returns only a new access token must not erase the
    refresh token that will be needed for the NEXT one. ``expires_at`` left as
    ``None`` is different on purpose: it CLEARS the recorded expiry, because
    keeping the old date beside a new token is not a conservative default, it
    is a wrong number that the expiry warner would act on. Unknown reads as
    unknown.

    ``allow_store_change`` is the operator's signature on a decision this
    function refuses to take by itself: accepting a credential for a merchant
    other than the one we hold. It is False for every automatic caller — the
    webhook worker and the refresh timer both leave it alone — and True only
    from a human-run script that has printed both store ids first.

    Raises :class:`CredentialError` when the value is not a credential we will
    write (terminal — nothing to retry), :class:`ForeignStoreCredential` when
    the credential belongs to another store (retryable, and the retry succeeds
    once the operator decides), and :class:`CredentialWriteError` when the
    write itself fails (retryable — the caller must NOT mark the event
    processed). Returns :class:`StoreOutcome` so a redelivery and a stale
    delivery are distinguishable from a real replacement.
    """
    path = env_file or ENV_FILE
    access_token = (access_token or "").strip()
    refresh_token = refresh_token.strip() if refresh_token is not None else None
    _validate(access_token, refresh_token or "")

    expiry_text = expires_at.astimezone(UTC).isoformat() if expires_at else ""
    store_text = str(store_id).strip() if store_id is not None else None
    for scalar in (expiry_text, store_text):
        if scalar and not _SCALAR_RE.match(scalar):
            raise CredentialError("salla authorize payload carries a "
                                  "malformed expiry or store id")

    # Serialize the whole read-modify-write. Two writers is not hypothetical:
    # the provisioning worker consumes an authorize event on one process while
    # the refresh timer writes on another, and both rewrite the SAME file from
    # a snapshot they read a moment earlier. Without this, the loser's write
    # silently reverts the winner's — including reverting a fresh token back
    # to a dead one.
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise CredentialWriteError(
            f"could not open the credential lock at {lock_path}"
        ) from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # THE FILE ONLY — deliberately not `current()`. This is a
        # read-modify-write of one file, so the comparison must be against
        # what that file holds. `current()` falls back to the process
        # environment when the file has no token, and here that fallback would
        # be a bug with teeth: a process whose environment still carries July's
        # values would compare a fresh delivery against them and could refuse
        # it as stale, or merge a token from the file with an expiry from the
        # environment.
        stored = _credentials_from(_read_env_file(path))

        # WHOSE credential is this, before HOW OLD it is. Getting these two
        # questions in this order is the 2026-08-07 fix: the ordering guard
        # below is a statement about one grant's history and says nothing
        # whatsoever about another merchant's grant.
        identity = store_identity(stored, store_text, access_token)

        if identity in (StoreIdentity.DIFFERENT, StoreIdentity.UNKNOWN):
            if not allow_store_change:
                # Store ids are merchant numbers, not secrets — provisioning
                # already puts one in the operator alert. No token, no expiry,
                # nothing that is a credential.
                raise ForeignStoreCredential(
                    "the offered salla credential is for store "
                    f"{store_text or '?'} and this host holds "
                    + (
                        f"store {stored.store_id}"
                        if identity is StoreIdentity.DIFFERENT
                        else "a credential whose store was never recorded"
                    )
                    + " — nothing was written; an operator must confirm the "
                      "change"
                )
            logger.warning(
                "storing a salla credential for a DIFFERENT store on explicit "
                "operator instruction — the previous grant is being replaced"
            )
        elif (
            # §29 idempotency, and the ordering guard that protects a live
            # token from an old webhook — see StoreOutcome.STALE. Reachable
            # only for SAME/UNCLAIMED/NO_INCUMBENT, i.e. one lineage.
            stored.expires_at is not None
            and expires_at is not None
            and expires_at < stored.expires_at
            and access_token != stored.access_token
        ):
            return StoreOutcome.STALE

        # An accepted store change makes the INHERITANCE defaults wrong. The
        # refresh token on disk buys the next rotation of the OLD merchant's
        # grant; pairing it with the new merchant's access token would let the
        # next refresh quietly resurrect the store we just left as the live
        # credential. Across a store change, unstated means absent — the same
        # reasoning that already clears the expiry rather than keeping a wrong
        # date beside a new token.
        crossing = identity in (StoreIdentity.DIFFERENT, StoreIdentity.UNKNOWN)
        wanted = {
            KEY_ACCESS: access_token,
            KEY_REFRESH: (
                refresh_token if refresh_token is not None
                else ("" if crossing else stored.refresh_token)
            ),
            KEY_EXPIRES: expiry_text,
            KEY_STORE: (
                store_text if store_text is not None
                else (stored.store_id or "")
            ),
        }
        _register(wanted[KEY_ACCESS], wanted[KEY_REFRESH])

        current_expiry = (
            stored.expires_at.astimezone(UTC).isoformat()
            if stored.expires_at else ""
        )
        if (
            wanted[KEY_ACCESS] == stored.access_token
            and wanted[KEY_REFRESH] == stored.refresh_token
            and wanted[KEY_STORE] == (stored.store_id or "")
            and expiry_text == current_expiry
        ):
            # A redelivered event. Not writing is the point: no backup churn,
            # no rename, no window in which the file does not exist.
            _publish_to_process_env(wanted)
            return StoreOutcome.UNCHANGED

        try:
            existing = (
                path.read_text(encoding="utf-8").splitlines()
                if path.exists() else []
            )
            # The backup is taken inside `_atomic_write`, before the replace
            # and only when there is something to lose — it owns the policy
            # (one generation, no credential in it) so that no caller can take
            # a copy of this file without it.
            _atomic_write(path, _merged_body(existing, wanted))
        except OSError as exc:
            # Path only. `exc` may name the file; it can never name a value.
            raise CredentialWriteError(
                f"could not persist the salla credential to {path}"
            ) from exc

        _publish_to_process_env(wanted)
        return StoreOutcome.STORED
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
