"""The quality panel — nothing reaches the customer on one attempt (F-PANEL §14).

Owner's finding from the first live rehearsal: a single rendering went straight
to WhatsApp, and «ترجمت كلامي بس ابدع» was the honest verdict — accurate but
unremarkable. The customer should meet the strongest version we can produce,
first time.

How it works:

1. THREE candidates are rendered from the SAME Arabic answer with different
   emphases (faithful / responsibility+scope / impact).
2. EVERY candidate passes :func:`bullet_is_grounded` — the deterministic
   cross-lingual guard. An inventive candidate is discarded here, before any
   judgement, so no reviewer can ever talk a fabrication through.
3. A judge scores the survivors on faithfulness, professional strength, and
   CV-fitness, and returns the winner.
4. If judging is unavailable, the longest surviving grounded candidate wins —
   the customer is never left with nothing because a reviewer was down.

Safety note: the panel widens *quality*, never *licence*. The guard is the
same one a single rendering faced, applied to each candidate independently,
and the customer's explicit confirmation is still the only door into the bank
(constant 5).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from career.onboarding.achievement_render import (
    AchievementRenderer,
    bullet_is_grounded,
)

logger = logging.getLogger("career.enrichment")

#: The three emphases. Deliberately complementary: one stays literal, one
#: foregrounds ownership/scope (what most CVs under-sell), one foregrounds the
#: outcome. All three are bound by the same anti-invention rules.
PANEL_ANGLES: tuple[str, ...] = (
    "Stay closest to the wording — the plainest faithful rendering.",
    "Foreground ownership and scope: what this person was responsible for, "
    "and how wide that responsibility reached.",
    "Foreground the outcome for the employer: what became better or easier "
    "because this person did it — strictly within what the Arabic states.",
)


class BulletJudge(Protocol):
    def judge(
        self, arabic_answer: str, candidates: list[str]
    ) -> dict[str, Any]:
        """{winner_index, reason} — which candidate serves the customer best."""
        ...


def _dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        key = c["english_bullet"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def run_panel(
    renderer: AchievementRenderer,
    *,
    arabic_answer: str,
    vocabulary: set[str],
    instruction: str = "",
    judge: BulletJudge | None = None,
    angles: tuple[str, ...] = PANEL_ANGLES,
) -> dict[str, Any]:
    """A DISCRIMINATED result — the caller must be able to tell «that wasn't an
    achievement» (ask differently) from «every draft invented something» (try
    another angle of asking). Review finding #3: collapsing both into None made
    §14-ب's whole point — classify, never dead-end — impossible.

    * ``{"status": "ok", english_bullet, arabic_gloss, qualitative_only, panel}``
    * ``{"status": "not_achievement", "panel": …}``
    * ``{"status": "ungrounded", "panel": …}``

    ``panel`` bookkeeping is for logs/tests only, never shown to a customer."""
    render_calls = 0
    achievement_candidates = 0
    grounded: list[dict[str, Any]] = []

    for angle in angles:
        try:
            result = renderer.render(
                arabic_answer, angle=angle, instruction=instruction
            )
        except Exception:  # noqa: BLE001 — one dead angle must not kill the panel
            logger.warning("panel candidate failed", exc_info=True)
            continue
        render_calls += 1
        if not result.get("is_achievement"):
            continue
        achievement_candidates += 1
        english = str(result.get("english_bullet") or "").strip()
        if not english:
            continue
        ok, reason = bullet_is_grounded(
            english, arabic_answer=arabic_answer, vocabulary=vocabulary
        )
        if not ok:
            logger.info("panel candidate rejected by guard: %s", reason)
            continue
        grounded.append({
            "english_bullet": english,
            "arabic_gloss": str(result.get("arabic_gloss") or "").strip(),
            "qualitative_only": bool(result.get("qualitative_only")),
        })

    grounded = _dedupe(grounded)
    book = {
        "render_calls": render_calls,
        "achievement_candidates": achievement_candidates,
        "grounded": len(grounded),
        "judged": False,
    }
    if not grounded:
        # review finding #3: these two are different customer situations
        status = "not_achievement" if achievement_candidates == 0 else "ungrounded"
        return {"status": status, "panel": {**book, "why": status}}

    winner = max(grounded, key=lambda c: len(c["english_bullet"]))
    # review finding #6: failure must be legible — a down judge and a lone
    # candidate used to produce the SAME why-string, which is exactly how the
    # 29-July schema bug hid for days.
    why = "single candidate" if len(grounded) == 1 else "judge unavailable"
    if judge is not None and len(grounded) > 1:
        try:
            verdict = judge.judge(
                arabic_answer, [c["english_bullet"] for c in grounded]
            )
            idx = int(verdict.get("winner_index", -1))
            if 0 <= idx < len(grounded):
                winner = grounded[idx]
                book["judged"] = True
                why = str(verdict.get("reason") or "judged best")[:120]
            else:
                why = f"judge verdict rejected: {verdict.get('reason', '?')}"[:120]
                logger.warning("panel judge returned an unusable index")
        except Exception:  # noqa: BLE001 — a down judge never blocks delivery
            logger.warning("panel judge failed", exc_info=True)

    return {"status": "ok", **winner, "panel": {**book, "why": why}}


# ── the live judge (one structured call) ─────────────────────────────────────

_JUDGE_MODEL = "claude-opus-4-8"

_JUDGE_SYSTEM = (
    "You choose which English CV bullet best serves a Saudi job-seeker, given "
    "the colloquial Arabic they actually wrote. Judge on three things, in this "
    "order: (1) FAITHFULNESS — it must claim nothing the Arabic does not say; "
    "a stronger-sounding line that overstates loses outright. (2) PROFESSIONAL "
    "STRENGTH — concrete verbs, ownership made visible, no filler. (3) CV "
    "FITNESS — reads naturally as one bullet under a job title, not as prose. "
    "Return the index of the best candidate and one short reason."
)

_JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["winner_index", "reason"],
    "properties": {
        "winner_index": {"type": "integer"},
        "reason": {"type": "string"},
    },
}


class AnthropicBulletJudge:  # pragma: no cover — live boundary
    def __init__(self, api_key: str | None = None, client: Any | None = None,
                 model: str = _JUDGE_MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model

    def judge(
        self, arabic_answer: str, candidates: list[str]
    ) -> dict[str, Any]:
        import json

        listing = "\n".join(f"[{i}] {c}" for i, c in enumerate(candidates))
        prompt = (
            f"What the job-seeker wrote (Arabic):\n{arabic_answer}\n\n"
            f"Candidates:\n{listing}"
        )
        response = self._client.messages.create(
            # review finding #6: max_tokens caps thinking + text TOGETHER on
            # this model; 512 truncated the judge into silent unavailability.
            model=self._model, max_tokens=2048,
            thinking={"type": "adaptive"}, system=_JUDGE_SYSTEM,
            output_config={
                "format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason != "end_turn":
            logger.warning("judge did not finish: stop_reason=%s",
                           response.stop_reason)
            return {"winner_index": -1, "reason": "judge incomplete"}
        for block in response.content:
            text = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(text, str):
                try:
                    return dict(json.loads(text))
                except ValueError:
                    return {"winner_index": -1, "reason": "unparseable"}
        return {"winner_index": -1, "reason": "empty"}
