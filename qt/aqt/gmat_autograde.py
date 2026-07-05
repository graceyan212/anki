# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: confidence-based auto-grading on the desktop reviewer.

When ``gmatAutoGradeEnabled`` is on, the A-E choices in the question view become
clickable. Tapping one asks the student how sure they are (Guessing / Fairly
sure / Confident), then the shared Rust engine (``col._backend.grade_answer`` ->
the ``GradeAnswer`` RPC, the SAME code the phone uses) turns correctness ×
confidence into Again/Hard/Good/Easy — calibration, not time. It then reveals the
answer and records the rating. On by default; the manual buttons are untouched
and always available as an override (toggle in Preferences).

Wiring: ``init()`` appends two gui_hooks. Registered from ``aqt.main`` AFTER
``gmat_theme`` so the choices are already the Bauhaus ``.gmat-card`` layout.

NOTE: this module is imported *during* ``aqt`` package initialisation (via
``aqt.main``), so it must NOT ``from aqt import mw`` at module load — ``aqt.mw``
does not exist yet and that would be a circular import. We ``import aqt`` and
reference ``aqt.mw`` lazily inside functions (same pattern as ``gmat_theme``).
"""

from __future__ import annotations

import re

import aqt
from aqt import gui_hooks

# Correct-answer letter in the rendered answer HTML ("Answer: X").
_ANSWER_LETTER_RE = re.compile(r"Answer:\s*(?:</b>)?\s*([A-E])", re.I)
# Each Bauhaus choice row in the QUESTION view (no "correct" class there).
_CHOICE_DIV_RE = re.compile(r'<div class="choice"><div class="marker">([A-E])</div>')


def _enabled() -> bool:
    return bool(aqt.mw and aqt.mw.col and aqt.mw.col.get_config("gmatAutoGradeEnabled", True))


def _inject_choice_taps(html: str) -> str:
    """Make each choice row clickable, reporting its letter via pycmd.

    A function replacement is used so the emitted HTML carries *real* double
    quotes: a string/backreference replacement with ``\\"`` mangles the quoting
    (the backslashes land in the DOM and the onclick attribute never parses)."""

    def _clickable(m: "re.Match[str]") -> str:
        letter = m.group(1)
        return (
            f'<div class="choice" style="cursor:pointer" '
            f"onclick=\"pycmd('gmatchoice:{letter}')\">"
            f'<div class="marker">{letter}</div>'
        )

    return _CHOICE_DIV_RE.sub(_clickable, html)


def _on_card_will_show(text: str, card: object, kind: str) -> str:
    """FILTER (runs after the theme): add tap handlers to the question's choices
    when auto-grade is on. Defensive — any issue passes the text through."""
    try:
        if kind == "reviewQuestion" and _enabled():
            return _inject_choice_taps(text)
    except Exception:  # pragma: no cover - never hide a card
        pass
    return text


def _ask_confidence() -> int | None:
    """Modal confidence prompt. Returns 0=guessing, 1=fairly sure, 2=confident,
    or None if dismissed."""
    from aqt.qt import QMessageBox

    box = QMessageBox(aqt.mw)
    box.setWindowTitle("How sure are you?")
    box.setText("How confident are you in this answer?")
    guessing = box.addButton("Guessing", QMessageBox.ButtonRole.NoRole)
    fairly = box.addButton("Fairly sure", QMessageBox.ButtonRole.NoRole)
    confident = box.addButton("Confident", QMessageBox.ButtonRole.YesRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked is confident:
        return 2
    if clicked is fairly:
        return 1
    if clicked is guessing:
        return 0
    return None


def _grade_and_answer(letter: str, confidence: int) -> None:
    reviewer = aqt.mw.reviewer
    card = reviewer.card if reviewer else None
    if card is None:
        return
    match = _ANSWER_LETTER_RE.search(card.answer())
    correct = bool(match and match.group(1).upper() == letter)
    try:
        ease = aqt.mw.col._backend.grade_answer(correct=correct, confidence=confidence).ease
    except Exception:
        ease = 3  # never block a review on a grading hiccup
    if ease not in (1, 2, 3, 4):
        ease = 3
    # Reveal the answer (so the student sees the explanation), then record.
    if reviewer.state == "question":
        reviewer._showAnswer()
    reviewer._answerCard(ease)  # type: ignore[arg-type]


def _on_js_message(handled: tuple[bool, object], message: str, context: object) -> tuple[bool, object]:
    """Handle the pycmd from a tapped choice: ask confidence, then grade."""
    if not message.startswith("gmatchoice:"):
        return handled
    try:
        letter = message.split(":", 1)[1].strip().upper()
        if letter and _enabled():
            confidence = _ask_confidence()
            if confidence is not None:
                _grade_and_answer(letter, confidence)
    except Exception:  # pragma: no cover
        pass
    return (True, None)


def init() -> None:
    gui_hooks.card_will_show.append(_on_card_will_show)
    gui_hooks.webview_did_receive_js_message.append(_on_js_message)
