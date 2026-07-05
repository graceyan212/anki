# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: confidence-based auto-grading, integrated into the desktop
reviewer's *bottom bar* (no modal, no Show Answer button).

Flow for a single-answer A-E card, mirroring the iPhone app:

  1. The A-E choices in the question view are clickable (pycmd ``gmatchoice:X``).
  2. Clicking one replaces the bottom bar's Show Answer with a Bauhaus
     "How sure?" prompt — GUESSING / FAIRLY SURE / CONFIDENT.
  3. Picking a confidence grades the card via the shared Rust engine
     (``col._backend.grade_answer`` -> the ``GradeAnswer`` RPC, the SAME code
     the phone uses), reveals the answer, and shows the engine's rating with a
     one-tap override (tap "AI · X" to reveal Again/Hard/Good/Easy).

On by default. Non-multiple-choice cards (and auto-grade off) fall back to
Anki's stock Show Answer + ease buttons. Every patched method is guarded so any
error falls back to stock behaviour — a bug here can never break the reviewer.

NOTE: imported during ``aqt`` init (via ``aqt.main``), so it must NOT
``from aqt import mw`` at module load — reference ``aqt.mw`` lazily.
"""

from __future__ import annotations

import json
import re

import aqt
from aqt import gui_hooks

# Correct-answer letter in the rendered answer HTML ("Answer: X").
_ANSWER_LETTER_RE = re.compile(r"Answer:\s*(?:</b>)?\s*([A-E])", re.I)
# Each Bauhaus choice row in the QUESTION view (no "correct" class there).
_CHOICE_DIV_RE = re.compile(r'<div class="choice"><div class="marker">([A-E])</div>')

# Bauhaus palette (matches gmat_theme / the iPhone app).
_INK = "#1A1A1A"
_PAPER = "#F5F1E6"
_RED = "#E2231A"
_YELLOW = "#F2C200"
_GREEN = "#2E9E4F"
_BLUE = "#1E52A8"
_RATING_COLOR = {1: _RED, 2: _YELLOW, 3: _GREEN, 4: _BLUE}
_RATING_LABEL = {1: "AGAIN", 2: "HARD", 3: "GOOD", 4: "EASY"}

# Stock Reviewer methods we wrap (captured in init()).
_orig_show_answer_button = None
_orig_show_ease_buttons = None
_orig_link_handler = None


def _enabled() -> bool:
    return bool(aqt.mw and aqt.mw.col and aqt.mw.col.get_config("gmatAutoGradeEnabled", True))


# --- choice-tap injection (question view) ---------------------------------

def _inject_choice_taps(html: str) -> str:
    """Make each choice row clickable, reporting its letter via pycmd. A
    function replacement is used so the emitted HTML carries real double quotes
    (a backreference replacement with ``\\"`` mangles the quoting)."""

    def _clickable(m: "re.Match[str]") -> str:
        letter = m.group(1)
        return (
            f'<div class="choice" style="cursor:pointer" '
            f"onclick=\"pycmd('gmatchoice:{letter}')\">"
            f'<div class="marker">{letter}</div>'
        )

    return _CHOICE_DIV_RE.sub(_clickable, html)


def _on_card_will_show(text: str, card: object, kind: str) -> str:
    try:
        if kind == "reviewQuestion" and _enabled():
            return _inject_choice_taps(text)
    except Exception:  # pragma: no cover - never hide a card
        pass
    return text


# --- gate: a clickable, single-answer MC card we can auto-grade -----------

def _gradeable(card: object) -> bool:
    try:
        if not _enabled():
            return False
        import aqt.gmat_theme as th

        if th._transform_card(card.question()) is None:  # type: ignore[attr-defined]
            return False
        return bool(_ANSWER_LETTER_RE.search(card.answer()))  # type: ignore[attr-defined]
    except Exception:
        return False


# --- Bauhaus bottom-bar HTML ----------------------------------------------

_BB_STYLE = (
    "<style>"
    "#middle .gbb{display:flex;width:100%%;height:46px;gap:2px;align-items:stretch;"
    "font-family:'Futura','Avenir Next','Helvetica Neue',sans-serif;}"
    "#middle .gbb .lbl{flex:0 0 auto;display:flex;align-items:center;padding:0 16px;background:%(ink)s;"
    "color:%(paper)s;font-weight:700;font-size:11px;letter-spacing:1.2px;white-space:nowrap;}"
    "#middle .gbb button{flex:1 1 0;border:0;margin:0;color:#fff;font-family:inherit;font-weight:700;"
    "font-size:12px;letter-spacing:1px;padding:13px 8px;cursor:pointer;white-space:nowrap;}"
    "#middle .gbb button:active{opacity:.82;}"
    "</style>"
) % dict(ink=_INK, paper=_PAPER)


def _hint_html() -> str:
    return _BB_STYLE + (
        '<div class="gbb"><span class="lbl" style="flex:1 1 auto;justify-content:center;'
        'letter-spacing:2px;">▲ PICK AN ANSWER ABOVE</span></div>'
    )


def _confidence_html(letter: str) -> str:
    return _BB_STYLE + (
        '<div class="gbb">'
        f'<span class="lbl">YOU PICKED {letter} · HOW SURE?</span>'
        f"<button style=\"background:{_RED}\" onclick=\"pycmd('gmatconf:0')\">GUESSING</button>"
        f"<button style=\"background:{_YELLOW}\" onclick=\"pycmd('gmatconf:1')\">FAIRLY SURE</button>"
        f"<button style=\"background:{_GREEN}\" onclick=\"pycmd('gmatconf:2')\">CONFIDENT</button>"
        "</div>"
    )


def _rating_html(reviewer: object) -> str:
    ease = getattr(reviewer, "_gmat_rating", 3)
    t = reviewer.card.time_taken() // 1000  # type: ignore[attr-defined]
    tstr = "%d:%02d" % (t // 60, t % 60)
    over = getattr(reviewer, "_gmat_overconfident", False)
    note = ("⚠ CONFIDENT BUT WRONG · " + tstr) if over else ("TIME " + tstr)
    note_bg = _RED if over else _INK

    if getattr(reviewer, "_gmat_expanded", False):
        btns = ""
        for e in (1, 2, 3, 4):
            mark = " ·AI" if e == ease else ""
            btns += (
                f'<button style="background:{_RATING_COLOR[e]}" '
                f"onclick=\"pycmd('gmatease:{e}')\">{_RATING_LABEL[e]}{mark}</button>"
            )
        return _BB_STYLE + f'<div class="gbb"><span class="lbl" style="background:{note_bg}">{note}</span>{btns}</div>'

    color = _RATING_COLOR.get(ease, _GREEN)
    label = _RATING_LABEL.get(ease, "GOOD")
    return _BB_STYLE + (
        '<div class="gbb">'
        f'<span class="lbl" style="background:{note_bg}">{note}</span>'
        f"<button style=\"background:{color}\" onclick=\"pycmd('gmatexpand')\">AI · {label} ▾</button>"
        f"<button style=\"background:{_INK}\" onclick=\"pycmd('gmatease:{ease}')\">NEXT →</button>"
        "</div>"
    )


def _apply_bar(reviewer: object, html: str) -> None:
    """Fill the bottom bar full-width with our Bauhaus content: hide Anki's
    flanking Edit/More cells and stretch #middle across the whole bar."""
    reviewer.bottom.web.eval(  # type: ignore[attr-defined]
        "(function(){var s=document.querySelectorAll('td.stat');"
        "for(var i=0;i<s.length;i++){s[i].style.display='none';}"
        "var m=document.getElementById('middle');"
        "if(m){m.style.width='100%%';m.style.padding='0';m.innerHTML=%s;}})();" % json.dumps(html)
    )


def _restore_bar(reviewer: object) -> None:
    """Undo _apply_bar — restore Anki's Edit/More cells (non-gradeable cards)."""
    reviewer.bottom.web.eval(  # type: ignore[attr-defined]
        "(function(){var s=document.querySelectorAll('td.stat');"
        "for(var i=0;i<s.length;i++){s[i].style.display='';}"
        "var m=document.getElementById('middle');if(m){m.style.width='';m.style.padding='';}})();"
    )


def _show_rating(reviewer: object) -> None:
    _apply_bar(reviewer, _rating_html(reviewer))


def _grade(reviewer: object, confidence: int) -> None:
    card = reviewer.card  # type: ignore[attr-defined]
    letter = getattr(reviewer, "_gmat_letter", None)
    m = _ANSWER_LETTER_RE.search(card.answer())
    correct = bool(letter and m and m.group(1).upper() == letter)
    try:
        ease = aqt.mw.col._backend.grade_answer(correct=correct, confidence=confidence).ease
    except Exception:
        ease = 3
    if ease not in (1, 2, 3, 4):
        ease = 3
    reviewer._gmat_rating = ease  # type: ignore[attr-defined]
    reviewer._gmat_overconfident = (not correct) and confidence != 0  # type: ignore[attr-defined]
    reviewer._gmat_expanded = False  # type: ignore[attr-defined]
    if reviewer.state == "question":  # type: ignore[attr-defined]
        reviewer._showAnswer()  # -> patched _showEaseButtons -> the rating bar


# --- patched Reviewer methods (guarded; fall back to stock on any issue) ---

def _show_answer_button(reviewer: object) -> None:
    try:
        if _gradeable(reviewer.card):  # type: ignore[attr-defined]
            reviewer._gmat_letter = None  # type: ignore[attr-defined]
            reviewer._gmat_rating = None  # type: ignore[attr-defined]
            reviewer._gmat_expanded = False  # type: ignore[attr-defined]
            _apply_bar(reviewer, _hint_html())
            return
        _restore_bar(reviewer)
    except Exception:
        pass
    _orig_show_answer_button(reviewer)


def _show_ease_buttons(reviewer: object) -> None:
    try:
        if _gradeable(reviewer.card) and getattr(reviewer, "_gmat_rating", None):  # type: ignore[attr-defined]
            if not reviewer._states_mutated:  # type: ignore[attr-defined]
                reviewer.mw.progress.single_shot(50, reviewer._showEaseButtons)  # type: ignore[attr-defined]
                return
            _show_rating(reviewer)
            return
        _restore_bar(reviewer)
    except Exception:
        pass
    _orig_show_ease_buttons(reviewer)


def _link_handler(reviewer: object, url: str) -> None:
    try:
        if url.startswith("gmatconf:"):
            _grade(reviewer, int(url.split(":", 1)[1]))
            return
        if url == "gmatexpand":
            reviewer._gmat_expanded = True  # type: ignore[attr-defined]
            _show_rating(reviewer)
            return
        if url.startswith("gmatease:"):
            reviewer._answerCard(int(url.split(":", 1)[1]))  # type: ignore[attr-defined]
            return
    except Exception:
        pass
    _orig_link_handler(reviewer, url)


def _on_js_message(handled: tuple, message: str, context: object) -> tuple:
    """Choice tapped in the QUESTION webview: remember the letter and swap the
    bottom bar to the confidence prompt (no grading yet)."""
    if isinstance(message, str) and message.startswith("gmatchoice:"):
        try:
            letter = message.split(":", 1)[1].strip().upper()
            r = aqt.mw.reviewer
            if letter and r and _enabled():
                r._gmat_letter = letter
                _apply_bar(r, _confidence_html(letter))
        except Exception:  # pragma: no cover
            pass
        return (True, None)
    return handled


def init() -> None:
    global _orig_show_answer_button, _orig_show_ease_buttons, _orig_link_handler
    from aqt.reviewer import Reviewer

    gui_hooks.card_will_show.append(_on_card_will_show)
    gui_hooks.webview_did_receive_js_message.append(_on_js_message)

    if _orig_show_answer_button is None:
        _orig_show_answer_button = Reviewer._showAnswerButton
        _orig_show_ease_buttons = Reviewer._showEaseButtons
        _orig_link_handler = Reviewer._linkHandler
        Reviewer._showAnswerButton = _show_answer_button
        Reviewer._showEaseButtons = _show_ease_buttons
        Reviewer._linkHandler = _link_handler
