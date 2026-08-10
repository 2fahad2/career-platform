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

HOW A READER ASKS — the one rule that is not a naming convention.

A reader of this table asks one of two questions and they are not mirror
images:

* «is this ticket DONE?» is answered by the enumerated ``CLOSED_STATUSES``;
* «is this ticket still OWED?» must be answered by the NEGATION of that set —
  ``~SupportEvent.status.in_(sorted(CLOSED_STATUSES))`` — and never by a
  positive list of queue words.

The reason is the author this file was written for: the one who invents a
fourth status and never opens this module. He does not add his word to
``ALL_STATUSES`` — that is what forgetting the file means — so any set DERIVED
from ``ALL_STATUSES`` does not contain it either, and a queue query written as
``status.in_(<queue set>)`` filters his ticket out of every screen. It was:
the tickets screen printed «no open tickets» over a customer waiting, the
sweep (which selects on the muting set) never touched him, and the close
button called his ticket already-closed. One page at write time and then
invisible to every reader — exactly the failure this table exists to prevent.

Asked by negation, an unknown word is merely NOISE on the operator's screen:
a row he can see, complain about, and close. That is the asymmetry the
comments below used to claim and the queries did not deliver.

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
#: THE ONLY SET A QUEUE READER MAY PUT IN A ``where`` CLAUSE, and it goes in
#: negated (module docstring, «HOW A READER ASKS»). This is the enumerated
#: half because a word nobody enumerated must land on the operator's screen
#: rather than fall off it, and only the negative question has that property.
CLOSED_STATUSES: frozenset[str] = frozenset({RESOLVED})

#: Still owed → listed on the tickets screen, oldest first, with a working
#: «إغلاق» button under it. RELEASED is here for the whole reason the release
#: is not a close.
#:
#: KEPT, AND NO LONGER A QUERY PREDICATE. It was both, and being both is what
#: made the screen lie: as a predicate it silently means «the queue words I
#: happened to know about», which is the wrong answer for the only author this
#: module was written for. Retiring it outright was the alternative and it
#: loses two things worth more than the line it saves:
#:
#: * it NAMES the three words' places, so the vocabulary above can be read as
#:   a table («resolved is done, the other two are owed») instead of inferred
#:   from a negation spread across two call sites;
#: * it is the left-hand side of the invariant the guard test asserts —
#:   ``MUTING_STATUSES <= QUEUED_STATUSES``, i.e. no known word both silences a
#:   customer and hides him. Without it that check has nothing to compare.
#:
#: So: display and invariants, never a ``where``. A reader who writes
#: ``status.in_(sorted(QUEUED_STATUSES))`` has re-introduced the defect, and
#: ``tests/test_promises.py`` fails on that shape by name rather than trusting
#: this comment.
QUEUED_STATUSES: frozenset[str] = ALL_STATUSES - CLOSED_STATUSES

#: The statuses that SILENCE the customer: while a ticket of this status
#: stands, the three dedupes above refuse to raise a new one, and
#: ``release_forgotten_tickets`` is the sweep that takes the mute off after 48
#: hours. One invariant, stated once, that stays true when a fourth dedupe is
#: written tomorrow — and it must stay a SUBSET of the queued statuses, or the
#: sweep would release a ticket the operator can no longer see.
#:
#: ENUMERATED, and it must stay enumerated — the opposite direction from the
#: queue readers above, on purpose. The muting question is «may this row stop
#: a paying customer from reaching a human», and the default answer for a word
#: nobody wrote down here is NO. Asked by negation it would be YES: a fourth
#: status invented by an author who never opened this file would silence every
#: channel that carried one, through three dedupes at once, with no exception
#: and no log line. The two directions are chosen by which default is safe for
#: the CUSTOMER, not by symmetry — an unknown word may cost the operator a
#: line of noise; it may never cost the customer his line.
#:
#: RELEASED is deliberately not here: that is the entire content of the word.
MUTING_STATUSES: frozenset[str] = frozenset({OPEN})
