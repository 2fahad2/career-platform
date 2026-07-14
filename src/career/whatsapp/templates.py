"""WhatsApp message-template specs (whatsapp §08) — DATA, not sent to Meta here.

Templates are submitted to Meta manually (a Fahad step); this registry is the
source of the wording/variables so rendering and submission stay in sync. The
daily template is provided in TWO wordings so its Utility-vs-Marketing
classification can be tested and the economics built on the approved one.

Free-form service messages inside an open window need no template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TemplateCategory(StrEnum):
    UTILITY = "utility"
    MARKETING = "marketing"


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    language: str
    category: TemplateCategory
    body: str
    buttons: tuple[str, ...] = ()
    variables: tuple[str, ...] = field(default_factory=tuple)


# Daily opportunity template — two wordings for the classification test (§08).
DAILY_UTILITY = TemplateSpec(
    name="daily_opportunities_utility",
    language="ar",
    category=TemplateCategory.UTILITY,
    body="لديك اليوم فرصتان مطابقتان لمسارك، وسيرتك الذاتية جاهزة. اضغط لعرضها.",
    buttons=("عرض الفرص",),
)
DAILY_MARKETING = TemplateSpec(
    name="daily_opportunities_marketing",
    language="ar",
    category=TemplateCategory.MARKETING,
    body="🎯 لقينا لك اليوم فرصتين متوافقتين مع مسارك والـCVs جاهزة — اعرض الفرص الآن!",
    buttons=("عرض الفرص",),
)

WELCOME_ACTIVATION = TemplateSpec(
    name="welcome_activation",
    language="ar",
    category=TemplateCategory.UTILITY,
    body="أهلًا بك في مساعد التوظيف. لنبدأ إعداد خدمتك — ردّ بأي رسالة للمتابعة.",
)
ONBOARDING_REMINDER = TemplateSpec(
    name="onboarding_reminder",
    language="ar",
    category=TemplateCategory.UTILITY,
    body="لم تكمل إعداد خدمتك بعد. أكمله الآن لنبدأ البحث لك يوميًا.",
)
RENEWAL_REMINDER = TemplateSpec(
    name="renewal_reminder",
    language="ar",
    category=TemplateCategory.UTILITY,
    body="اشتراكك ينتهي قريبًا. جدّد الآن لمواصلة استقبال الفرص اليومية.",
    buttons=("تجديد الاشتراك",),
)
RECOVERY = TemplateSpec(
    name="recovery",
    language="ar",
    category=TemplateCategory.MARKETING,  # marketing → opt-in list only
    body="نفتقدك! عد إلى مساعد التوظيف وواصل رحلتك نحو الفرصة المناسبة.",
    buttons=("العودة",),
)
ZERO_DAY_REPORT = TemplateSpec(
    name="zero_day_report",
    language="ar",
    category=TemplateCategory.UTILITY,
    body="لا فرص مطابقة اليوم. قد توسيع معاييرك يفتح فرصًا أكثر — اضغط للمراجعة.",
    buttons=("مراجعة المعايير",),
)

REGISTRY: dict[str, TemplateSpec] = {
    t.name: t
    for t in (
        DAILY_UTILITY, DAILY_MARKETING, WELCOME_ACTIVATION, ONBOARDING_REMINDER,
        RENEWAL_REMINDER, RECOVERY, ZERO_DAY_REPORT,
    )
}
