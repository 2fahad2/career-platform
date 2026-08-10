"""``support_events.status`` — the one vocabulary, and why it lives here.

An open ticket **mutes a customer's channel.** Three escalation paths refuse
to raise a second one while a row for that customer still says «open» — the
لمّاح+ direct line (``promises.career_session``), the funnel's consent stall
(``funnel.flow``) and the paid-while-silenced alert
(``whatsapp.activation_flow``) — and every one of them is right to: a customer
who writes four messages in a row is one human waiting, not four. But it means
these three strings are not a description of a state. They jointly decide
whether a paying customer can reach a human, and the decision is only correct
while every copy of them agrees.

They were spelled out as bare literals in five places that had to agree from
memory. A sixth caller spelling it differently, or a fourth status added to
four of the five, silences somebody — with no error, no failing test and no
line in any log; the only symptom is a customer who writes and is never paged
about. ``telegram/console`` says the general form of this in its own words: a
list of the kinds copied from four other modules is a list that rots silently.

WHY A TOP-LEVEL MODULE, and what was rejected.

``support_events`` is WRITTEN from ``promises``, ``funnel`` and ``whatsapp``
and READ from ``telegram``, and none of the four owns it:

* the console owning it would mean the three domain modules importing a SCREEN
  to decide whether to page anybody — backwards, and a cycle waiting to happen
  (``telegram.console`` already imports from ``promises`` and ``salla``);
* any one writer owning it makes the other two import a stranger, and picks
  the owner by which path happened to be written first;
* ``db.models`` owning it is the closest call — the column is declared there —
  and it is still wrong: models is the schema, not the policy, and «which
  statuses MUTE» is a product decision that no ``String(16)`` implies. It is
  also another agent's file, which is a fact about today and not the argument.

So: a leaf module beside ``career.audit`` and ``career.notify``, importing
NOTHING from ``career`` — which is what makes it importable from all four
without a cycle, and what keeps it readable by a test that must not drag the
application in to ask what the words are.

The SHAPE is ``career.salla.subscriptions``'s, deliberately, rather than a new
one: the constants, the sets DERIVED from them, and no queries. Callers write
their own ``where`` clauses against these names, exactly as ``salla.renewal``
does with ``RENEWABLE_PLANS`` (and, like it, they sort the frozenset before
``.in_()`` so the emitted SQL is stable).

The ratchet that keeps the sixth copy from happening lives in
``tests/test_promises.py`` and fails on any status literal written beside a
``SupportEvent`` in ``src/``.
"""

from __future__ import annotations

# ── the three values ─────────────────────────────────────────────────────────

#: Raised, unanswered, and MUTING: no second ticket may be raised for this
#: customer while one of these stands. Written by every escalation path.
OPEN = "open"

#: Open too long and nobody closed it. NOT a closure — a released ticket is
#: still owed, still on the operator's queue, still carrying its original age,
#: and still waiting for his own button. What it stops being is the mute, so
#: the customer's next message can raise a ticket of its own with its own age
#: and its own message id. Written only by
#: ``telegram.console.release_forgotten_tickets``.
RELEASED = "released"

#: A human was dealt with. Only an operator can claim that, so this is written
#: in exactly one place — ``telegram.console._run_ticket_close`` — and nothing
#: in this system ever writes it automatically.
RESOLVED = "resolved"

ALL_STATUSES: frozenset[str] = frozenset({OPEN, RELEASED, RESOLVED})

# ── what the values MEAN, derived and never restated ─────────────────────────

#: A ticket nobody owes anything on any more; off the queue, invisible.
#:
#: This is the set that is enumerated, and the queue below is what is left
#: over — that direction and not the other, because the two failures are not
#: symmetrical. A fourth status added by an author who forgets this file
#: appears ON the operator's screen (noise he can see and complain about); the
#: other direction would drop it off the screen (a customer nobody is told
#: about, which is the failure the whole table exists to prevent).
CLOSED_STATUSES: frozenset[str] = frozenset({RESOLVED})

#: Still owed → listed on the tickets screen, oldest first, with a working
#: «إغلاق» button under it. RELEASED is here for the whole reason the release
#: is not a close.
QUEUED_STATUSES: frozenset[str] = ALL_STATUSES - CLOSED_STATUSES

#: The statuses that SILENCE the customer: while a ticket of this status
#: stands, the three dedupes above refuse to raise a new one, and
#: ``release_forgotten_tickets`` is the sweep that takes the mute off after 48
#: hours. One invariant, stated once, that stays true when a fourth dedupe is
#: written tomorrow — and it must stay a SUBSET of the queued statuses, or the
#: sweep would release a ticket the operator can no longer see.
#:
#: RELEASED is deliberately not here: that is the entire content of the word.
MUTING_STATUSES: frozenset[str] = frozenset({OPEN})
