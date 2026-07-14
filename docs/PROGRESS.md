# PROGRESS — سجل الحالة

> يُحدَّث في نهاية كل جلسة: ما أُنجز، حالة شروط الخروج، الخطوة التالية، وأي قرار معلّق على فهد.
> المصدر: `docs/WHITEPAPER.html` §13 (خطة C0–C9 بشروط الخروج) + §15 (الثوابت).
> الترتيب تبعيات لا تأجيل — لا تُفتح مرحلة قبل تحقق شرط خروج سابقتها.

آخر تحديث: 14 يوليو 2026.

---

## نظرة عامة على المراحل

| المرحلة | العنوان | الحالة |
|---|---|---|
| C0 | التحقق التجاري | خارج نطاق كلود (نماذج ويب/أسرار/قرارات فهد) — جارٍ يدويًا |
| C1 | عزل المشروع / تهيئة المستودع | ✅ مكتملة |
| C2 | قاعدة المنصة | 🟡 النطاق المطلوب هذه الجلسة منجز ومُختبَر · شرط الخروج الكامل (§13) **جزئي** — انظر أدناه |
| P-A | النواة النقية career_core + اختبارات القبول | ✅ مكتملة (14 يوليو) |
| P-B | إغلاق C2 (CI، نسخ/Restore، Queue/Outbox، Audit، فلتر أسرار) | 🟡 P-B.1 CI ✅ · P-B.2 فلتر أسرار ✅ · P-B.3 Queue/Outbox/Worker ✅ · P-B.4 Audit ✅ · **يتبقّى فقط: النسخ/Restore (مؤجل بقرار فهد)** |
| C3 | سلة: webhooks · اشتراكات · تفعيل | 🟡 الكود منجز ومُختبَر (25 اختبارًا) · شرط الخروج يحتاج شراء ريال حي + إعداد فهد اليدوي |
| C4 | واتساب: تسليم تكيفي · تفعيل · دعم | 🟡 الكود منجز ومُختبَر (42 اختبارًا) · شرط الخروج يحتاج WABA حي + إعداد Meta اليدوي |
| C5–C9 | — | لم تبدأ |

---

## C1 — تهيئة المستودع ✅

**أُنجز:**
- إعادة ترتيب الوثائق: `docs/` (WHITEPAPER.html, CHANGELOG-v1.1.md) و`docs/policies/` (policy-*.md الثلاثة)؛ `CLAUDE.md` و`START_HERE.md` في الجذر.
- `pyproject.toml`: Python 3.11+ · FastAPI · SQLAlchemy 2 · Alembic · psycopg 3 · redis · pydantic-settings؛ أدوات ruff + mypy + pytest. (النظام يشغّل Python 3.12.3، متوافق مع `>=3.11`؛ صورة Docker مثبّتة على `python:3.11`.)
- `.gitignore` يستثني `.env*` و`data/` والنسخ والكاش؛ `.env.example` + `.env.staging.example` + `.env.production.example` (بلا أي سر حقيقي — الثابت §15.15).
- `README.md` مختصر بالإنجليزية.
- حزمة `src/career/`: `config.py` (إعدادات من البيئة، DSN منفصل لدور التطبيق ودور المالك)، `main.py` (FastAPI + `/health` يفحص DB+Redis)، `db/` (base, session بحقن `app.tenant_id`, models)، `storage/` (StorageAdapter + Filesystem بكتابة ذرّية).
- سقالة Alembic: `alembic.ini` + `migrations/env.py` (يعمل بدور المالك) + template.
- اختبارات storage بلا قاعدة (6/6 ناجحة)، ruff نظيف، الحزمة تُستورد.

**تقييم شرط الخروج (§13):** «Repo تجاري يبني ويختبر … والشخصي لم يُمس.»
- ✅ يبني ويُستورد ويلتّ (ruff) ويشغّل اختبارات (pytest — storage).
- ✅ المشروع الشخصي (`cef71c3`) لم يُلمس — بُني كل شيء من الصفر، صفر بيانات منسوخة.
- ⚠️ ملاحظة صدق: الـTag على الشخصي `cef71c3` إجراء على مستودع منفصل لا أملك وصوله من هنا — إجراء فهد اليدوي. لا يمنع بناء/اختبار هذا الريبو.
- ملاحظة استضافة (CHANGELOG §2): «السيرفر السعودي» في نص §13 مخفّف إلى مرحلتين — هذا السيرفر (EU) = staging، والانتقال للسعودي شرط خروج قبل الموجة 2.

**الخطوة التالية:** C2.

---

## C2 — قاعدة المنصة 🟡

**أُنجز ومُختبَر (النطاق المطلوب هذه الجلسة):**
- **Docker على السيرفر:** Docker 29.1.3 + Compose v2.40 (مُثبّتان).
- **بيئتان معزولتان تمامًا:** `docker-compose.staging.yml` (name=`career_staging`) و`docker-compose.production.yml` (name=`career_production`) — مشروعان مستقلان بشبكات وvolumes ومنافذ وملفات أسرار (`.env.staging` / `.env.production`) وقواعد منفصلة تمامًا؛ لا يتشاركان شيئًا (§10). كلاهما `config` صحيح بنيويًا.
- **PostgreSQL 16 + Redis 7 لكل بيئة**، ومنافذ الداتاستور مربوطة على `127.0.0.1` فقط (لا تُكشف على واجهة السيرفر العامة).
- **خدمة FastAPI + healthcheck:** `/health` يفحص DB+Redis؛ healthcheck في Compose. تم رفع staging فعليًا والحاويات الثلاث `healthy`، و`/health` يعيد `{"status":"ok","checks":{"database":true,"redis":true}}`.
- **StorageAdapter:** واجهة S3-متوافقة مجرّدة + `FilesystemStorageAdapter` بمسار `tenants/<tenant_id>/...` وكتابة ذرّية (temp→fsync→replace + fsync للمجلد، §15.7). 6 اختبارات خضراء بلا قاعدة.
- **أساس RLS + أول migration (`0001`):** جدول `tenants` (سجل، ENABLE-only ليؤسّسه دور المالك) + جدول `documents` (tenant-scoped، ENABLE+FORCE). سياسات على `NULLIF(current_setting('app.tenant_id',true),'')::uuid` — تفشل **مغلقة** عند غياب السياق. دور تطبيق `career_app` غير superuser وبلا BYPASSRLS (يُنشأ عبر initdb)، والمهاجرات تعمل بدور المالك.
- **اختبار cross-tenant هجومي (§15.10):** 5 اختبارات خضراء تتصل كـ`career_app`: (1) الدور ليس superuser، (2) كل مستأجر يرى صفوفه فقط، (3) القراءة عبر الحدود تُرجع صفرًا حتى مع WHERE صريح، (4) الكتابة عبر الحدود تُرفض بـWITH CHECK، (5) بلا سياق مستأجر لا يُرى شيء (fail-closed).
- **إجمالي الاختبارات: 11/11 خضراء · ruff نظيف · mypy strict نظيف.**

**بق أُصلح أثناء التنفيذ:** السياسة الأولى استخدمت `current_setting(...)::uuid` مباشرة، فكان GUC مخصّص يعود `''` (لا NULL) على اتصال من الـpool بعد انتهاء المعاملة → خطأ `''::uuid`. أمسكه اختبار fail-closed؛ أُصلح بـ`NULLIF`.

**تقييم شرط خروج C2 الكامل (§13) — مُحدَّث بعد P-B:** «اختبارات Cross-tenant تفشل في الاختراق وتنجح في CI، وRestore تجريبي ناجح من نسخة خارجية.»
- ✅ **Cross-tenant:** محقّق فعليًا (اختبارات عزل ضد Postgres 16 حقيقي).
- ✅ **CI:** **دُفع ونجح فعليًا على GitHub Actions (run 29368124941، أخضر في 53 ثانية).** يشغّل ruff+mypy+alembic upgrade+alembic check+pytest على Postgres 16 + Redis 7، ومع `CI_REQUIRE_DB=1` تُنفَّذ اختبارات العزل الهجومية على عنقود حقيقي (لا تُتخطّى). هذا يحقّق شق «تنجح في CI» من شرط الخروج.
- ❌ **نسخ خارجي مشفّر + اختبار Restore:** **مؤجل بقرار فهد** («بسويه بعدين») — التصميم كامل في PLAN P-B.5. هذا البند الوحيد المتبقّي لإغلاق شرط خروج C2.
- ✅ **Queue + Outbox + Audit** (كانت بنود §10 الأوسع): أُنجزت في P-B.3/P-B.4 (migrations 0002/0003) مع اختبارات عزل هجومية. StorageAdapter مهيّأ لاستبدال S3 لاحقًا دون تغيير الطبقات الأعلى.

> **خلاصة صادقة (§15.12 لا نجاح صامت):** بعد P-B، **كل بنود C2 منجزة ومُختبَرة، وCI أخضر على GitHub**. يتبقّى بند **واحد فقط** لإغلاق شرط خروج C2 رسميًا: **النسخ الخارجي المشفّر + اختبار Restore** (مؤجل بقرارك، تصميمه في PLAN P-B.5).

> **قرار فهد صريح (14 يوليو 2026):** «نؤجله» — نبدأ C3 مع بقاء بند النسخ/Restore **مفتوحًا موثّقًا**. يصبح **شرط خروج إلزامي قبل الموجة 1 الفعلية** (أول شراء حقيقي حتى بالريال) — لا نُطلق على أي رقم حقيقي بلا نسخ احتياطي مُختبَر. شرط خروج C2 يُغلق رسميًا عند تنفيذه.

---

## اعتماد المراجع الهندسية (14 يوليو 2026)

- `docs/LEGACY_KNOWLEDGE.md` (2676 سطرًا، sha256 مُتحقق: `23295ff1…9a86`) اعتُمد مرجعًا هندسيًا **للقراءة فقط** — خلاصة المحرك الشخصي المُثبت (يقابل «الموروث» في الورقة §07). قُرئ كاملًا.
- `docs/DEVIATIONS.md` أُنشئ: 12 انحرافًا معتمدًا (Anthropic-only، واتساب للعميل، Postgres بدل JSON، حد الراتب per-tenant، LinkedIn بلا تسجيل دخول، اسم الملف المعروض للعميل، …).
- `docs/PLAN.md` أُنشئ: الخطة التفصيلية الكاملة P-A → P-B → C3…C9 بشروط الخروج — العقد المضاد للانحراف.
- قرارات فهد المثبتة: multi-tenant ✓ · واتساب/تيليجرام ✓ · Postgres ✓ · لا auto-apply أبدًا ✓ · المفاتيح تُطلب باسم مزودها عند مرحلتها ✓ · الترتيب: P-A ثم P-B ✓.

## P-A — النواة النقية `career_core` ✅ (14 يوليو 2026)

**أُنجز (اختبارات أولًا ثم التنفيذ — صفر شبكة/LLM/DB):**
- `tests/acceptance/` — **117 اختبار قبول** مشتقة من الـVerification Record (الملحق الأخير في LEGACY): الهوية والتطبيع، الراتب بكل حالاته الحدية، البوابة والترتيب، الجمل الكاملة، SSRF، مفاتيح الكبت.
- `src/career_core/`: `urltools` (سلطة التطبيع الواحدة + delivered_key) · `identity` (URL-v1) · `salary` (parsing + بوابة أدلة بعتبة per-tenant محقونة — D4) · `sentences` (§1.6 حرفيًا) · `ssrf` (fail-closed، resolver قابل للحقن، قائمتا حجب Legacy/Default — D7) · `gate` (tier/source/role-match/decision/ranking/fit — أوزان قابلة للحقن D10).
- **مرساة الـVerification Record مُتحققة:** role match = 70 بالضبط لـ"IT Operations Manager" بلا JD.
- النتيجة: **128/128 اختبارًا أخضر** (قبول + storage + RLS ضد Postgres حي) · ruff نظيف · mypy strict نظيف.

**شرط الخروج (PLAN):** ✅ كل اختبارات القبول خضراء بلا شبكة، الحزمة مستقلة، الانحرافات مسجلة في DEVIATIONS.

## P-B — إغلاق C2 🟡 (14 يوليو 2026)

- **P-B.1 CI ✅:** `.github/workflows/ci.yml` (Postgres 16 + Redis 7 services) → ruff · mypy · alembic upgrade · alembic check (drift) · pytest، على Python 3.11. `scripts/ci_create_app_role.py` ينشئ دور `career_app` غير الـsuperuser (hermetic عبر psycopg). `CI_REQUIRE_DB=1` يحوّل تخطّي اختبارات العزل إلى **فشل صريح** (تحقّقت من المسارين). بادج في README.
- **P-B.2 فلتر أسرار ✅:** `src/career/logging_filters.py` — نقل §9.2 معمَّم لكل القنوات (bot-URL، أسرار query، KV مع Bearer/Basic، أسرار مسجّلة)، تعقيم msg/args/traceback، يُركّب على handlers، `install_secret_redaction` يثبّت httpx/httpcore على WARNING. 18 اختبارًا (منها حالتان أمسكتا بق أول نقل).
- **P-B.3 Queue/Outbox/Worker ✅:** migration 0002 — `outbox_events` (ENABLE-only، relay بدور المالك عبر المستأجرين) + `processed_messages` (idempotency، ENABLE+FORCE). `QueueMessage` (المفاتيح الأربعة) · InMemory/Redis queue · outbox ذرّي same-txn + relay · **worker يعيد التحقق من الملكية من القاعدة عبر RLS (§15.11)**: مرجع مزوّر عبر المستأجرين غير مرئي → يُرفض؛ claim بـON CONFLICT قبل المعالجة → redelivery = no-op؛ فشل المعالج يرجّع الـclaim. 22 اختبارًا.
- **P-B.4 Audit ✅:** migration 0003 — `audit_events` tenant-scoped (ENABLE+FORCE، **append-only**: INSERT/SELECT فقط للتطبيق). `record_audit` مركزي يمرّر details عبر مُنقّي الأسرار (defense-in-depth). 5 اختبارات: عزل، رفض tenant مزوّر (WITH CHECK)، تنقية السر، منع UPDATE/DELETE. 
- **الإجمالي: 173/173 اختبارًا أخضر · ruff نظيف · mypy strict نظيف · محاكاة CI من الصفر (migrations 0001–0003) ناجحة.**

**المتبقّي في P-B:** بند واحد فقط — النسخ/Restore الخارجي (مؤجل بقرار فهد). CI دُفع ونجح على GitHub ✅.

**التالي:** تنفيذ النسخ/Restore (عند إذن فهد) → إغلاق C2 → C3 (سلة).

## C3 — سلة 🟡 (14 يوليو 2026)

**الكود منجز ومُختبَر (migration 0004 + 25 اختبارًا):**
- **الجداول:** `plan_entitlements` (مرجعية مبذورة من §04: 149/279/449 بصلاحياتها) · `subscriptions` · `subscription_events` · `activation_tokens` (tenant-scoped، ENABLE+FORCE RLS) · `webhook_events` (intake نظامي بلا RLS، fingerprint فريد).
- **توقيع سلة** (`signature.py`): HMAC-SHA256، مقارنة ثابتة الزمن، fail-closed.
- **الاستقبال** (`webhook.py`): تحقّق التوقيع → fingerprint → dedupe بـON CONFLICT → 200 فورًا؛ توقيع خاطئ = 401 **بلا كتابة**؛ مكرّر = 200 duplicate.
- **العميل** (`client.py`): `SallaClient` Protocol + `FakeSallaClient` + `HttpSallaClient` هيكل يُوصَل عند وصول المفتاح.
- **التزويد** (`provisioning.py`): يعيد التحقق من الطلب عبر Salla API (لا يثق بالـwebhook) → **provisioning فقط عند `paid`** → tenant + اشتراك `PAID_UNCLAIMED` + activation token (الخام يُعاد مرة، يُخزَّن hash فقط). idempotent عبر `salla_order_id` فريد + pre-check. `process_pending_webhooks` = الـworker.
- **آلة الحالات** (`subscriptions.py`): 11 حالة + انتقالات مُتحقّقة؛ refund/cancel/chargeback تُعطّل فورًا؛ النهائية idempotent.
- **Endpoint** `POST /webhooks/salla` + `install_secret_redaction()` فُعّل قبل أول تكامل خارجي.
- **الاختبارات (25):** توقيع·حالات·استقبال·تزويد·دورة حياة·endpoint — منها: **حدث مكرّر → اشتراك واحد**، توقيع خاطئ يُرفض، غير مدفوع لا يُزوَّد، منتج مجهول يُتجاهل، **استرداد يُعطّل فورًا**.
- **الإجمالي المتراكم: 198/198 أخضر · ruff+mypy نظيفان · محاكاة CI من الصفر (0001–0004) ناجحة.**

**شرط خروج C3 (§13):** المنطق مُثبت بالاختبارات، لكنه يُغلق فقط بشراء حي. يحتاج (👤 فهد): Partner App في portal.salla.partners + `SALLA_WEBHOOK_SECRET`/`SALLA_API_KEY` + منتج الريال المخفي + شراء تجريبي → عندها أُوصِّل `HttpSallaClient` وأتحقّق حيًّا.

## C4 — واتساب 🟡 (15 يوليو 2026)

**الكود منجز ومُختبَر (migration 0005 + 42 اختبارًا). كرّرنا معمارية C3 (سلطة intake واحدة، worker بدور المالك):**
- **الجداول:** `customer_channels` (الرقم PII، `unique(provider, phone)`، نافذة عبر `last_inbound_at`) · `deliveries` (وحدة التسليم التكيفي، `unique(tenant, run_date)`) · `delivery_messages` · `inbound_messages` (`wa_message_id` فريد = idempotency) · `support_events`. كلها ENABLE+FORCE RLS.
- **سلطة intake موحّدة** `webhooks.intake.persist_deduped_event` يستخدمها سلة وواتساب (لا تفرّع).
- **المنطق النقي (now محقون):** نافذة 24 ساعة (OPEN/CLOSED/OPTED_OUT، opt-out يتجاوز) · تصنيف الوارد (STOP/دعم/activation/other، مطابقة صارمة) · مخطّط التسليم التكيفي (§08).
- **التفعيل (جوهر شرط الخروج):** وارد `تفعيل <token>` → hash → `activation_tokens` → إنشاء channel (رقم↔tenant) + تعليم التوكن مستخدمًا + اشتراك `PAID_UNCLAIMED → ONBOARDING`. idempotent + رفض invalid/expired/used/conflict مع رد عربي وتنبيه إداري بلا PII.
- **التسليم التكيفي:** نافذة مفتوحة → إرسال مباشر؛ مغلقة → قالب صباحي + حفظ الـbundle → **descent** عند أول تفاعل؛ opted-out → لا إرسال.
- **الوارد:** STOP → opt-out فوري + تأكيد · دعم → `support_events` + تصعيد إداري (TEN-#### فقط) · other → descent. إيصالات التسليم تُحدِّث `delivery_messages`.
- **الحدود المحقونة:** `WhatsAppClient`/`TelegramAdminClient` (Protocol + Fake + Http skeleton) · **توقيع Meta** `X-Hub-Signature-256` + تحدّي GET · endpoints `GET/POST /webhooks/whatsapp` · فلتر تنقية الأسرار مُفعّل.
- **القوالب كبيانات** (بما فيها اليومي بصياغتين utility/marketing §08) — تُقدَّم لـMeta يدويًا.
- **الاختبارات (42):** المجال النقي · التوقيع/التحدّي · التفعيل (يربط الطلب بالرقم، ONBOARDING، idempotent، invalid/expired/conflict) · التسليم (مفتوح/مغلق+descent/opted-out) · الـworker (STOP فوري، دعم بلا PII، descent، status callback، idempotency) · endpoints.
- **الإجمالي المتراكم: 240/240 أخضر · ruff+mypy نظيفان · محاكاة CI من الصفر (0001–0005) ناجحة.**

**شرط خروج C4 (§13):** «توكن التفعيل يربط طلبًا برقم، والتسليم التكيفي يعمل بالحالتين، وSTOP يوقف فورًا» — **مُثبت بالاختبارات**. يُغلق حيًّا عند (👤 فهد): Meta Business Portfolio + تطبيق + WABA تجريبي + رقم اختبار + `WHATSAPP_*`/`TELEGRAM_ADMIN_*` + اعتماد القوالب → عندها أُوصِّل `HttpWhatsAppClient`/`HttpTelegramAdminClient` وأتحقّق حيًّا.

## قرارات معلّقة على فهد

- **Q1:** النسخ الاحتياطي — **أجّله فهد 14 يوليو** («بسويه بعدين»). التصميم الكامل جاهز في PLAN P-B.5؛ يبقى بندًا مفتوحًا يمنع إغلاق شرط خروج C2 حتى التنفيذ.
- ~~Q2~~ **محسوم 14 يوليو:** يُجمع «راتب متوقع» من العميل (ليس شرط قبول صارمًا) — المعلن الأقل من المتوقع يُحجب مع near-miss؛ غير المعلن **لا يُحجب افتراضيًا** (السياسة الافتراضية «متوازنة»). مسجل في DEVIATIONS D4.
- ~~Q3~~ **محسوم 14 يوليو:** `FirstName LastName - Job Title.pdf`، وعند التصادم اليومي تُضاف الشركة. مسجل في DEVIATIONS D8.
- أسرار البيئتين الحقيقية تُلصق في `.env.staging` / `.env.production` (غير متتبعة) — placeholders فقط الآن.
- قرارات قسم 16 من الورقة (الاسم، تخصصا الصديقين، رقم WABA، السيرفر السعودي…) تفتح C0/C3+ لكنها لا تعيق P-A/P-B.
