"""WhatsApp message-template specs (whatsapp §08) — DATA, not sent to Meta here.

Templates are submitted to Meta manually (a Fahad step); this registry is the
source of the wording/variables so rendering and submission stay in sync. The
daily template is provided in TWO wordings so its Utility-vs-Marketing
classification can be tested and the economics built on the approved one.

**A CATEGORY IS META'S ANSWER, NOT OUR CLAIM** (audit 2026-08-08). This module
used to carry one field, ``category``, and every reader in the tree — the daily
chooser, the WhatsApp bill in `cv/close.whatsapp_spend`, the nightly journal
line — read it as a statement of fact about the live account. It was not. It
was the category we ASKED for at submission, and a read-only
``GET /{waba}/message_templates?fields=name,status,category,previous_category``
run on 2026-08-08 showed the two diverging on FIVE of the eight live templates,
each of them carrying ``previous_category: UTILITY``:

===============================  ==========  =================================
name                             at Meta     previously
===============================  ==========  =================================
subscription_daily_report        UTILITY     — (held)
daily_service_update             MARKETING   UTILITY
daily_opportunities_utility      MARKETING   UTILITY
welcome_activation               MARKETING   UTILITY
onboarding_reminder              MARKETING   UTILITY
renewal_reminder                 MARKETING   UTILITY
daily_opportunities_marketing    MARKETING   — (asked for, got it)
recovery                         MARKETING   — (asked for, got it)
===============================  ==========  =================================

``previous_category`` is the load-bearing column: Meta ACCEPTED these as
UTILITY and moved them to MARKETING **after approval**, without us touching
them. So the failure is not «somebody typed the wrong constant» — a hand-typed
constant CANNOT be right, because the value it names is edited by somebody
else, later, silently. ``daily_opportunities_utility`` is the extreme case: a
name that asserts a category Meta had already taken away.

The field is therefore split in two, and neither half pretends to be the other:

* :attr:`TemplateSpec.requested_category` — what we submit. Ours, permanent,
  and never evidence of anything about the live account.
* :data:`META_CATEGORY_OBSERVED` — what Meta answered, dated, with the query
  that produced it written down. Refreshed by the nightly probe, which already
  calls this endpoint; :func:`category_divergences` is what makes a drift loud
  instead of a thing discovered in an invoice.

BOTH DIRECTIONS ARE WIRED, and they are not redundant. Meta PUSHES
`template_category_update` the moment it moves a template — immediate, and
missable: it goes to one URL, once, and an app that is not subscribed (which
this one was not, verified 2026-08-08) never receives it at all. The nightly
GET is slow — up to a day late — and cannot be missed: it re-reads the whole
list from scratch every night whatever happened to any webhook. The push
tells you tonight; the poll guarantees you find out. :func:`record_observed_category`
is the one door both come through, so a measurement arriving either way
outranks the constant in this file and says so at ERROR.

WHAT META'S RULE ACTUALLY IS, checked against the live documentation on
2026-08-08 rather than remembered (developers.facebook.com → business-messaging
→ whatsapp → templates/template-categorization). UTILITY needs BOTH halves:
«must be non-promotional, not containing any promotional or persuasive intent»
AND «specific to or requested by the user» or «essential or critical to the
user». Having already paid does NOT make a message utility — Meta's own two
examples are a subscription pair: *«Reminder: Your monthly payment for
{{service}} will be billed on {{date}}»* is UTILITY, and *«Your subscription
will expire on {{date}}! Renew today to save {{discount}}»* is MARKETING, filed
under retargeting. Persuasion decides, not entitlement. «Mixed content» and
«contents are unclear» both fall to MARKETING by rule.

The three consequences, which are separate risks and only one of them is money:

1. **Price.** Saudi Arabia, Meta's live card on 2026-08-08: marketing $0.0501
   per delivered message, utility $0.0107 — ~4.7×, not the ~2.4× the older
   comments in this tree assumed (KSA's marketing rate rose 2026-04-01).
   Utility has volume tiers down to $0.0080; marketing has none. Since
   2025-07-01 a UTILITY template delivered inside an open 24h customer service
   window is FREE; a marketing one never is.
2. **Throughput.** Messaging limits are portfolio-wide and CATEGORY-NEUTRAL
   (they count unique recipients reached outside a service window), and quality
   rating is per TEMPLATE, not per category — so the category does not change
   the throttle directly. What it changes is the feedback: marketing content is
   what draws the blocks and low read-rates that drive a rating down into
   template pausing (132015) and permanent disabling (132016).
3. **DELIVERABILITY, which is the one that is not about billing.** A recipient
   who has switched «Offers and announcements» off for this business does not
   receive a MARKETING template at all: «the API will process the request but
   not send the message» — HTTP 200, error 131050 on the status webhook only,
   and Meta's own advice is «do not retry». The per-user marketing cap (131049)
   is a second such path and is active in Saudi Arabia. A customer who paid for
   a daily service and does not receive it because it was filed as an
   advertisement is a product failure, not a billing error. That is why
   :data:`FALLBACK_DAILY` and :func:`preferred_daily_template` prefer a
   measured-UTILITY template over a cheaper-looking name.

Free-form service messages inside an open window need no template.

**A button label is a routing decision, not decoration.** A tap arrives as an
ordinary inbound whose text is the LABEL (worker._text_of reads
``button.text``), so a label nobody routes is a dead end with a nice name:
«تجديد الاشتراك» and «العودة» matched no standing command and no outcome
button, and the customer who tapped the renewal reminder on day 27 was
answered with the generic «أنا معك يوميًا» — no link, no price, no
instruction. The two lifecycle templates now carry a label the router already
understands, so the tap lands on the subscription-status reply, which states
the days left and prints the renewal route (the store link, or «دعم» while no
storefront is configured).

That is the fix that works with no other change; the fuller one is to accept
the natural labels as aliases in ``orchestrator._PRIVACY_COMMANDS`` and give
the buttons back their marketing wording. Templates are submitted to Meta by
hand, so any label change here is only real once resubmitted — which is free
today, since these two are not approved yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

logger = logging.getLogger("career.whatsapp")


class TemplateCategory(StrEnum):
    UTILITY = "utility"
    MARKETING = "marketing"


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    language: str
    #: The category we ASK Meta for when the template is submitted. It is a
    #: request, never a fact: Meta decides at review and — as five of these
    #: prove — may decide again after approval. Nothing that costs money or
    #: decides deliverability may read this field; read
    #: :func:`observed_category` or :func:`billed_category` instead.
    requested_category: TemplateCategory
    body: str
    buttons: tuple[str, ...] = ()
    variables: tuple[str, ...] = field(default_factory=tuple)

    # `TemplateSpec.category` IS GONE, on purpose. It was a shim that resolved
    # to the observed category and fell back to `requested_category` when Meta
    # had never been asked — i.e. it could answer «utility» about a template
    # nobody had ever measured, which is precisely the claim this module exists
    # to stop making. Its only two readers were the log lines in
    # `engine/cli.py`, and they now read :func:`billed_category`. New code
    # reads :func:`observed_category` (which can answer «I do not know») or
    # :func:`billed_category` (which errs expensive).


#: Strictly transactional wording — and, on 2026-08-08, THE ONLY ONE OF THE
#: EIGHT THAT IS UTILITY AT META. `daily_service_update` was accepted as
#: UTILITY too and was later moved to MARKETING (it still carries
#: ``previous_category: UTILITY``); the difference between the two bodies is
#: that this one never mentions «الفرص». Their classifier reads a message that
#: announces opportunities as promotion, no matter what the customer paid for.
#: So this says only what is transactionally true: the report you subscribe to
#: is ready, and your files are with it.
#:
#: It is young — approved 2026-08-05 — and «held UTILITY» is a claim with a
#: date on it, not a property. The nightly divergence check is what will say
#: whether it keeps holding.
SUBSCRIPTION_DAILY_REPORT = TemplateSpec(
    name="subscription_daily_report",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body=("تحديث اشتراكك: تقريرك اليومي جاهز، ومعه ملفاتك بصيغة PDF. "
          "اضغط لاستلامه."),
    buttons=("استلام",),
)

#: Submitted 2 August as the UTILITY-shaped replacement for
#: `daily_opportunities_utility`, accepted as UTILITY — **and MARKETING at Meta
#: today** (``previous_category: UTILITY``). It is the second template on this
#: account to be accepted as utility and re-categorised afterwards, which is
#: the evidence that «Meta accepted the wording» is not a durable result.
#:
#: The wording is still right and is kept: no offer, no enticement, no
#: promotional punctuation, tied to «اشتراكك». What changed is the conclusion
#: drawn from it — a body can earn UTILITY at review and lose it later, so the
#: only honest reading of a category is a measured one.
DAILY_SERVICE_UPDATE = TemplateSpec(
    name="daily_service_update",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body=("تحديث خدمتك اليومي: اكتمل بحث اليوم على اشتراكك، وجهّزنا سيرتك "
          "الذاتية للفرص المختارة. اضغط للاطلاع على التفاصيل."),
    buttons=("عرض التفاصيل",),
)

# Daily opportunity template — two wordings for the classification test (§08).

#: **THE NAME IS A LIE AND CANNOT BE FIXED.** `daily_opportunities_utility` is
#: MARKETING at Meta (``previous_category: UTILITY``) and a template name is
#: immutable once created, so the string that goes on the wire will say
#: «utility» for as long as this template exists. The Python symbol is left
#: alone for the same reason a rename would be theatre — `cv/daily_run.py`
#: imports it and the wire name would not change anyway. What is fixed is that
#: nothing in the tree now DERIVES anything from the word: the category comes
#: from :data:`META_CATEGORY_OBSERVED`, and the test suite pins this name to
#: MARKETING so a reader who trusts the name is contradicted immediately.
DAILY_UTILITY = TemplateSpec(
    name="daily_opportunities_utility",
    language="ar",
    # Historical: this IS what was asked for, and Meta said no.
    requested_category=TemplateCategory.UTILITY,
    body="لديك اليوم فرصتان مطابقتان لمسارك، وسيرتك الذاتية جاهزة. اضغط لعرضها.",
    buttons=("عرض الفرص",),
)
#: Honest on both counts: promotional wording, asked for MARKETING, got it, and
#: it is the ONE daily template whose name, request and live category agree.
DAILY_MARKETING = TemplateSpec(
    name="daily_opportunities_marketing",
    language="ar",
    requested_category=TemplateCategory.MARKETING,
    body="🎯 لقينا لك اليوم فرصتين متوافقتين مع مسارك والـCVs جاهزة — اعرض الفرص الآن!",
    buttons=("عرض الفرص",),
)

#: THE FIRST MESSAGE A PAYING BUYER EVER RECEIVES (zero-touch activation,
#: `salla/provisioning._announce_provision`), and MARKETING at Meta today. It
#: is the send with the least margin for a category error in the whole
#: product: the buyer's REPLY to it is what claims the subscription, so a
#: message that does not arrive is a paid order that never activates and a
#: customer who is never onboarded — with no error anywhere, because a
#: message a recipient's settings suppress is not a send failure.
WELCOME_ACTIVATION = TemplateSpec(
    name="welcome_activation",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body="أهلًا بك في مساعد التوظيف. لنبدأ إعداد خدمتك — ردّ بأي رسالة للمتابعة.",
)
#: Sent only to a customer who has ALREADY PAID and stalled mid-setup
#: (`onboarding/orchestrator.send_due_reminders`), and only when the 24h window
#: is shut — so this template is the only way to reach him at all. MARKETING at
#: Meta today.
ONBOARDING_REMINDER = TemplateSpec(
    name="onboarding_reminder",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body="لم تكمل إعداد خدمتك بعد. أكمله الآن لنبدأ البحث لك يوميًا.",
)
#: The one label the inbound router resolves to a real answer about billing
#: (orchestrator._PRIVACY_COMMANDS → subscription_status_summary, which prints
#: the days left AND the way to renew). Every lifecycle template's button uses
#: it, so no nudge can end in the generic fallback again.
RENEW_BUTTON_AR = "حالة اشتراكي"

#: Goes to a LIVE, PAYING customer three days before his period ends
#: (`salla/lifecycle`), and MARKETING at Meta today. Its body — «جدّد الآن»,
#: an instruction to buy — is the one of the three that a reviewer can
#: reasonably read as promotion, and it is also the one whose non-arrival is
#: least catastrophic: the customer stays served until the period ends.
RENEWAL_REMINDER = TemplateSpec(
    name="renewal_reminder",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body="اشتراكك ينتهي قريبًا. جدّد الآن لمواصلة استقبال الفرص اليومية.",
    buttons=(RENEW_BUTTON_AR,),
)
#: Honestly marketing and always was: re-engagement of a customer whose
#: subscription expired seven days ago. He is not owed this message, and a
#: recipient who has switched marketing off is entitled not to get it.
RECOVERY = TemplateSpec(
    name="recovery",
    language="ar",
    requested_category=TemplateCategory.MARKETING,  # opt-in list only
    body="نفتقدك! عد إلى مساعد التوظيف وواصل رحلتك نحو الفرصة المناسبة.",
    buttons=(RENEW_BUTTON_AR,),
)
#: NEVER SUBMITTED TO META AND NEVER SENT — it has no call site anywhere in the
#: tree and does not appear in the live template list. It is kept as approved
#: WORDING for the zero-match day; sending it today would be rejected outright.
#: Its absence from :data:`META_CATEGORY_OBSERVED` is the machine-readable form
#: of that sentence, and is pinned by a test.
ZERO_DAY_REPORT = TemplateSpec(
    name="zero_day_report",
    language="ar",
    requested_category=TemplateCategory.UTILITY,
    body="لا فرص مطابقة اليوم. قد توسيع معاييرك يفتح فرصًا أكثر — اضغط للمراجعة.",
    buttons=("مراجعة المعايير",),
)

REGISTRY: dict[str, TemplateSpec] = {
    t.name: t
    for t in (
        SUBSCRIPTION_DAILY_REPORT, DAILY_SERVICE_UPDATE, DAILY_UTILITY,
        DAILY_MARKETING, WELCOME_ACTIVATION, ONBOARDING_REMINDER,
        RENEWAL_REMINDER, RECOVERY, ZERO_DAY_REPORT,
    )
}

#: ── WHAT META SAYS ──────────────────────────────────────────────────────────
#:
#: Measured, not asserted. Source, reproducible with a read-only GET and no
#: side effects (never a POST — a category change at Meta is Fahad's to make):
#:
#:     GET https://graph.facebook.com/v21.0/{WABA_ID}/message_templates
#:         ?fields=name,status,category,previous_category&limit=100
#:
#: Observed 2026-08-08. All eight are APPROVED; the three `jaspers_market_*`
#: and `hello_world` samples Meta ships with a new account are deliberately
#: absent — they are not ours and we never send them.
META_CATEGORY_OBSERVED_AT = date(2026, 8, 8)
META_CATEGORY_OBSERVED: dict[str, TemplateCategory] = {
    "subscription_daily_report": TemplateCategory.UTILITY,
    "daily_service_update": TemplateCategory.MARKETING,       # was UTILITY
    "daily_opportunities_utility": TemplateCategory.MARKETING,  # was UTILITY
    "daily_opportunities_marketing": TemplateCategory.MARKETING,
    "welcome_activation": TemplateCategory.MARKETING,         # was UTILITY
    "onboarding_reminder": TemplateCategory.MARKETING,        # was UTILITY
    "renewal_reminder": TemplateCategory.MARKETING,           # was UTILITY
    "recovery": TemplateCategory.MARKETING,
}


#: ── A MEASUREMENT NEWER THAN THIS FILE ──────────────────────────────────────
#:
#: :data:`META_CATEGORY_OBSERVED` is dated, and the date is the point: it was
#: true on 2026-08-08. Meta then PUSHES `template_category_update` the moment
#: it moves a template, and the nightly probe GETs the same fact — both are
#: measurements, both are newer than the file, and neither can edit it. A
#: running process that has just been TOLD `welcome_activation` is MARKETING
#: and goes on answering UTILITY from a week-old constant is making exactly the
#: claim this module was rewritten to stop making.
#:
#: WHAT WAS REJECTED, and why:
#:
#: * **Alert only, leave the constant.** The operator learns; the code does
#:   not. Between the alert and the hand edit — hours at best, and the five
#:   proved it can be weeks — every bill and every daily-template choice is
#:   made from a value we know to be wrong. The alert is necessary and it is
#:   not sufficient.
#: * **Rewrite the source file.** A process that edits its own module is a
#:   deploy nobody reviewed, it is lost on the next `git checkout`, and it puts
#:   a write into a path that must never fail. Refused outright.
#: * **A new table.** The right long-term shape and it needs a migration —
#:   the one thing the change that DISCOVERS a problem cannot ship. Named in
#:   the report, deliberately not run.
#:
#: WHAT THIS IS. An in-memory overlay, per process, holding measurements
#: carrying THEIR OWN date, and read only when that date is not older than the
#: file's. It is a BRIDGE, not a store, and its three limits are stated rather
#: than discovered:
#:
#: 1. it dies with the process, so the worker's knowledge does not reach the
#:    nightly run — which is why `engine/cli._preferred_daily` records the
#:    same fact from its own GET at the start of every night;
#: 2. it never becomes the file, so the durable record is the `webhook_events`
#:    row, the ERROR line below, and the operator's hand;
#: 3. it holds only names in :data:`REGISTRY`. The live account also carries
#:    Meta's own `hello_world` and `jaspers_market_*` samples, and this module
#:    saying anything at all about a template we never send would be the same
#:    unearned claim in a new place.
_LIVE_OBSERVED: dict[str, tuple[TemplateCategory, date]] = {}


def record_observed_category(
    name: str, category: str, *, observed_on: date, source: str,
) -> bool:
    """Record what Meta just said about ``name``. True if it changed anything.

    Callers: the webhook push (`whatsapp.worker`) and the nightly poll
    (`engine.cli`). Both hand over Meta's own words and the date Meta said
    them; neither passes a guess.

    A measurement that CONTRADICTS the file is logged at ERROR with the exact
    edit that would make the file true again — ERROR because the operator's
    harvester (`run_admin_bot._HealthProbes.error_lines`) forwards ERROR lines
    and nothing quieter, and with the edit spelled out because «a divergence
    was detected» is not something anyone can act on at 6am.

    Refuses, silently and by design: an unparseable category (we do not guess
    at a word Meta did not send), a name outside :data:`REGISTRY`, and a
    measurement older than the file — a late webhook redelivery must not undo
    a fresher reading.
    """
    try:
        measured = TemplateCategory(str(category).lower())
    except ValueError:
        logger.error(
            "meta reported an unknown template category %r for %s — not "
            "recorded; add it to TemplateCategory or the pricing stays blind",
            category, name,
        )
        return False
    if name not in REGISTRY:
        return False
    if observed_on < META_CATEGORY_OBSERVED_AT:
        return False
    previous = _LIVE_OBSERVED.get(name)
    if previous is not None and previous[1] > observed_on:
        return False
    _LIVE_OBSERVED[name] = (measured, observed_on)
    on_file = META_CATEGORY_OBSERVED.get(name)
    changed = on_file != measured or (previous or (None, None))[0] != measured
    if on_file is not None and on_file != measured:
        logger.error(
            "template category DIVERGED from this file (%s): meta says %s is "
            "%s, META_CATEGORY_OBSERVED says %s — edit "
            "whatsapp/templates.py to '\"%s\": TemplateCategory.%s,' and move "
            "META_CATEGORY_OBSERVED_AT to %s",
            source, name, measured, on_file, name, measured.name,
            observed_on.isoformat(),
        )
    elif on_file is None:
        logger.error(
            "meta reports a category for %s (%s), a template this file has "
            "never measured — %s; record it in META_CATEGORY_OBSERVED",
            name, source, measured,
        )
    return changed


def live_observations() -> dict[str, tuple[TemplateCategory, date]]:
    """A copy of the overlay — for tests and for anyone reporting on it."""
    return dict(_LIVE_OBSERVED)


def forget_live_observations() -> None:
    """Drop every runtime observation. Tests use it; nothing in `src/` does."""
    _LIVE_OBSERVED.clear()


def observed_category(name: str) -> TemplateCategory | None:
    """Meta's category for ``name``, or None when we have never measured it.

    None is a real answer and callers must handle it. It is what a hand-typed
    ``category`` field could never say, and saying it is most of the fix.

    The NEWEST measurement wins: a `template_category_update` push or tonight's
    probe outranks the dated constant, and the constant answers whenever
    nothing fresher has arrived in this process. Neither half ever invents an
    answer — a name measured by nobody still returns None.
    """
    live = _LIVE_OBSERVED.get(name)
    if live is not None:
        return live[0]
    return META_CATEGORY_OBSERVED.get(name)


def billed_category(name: str) -> TemplateCategory:
    """The category the WhatsApp bill must assume for ``name``.

    Observed when we have it, MARKETING when we do not — the same rule
    `cv/close._wa_kind_of` already applied to unrecognised template names, for
    the same reason: **an unverified category must never make a bill look
    smaller than it is.** Every one of the five templates that quietly moved to
    MARKETING had been billed at the utility rate until this existed. On Meta's
    live Saudi Arabia rate card (verified 2026-08-08: utility $0.0107,
    marketing $0.0501 per delivered message) that is **21% of the true price**,
    on what `salla/lifecycle._live_channel` documents as the MAJORITY of this
    account's billed template traffic.

    KNOWN OVERSTATEMENT, in the other direction and much smaller: since
    2025-07-01 Meta delivers UTILITY templates free inside an open 24-hour
    customer service window. `salla/lifecycle._send_template` does not check
    the window, so a lifecycle template that lands inside one is billed here at
    a price Meta did not charge. It cannot be netted out without recording the
    window state on the ledger row, and over-reporting is the safe direction.
    """
    return observed_category(name) or TemplateCategory.MARKETING


def category_divergences(
    live: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """``{name: (recorded, live)}`` for every template whose category at Meta
    is not the one recorded above — the check that makes this snapshot a
    measurement instead of a stale constant.

    Meta re-categorises APPROVED templates without asking (five of eight here),
    so a snapshot with no re-check is exactly the defect it replaced, one week
    older. The nightly probe already fetches this list; comparing it costs one
    dict lookup per row and turns «found out from the invoice» into a log line
    the operator's ERROR harvester forwards the same night.

    Names Meta reports that we have never recorded are NOT divergences — the
    account carries Meta's own sample templates — but a name we record and Meta
    does not is, and it reads as ``(recorded, "absent")``: a template we would
    happily try to send and that no longer exists.
    """
    out: dict[str, tuple[str, str]] = {}
    for name, recorded in META_CATEGORY_OBSERVED.items():
        actual = str(live.get(name, "absent")).lower()
        if actual != str(recorded):
            out[name] = (str(recorded), actual)
    return out


#: The daily template the delivery run should use, best first. A UTILITY
#: template is cheaper per message AND — the part that is not about money — is
#: not subject to Meta's marketing controls, so it reaches a recipient who has
#: switched marketing messages off. A paying customer must never miss the
#: delivery he bought because it was filed as an advertisement. Falling back is
#: deliberate: a template awaiting Meta's review would otherwise stop the day.
DAILY_PREFERENCE: tuple[TemplateSpec, ...] = (
    SUBSCRIPTION_DAILY_REPORT, DAILY_SERVICE_UPDATE, DAILY_UTILITY,
    DAILY_MARKETING,
)

#: Where the day goes when Meta cannot be asked. It was `DAILY_UTILITY` on the
#: reasoning «the only name we have ever seen Meta accept» — true when written,
#: false since: `subscription_daily_report` is APPROVED (verified 2026-08-08)
#: and is the ONLY daily template that is UTILITY at Meta. Both names are
#: equally approved, so the fallback now costs the utility rate instead of the
#: marketing one and is not exposed to marketing suppression — on the one path
#: where nobody is watching, which is precisely where the cheap-and-deliverable
#: option belongs.
FALLBACK_DAILY: TemplateSpec = SUBSCRIPTION_DAILY_REPORT


def preferred_daily_template(
    approved: Mapping[str, str] | Iterable[str] | None,
) -> TemplateSpec:
    """The cheapest, most deliverable daily template Meta has actually approved.

    ``approved`` is what the live account reports, and its TYPE says how much
    the caller managed to find out:

    * ``Mapping[name, category]`` — status *and* category came back. The choice
      is then made on Meta's own answer: any approved candidate Meta calls
      utility beats every candidate it calls marketing, whatever our names or
      our submissions claim. This is the only form that cannot be wrong.
    * an iterable of names — approved, category unknown (the old contract, kept
      so a caller that has not been updated still works). Falls back to the
      hand-ordered preference, which is a guess about categories.
    * ``None`` — we could not ask at all (no credentials, or Meta unreachable),
      so we do not gamble the day on an unverified name: :data:`FALLBACK_DAILY`.
    """
    if approved is None:
        return FALLBACK_DAILY
    live: dict[str, str] = {}
    if isinstance(approved, Mapping):
        live = {str(k): str(v).lower() for k, v in approved.items()}
        names = set(live)
    else:
        names = {str(n) for n in approved}
    candidates = [spec for spec in DAILY_PREFERENCE if spec.name in names]
    if not candidates:
        return FALLBACK_DAILY
    if live:
        utility = [
            spec for spec in candidates
            if live.get(spec.name) == TemplateCategory.UTILITY
        ]
        if utility:
            return utility[0]
        # Every approved candidate is marketing today. Say so: the day still
        # runs, but it runs at the marketing rate and can be suppressed for a
        # customer who has turned marketing off, and that is not a silent fact.
        logger.error(
            "no daily template is UTILITY at Meta — sending %s at the "
            "marketing rate, which a recipient's marketing settings may "
            "suppress entirely", candidates[0].name,
        )
    return candidates[0]
