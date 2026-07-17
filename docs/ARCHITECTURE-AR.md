<div dir="rtl" align="right">

<h1>الدليل المعماري الكامل — شرح كل ملف في المشروع</h1>

<p>هذا الدليل يشرح <b>فائدة كل ملف</b> في المستودع ودوره في المنظومة.
النسخة الهندسية الإنجليزية في <code dir="ltr">README.md</code>،
والدستور الملزم في <code dir="ltr">CLAUDE.md</code>،
ومصدر الحقيقة المنتجي في <code dir="ltr">docs/WHITEPAPER.html</code>
مع <code dir="ltr">docs/CHANGELOG-v1.1.md</code>.</p>

<h2>الصورة الكبيرة — خمسة تدفقات</h2>

<ol>
<li><b>شراء ← تفعيل:</b> ويبهوك سلة ← تحقق توقيع ← تخزين بلا تكرار ← إعادة تحقق من سلة مباشرة ← تزويد عند «مدفوع» فقط ← رمز تفعيل (يُخزن مجزّأً) ← العميل يرسله واتساب ← يرتبط رقمه بحسابه.</li>
<li><b>إعداد ← نشط:</b> موافقة مدمجة بضغطة ← ١٤ سؤالًا بعدادات ← رفع السيرة عبر ست مراحل فحص ← تجريد الهوية ثم استخراج الحقائق بالذكاء الاصطناعي ← تأكيد كل حقيقة في «بنك الإنجازات» والمرفوض يدخل قائمة المحرمات ← تقييم المسار بثلاث طبقات ← سياسة بحث مرقمة ← نشط.</li>
<li><b>محرك الليل:</b> عائلات استعلام مشتقة من مسارات العملاء النشطين ← مصدران معزولان ← إزالة تكرار ← مخزون مشترك ← إثراء خلف حراس الاستدراج ← بوابة لكل عميل بسجل قرار يجيب «ليش أرسلتوها لي؟» ← حجب المكرر قبل السقف ← ترتيب حتمي بأثر معلل.</li>
<li><b>سيرة ← تسليم ← إغلاق صادق:</b> توليد بسلسلة حراس (مفردات البنك، منع الاختراع، المحرمات، تسرب العربية) ← نشر ذري ← سلطة الربط قبل أي إرسال ← حزمة واتساب: بطاقة ثم ملف ثم أزرار قرار ← حجب المسلَّم ← حالة يومية واحدة صادقة من سبع. الهوية الحقيقية تُحقن محليًا — <b>ولا نداء ذكاء اصطناعي يرى اسمًا أو رقمًا</b>.</li>
<li><b>قمع الاكتساب:</b> منتج تحليل السيرة بـ٢٩ ريالًا ← نفس سلطات الموافقة والرفع والاستخراج ← تقييم حتمي بنفس محرك المسارات ← تقرير عربي بصفحة واحدة ← زر ترقية يرث كل شيء.</li>
</ol>

<h2>جذر المستودع</h2>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">CLAUDE.md</code></td><td>دستور العمل: مصادر الحقيقة، القرارات المقفلة، ملخص الثوابت الخمسة عشر، الممنوعات بدون إذن صريح.</td></tr>
<tr><td><code dir="ltr">pyproject.toml</code></td><td>التبعيات وإعدادات الفحص الصارم (جودة وأمان وأنواع).</td></tr>
<tr><td><code dir="ltr">docker-compose.staging.yml</code></td><td>حزمة بيئة التجريب: قاعدة البيانات وريدس على منافذ محلية فقط + حاوية الواجهة.</td></tr>
<tr><td><code dir="ltr">Dockerfile</code></td><td>صورة حاوية الواجهة.</td></tr>
<tr><td><code dir="ltr">alembic.ini</code></td><td>مدخل هجرات قاعدة البيانات (تعمل بدور المالك).</td></tr>
<tr><td><code dir="ltr">.env.*.example</code></td><td>قوالب كاملة لكل متغير بيئة — الملفات الحقيقية لا تدخل قيت أبدًا.</td></tr>
<tr><td><code dir="ltr">Caddyfile</code></td><td>بوابة التشفير — لا يُكشف للإنترنت إلا مسارا الويبهوك وفحص الصحة.</td></tr>
<tr><td><code dir="ltr">README.md</code></td><td>الواجهة الهندسية الإنجليزية للمستودع.</td></tr>
</table>

<h2>النواة النقية <code dir="ltr">src/career_core/</code></h2>

<p>دوال محضة بلا شبكة وبلا قاعدة بيانات — «عقل» القرارات الحتمية، قابلة للاختبار بمعزل عن كل شيء.</p>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">gate.py</code></td><td>حساب البوابة النقي: تصنيف يقين الراتب، درجات جودة الشركة والدور، ومفتاح الترتيب الحتمي.</td></tr>
<tr><td><code dir="ltr">salary.py</code></td><td>قراءة الراتب من نص الإعلان وتصنيفه من «معلن مؤكد» إلى «مجهول».</td></tr>
<tr><td><code dir="ltr">identity.py</code></td><td>اشتقاق هوية الوظيفة القانونية — نفس الهوية للنشر والربط والحجب.</td></tr>
<tr><td><code dir="ltr">sentences.py</code></td><td>بوابة جودة الجملة الإنجليزية: ترفض الملخص المبتور أو المعلق.</td></tr>
<tr><td><code dir="ltr">ssrf.py</code></td><td>حارس فاشل-مغلق ضد استدراج السيرفر لعناوين داخلية، يفحص قبل التحويلات وبعدها.</td></tr>
<tr><td><code dir="ltr">urltools.py</code></td><td>تطبيع الروابط وإزالة وسوم التتبع.</td></tr>
</table>

<h2>قلب التطبيق <code dir="ltr">src/career/</code></h2>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">main.py</code></td><td>تطبيق الويب: استقبال ويبهوك سلة وواتساب (تحقق ← تخزين ← رد فوري، بلا منطق أعمال) + فحص الصحة.</td></tr>
<tr><td><code dir="ltr">config.py</code></td><td>كل إعدادات البيئة بأنواع صارمة؛ وعند التحميل تُسلَّح ماسحة الأسرار الحرفية.</td></tr>
<tr><td><code dir="ltr">logging_filters.py</code></td><td>فلتر يمسح الأسرار من كل سطر سجل: أنماط الروابط والمفاتيح + الأسرار المسجلة حرفيًا.</td></tr>
<tr><td><code dir="ltr">audit.py</code></td><td>كاتب سجل التدقيق — إلحاق فقط، لكل مستأجر.</td></tr>
<tr><td><code dir="ltr">tokens.py</code></td><td>رموز التفعيل — الخام يُعرض مرة واحدة والمخزن تجزئة فقط.</td></tr>
</table>

<h3>قاعدة البيانات <code dir="ltr">db/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">base.py</code></td><td>الأساس التصريحي بقواعد تسمية حتمية — نفس البنية في كل البيئات.</td></tr>
<tr><td><code dir="ltr">models.py</code></td><td>كل الجداول (فوق الثلاثين) مع ملاحظات العزل لكل جدول؛ ملفات PDF لا تدخل القاعدة أبدًا.</td></tr>
<tr><td><code dir="ltr">session.py</code></td><td>انضباط الدورين: جلسات التطبيق بهوية مستأجر فاشلة-مغلقة على مستوى المعاملة، وجلسات المالك للأعمال العابرة.</td></tr>
</table>

<h3>الاستقبال والطوابير <code dir="ltr">webhooks/ + queue/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">webhooks/intake.py</code></td><td>الاستقبال السريع المشترك: بصمة لكل حدث تمنع التكرار.</td></tr>
<tr><td><code dir="ltr">queue/adapter.py</code></td><td>مهايئ طابور ريدس خلف بروتوكول.</td></tr>
<tr><td><code dir="ltr">queue/message.py</code></td><td>مغلف رسالة الطابور — بلا هوية شخصية تعاقديًا.</td></tr>
<tr><td><code dir="ltr">queue/outbox.py</code></td><td>صندوق الصادر المعاملاتي — الحدث يُكتب مع التغيير التجاري ويُنشر بعد الالتزام.</td></tr>
</table>

<h3>سلة — التجارة <code dir="ltr">salla/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">signature.py</code></td><td>تحقق التوقيع بوقت ثابت — سر فارغ يعني رفض كل شيء.</td></tr>
<tr><td><code dir="ltr">webhook.py</code></td><td>قراءة الحدث (رقم الطلب داخل الغلاف) والاستقبال.</td></tr>
<tr><td><code dir="ltr">client.py</code></td><td>عميل سلة + القائمة البيضاء للمدفوع: طريقة دفع مجهولة = «معلق»، لا تزويد أبدًا.</td></tr>
<tr><td><code dir="ltr">provisioning.py</code></td><td>طلب ← مستأجر واشتراك ورمز تفعيل؛ لا يصدق الويبهوك بل يعيد التحقق مباشرة؛ آمن ضد التكرار.</td></tr>
<tr><td><code dir="ltr">subscriptions.py</code></td><td>آلة حالات الاشتراك الإحدى عشرة بجدول انتقالات صريح وسجل أحداث.</td></tr>
</table>

<h3>واتساب — المراسلة <code dir="ltr">whatsapp/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">signature.py</code></td><td>تحقق توقيع ميتا — فاشل-مغلق.</td></tr>
<tr><td><code dir="ltr">webhook.py</code></td><td>مصافحة ميتا واستقبال الوارد.</td></tr>
<tr><td><code dir="ltr">client.py</code></td><td>حدود الإرسال القابلة للحقن: نص ومستند وقالب وأزرار وتنزيل وسائط؛ الأخطاء تحمل الرمز فقط لا المحتوى.</td></tr>
<tr><td><code dir="ltr">window.py</code></td><td>حساب نافذة الأربع والعشرين ساعة + منبئ تذكير المساء للمشغّل.</td></tr>
<tr><td><code dir="ltr">adaptive.py</code></td><td>تخطيط التسليم: نافذة مفتوحة ← مباشر، مقفولة ← قالب وانتظار، موقف ← لا شيء.</td></tr>
<tr><td><code dir="ltr">delivery.py</code></td><td>التنفيذ: الحزم المجمعة (ترويسة ثم لكل وظيفة بطاقتها فملفها فأزرارها)، حالات صادقة، وعزل فشل كل وظيفة عن أخواتها.</td></tr>
<tr><td><code dir="ltr">templates.py</code></td><td>سجل القوالب المعتمدة.</td></tr>
<tr><td><code dir="ltr">inbound.py</code></td><td>تصنيف الوارد: رمز تفعيل أو إيقاف أو دعم أو غيره.</td></tr>
<tr><td><code dir="ltr">activation_flow.py</code></td><td>ربط الرمز بالمستأجر؛ كل فحوص الصلاحية قبل أي تعديل؛ واستثناء وراثة القمع الموثق.</td></tr>
<tr><td><code dir="ltr">worker.py</code></td><td>عامل المحادثة: عصمة لكل رسالة، إعادة تحقق الملكية من القاعدة، التوجيه للإعداد أو القمع، تسجيل أزرار «قدمت»، الإيصالات، الإيقاف والدعم.</td></tr>
</table>

<h3>الإعداد <code dir="ltr">onboarding/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">fsm.py</code></td><td>آلة الرحلة بعشر حالات للأمام فقط + حافة التراجع الوحيدة الموثقة + منبئ استحقاق التذكير.</td></tr>
<tr><td><code dir="ltr">orchestrator.py</code></td><td>عقل المحادثة: الموافقة المدمجة، الأسئلة بعداداتها، تأكيد الدفعة الواحدة، أوامر الخصوصية في كل حالة، ومشغّل التذكيرات.</td></tr>
<tr><td><code dir="ltr">collection.py</code></td><td>بنك الأسئلة (كل تسمية زر ضمن سقف واتساب بالتصميم) وقراءة الإجابات.</td></tr>
<tr><td><code dir="ltr">consents.py</code></td><td>سجل الموافقات المنفصلة بالغرض (إلحاق فقط) وبوابة فاشلة-مغلقة للمطلوب.</td></tr>
<tr><td><code dir="ltr">upload.py</code></td><td>خط رفع السيرة المحصن بست مراحل: حجم ← بصمة النوع الحقيقية ← ماسح ← فحص بنيوي ← تعقيم ← استخراج معزول.</td></tr>
<tr><td><code dir="ltr">extract_worker.py</code></td><td>الطفل المعزول: حدود ذاكرة ووقت قبل القراءة ومكتبات آمنة.</td></tr>
<tr><td><code dir="ltr">extraction.py</code></td><td>تجريد الهوية ← تأكيد الخلو (يرفض بدل أن يرسل) ← استخراج مهيكل؛ والنتيجة «مستخرجة» لا حقيقة.</td></tr>
<tr><td><code dir="ltr">confirmation.py</code></td><td>تأكيد الحقائق فردًا أو دفعة إلى بنك الإنجازات؛ والمرفوض يغذي قائمة المحرمات.</td></tr>
<tr><td><code dir="ltr">paths.py</code></td><td>تقييم المسار بثلاث طبقات (مطلوب/مقترح/معتمد) مع مسار الإصرار الموثق.</td></tr>
<tr><td><code dir="ltr">policy.py</code></td><td>بناء سياسة البحث المرقمة إصدارًا وبطاقة الملخص العربية.</td></tr>
<tr><td><code dir="ltr">privacy.py</code></td><td>تصدير وإيقاف واستئناف وحذف بخطوتين يحترم الاحتفاظ النظامي.</td></tr>
</table>

<h3>محرك الليل <code dir="ltr">engine/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">run.py</code></td><td>تأليف التشغيلة: خطة ← اكتشاف ← إزالة تكرار ← مخزون ← إثراء ← بوابة ← حجب ← ترتيب ← أثر؛ بحالات صادقة وإغلاق للتشغيلة في كل مخرج.</td></tr>
<tr><td><code dir="ltr">families.py</code></td><td>عائلات الاستعلام مشتقة ديناميكيًا من مسارات العملاء النشطين — لا شيء مثبت بالكود.</td></tr>
<tr><td><code dir="ltr">sources.py</code></td><td>مهايئا المصدرين: عزل لكل استعلام، مهلات وإعادات، تجريد الوسوم، وتوجيه رابط التقديم — دروس التشغيل الحي كلها هنا.</td></tr>
<tr><td><code dir="ltr">identity.py</code></td><td>إزالة تكرار نفس التشغيلة وقواعد دمج المصادر.</td></tr>
<tr><td><code dir="ltr">enrichment.py</code></td><td>إثراء الصفحة مرة لكل وظيفة خلف حارسي الاستدراج؛ والفشل يُخزن كحكم.</td></tr>
<tr><td><code dir="ltr">gate.py</code></td><td>بوابة كل عميل: صرامة السياسة، تكافؤ المناطق، سجل قرار بالأسباب، والتقاط «القريب من النجاح».</td></tr>
<tr><td><code dir="ltr">ranking.py</code></td><td>فلتر الحجب قبل السقف، الترتيب الحتمي، الخانات، الآثار المرقمة، وكاتب سجل الحجب.</td></tr>
<tr><td><code dir="ltr">quota.py</code></td><td>قرار إنذار رصيد البحث: صامت تجريبيًا، برتقالي عند العشرة بالمئة، أحمر عند الصفر.</td></tr>
<tr><td><code dir="ltr">cli.py</code></td><td>مدخل المؤقت: تشغيلة المحرك ← إنذارات الإدارة ← فحص الرصيد ← مرحلة التسليم.</td></tr>
</table>

<h3>السيرة الذاتية <code dir="ltr">cv/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">schemas.py</code></td><td>نماذج البيانات حرفيًا من المرجع.</td></tr>
<tr><td><code dir="ltr">template.py</code></td><td>القالب مستخرج بايتًا-بايتًا من المرجع الأصلي وتحرسه اختبارات إعادة الاستخراج.</td></tr>
<tr><td><code dir="ltr">render.py</code></td><td>الرندر: بايتات حتمية وخطوط مثبتة وصفحة واحدة بالضبط.</td></tr>
<tr><td><code dir="ltr">normalize.py</code></td><td>من البنك إلى السيرة الأم: ترتيب الجهات وقواعد الدور الحالي والمهارات بلا تكرار.</td></tr>
<tr><td><code dir="ltr">enforce.py</code></td><td>فرض الصفحة الواحدة: السقوف والإيقاع والملء المرتب حسب الإعلان — بلا اختراع.</td></tr>
<tr><td><code dir="ltr">validate.py</code></td><td>فاحص ما قبل الرندر: حارس تسرب العربية (مانع) والحدود البنيوية — رموز أسباب فقط.</td></tr>
<tr><td><code dir="ltr">prompts.py</code></td><td>البرومبتات حرفيًا؛ والتعبئة بإحلال حرفي محصن من حقن القوالب.</td></tr>
<tr><td><code dir="ltr">generate.py</code></td><td>سلسلة التفصيل وكل الحراس (منع الاختراع، المحرمات، بوابة الملخص) وبدائل قواعدية لكل مرحلة وعميل الذكاء الاصطناعي بعداد التوكنز.</td></tr>
<tr><td><code dir="ltr">publish.py</code></td><td>النشر الذري للزوج وسلطة الربط الوحيدة قبل الإرسال والمحلل والميزانية والحجر.</td></tr>
<tr><td><code dir="ltr">deliver.py</code></td><td>البطاقات العربية وأسماء الملفات المعروضة وباني الحزمة وأزرار القرار.</td></tr>
<tr><td><code dir="ltr">close.py</code></td><td>سلطة الحالات السبع اليومية وإغلاق اليوم والملخص الإداري وتجميع التكاليف.</td></tr>
<tr><td><code dir="ltr">daily_run.py</code></td><td>تنسيق يوم التسليم: عزل كل عميل، إغلاق المعلق المنتهي، الإغلاقات الصادقة، وعد التكلفة.</td></tr>
</table>

<h3>قمع الاكتساب <code dir="ltr">funnel/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">flow.py</code></td><td>شراء ← موافقة ← رفع ← تقرير ← اكتمال؛ يعيد استخدام سلطات الإعداد حرفيًا ويعيد الفتح عند شراء ثانٍ.</td></tr>
<tr><td><code dir="ltr">evaluation.py</code></td><td>التحليل الحتمي بخمس درجات على الحقائق المستخرجة بنفس محرك المسارات، بملاحظات عربية وخطوات ترقية.</td></tr>
<tr><td><code dir="ltr">report.py</code></td><td>تقرير عربي بصفحة واحدة وملخص واتساب.</td></tr>
</table>

<h3>برج المراقبة <code dir="ltr">telegram/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">admin.py</code></td><td>عميل بوت تيليجرام: إرسال معقم ولوحات أزرار وسحب طويل وأخطاء بلا محتوى.</td></tr>
<tr><td><code dir="ltr">console.py</code></td><td>الموجه عديم الحالة: قائمة سماح للمشغل وأزرار تحمل وجهتها كاملة وقراءات بلا أي هوية شخصية.</td></tr>
<tr><td><code dir="ltr">views.py</code></td><td>عارضو الشاشات العربية النقيون — مختبرون ذهبيًا.</td></tr>
<tr><td><code dir="ltr">messages.py</code></td><td>بناة رسائل الإدارة الصغيرة برموز العملاء فقط.</td></tr>
</table>

<h3>التخزين <code dir="ltr">storage/</code></h3>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">adapter.py</code></td><td>بروتوكول التخزين بواجهة متوافقة مع الخدمات السحابية ومفاتيح مسارات المستأجرين.</td></tr>
<tr><td><code dir="ltr">filesystem.py</code></td><td>التنفيذ الذري: مؤقت ← كتابة ← تثبيت ← استبدال ← تثبيت المجلد.</td></tr>
</table>

<h2>هجرات القاعدة <code dir="ltr">migrations/versions/</code></h2>

<table>
<tr><th>الهجرة</th><th>تضيف</th></tr>
<tr><td><code dir="ltr">0001</code></td><td>المستأجرين والمستندات — أساس العزل الفاشل-المغلق.</td></tr>
<tr><td><code dir="ltr">0002</code></td><td>صندوق الصادر وسجل العصمة.</td></tr>
<tr><td><code dir="ltr">0003</code></td><td>سجل التدقيق — إلحاق فقط.</td></tr>
<tr><td><code dir="ltr">0004</code></td><td>فوترة سلة: الخطط والاشتراكات ورموز التفعيل والاستقبال.</td></tr>
<tr><td><code dir="ltr">0005</code></td><td>واتساب: القنوات والتسليمات والرسائل.</td></tr>
<tr><td><code dir="ltr">0006</code></td><td>الإعداد: تسعة جداول من الجلسات إلى الرفعات.</td></tr>
<tr><td><code dir="ltr">0007</code></td><td>ترتيب كلي لأحداث الموافقة.</td></tr>
<tr><td><code dir="ltr">0008</code></td><td>المحرك: المخزون المشترك والتشغيلات وقرارات وحجوبات كل مستأجر.</td></tr>
<tr><td><code dir="ltr">0009</code></td><td>حقول التواصل: بريد ولينكدإن ومنطقة ومدينة.</td></tr>
<tr><td><code dir="ltr">0010</code></td><td>أحداث النتيجة — وقود القياس.</td></tr>
<tr><td><code dir="ltr">0011</code></td><td>الإغلاق الصادق: الاستخدام والتكاليف وحالات اليوم.</td></tr>
<tr><td><code dir="ltr">0012</code></td><td>جلسات القمع.</td></tr>
<tr><td><code dir="ltr">0013</code></td><td>خطة منتج التحليل باستحقاقات بحث مصفرة عمدًا.</td></tr>
<tr><td><code dir="ltr">0014</code></td><td>مؤشر بوت الإدارة.</td></tr>
</table>

<h2>المشغلات الحية <code dir="ltr">scripts/</code></h2>

<table>
<tr><th>السكربت</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">run_worker_loop.py</code></td><td>خدمة المحادثة: واتساب وسلة ومسح التذكيرات كل ساعة وتذكير النافذة المسائي ونبضة تيليجرام.</td></tr>
<tr><td><code dir="ltr">run_nightly.py</code></td><td>غلاف مدخل المحرك — هدف المؤقت.</td></tr>
<tr><td><code dir="ltr">run_admin_bot.py</code></td><td>حلقة برج المراقبة ومجسات الصحة الحية.</td></tr>
<tr><td><code dir="ltr">ci_create_app_role.py</code></td><td>إنشاء دور التطبيق غير-الخارق في خط التكامل.</td></tr>
<tr><td><code dir="ltr">demo_engine_tenants.py</code></td><td>بذر وتنظيف مستأجري العرض لإثباتات المحرك الحية.</td></tr>
</table>

<h2>البنية التحتية <code dir="ltr">ops/</code></h2>

<table>
<tr><th>المسار</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">career-worker.service</code></td><td>خدمة المحادثة — تعيد نفسها دائمًا.</td></tr>
<tr><td><code dir="ltr">career-engine-nightly.timer</code></td><td>تشغيلة الرابعة والنصف فجرًا بتوقيت الرياض — تعوض ما فاتها.</td></tr>
<tr><td><code dir="ltr">career-admin-bot.service</code></td><td>برج المراقبة — معزول عن عامل العملاء.</td></tr>
<tr><td><code dir="ltr">backup/backup.sh</code></td><td>نسخ مشفر خارج السيرفر: قاعدة البيانات ومخزن الملفات الحقيقي.</td></tr>
<tr><td><code dir="ltr">backup/restore-test.sh</code></td><td>تمرين الاسترجاع الشهري في قاعدة خردة مع تحقق العزل.</td></tr>
</table>

<h2>الحوكمة <code dir="ltr">docs/</code></h2>

<table>
<tr><th>الملف</th><th>فائدته</th></tr>
<tr><td><code dir="ltr">WHITEPAPER.html</code></td><td>دستور المنتج: المراحل بشروط خروجها والثوابت الخمسة عشر والباقات.</td></tr>
<tr><td><code dir="ltr">CHANGELOG-v1.1.md</code></td><td>القرارات المعتمدة بعد الإصدار الأول — تفوق ما يخالفها.</td></tr>
<tr><td><code dir="ltr">DEVIATIONS.md</code></td><td>كل انحراف معتمد بمسوغه.</td></tr>
<tr><td><code dir="ltr">PLAN.md</code></td><td>خطة التنفيذ الكاملة.</td></tr>
<tr><td><code dir="ltr">PROGRESS.md</code></td><td>الحالة جلسة بجلسة: المنجز وشروط الخروج والقرارات المعلقة.</td></tr>
<tr><td><code dir="ltr">ADMIN_BOT_DESIGN.md</code></td><td>تصميم برج المراقبة وتقييم الاقتراح الخارجي.</td></tr>
<tr><td><code dir="ltr">LEGACY_KNOWLEDGE.md</code></td><td>المرجع المقروء فقط من المحرك الشخصي المجرب.</td></tr>
<tr><td><code dir="ltr">policies/</code></td><td>الخصوصية والاسترجاع والشروط — للعملاء.</td></tr>
</table>

<h2>الاختبارات — ٦٤٦ اختبارًا</h2>

<p>أبرز العائلات: اختبارات هجومية على العزل (تزوير وقراءة عابرة)، عصمة
الويبهوك، ملفات هجوم الرفع، ربط السيرة والحجر، مصفوفة الحالات السبع،
عزل فشل الحزمة، العارضون الذهبيون، حراس المطابقة الحرفية للقوالب
والبرومبتات، ورحلات كاملة من حمولة ميتا الخام حتى النشاط.</p>

<p><b>قاعدة ذهبية:</b> الاختبارات على قاعدة الاختبار المهملة فقط —
أبدًا على بيئة التشغيل.</p>

</div>
