# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: auto-grade a tapped multiple-choice answer on the desktop reviewer.

When ``gmatAutoGradeEnabled`` is on, the A-E choices in the question view become
clickable. Tapping one asks the shared Rust engine
(``col._backend.grade_answer`` -> the ``GradeAnswer`` RPC, the SAME code the
phone uses) to decide Again/Hard/Good/Easy from correctness, response time, and
the item's difficulty vs the learner's ability, then reveals the answer and
records that rating. Off by default; the manual Again/Hard/Good/Easy buttons are
untouched and always available.

Wiring: ``init()`` appends three gui_hooks. Registered from ``aqt.main`` AFTER
``gmat_theme`` so the choices are already the Bauhaus ``.gmat-card`` layout when
we add the click handlers.
"""

from __future__ import annotations

import re
import time

from aqt import gui_hooks, mw

# Set when a question is shown, to measure response time.
_question_shown_at: float = 0.0

# Correct-answer letter in the rendered answer HTML ("Answer: X").
_ANSWER_LETTER_RE = re.compile(r"Answer:\s*(?:</b>)?\s*([A-E])", re.I)
# Each Bauhaus choice row in the QUESTION view (no "correct" class there).
_CHOICE_DIV_RE = re.compile(r'<div class="choice"><div class="marker">([A-E])</div>')


def _enabled() -> bool:
    return bool(mw and mw.col and mw.col.get_config("gmatAutoGradeEnabled", False))


def _on_show_question(card: object) -> None:
    global _question_shown_at
    _question_shown_at = time.time()


def _inject_choice_taps(html: str) -> str:
    """Make each choice row clickable, reporting its letter via pycmd."""
    return _CHOICE_DIV_RE.sub(
        r'<div class="choice" style="cursor:pointer" '
        r"onclick=\"pycmd('gmatgrade:\1')\"><div class=\"marker\">\1</div>",
        html,
    )


def _on_card_will_show(text: str, card: object, kind: str) -> str:
    """FILTER (runs after the theme): add tap handlers to the question's choices
    when auto-grade is on. Defensive — any issue passes the text through."""
    try:
        if kind == "reviewQuestion" and _enabled():
            return _inject_choice_taps(text)
    except Exception:  # pragma: no cover - never hide a card
        pass
    return text


def _grade_and_answer(letter: str) -> None:
    reviewer = mw.reviewer
    card = reviewer.card if reviewer else None
    if card is None:
        return
    match = _ANSWER_LETTER_RE.search(card.answer())
    correct = bool(match and match.group(1).upper() == letter)
    elapsed_ms = int(max(0.0, time.time() - _question_shown_at) * 1000)
    try:
        ease = mw.col._backend.grade_answer(
            card_id=card.id,
            correct=correct,
            elapsed_ms=elapsed_ms,
            target_seconds=0,
        )
    except Exception:
        ease = 3  # never block a review on a grading hiccup
    if ease not in (1, 2, 3, 4):
        ease = 3
    # Reveal the answer (so the student sees the explanation), then record.
    if reviewer.state == "question":
        reviewer._showAnswer()
    reviewer._answerCard(ease)  # type: ignore[arg-type]


def _on_js_message(handled: tuple[bool, object], message: str, context: object) -> tuple[bool, object]:
    """Handle the pycmd from a tapped choice."""
    if not message.startswith("gmatgrade:"):
        return handled
    try:
        letter = message.split(":", 1)[1].strip().upper()
        if letter and _enabled():
            _grade_and_answer(letter)
    except Exception:  # pragma: no cover
        pass
    return (True, None)


def init() -> None:
    gui_hooks.reviewer_did_show_question.append(_on_show_question)
    gui_hooks.card_will_show.append(_on_card_will_show)
    gui_hooks.webview_did_receive_js_message.append(_on_js_message)
