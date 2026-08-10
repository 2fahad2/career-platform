"""«لا تسلّم ما لا تستطيع تسجيله» — the schema, measured, before the first send.

THE INCIDENT (2026-08-10, this host, 09:55–10:01). The nightly run discovered,
ranked, generated and **actually sent four WhatsApp messages to a real
customer** — two texts, a document, an interactive card. Meta's own receipts
show ``sent`` then ``delivered``. The ledger INSERT that followed raised::

    psycopg.errors.UndefinedColumn: column "meta_error_code"
    of relation "delivery_messages" does not exist

and took the whole transaction with it: no ``delivery_messages`` rows, no
``deliveries`` row for the day, the unit exited 3, and the honest state
recorded for the customer's day was ``CV_GENERATION_FAILED`` — while the
customer was reading his CV on his phone. Nothing was wrong with the CV, the
generation, WhatsApp or the code. The host's database was at
``alembic_version = 0028`` and the deployed code writes columns that ``0030``
adds. Four irreversible external side effects, zero durable record.

That is the whole rule this module exists to enforce, and it is one sentence:
**do not deliver what you cannot record.**

────────────────────────────────────────────────────────────────────────────
IT MEASURES. IT DOES NOT ASSERT.
────────────────────────────────────────────────────────────────────────────

This repository has now built three guards that checked what SHOULD be true
instead of what IS true, and each one was green on the morning it should have
fired: a unit test reading the repository's copy of the systemd units while
the host ran July's files; an entitlement scan over modules nothing imports;
a ratchet that could only see the names written in front of it. The shape of
that mistake is always the same — the guard reads a **belief** (a constant, a
file in the repo, a name in an AST) rather than the **thing that will be
used**.

So there are exactly two readings here and both are of the real thing:

* **The head** comes from alembic's own :class:`~alembic.script.ScriptDirectory`
  over ``migrations/versions`` — the same object ``alembic upgrade head``
  resolves, so a revision file that exists is counted whether or not anyone
  remembered to write it down anywhere else.
* **The current revision** comes from ``SELECT version_num FROM
  alembic_version`` **through the session that is about to write**, not
  through a second engine, a second DSN, or a settings object. If that session
  can see the row, the row is what governs the INSERT that follows.

There is deliberately **no expected-head constant** in this module, and there
must never be one. A hardcoded ``EXPECTED_HEAD = "0030"`` would have to be
edited by the same person, in the same commit, who adds the migration the
constant is supposed to police — which is to say it would be a second copy of
the fact, drifting from the first, telling us what we already believed. That
is not a fix for this incident; it is a re-implementation of it.

────────────────────────────────────────────────────────────────────────────
BRANCHING, HONESTLY
────────────────────────────────────────────────────────────────────────────

``ScriptDirectory.get_heads()`` returns a **tuple** and ``alembic_version``
holds a **set of rows**, because both sides can legitimately branch. Every
combination gets an answer rather than an assumption:

* **db == heads** (one each, or several each) → :data:`MATCH`. This is checked
  FIRST, before the branch complaint below, because a database that carries
  every head the checkout describes has every column the checkout can write,
  and that is the only property this guard is entitled to care about.
* **code has several heads and the db does not hold them all** →
  :data:`CODE_BRANCHED`. Not merely «behind»: ``alembic upgrade head`` will
  refuse to run at all («Multiple head revisions are present»), so telling the
  operator to run it would be sending him at a command that cannot work. The
  fix names ``alembic merge heads`` first.
* **the db holds a revision this checkout has never heard of** →
  :data:`DB_AHEAD`. See the direction argument below.
* **alembic_version exists but is empty** → :data:`DB_UNSTAMPED`. A database
  nobody ever stamped is not «at zero»; it is unknown, and its shape is
  whatever some human did by hand.
* **the table is missing, or alembic will not import, or the query raises** →
  :data:`UNMEASURABLE`, and we refuse. Argued below.

────────────────────────────────────────────────────────────────────────────
WHICH DIRECTION IS FATAL — both, for different reasons
────────────────────────────────────────────────────────────────────────────

**DB behind code** is tonight's incident, and it is fatal because the code
writes columns that do not exist. The write fails AFTER the send, because the
send is the fast, external, irreversible half and the ledger is the slow,
local, revocable half. There is no ordering of those two that makes an
unmigrated database safe, which is why the check has to come before both.

**DB ahead of code** is the rollback case — the deploy is reverted and the
database keeps the schema the newer migrations gave it — and it is also fatal,
for the mirror-image reason. ``0029`` in this very tree drops tables; code
from before it still selects them. A ``NOT NULL`` column added without a
default by a later revision rejects every insert the older code writes.
«Ahead» is not «a superset that happens to work»; it is a schema this code was
never tested against, and the failure mode is identical — a send we cannot
record.

The two differ in the **fix**, and that is why they are separate verdicts
rather than one «mismatch». Behind → migrate. Ahead → **deploy the matching
code**. The guard must never tell an operator to `alembic downgrade` his way
out of it: the newer columns already hold rows, and downgrading destroys them
to make a report look tidy. So :attr:`SchemaVerdict.fix` names a migration
command in one direction and a deploy in the other, and never inverts them.

────────────────────────────────────────────────────────────────────────────
WHY IT FAILS CLOSED WHEN IT CANNOT MEASURE
────────────────────────────────────────────────────────────────────────────

If alembic will not import, or ``migrations/versions`` is not on disk, or the
``alembic_version`` table is absent, this returns :data:`UNMEASURABLE` and the
delivery phase refuses. That is a real cost — a broken guard can take the
product dark for a night — and it was weighed against the alternative rather
than assumed:

* refusing on an unmeasurable night costs one day's delivery, is announced in
  the journal, in the admin channel and in the exit code, and is fixed by a
  human in minutes;
* proceeding on an unmeasurable night costs exactly what 2026-08-10 cost: real
  messages to a real customer with no record that they happened, discovered by
  reading logs.

A guard that answers «I could not check, carry on» is the guard that was not
installed. It fails closed, and it says so out loud rather than silently.

What it must NEVER do is take the run down with a traceback: every path here
returns a verdict, no path raises, and a failed probe rolls its own
transaction back so the caller's session is usable afterwards (a failed
``SELECT`` poisons a Postgres transaction, and a guard that leaves the session
in ``PendingRollbackError`` has destroyed the very information the night was
supposed to record — that is literally the second half of the 2026-08-10
traceback).

────────────────────────────────────────────────────────────────────────────
SCOPE — what this deliberately does NOT guard
────────────────────────────────────────────────────────────────────────────

**The conversation worker is out of scope, on purpose.** ``whatsapp/worker``
replies to inbound customer messages. Refusing there would convert «a reply we
cannot fully record» into «total silence at a paying customer who just typed a
question», which is a worse trade and a different one: the nightly delivery is
a scheduled push that can be re-run tomorrow with nothing lost but a day,
while a dropped reply is an unanswered human. The worker's own drift response
belongs to whoever owns that trade — the honest options there are «degrade to
a reply we can record» or «alert while still answering», not «refuse» — and
picking it silently from inside a schema guard would be exactly the kind of
unowned product decision this repository keeps paying for.

**The §05 subscription lifecycle sweep is also not gated here**, and this one
is a genuine gap rather than a settled question. That sweep runs before the
engine, and it *does* send (renewal and recovery nudges) while writing
``subscription_events`` — the same shape as the incident, with a smaller blast
radius, on tables that predate the drift. Gating it would mean a schema drift
also stops every customer's renewal clock, trading a delivery outage for a
billing one. It is named here so the next reader finds a considered gap
instead of an oversight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # the runtime import is deferred — see _script_directory
    from alembic.script import ScriptDirectory

logger = logging.getLogger("career.db.schema_guard")

#: The database carries exactly the heads this checkout describes. The ONLY
#: verdict that lets the delivery phase send anything.
MATCH = "match"
#: Tonight's incident: the code writes columns the database does not have.
DB_BEHIND = "db-behind-code"
#: The rollback case: the database carries revisions this checkout never saw.
DB_AHEAD = "db-ahead-of-code"
#: ``alembic_version`` exists and is empty — nobody ever stamped this database.
DB_UNSTAMPED = "db-unstamped"
#: Two unmerged heads on disk. ``upgrade head`` cannot run; a merge comes first.
CODE_BRANCHED = "code-branched"
#: The measurement itself did not complete. Fails closed — see the docstring.
UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True)
class SchemaVerdict:
    """One measurement of «can this database record what this code sends».

    Both revision fields are SORTED TUPLES rather than sets so the verdict is
    hashable, comparable and prints in a stable order in the journal — an
    operator diffing two nights' JSON must not see a spurious change because a
    set iterated differently.
    """

    verdict: str
    #: What ``alembic_version`` actually holds, right now, in the session used.
    db_revisions: tuple[str, ...]
    #: What ``migrations/versions`` on disk actually resolves to.
    code_heads: tuple[str, ...]
    #: One English sentence for the journal — never PII, only revision ids.
    detail: str
    #: The operator's next action. A runnable command wherever one exists.
    fix: str

    @property
    def deliverable(self) -> bool:
        """May the delivery phase send? Only on an exact match.

        Deliberately not «is it not DB_BEHIND»: a new verdict added later is
        un-deliverable until somebody decides otherwise, which is the safe way
        round for a property whose false answer sends real messages.
        """
        return self.verdict == MATCH

    def summary(self) -> dict[str, object]:
        """The journal shape. Revision ids and English only (§15.13)."""
        return {
            "verdict": self.verdict,
            "db": list(self.db_revisions),
            "code_heads": list(self.code_heads),
            "detail": self.detail,
            "fix": self.fix,
        }


def repo_root() -> Path | None:
    """The checkout this module was imported from — found, not configured.

    Walks up from ``__file__`` looking for ``migrations/versions``. An env var
    or a settings field was rejected: the whole point is to measure the code
    that is RUNNING, and a configured path is one more thing that can point at
    a directory other than the one Python just imported.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "migrations" / "versions").is_dir():
            return parent
    return None


def _script_directory(root: Path) -> ScriptDirectory:
    """Alembic's own view of ``migrations/versions``.

    ``alembic.ini`` is honoured when present — it is what ``alembic upgrade``
    reads, so resolving the tree any other way would be measuring a different
    tree than the operator's fix command will touch. Its ``script_location`` is
    made ABSOLUTE against ``root`` first: alembic resolves a relative location
    against the process's working directory, and the nightly's
    ``WorkingDirectory=`` is a fact about a systemd unit, not about this
    function.

    The alembic import is DEFERRED to here rather than done at module scope so
    that an environment without alembic produces :data:`UNMEASURABLE` — a
    refusal the operator can read — instead of an ImportError at the top of
    ``engine.cli``, which would take the run down before it could say why.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = root / "alembic.ini"
    config = Config(str(ini)) if ini.is_file() else Config()
    location = Path(config.get_main_option("script_location") or "migrations")
    if not location.is_absolute():
        location = root / location
    config.set_main_option("script_location", str(location))
    return ScriptDirectory.from_config(config)


def code_heads(root: Path) -> tuple[str, ...]:
    """The head(s) of ``migrations/versions``, via alembic's own resolver.

    A tuple, sorted, because a branched tree legitimately has more than one and
    :func:`measure_schema` has to say which case it is looking at rather than
    quietly taking the first.
    """
    return tuple(sorted(_script_directory(root).get_heads()))


def known_revisions(root: Path) -> frozenset[str]:
    """Every revision this checkout has a script for — heads and ancestors.

    Used only to tell «behind» from «ahead»: a revision the database names that
    this tree cannot even resolve is one the database got from other code.
    """
    return frozenset(rev.revision for rev in _script_directory(root).walk_revisions())


def db_revisions(session: Session) -> tuple[str, ...]:
    """What ``alembic_version`` holds in THIS session. May raise; caller wraps.

    Read through the caller's session on purpose. A private engine here would
    answer a question about some database — this answers a question about the
    connection that is about to write the ledger.
    """
    rows = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return tuple(sorted(str(row) for row in rows))


def measure_schema(session: Session) -> SchemaVerdict:
    """Measure the live database against the migrations on disk. Never raises.

    Call this immediately before the first send of a phase, through the session
    that phase will write with. Everything it can go wrong on — a missing
    table, an unimportable alembic, an empty tree — becomes a verdict that
    refuses, never an exception that ends the run somewhere unpredictable.
    """
    root = repo_root()
    if root is None:
        return SchemaVerdict(
            UNMEASURABLE, (), (),
            "no migrations/versions directory above this module — the running "
            "code cannot say what schema it expects",
            "reinstall the checkout so migrations/versions ships with the code",
        )

    try:
        heads = code_heads(root)
        known = known_revisions(root)
    except Exception as exc:  # noqa: BLE001 — a guard reports, it never raises
        logger.error("could not read migration heads from disk", exc_info=True)
        return SchemaVerdict(
            UNMEASURABLE, (), (),
            f"migration scripts unreadable ({type(exc).__name__})",
            f"cd {root} && .venv/bin/alembic heads",
        )

    try:
        current = db_revisions(session)
    except Exception as exc:  # noqa: BLE001 — same, and it must not poison
        # A failed SELECT aborts the Postgres transaction; leaving the session
        # in that state would make the caller's next honest write raise
        # PendingRollbackError — the second half of the 2026-08-10 traceback.
        session.rollback()
        logger.error("could not read alembic_version", exc_info=True)
        return SchemaVerdict(
            UNMEASURABLE, (), heads,
            f"alembic_version unreadable ({type(exc).__name__}) — this "
            "database may never have been migrated",
            f"cd {root} && .venv/bin/alembic upgrade head",
        )

    upgrade = f"cd {root} && .venv/bin/alembic upgrade head"

    if not heads:
        return SchemaVerdict(
            UNMEASURABLE, current, heads,
            "migrations/versions resolves to no head at all",
            f"cd {root} && .venv/bin/alembic heads",
        )
    if not current:
        return SchemaVerdict(
            DB_UNSTAMPED, current, heads,
            "alembic_version is empty — this database was never stamped, so "
            "its shape is whatever somebody built by hand",
            upgrade,
        )
    # FIRST, and before the branch complaint: a database holding every head the
    # checkout describes has every column the checkout can write. That is the
    # only property this guard is entitled to have an opinion about.
    if set(current) == set(heads):
        return SchemaVerdict(
            MATCH, current, heads,
            "database is at the migration head this code was written against",
            "",
        )
    unknown = tuple(sorted(set(current) - known))
    if unknown:
        return SchemaVerdict(
            DB_AHEAD, current, heads,
            "the database carries revision(s) this checkout has no script "
            f"for ({', '.join(unknown)}) — the deployed code is older than "
            "the schema, most likely a rolled-back deploy",
            # NEVER a downgrade: the newer columns already hold rows, and
            # downgrading destroys them to make a mismatch report look tidy.
            "deploy the code whose migrations the database already has — do "
            "NOT downgrade the database",
        )
    if len(heads) > 1:
        return SchemaVerdict(
            CODE_BRANCHED, current, heads,
            f"migrations/versions has {len(heads)} unmerged heads "
            f"({', '.join(heads)}) — «upgrade head» cannot run until they "
            "are merged",
            f"cd {root} && .venv/bin/alembic merge heads && "
            ".venv/bin/alembic upgrade head",
        )
    return SchemaVerdict(
        DB_BEHIND, current, heads,
        "the database is behind the deployed code — this code writes columns "
        "that the migrations it is missing would have added",
        upgrade,
    )
