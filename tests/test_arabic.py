"""career.arabic — the fold, and the guard that keeps it a LEAF.

Two jobs, and the second one is the reason the file exists.

BEHAVIOUR. ``normalize_ar`` decides whether «اوافق» is the same word as
«أوافق», and every compliance surface in the product is downstream of that
answer: the consent verdict that becomes a PDPL record, the Meta-mandated
STOP/RESUME opt-out reading, the standing privacy commands, and the name
matching that keeps a customer's name out of every model prompt. A silent
change here does not fail loudly anywhere — it re-opens defects that were
found the hard way, in production. So the expectations below are written out
as literal before/after pairs rather than derived from the implementation:
a test that recomputes the fold would agree with any fold.

STRUCTURE. The fold used to live in ``onboarding.achievement_render``, which
imports ``career.cv.generate`` at module scope. ``onboarding.extraction`` could
not import it without closing a cycle and kept a second copy; ``whatsapp
.inbound``, documented as pure, dragged in ``career.cv.*``, ``career.db.models``
and SQLAlchemy to fold a string. :class:`TestLeaf` fails the build the moment
``career/arabic.py`` grows an import from ``career.*`` — which is the only
thing standing between us and that situation returning.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from career import arabic
from career.arabic import fold_token, normalize_ar

ARABIC_PY = Path(arabic.__file__)           # …/src/career/arabic.py
SRC = ARABIC_PY.parents[1]                  # …/src
REPO_ROOT = SRC.parent


class TestLeaf:
    """The guard. ``career.arabic`` imports nothing from ``career``."""

    def test_no_career_import_anywhere_in_the_module(self) -> None:
        """Static, and it reads the WHOLE tree — a deferred import inside a
        function would re-open the cycle just as effectively as a top-level
        one, only later and in production."""
        tree = ast.parse(ARABIC_PY.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    a.name for a in node.names
                    if a.name == "career" or a.name.startswith("career.")
                ]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:  # a relative import IS a career import
                    offenders.append(f"level-{node.level} relative import")
                elif module == "career" or module.startswith("career."):
                    offenders.append(module)
        assert offenders == [], (
            "career/arabic.py must import nothing from career — it is the leaf "
            f"every compliance path folds through. Found: {offenders}"
        )

    def test_imports_stay_cheap(self) -> None:
        """Nothing heavy from the standard library either. The fold must never
        fail to load, and the cheapest way to promise that is to give it
        nothing that can fail."""
        tree = ast.parse(ARABIC_PY.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"re", "__future__"}, imported

    def test_fresh_interpreter_loads_only_the_leaf(self) -> None:
        """The static guard's runtime twin: importing the module in a clean
        process must pull in no other ``career`` module at all."""
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys, career.arabic; "
             "print(sorted(m for m in sys.modules if m.startswith('career.')))"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "['career.arabic']", proc.stdout

    def test_inbound_is_pure_again(self) -> None:
        """whatsapp.inbound says "pure" on its first line, and it was not:
        folding a string cost it career.cv.*, career.db.models and all of
        SQLAlchemy, because the fold lived above it. This pins the claim."""
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys, career.whatsapp.inbound; "
             "print(sorted(m for m in sys.modules "
             "if m.startswith(('career.cv','career.db','career.engine','sqlalchemy'))))"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]", proc.stdout

    def test_both_import_directions_work(self) -> None:
        """The cycle the duplicate in extraction.py existed to avoid: import
        the two former copies in either order, in a fresh interpreter."""
        for first, second in (
            ("career.onboarding.extraction", "career.onboarding.achievement_render"),
            ("career.onboarding.achievement_render", "career.onboarding.extraction"),
        ):
            proc = subprocess.run(  # noqa: S603 — our own interpreter, fixed argv
                [sys.executable, "-c", f"import {first}; import {second}; print('ok')"],
                capture_output=True, text=True, cwd=REPO_ROOT,
                env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            )
            assert proc.returncode == 0, f"{first} → {second}: {proc.stderr}"
            assert proc.stdout.strip() == "ok"


class TestOneAuthority:
    """No copies. A future reader who greps for the fold finds one function."""

    def test_achievement_render_re_exports_the_same_object(self) -> None:
        from career.onboarding import achievement_render

        assert achievement_render.normalize_ar is normalize_ar

    def test_extraction_uses_the_same_object(self) -> None:
        from career.onboarding import extraction

        assert extraction.fold_token is fold_token
        assert extraction.AR_DIACRITICS is arabic.AR_DIACRITICS

    def test_inbound_and_orchestrator_use_the_same_object(self) -> None:
        from career.onboarding import orchestrator
        from career.whatsapp import inbound

        assert inbound.normalize_ar is normalize_ar
        assert orchestrator.normalize_ar is normalize_ar

    def test_no_second_copy_of_the_fold_table(self) -> None:
        """The hamza fold is a one-line mapping that is trivial to re-type,
        and it WAS re-typed — extraction.py carried its own copy for months.
        Exactly one file under ``src`` may spell it.

        (Deliberately narrow: the Arabic-Indic DIGIT tables in collection.py,
        console.py and achievement_render.py are a different translation with
        a different purpose, and they are not what went wrong.)"""
        needle = '"\u0623": "\u0627"'
        carriers = sorted(
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if needle in path.read_text(encoding="utf-8")
        )
        assert carriers == ["career/arabic.py"], carriers


class TestNormalizeAr:
    """Byte-frozen expectations, written by hand."""

    def test_empty_and_whitespace(self) -> None:
        assert normalize_ar(None) == ""
        assert normalize_ar("") == ""
        assert normalize_ar("   ") == ""
        assert normalize_ar("\t\n  \r") == ""
        assert normalize_ar("  متعدد     المسافات  ") == "متعدد المسافات"

    def test_hamza_seats_fold_to_bare_alef(self) -> None:
        """The live defect: «الغاء الإشتراك» was not read as the Meta-mandated
        opt-out because the hamza seat differed by one byte."""
        for spelling in ("أوافق", "إوافق", "آوافق", "ٱوافق", "اوافق"):
            assert normalize_ar(spelling) == "اوافق", spelling
        assert normalize_ar("إلغاء الاشتراك") == normalize_ar("الغاء الإشتراك")

    def test_taa_marbuta_folds_to_haa(self) -> None:
        """The other live miss: «مساعده» — what most Saudi keyboards produce —
        reached nobody."""
        assert normalize_ar("مساعدة") == "مساعده"
        assert normalize_ar("مساعده") == "مساعده"

    def test_alef_maqsura_waw_and_yaa_seats(self) -> None:
        assert normalize_ar("الفلانى") == "الفلاني"
        assert normalize_ar("مسؤول") == "مسوول"
        assert normalize_ar("مسئول") == "مسيول"

    def test_diacritics_and_tatweel_disappear(self) -> None:
        assert normalize_ar("فُلان") == "فلان"
        assert normalize_ar("مُوَافِقٌ") == "موافق"
        assert normalize_ar("أَوَّلًا") == "اولا"
        assert normalize_ar("ـتـطـويـر") == "تطوير"
        assert normalize_ar("أوافـــق") == "اوافق"

    def test_the_abda_collision_is_real_and_pinned(self) -> None:
        """«ابدأ» (start) and «أبدًا» (never) fold to the SAME string. That is
        why «ابدأ» was taken out of the RESUME set — a customer answering
        «never» had their opt-out cleared. If this ever stops being true the
        comment in inbound.py needs revisiting, not deleting."""
        assert normalize_ar("ابدأ") == normalize_ar("أبدًا") == "ابدا"

    def test_punctuation_and_emoji_become_spaces_not_nothing(self) -> None:
        """Spaces, deliberately: callers read whole TOKENS, and gluing two
        words together across a comma would invent a token nobody typed."""
        assert normalize_ar("مضبوط ✅") == "مضبوط"
        assert normalize_ar("😀موافق😀") == "موافق"
        assert normalize_ar("تسويق، دعم، مبيعات") == "تسويق دعم مبيعات"
        assert normalize_ar("«مضبوط»") == "مضبوط"
        assert normalize_ar("(موافق)") == "موافق"
        assert normalize_ar("لا تراسلوني، إلغاء الاشتراك") == (
            "لا تراسلوني الغاء الاشتراك"
        )

    def test_the_apostrophe_becomes_a_space(self) -> None:
        """Why «don't» was a dead entry in inbound's negation set: it folded
        to the two-word string "don t" and could never equal a token."""
        assert normalize_ar("don't") == "don t"
        assert normalize_ar("dont") == "dont"

    def test_arabic_indic_digits_survive(self) -> None:
        """They are ``\\w``. The achievement grounding guard reads numbers out
        of the customer's own answer — folding «٤٠» away would unground the
        one fact it exists to check."""
        assert normalize_ar("زدت المبيعات بنسبة ٤٠٪") == "زدت المبيعات بنسبه ٤٠"
        assert normalize_ar("٠١٢٣٤٥٦٧٨٩") == "٠١٢٣٤٥٦٧٨٩"
        assert normalize_ar("۰۱۲۳۴۵۶۷۸۹") == "۰۱۲۳۴۵۶۷۸۹"
        assert normalize_ar("2019 - 2023") == "2019 2023"

    def test_latin_lowercases_and_underscore_survives(self) -> None:
        assert normalize_ar("STOP") == "stop"
        assert normalize_ar("Unsubscribe") == "unsubscribe"
        assert normalize_ar("CamelCase") == "camelcase"
        assert normalize_ar("under_score") == "under_score"
        assert normalize_ar("kebab-case") == "kebab case"
        assert normalize_ar("[EMAIL_1]") == "email_1"

    def test_mixed_arabic_and_latin(self) -> None:
        assert normalize_ar("خبرة ١٠ سنوات في SQL") == "خبره ١٠ سنوات في sql"
        assert normalize_ar("ServiceNow و PowerBI") == "servicenow و powerbi"

    def test_non_string_input_does_not_raise(self) -> None:
        """A sticker carries no text; a classifier that raises there turns a
        photo into a 500."""
        assert normalize_ar(None) == ""


class TestFoldToken:
    """The name matcher's key. Same folds, but punctuation is REMOVED rather
    than spaced — the caller has already cut this word out of the text."""

    def test_internal_punctuation_is_removed_not_split(self) -> None:
        assert fold_token("Al-Fulani") == "alfulani"
        assert fold_token("alfulani") == "alfulani"
        assert fold_token("O'Hara") == "ohara"

    def test_arabic_spellings_collapse(self) -> None:
        assert fold_token("الفلانى") == fold_token("الفلاني") == "الفلاني"
        assert fold_token("فُلان") == "فلان"
        assert fold_token("نبذة") == "نبذه"

    def test_it_differs_from_normalize_ar_exactly_where_documented(self) -> None:
        """The one behavioural difference, pinned so the two never quietly
        converge: a whole-message fold keeps the boundary, a token fold does
        not."""
        assert normalize_ar("Al-Fulani") == "al fulani"
        assert fold_token("Al-Fulani") == "alfulani"

    def test_empty(self) -> None:
        assert fold_token("") == ""
        assert fold_token("!!!") == ""
