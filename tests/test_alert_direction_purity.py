"""The bidi guard: every operator-facing line is direction-pure, forever.

Fahad reads every alert this system produces on a chat client that REVERSES
any line mixing Arabic with Latin letters or Latin digits. A line that reads
«↻ تجديد TEN-0002 — الفترة الجديدة تنتهي» on our side arrives at him
scrambled, and it arrived scrambled on every single renewal. §15.13 already
forbids a phone or a name in that channel; this file enforces the other half
of the same rule — that what we DO send him is readable.

The repo has fixed individual bidi warts at least three times and they came
back, because every fix was a hand-written assertion about one string
(`tests/test_activation_flow.py`, `test_engine_cli_exit.py`,
`test_ops_watchdogs.py`, `test_admin_console.py` … each with its own private
copy of the check). A string nobody remembered to list was a string nobody
checked. So this guard takes NO list of strings. It reads the source.

────────────────────────────────────────────────────────────────────────────
WHAT COUNTS AS OPERATOR-FACING — derived, not listed
────────────────────────────────────────────────────────────────────────────

Two structural rules, both read off the tree:

1. **The sinks.** ``career/telegram/admin.py`` defines the operator client.
   Its methods that carry a ``text`` parameter ARE the operator channel —
   ``send_admin``, ``send_screen``, ``edit_screen``, ``answer_callback`` —
   and the guard learns those names by parsing that class, so a fifth method
   added tomorrow is covered without touching this file. Every call to them
   anywhere in ``src/`` and ``scripts/`` is a checkpoint, and the text
   argument is resolved BACKWARDS to the strings that can reach it.

   The closure matters as much as the seed: ``salla/provisioning._alert``,
   ``promises/guarantee._alert`` and ``promises/career_session`` are thin
   wrappers that forward their own ``text`` parameter into ``send_admin``, so
   a guard that only looked at ``send_admin`` call sites would see almost
   nothing. A fixpoint promotes any function that passes a parameter of its
   own into a sink position to being a sink itself, on that parameter.

2. **The console package.** Everything under ``career/telegram/`` is the
   operator's out-of-band channel by construction — that package's own
   docstring says so («It NEVER renders PII: only TEN-#### codes, counts, and
   opaque status»). Its screens are assembled into ``lines`` lists and joined
   by a dispatcher the backward resolution cannot follow without guessing, so
   the package is checked whole. There is no customer copy in it to
   false-positive on.

Everything else is OUT of scope, deliberately. Customer-facing Arabic mixes
scripts ON PURPOSE — «اكتب اسمك بالأحرف الإنجليزية (مثال: Fahad Almulhim)»,
«أرسل سيرتك كملف PDF أو Word (DOCX)» — and those messages are read by
customers on their own clients, not by Fahad on his. A guard that flagged
them would be wrong 100+ times on the first run and switched off by lunch.

────────────────────────────────────────────────────────────────────────────
WHAT COUNTS AS MIXED — the precise rule
────────────────────────────────────────────────────────────────────────────

The unit is the **LINE**, never the string: every operator message here is
multi-line, and the whole discipline is «put the Latin bit on its own line».

A line is a violation when it contains Arabic script AND any of:

* a **Latin letter** (ASCII, or any code point whose Unicode name starts with
  LATIN — an accented identifier scrambles exactly like a plain one);
* an **ASCII digit** ``0-9``. This is the one that fires on real alerts:
  counts, day totals, riyal amounts and ISO dates all render as European
  digits and drag the line's direction with them;
* an **unresolved interpolation** — an ``{…}`` slot whose value the guard
  could not prove is Arabic. In this codebase a slot is a TEN code, a count,
  a date, a URL, a store id or a shell command far more often than it is
  Arabic prose, so an unknown slot is treated as Latin. It is not a guess
  where it can be avoided: the resolver follows module constants, local
  assignments (including ``+=``), f-strings, ``.format()`` templates,
  ``"…".join(lines)`` elements, both branches of a conditional expression,
  and function returns across module boundaries — so
  ``f"…والتجديد التلقائي يفشل — {_REINSTALL_AR}"`` resolves to its Arabic
  text and passes, while ``f"↻ تجديد {code} —…"`` cannot and fails.

Explicitly **allowed**, because getting this wrong is how a guard gets muted:

* **Arabic-Indic digits** ``٠١٢٣٤٥٦٧٨٩`` and their Eastern forms — they are
  Arabic script, they are used deliberately (the console's own «٧ أيام» /
  «٣٠ يومًا» buttons), and they do not flip a line.
* **Emoji and symbols** — 🔴 ⚠️ ↻ ⭐ 🪑 are not Latin. Neither are «», —, ·,
  ← → or ٪ (U+066A, the Arabic percent sign). Directionally neutral
  characters never break a line and are never reported.
* **Docstrings and comments**, which no operator ever reads.

────────────────────────────────────────────────────────────────────────────
SCOPE HONESTY
────────────────────────────────────────────────────────────────────────────

The same defect lives in modules this agent does not own, so the guard is
written wide and left red there rather than narrowed to the two files that
could be fixed. ``test_operator_alerts_are_direction_pure_tree_wide`` FAILS
today, naming every offender with its file and line, and the failure belongs
to those files' owners: the admin console screens carry the bulk of it
(``telegram/views``, ``telegram/weekly_report``, ``telegram/console``), with
smaller counts in ``whatsapp/worker``, ``salla/lifecycle``, ``engine/quota``,
``onboarding/retention``, ``cv/daily_run``, ``promises/career_session``,
``whatsapp/activation_flow`` and ``scripts/run_admin_bot``. Shrinking the
scope to make the suite green would certify a cleanliness the tree does not
have; the two repaired files have their own green test below, so the fix is
provable while the remaining debt stays visible and assignable.

(``salla/renewal`` needed no repair: its three constants are the CUSTOMER's
confirmation, pure Arabic, and the announcer appends the date on a line of
its own — the shape the operator's notice has now been brought into.)
"""

from __future__ import annotations

import ast
import re
import unicodedata
from functools import cache
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
ADMIN_CLIENT = SRC / "career/telegram/admin.py"
CONSOLE_PACKAGE = SRC / "career/telegram"

#: The two files this agent owns and has repaired.
OWNED = ("src/career/salla/provisioning.py", "src/career/salla/renewal.py")

#: Marker standing in for an interpolation whose value could not be proved
#: Arabic. NUL can never occur in source text, so it cannot be forged.
SLOT = "\x00"

#: Arabic script: the main block plus supplements and presentation forms.
#: Arabic-Indic digits (U+0660-U+0669) live inside it on purpose — they are
#: Arabic, they are allowed, and a line made only of them is an Arabic line.
_ARABIC = re.compile(
    "[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"
)
_ASCII_DIGIT = re.compile("[0-9]")

#: Traversal limits. The resolver walks a call graph, so it needs both a
#: recursion bound and a cap on how many alternative renderings one
#: expression may fan out to (a screen built from six conditionals would
#: otherwise multiply out into thousands of near-identical strings).
_MAX_DEPTH = 8
_MAX_ALTS = 32


def _is_latin(ch: str) -> bool:
    if ch.isascii():
        return ch.isalpha()
    try:
        return unicodedata.name(ch).startswith("LATIN")
    except ValueError:
        return False


def _mixes_or_latin(text: str) -> bool:
    """Could this fragment drag an Arabic line's direction? Used to decide
    whether a resolved interpolation is safe to inline."""
    return (
        SLOT in text
        or bool(_ASCII_DIGIT.search(text))
        or any(_is_latin(c) for c in text)
    )


def line_is_mixed(line: str) -> bool:
    """The rule itself, on one line. Public so a reader can check it by eye."""
    if not _ARABIC.search(line):
        return False
    return (
        SLOT in line
        or bool(_ASCII_DIGIT.search(line))
        or any(_is_latin(c) for c in line)
    )


# ── source model ────────────────────────────────────────────────────────────


@cache
def _parse(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"))


@cache
def _module_path(dotted: str) -> str | None:
    """``career.salla.renewal`` → the file, if it is ours. None for stdlib."""
    candidate = SRC / (dotted.replace(".", "/") + ".py")
    if candidate.exists():
        return str(candidate)
    candidate = SRC / dotted.replace(".", "/") / "__init__.py"
    return str(candidate) if candidate.exists() else None


class _Module:
    """One file, indexed the three ways the resolver asks about it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.tree = _parse(path)
        self.consts: dict[str, ast.expr] = {}
        self.funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        #: local name → dotted module (``import x.y as z``, ``from a import b``
        #: where b is itself a module — the shape used all over this codebase)
        self.modules: dict[str, str] = {}
        #: local name → (dotted module, attribute) for ``from a.b import c``
        self.imports: dict[str, tuple[str, str]] = {}
        self._index()

    def _index(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.modules[alias.asname or alias.name.split(".")[0]] = (
                        alias.name
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue          # relative imports are not used here
                for alias in node.names:
                    local = alias.asname or alias.name
                    dotted = f"{node.module}.{alias.name}"
                    if _module_path(dotted):
                        self.modules[local] = dotted
                    else:
                        self.imports[local] = (node.module, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
        for stmt in self.tree.body:      # module-level constants only
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self.consts[target.id] = stmt.value
            elif (isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name) and stmt.value):
                self.consts[stmt.target.id] = stmt.value


@cache
def _load(path: str) -> _Module:
    return _Module(path)


#: Arabic-Indic and Eastern-Arabic-Indic digits — allowed inside an Arabic
#: line, and used on purpose (`telegram/console._ar_digits`).
_AR_DIGITS = set("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")


@cache
def arabic_digit_folders(path: str) -> frozenset[str]:
    """Functions in this file that turn a number into ARABIC digits.

    ``telegram/console._ar_digits`` is the one that exists today, and its
    docstring states the rule this guard enforces: «a number can live INSIDE
    an Arabic line without scrambling it». A guard that did not recognise it
    would flag «منذ ٣ ساعات» as mixed — the textbook way to get switched off.

    Recognised structurally, not by name: the function returns
    ``….translate(T)`` where ``T`` is a module-level translation table whose
    OUTPUT alphabet is Arabic-Indic digits. Rename the helper, add a second
    one, and the guard still knows.
    """
    module = _load(path)
    tables = set()
    for name, value in module.consts.items():
        if not (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "maketrans"):
            continue
        produced = "".join(
            arg.value for arg in value.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        )[-10:]
        if produced and set(produced) <= _AR_DIGITS:
            tables.add(name)
    folders = set()
    for fname, func in module.funcs.items():
        for node in ast.walk(func):
            if (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "translate"
                    and node.value.args
                    and isinstance(node.value.args[0], ast.Name)
                    and node.value.args[0].id in tables):
                folders.add(fname)
    return frozenset(folders)


def _scopes(tree: ast.Module) -> dict[int, ast.AST | None]:
    """node id → the nearest enclosing function, for local-variable lookup."""
    out: dict[int, ast.AST | None] = {}

    def walk(node: ast.AST, current: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = current
            walk(
                child,
                child
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current,
            )

    walk(tree, None)
    return out


# ── sinks: discovered, then closed over wrappers ────────────────────────────


def _text_params(func: ast.FunctionDef, drop_self: bool) -> set[int]:
    args = [a.arg for a in func.args.args]
    if drop_self and args and args[0] in ("self", "cls"):
        args = args[1:]
    return {i for i, name in enumerate(args) if name == "text"}


def discover_sinks() -> dict[str, set[int]]:
    """The operator channel, read off the client class that defines it.

    Not a list this file maintains: it is whatever ``TelegramAdminClient`` and
    its implementations expose with a ``text`` parameter, so the guard tracks
    the channel as the channel changes.
    """
    module = _load(str(ADMIN_CLIENT))
    sinks: dict[str, set[int]] = {}
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                positions = _text_params(item, drop_self=True)
                if positions:
                    sinks.setdefault(item.name, set()).update(positions)
    assert sinks, "no operator sink found — telegram/admin.py changed shape"
    return sinks


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _sink_args(node: ast.Call, positions: set[int]) -> list[ast.expr]:
    out = [node.args[i] for i in sorted(positions) if i < len(node.args)]
    out += [kw.value for kw in node.keywords if kw.arg == "text"]
    return out


def close_over_wrappers(
    paths: list[Path], sinks: dict[str, set[int]]
) -> dict[str, set[int]]:
    """A function that forwards its own parameter into a sink IS a sink.

    ``_alert(admin_client, text)`` is the whole reason this exists: nearly
    every alert in the system goes through one of those two-line wrappers, so
    without the fixpoint the guard would inspect a handful of direct
    ``send_admin`` calls and declare the tree clean.
    """
    sinks = {name: set(pos) for name, pos in sinks.items()}
    changed = True
    while changed:
        changed = False
        for path in paths:
            module = _load(str(path))
            for func in module.funcs.values():
                params = [a.arg for a in func.args.args]
                if params and params[0] in ("self", "cls"):
                    params = params[1:]
                for call in ast.walk(func):
                    if not isinstance(call, ast.Call):
                        continue
                    name = _call_name(call)
                    if name not in sinks:
                        continue
                    for arg in _sink_args(call, sinks[name]):
                        if not isinstance(arg, ast.Name) or arg.id not in params:
                            continue
                        index = params.index(arg.id)
                        known = sinks.setdefault(func.name, set())
                        if index not in known:
                            known.add(index)
                            changed = True
    return sinks


# ── backward resolution: expression → the texts it can hold ─────────────────


class _Frag(NamedTuple):
    """One possible rendering, and WHERE its Arabic actually comes from.

    The origin is not bookkeeping: ``engine/cli`` sends ``quota_alert(account)``
    to the operator, and the string that mixes «جدد الاشتراك الآن» with
    ``searchapi.io/pricing`` lives in ``engine/quota``. Reporting the sink's
    file would send the wrong owner looking for a string that is not there.
    """

    text: str
    path: str
    lineno: int


def _stamp(frags: list[_Frag], module: _Module, node: ast.AST) -> list[_Frag]:
    return [_Frag(f.text, module.path, node.lineno) for f in frags]


class _Resolver:
    """Renders an expression to every string it can evaluate to.

    Unknown pieces become :data:`SLOT` rather than disappearing — a guard that
    silently dropped what it could not follow would report a clean line where
    a TEN code is about to be interpolated.
    """

    def __init__(self) -> None:
        self._active: set[tuple[int, int]] = set()

    def alts(
        self, node: ast.expr | None, module: _Module,
        scope: ast.AST | None, depth: int,
    ) -> list[_Frag] | None:
        if node is None or depth > _MAX_DEPTH:
            return None
        key = (id(node), id(scope))
        if key in self._active:          # recursion / mutual calls
            return None
        self._active.add(key)
        try:
            return self._alts(node, module, scope, depth)
        finally:
            self._active.discard(key)

    def _alts(self, node, module, scope, depth):
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                return None
            return [_Frag(node.value, module.path, node.lineno)]
        if isinstance(node, ast.JoinedStr):
            out = [_Frag("", "", 0)]
            own_arabic = False
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    own_arabic = own_arabic or bool(_ARABIC.search(part.value))
                    out = [_Frag(f.text + part.value, f.path, f.lineno)
                           for f in out]
                elif isinstance(part, ast.FormattedValue):
                    subs = self.alts(part.value, module, scope, depth + 1)
                    subs = subs or [_Frag(SLOT, "", 0)]
                    out = [
                        _Frag(f.text + s.text,
                              f.path or s.path, f.lineno or s.lineno)
                        for f in out for s in subs
                    ][:_MAX_ALTS]
                else:
                    out = [_Frag(f.text + SLOT, f.path, f.lineno) for f in out]
            # The f-string's own literal text is what makes this line Arabic,
            # so it owns the report; a bare `f"{a}{b}"` defers to its parts.
            if own_arabic:
                return _stamp(out, module, node)
            return [f if f.path else _Frag(f.text, module.path, node.lineno)
                    for f in out]
        if isinstance(node, ast.IfExp):
            left = self.alts(node.body, module, scope, depth + 1)
            right = self.alts(node.orelse, module, scope, depth + 1)
            unknown = [_Frag(SLOT, module.path, node.lineno)]
            return ((left or unknown) + (right or unknown))[:_MAX_ALTS]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.alts(node.left, module, scope, depth + 1)
            right = self.alts(node.right, module, scope, depth + 1)
            if left is None and right is None:
                return None
            unknown = [_Frag(SLOT, "", 0)]
            return [
                _Frag(a.text + b.text, a.path or b.path, a.lineno or b.lineno)
                for a in (left or unknown) for b in (right or unknown)
            ][:_MAX_ALTS]
        if isinstance(node, ast.Name):
            return self._name(node.id, module, scope, depth)
        if isinstance(node, ast.Attribute):
            return self._attribute(node, module, depth)
        if isinstance(node, ast.Call):
            return self._call(node, module, scope, depth)
        return None

    def _name(self, name, module, scope, depth):
        out: list[_Frag] = []
        if scope is not None:
            for stmt in ast.walk(scope):
                values: list[ast.expr] = []
                if isinstance(stmt, ast.Assign):
                    values += [stmt.value for t in stmt.targets
                               if isinstance(t, ast.Name) and t.id == name]
                elif (isinstance(stmt, ast.AugAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == name):
                    # `line += "\n" + …` — the appended text is its own line
                    values.append(stmt.value)
                elif (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == name and stmt.value):
                    values.append(stmt.value)
                elif (isinstance(stmt, ast.Call)
                        and isinstance(stmt.func, ast.Attribute)
                        and stmt.func.attr in ("append", "extend")
                        and isinstance(stmt.func.value, ast.Name)
                        and stmt.func.value.id == name):
                    # a screen assembled as `lines.append(...)`: every element
                    # is a line-unit in its own right
                    values += list(stmt.args)
                for value in values:
                    got = self.alts(value, module, scope, depth + 1)
                    if got:
                        out.extend(got)
        if out:
            return out[:_MAX_ALTS]
        if name in module.consts:
            return self.alts(module.consts[name], module, None, depth + 1)
        if name in module.imports:
            dotted, attr = module.imports[name]
            path = _module_path(dotted)
            if path:
                other = _load(path)
                if attr in other.consts:
                    return self.alts(other.consts[attr], other, None, depth + 1)
                if attr in other.funcs:
                    return self.returns(other.funcs[attr], other, depth + 1)
        return None

    def _attribute(self, node: ast.Attribute, module, depth):
        base = node.value
        if isinstance(base, ast.Name) and base.id in module.modules:
            path = _module_path(module.modules[base.id])
            if path:
                other = _load(path)
                if node.attr in other.consts:
                    return self.alts(
                        other.consts[node.attr], other, None, depth + 1
                    )
        return None

    def _call(self, node: ast.Call, module, scope, depth):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "format":
                base = self.alts(func.value, module, scope, depth + 1)
                if base is None:
                    return None
                # Each `{field}` is resolved against the ACTUAL argument, so
                # `TICKET_AR.format(ttl=_ar_digits(n))` keeps its Arabic
                # numeral while `LOCK_HONOURED_ADMIN_AR.format(code=…)`
                # correctly becomes an unproven slot holding a TEN code.
                bound: dict[str, str] = {}
                for kw in node.keywords:
                    if kw.arg is None:
                        continue
                    got = self.alts(kw.value, module, scope, depth + 1)
                    texts = {f.text for f in got or []}
                    if texts and all(
                        not _mixes_or_latin(t) for t in texts
                    ):
                        bound[kw.arg] = next(iter(texts))

                def fill(match: re.Match[str]) -> str:
                    field = match.group(0)[1:-1].split(".")[0].split("[")[0]
                    field = field.split("!")[0].split(":")[0].strip()
                    return bound.get(field, SLOT)

                return [_Frag(re.sub(r"\{[^{}]*\}", fill, f.text),
                              f.path, f.lineno) for f in base]
            if func.attr == "join":
                out: list[_Frag] = []
                for arg in node.args:
                    got = self.alts(arg, module, scope, depth + 1)
                    if got:
                        out.extend(got)
                return out[:_MAX_ALTS] or None
            if func.attr == "get" and node.args:
                # A dispatch table of Arabic labels — `{...}.get(kind, "…")`,
                # `_KINDS_AR.get(kind)` — is a real pattern here, and calling
                # it unknown would report a mixed line where every possible
                # value is pure Arabic. Keys are never rendered; values and
                # the DEFAULT are, and the default is usually the raw Latin
                # token, so an unresolvable one stays a slot.
                # Collapsed to ONE verdict rather than fanned out: a lookup
                # is a single value at runtime, and multiplying twelve table
                # entries through three interpolations would bury the real
                # finding under a hundred near-identical lines.
                texts: list[str] = []
                for candidate in (func.value, *node.args[1:]):
                    table = candidate
                    if isinstance(table, ast.Name):
                        table = module.consts.get(table.id, table)
                    values = (
                        list(table.values) if isinstance(table, ast.Dict)
                        else [candidate]
                    )
                    for value in values:
                        got = self.alts(value, module, scope, depth + 1)
                        if not got:
                            return [_Frag(SLOT, module.path, node.lineno)]
                        texts.extend(f.text for f in got)
                if not texts or any(_mixes_or_latin(t) for t in texts):
                    return [_Frag(SLOT, module.path, node.lineno)]
                return [_Frag(texts[0], "", 0)]
            if isinstance(func.value, ast.Name) and func.value.id in module.modules:
                path = _module_path(module.modules[func.value.id])
                if path:
                    other = _load(path)
                    target = other.funcs.get(func.attr)
                    if target is not None:
                        return self.returns(target, other, depth + 1)
            return None
        if isinstance(func, ast.Name):
            if func.id in arabic_digit_folders(module.path):
                # a proven Arabic numeral — allowed inside an Arabic line
                return [_Frag("٠", module.path, node.lineno)]
            target = module.funcs.get(func.id)
            if target is not None:
                return self.returns(target, module, depth + 1)
            if func.id in module.imports:
                dotted, attr = module.imports[func.id]
                path = _module_path(dotted)
                if path:
                    other = _load(path)
                    imported = other.funcs.get(attr)
                    if imported is not None:
                        return self.returns(imported, other, depth + 1)
        return None

    def returns(self, func, module: _Module, depth: int) -> list[_Frag] | None:
        if depth > _MAX_DEPTH:
            return None
        out: list[_Frag] = []
        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None:
                got = self.alts(node.value, module, func, depth + 1)
                if got:
                    out.extend(got)
        return out[:_MAX_ALTS] or None


# ── collection ──────────────────────────────────────────────────────────────


def _docstring_ids(tree: ast.Module) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _fstring_part_ids(tree: ast.Module) -> set[int]:
    """Constant chunks INSIDE an f-string: already covered by the whole."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                out.add(id(part))
    return out


def _format_template_ids(module: _Module) -> set[int]:
    """``.format()`` templates, checked at their call sites instead.

    A raw template reads as mixed because ``{ttl}`` is Latin letters, and it
    is never rendered that way — the console fills it with an Arabic numeral.
    Judging the template alone is how a guard invents a violation.
    """
    out: set[int] = set()
    for node in ast.walk(module.tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and isinstance(node.func.value, ast.Name)):
            const = module.consts.get(node.func.value.id)
            if const is not None:
                out.add(id(const))
    return out


def operator_texts(path: Path, sinks: dict[str, set[int]]) -> list[_Frag]:
    """Every string this file can put on the operator's screen.

    A fragment's ``path``/``lineno`` name where its Arabic literal lives,
    which is not always this file — see :class:`_Frag`.
    """
    module = _load(str(path))
    scopes = _scopes(module.tree)
    resolver = _Resolver()
    found: list[_Frag] = []

    def keep(frags: list[_Frag] | None, fallback: ast.AST) -> None:
        for frag in frags or []:
            found.append(
                frag if frag.path
                else _Frag(frag.text, module.path, fallback.lineno)
            )

    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in sinks:
            continue
        for arg in _sink_args(node, sinks[name]):
            keep(resolver.alts(arg, module, scopes.get(id(node)), 0), node)

    if CONSOLE_PACKAGE in path.parents:
        skip = (_docstring_ids(module.tree) | _fstring_part_ids(module.tree)
                | _format_template_ids(module))
        for node in ast.walk(module.tree):
            is_str = isinstance(node, ast.Constant) and isinstance(node.value, str)
            if not (is_str or isinstance(node, ast.JoinedStr)):
                continue
            if id(node) in skip:
                continue
            keep(resolver.alts(node, module, scopes.get(id(node)), 0), node)
    return found


def scanned_files() -> list[Path]:
    return sorted(
        [p for p in SRC.rglob("*.py")] + [p for p in SCRIPTS.rglob("*.py")]
    )


def verdict() -> dict[str, list[tuple[int, str]]]:
    """The whole tree's answer: offending file → [(line number, line)].

    Grouped by the file the STRING lives in, not the file that sends it, so
    the report hands each line to the person who can fix it.
    """
    files = scanned_files()
    sinks = close_over_wrappers(files, discover_sinks())
    seen: dict[str, dict[str, int]] = {}
    for path in files:
        for frag in operator_texts(path, sinks):
            for line in frag.text.split("\n"):
                if line_is_mixed(line):
                    origin = str(Path(frag.path).relative_to(ROOT))
                    seen.setdefault(origin, {}).setdefault(line, frag.lineno)
    return {
        name: sorted(((n, line) for line, n in hits.items()))
        for name, hits in sorted(seen.items())
    }


def _report(hits: dict[str, list[tuple[int, str]]]) -> str:
    lines = []
    for name, items in sorted(hits.items()):
        lines.append(f"\n{name}  ({len(items)} mixed line(s))")
        for lineno, line in items:
            lines.append(f"  L{lineno}: {line.replace(SLOT, '{…}')!r}")
    return "\n".join(lines)


# ── the guard itself ────────────────────────────────────────────────────────


def test_the_rule_is_what_it_claims_to_be() -> None:
    """The definition, pinned. If someone loosens `line_is_mixed`, this is
    the test that says so before the tree-wide one goes quiet."""
    # allowed: Arabic-Indic digits, emoji, neutral punctuation, the Arabic %
    assert not line_is_mixed("🔴 وصلنا ٧ طلبات — راجعها")
    assert not line_is_mixed("↻ تجديد — الفترة الجديدة تنتهي")
    assert not line_is_mixed("قرارات العملاء: ٥٠٪ تقديم «والباقي تجاهل»")
    assert not line_is_mixed("TEN-0002")            # pure Latin line
    assert not line_is_mixed("2026-09-06")
    assert not line_is_mixed(f"{SLOT}")             # a slot alone is fine
    # forbidden: Latin letters, ASCII digits, unproven interpolations
    assert line_is_mixed("↻ تجديد TEN-0002 — الفترة الجديدة تنتهي")
    assert line_is_mixed("الإيراد: 149 ريال")
    assert line_is_mixed(f"↻ تجديد {SLOT} — الفترة الجديدة تنتهي")
    assert line_is_mixed("أرسل /start")


def test_the_sinks_are_read_from_the_client_not_from_a_list() -> None:
    """The seed comes from telegram/admin.py, and the fixpoint finds the
    `_alert` wrappers that almost every alert in the system goes through."""
    sinks = discover_sinks()
    assert "send_admin" in sinks
    closed = close_over_wrappers(scanned_files(), sinks)
    assert "_alert" in closed, (
        "the wrapper closure stopped working — nearly every operator alert "
        "in this codebase is sent through a two-line _alert() forwarder, and "
        "without it this guard inspects almost nothing"
    )
    assert len(closed) > len(sinks)


def test_the_guard_can_see_a_violation_at_all() -> None:
    """A guard nobody has watched fail is a guard nobody should trust.

    The synthetic offender is built here rather than written into a source
    file, so the proof runs on every CI pass instead of once by hand.
    """
    bad = "⚠️ تجديد TEN-0002 — الفترة الجديدة تنتهي\n2026-09-06"
    mixed = [line for line in bad.split("\n") if line_is_mixed(line)]
    assert mixed == ["⚠️ تجديد TEN-0002 — الفترة الجديدة تنتهي"]

    good = "⚠️ تجديد\nTEN-0002\nالفترة الجديدة تنتهي\n2026-09-06"
    assert [line for line in good.split("\n") if line_is_mixed(line)] == []


def test_the_salla_provisioning_and_renewal_alerts_are_direction_pure() -> None:
    """The two files repaired here, held green.

    The renewal notice fires on EVERY renewal and was the one alert Fahad was
    guaranteed to receive scrambled.
    """
    dirty = {name: hits for name, hits in verdict().items() if name in OWNED}
    assert not dirty, "mixed-direction operator lines:" + _report(dirty)


def test_operator_alerts_are_direction_pure_tree_wide() -> None:
    """THE guard. Wide on purpose, and red on purpose.

    It fails today for files owned by other agents — see SCOPE HONESTY in the
    module docstring for the roll-call. Those failures are the point: the same
    defect lives there, and narrowing this test to the files that happened to
    be fixed would certify a cleanliness the tree does not have. Fix the
    strings — never this scope.

    Each finding is one LINE that needs splitting: put the count, the code,
    the date, the URL or the command on a line of its own, exactly as
    ``salla/provisioning._announce_provision`` and ``_announce_renewal`` do.
    Where a number genuinely belongs inside the sentence, render it with an
    Arabic-Indic digit helper (``telegram/console._ar_digits``) — this guard
    recognises that helper and allows it.
    """
    hits = verdict()
    assert not hits, (
        f"{sum(len(v) for v in hits.values())} mixed-direction operator "
        f"line(s) in {len(hits)} file(s) — each Latin/digit fragment needs a "
        "line of its own:" + _report(hits)
    )
