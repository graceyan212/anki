# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Whole-app Bauhaus theme LAYER (track T3).

A contained recolour + refont + flatten layer that gives the desktop app the
same visual language as the iOS app and the GMAT readiness dashboard: warm
PAPER backgrounds, INK text/borders, the Futura font, flat fills, and HARD
edges (no gradients, no border-radius, no drop shadows). Primary accents (blue
for focus/links/selection, yellow for highlight) are used sparingly.

This is a LAYER, not a redesign: it never edits Anki's core theme modules
(``aqt.theme`` / ``aqt.stylesheets`` / ``ts/lib/sass``). Instead it rides three
public seams:

  1. ``gui_hooks.style_did_init`` -- a FILTER over the whole-app Qt QSS. We
     receive the core stylesheet and RETURN it with our Bauhaus block appended,
     so our rules win by cascade order (equal specificity, appended last).
  2. ``gui_hooks.webview_will_set_content`` -- fires for every ``AnkiWebView``
     (deck browser, overview, reviewer chrome, editor, ...). We append a
     ``<style>`` to ``web_content.head`` (emitted last in ``<head>``) that
     overrides the CSS custom properties on ``:root``.
  3. The real ``QApplication`` base font, set once to Futura.

The theme is deliberately LIGHT-ONLY: we force the paper/ink palette on both
the light and ``night-mode`` selectors so it looks right regardless of Anki's
night-mode setting (for brand consistency with the iOS app), rather than
fighting the built-in dark theme.

Importing this module then calling ``init()`` is the only wiring required
(done from ``aqt.main``, mirroring ``aqt.gmat_dashboard``).
"""

from __future__ import annotations

import re

import aqt
from aqt import gui_hooks
from aqt.qt import QFont
from aqt.webview import WebContent

# --- Shared Bauhaus tokens (must match the iOS app + approved dashboard) ---
BAUHAUS_RED = "#E2231A"
BAUHAUS_YELLOW = "#F2C200"
BAUHAUS_GREEN = "#2E9E4F"
BAUHAUS_BLUE = "#1E52A8"
BAUHAUS_INK = "#1A1A1A"
BAUHAUS_PAPER = "#F5F1E6"
BAUHAUS_MUTED = "#8a8577"
# Slightly elevated paper for containers/inputs so they read against the canvas.
BAUHAUS_PAPER_ELEVATED = "#FBF8F0"
# A hairline border colour lifted from the dashboard mockup.
BAUHAUS_HOLLOW = "#c9c3b2"
# White text on ink/blue fills.
BAUHAUS_ON_INK = "#FFFFFF"
# Qt QFont + CSS use the "Futura" family, with Avenir Next as the fallback
# (both ship on macOS; harmless substitution elsewhere).
BAUHAUS_FONT_FAMILY = "Futura"
BAUHAUS_FONT_STACK = '"Futura", "Avenir Next", sans-serif'


# --- 1) Whole-app Qt QSS (appended via the style_did_init FILTER) ------------
#
# Runs AFTER the core stylesheets.py QSS is concatenated, and is appended LAST,
# so equal-specificity rules override the core gradient/rounded buttons. We set
# border-radius:0 and border explicitly to flatten -- omitting them leaves the
# core props.BORDER_RADIUS in effect.
_BAUHAUS_QSS = f"""
/* --- GMAT Bauhaus theme layer (recolour + refont + flatten) --- */
* {{
    font-family: {BAUHAUS_FONT_STACK};
}}
QMainWindow, QDialog, QWidget {{
    background-color: {BAUHAUS_PAPER};
    color: {BAUHAUS_INK};
}}
/* Toolbar / menu bar recolour: paper canvas, ink text. */
QMenuBar {{
    background-color: {BAUHAUS_PAPER};
    color: {BAUHAUS_INK};
    border: none;
}}
QMenuBar::item {{
    background: transparent;
    color: {BAUHAUS_INK};
    padding: 4px 10px;
}}
QMenuBar::item:selected, QMenuBar::item:pressed {{
    background-color: {BAUHAUS_BLUE};
    color: {BAUHAUS_ON_INK};
}}
QMenu {{
    background-color: {BAUHAUS_PAPER_ELEVATED};
    color: {BAUHAUS_INK};
    border: 1px solid {BAUHAUS_INK};
    border-radius: 0px;
}}
QMenu::item {{
    background: transparent;
    padding: 5px 22px;
}}
QMenu::item:selected {{
    background-color: {BAUHAUS_BLUE};
    color: {BAUHAUS_ON_INK};
}}
QMenu::separator {{
    height: 1px;
    background: {BAUHAUS_HOLLOW};
    margin: 4px 0;
}}
/* Flat, hard-edged buttons: kill the gradient + radius from core stylesheets. */
QPushButton {{
    background-color: {BAUHAUS_INK};
    color: {BAUHAUS_ON_INK};
    border: none;
    border-radius: 0px;
    padding: 6px 18px;
    margin: 1px;
}}
QPushButton:hover, QPushButton:default:hover {{
    background-color: {BAUHAUS_BLUE};
    border: none;
}}
QPushButton:pressed, QPushButton:checked {{
    background-color: {BAUHAUS_BLUE};
    color: {BAUHAUS_ON_INK};
}}
QPushButton:disabled {{
    background-color: {BAUHAUS_MUTED};
    color: {BAUHAUS_PAPER};
}}
/* Text entry surfaces: elevated paper, ink hairline, flat. */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background-color: {BAUHAUS_PAPER_ELEVATED};
    color: {BAUHAUS_INK};
    border: 1px solid {BAUHAUS_HOLLOW};
    border-radius: 0px;
    selection-background-color: {BAUHAUS_BLUE};
    selection-color: {BAUHAUS_ON_INK};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {BAUHAUS_BLUE};
}}
/* Tabs: flat, hard-edged, ink on paper with a blue active marker. */
QTabWidget::pane {{
    border: 1px solid {BAUHAUS_HOLLOW};
    border-radius: 0px;
}}
QTabBar::tab {{
    background-color: {BAUHAUS_PAPER};
    color: {BAUHAUS_INK};
    border: 1px solid {BAUHAUS_HOLLOW};
    border-radius: 0px;
    padding: 6px 14px;
}}
QTabBar::tab:selected {{
    background-color: {BAUHAUS_BLUE};
    color: {BAUHAUS_ON_INK};
    border: 1px solid {BAUHAUS_BLUE};
}}
"""


def _append_bauhaus_qss(style: str) -> str:
    """FILTER: receive the core app QSS and return it with our Bauhaus block
    appended, so our rules win by cascade order. Idempotent -- always returns
    ``core + our block`` (never accumulates into a module buffer)."""
    return style + _BAUHAUS_QSS


# --- 2) Webview CSS (injected into every AnkiWebView's <head>) ---------------
#
# Overrides the CSS custom properties on the <html> element. We set the light
# values on BOTH ``:root`` and ``:root.night-mode`` -- the latter has higher
# specificity in _root-vars.scss, so plain ``:root`` alone would lose in dark
# mode. This forces the light Bauhaus palette regardless of night mode.
_BAUHAUS_WEBVIEW_CSS = f"""
<style id="gmat-bauhaus-theme">
:root, :root.night-mode {{
    /* type */
    --font-size: 15px;

    /* canvas / backgrounds -> paper */
    --canvas: {BAUHAUS_PAPER};
    --canvas-elevated: {BAUHAUS_PAPER_ELEVATED};
    --canvas-inset: {BAUHAUS_PAPER_ELEVATED};
    --canvas-overlay: {BAUHAUS_PAPER_ELEVATED};
    --canvas-code: {BAUHAUS_PAPER};

    /* foreground / text -> ink */
    --fg: {BAUHAUS_INK};
    --fg-subtle: {BAUHAUS_MUTED};
    --fg-disabled: {BAUHAUS_HOLLOW};
    --fg-faint: {BAUHAUS_HOLLOW};
    --fg-link: {BAUHAUS_BLUE};

    /* borders -> ink hairlines */
    --border: {BAUHAUS_INK};
    --border-subtle: {BAUHAUS_HOLLOW};
    --border-strong: {BAUHAUS_INK};
    --border-focus: {BAUHAUS_BLUE};

    /* buttons -> flat paper w/ ink, blue primary (kill gradients) */
    --button-bg: {BAUHAUS_PAPER_ELEVATED};
    --button-gradient-start: {BAUHAUS_PAPER_ELEVATED};
    --button-gradient-end: {BAUHAUS_PAPER_ELEVATED};
    --button-hover-border: {BAUHAUS_INK};
    --button-primary-bg: {BAUHAUS_BLUE};
    --button-primary-gradient-start: {BAUHAUS_BLUE};
    --button-primary-gradient-end: {BAUHAUS_BLUE};

    /* accents (primary blue / yellow / red) */
    --accent-card: {BAUHAUS_BLUE};
    --accent-note: {BAUHAUS_YELLOW};
    --accent-danger: {BAUHAUS_RED};

    /* highlight (yellow) & selection (blue) */
    --highlight-bg: rgba(242, 194, 0, 0.45);
    --highlight-fg: {BAUHAUS_INK};
    --selected-bg: rgba(30, 82, 168, 0.30);
    --selected-fg: {BAUHAUS_INK};

    /* flatten radius (Bauhaus = hard edges) */
    --border-radius: 0px;
    --border-radius-medium: 0px;
    --border-radius-large: 0px;
}}
/* Futura on every webview element. */
:root, body, body * {{
    font-family: {BAUHAUS_FONT_STACK} !important;
}}
</style>
"""


def _on_webview_will_set_content(
    web_content: WebContent, context: object | None
) -> None:
    """Append our <style> (per the WebContent docstring: append, never
    overwrite). ``head`` is emitted last in <head>, after Anki's :root vars, so
    our overrides win. Re-fires automatically on theme change, so it persists
    across night-mode toggles."""
    web_content.head += _BAUHAUS_WEBVIEW_CSS + _CARD_CSS


# --- 3) QApplication base font ----------------------------------------------
def _set_base_font() -> None:
    """Set the real Qt base font once the QApplication exists. Defensive:
    no-op if the main window isn't up yet."""
    if aqt.mw is not None:
        aqt.mw.app.setFont(QFont(BAUHAUS_FONT_FAMILY))


# --- 4) GMAT card content transform (desktop reviewer) -----------------------
#
# Reshapes GMAT multiple-choice cards into the same Bauhaus layout the iOS app
# renders (square A-E markers, green correct-answer highlight, EXPLANATION
# block), so the desktop reviewer matches the mockup rather than being stock
# layout in Futura. Done server-side via the card_will_show FILTER: a <script>
# injected into card HTML would not execute, so the reshape happens in Python
# and returns ready-to-show HTML. Non-MC cards (plain front/back memory cards,
# or any non-GMAT deck) return None and pass through unchanged, styled by the
# theme layer alone. The .gmat-card CSS ships in _CARD_CSS (injected into every
# webview head above).

_CARD_CSS = (
    """
<style id="gmat-bauhaus-card">
.gmat-card { font-family: __FONT__; color: __INK__; text-align: left; }
.gmat-card .stem { font-weight: 500; font-size: 20px; line-height: 1.42; margin: 0 0 22px; }
.gmat-card .choices { display: flex; flex-direction: column; gap: 12px; margin: 0; }
.gmat-card .choice { display: flex; align-items: flex-start; gap: 14px; padding: 4px; border: 2.5px solid transparent; }
.gmat-card .marker { flex: 0 0 auto; width: 32px; height: 32px; box-sizing: border-box; border: 2.5px solid __INK__; background: __PAPER__; color: __INK__; font-weight: 700; font-size: 16px; line-height: 1; display: flex; align-items: center; justify-content: center; }
.gmat-card .choice-text { font-weight: 500; font-size: 19px; line-height: 1.35; padding-top: 4px; }
.gmat-card .choice.correct { border: 2.5px solid __GREEN__; }
.gmat-card .choice.correct .marker { background: __GREEN__; border-color: __GREEN__; color: __PAPER__; }
.gmat-card .answer-flag { align-self: flex-start; margin-left: auto; background: __GREEN__; color: __PAPER__; font-weight: 700; font-size: 12px; letter-spacing: .12em; text-transform: uppercase; padding: 4px 8px; line-height: 1; }
.gmat-card .rule { border: 0; height: 5px; background: __INK__; margin: 26px 0 0; }
.gmat-card .explanation-tab { display: inline-block; background: __INK__; color: __PAPER__; font-weight: 700; font-size: 12px; letter-spacing: .14em; text-transform: uppercase; padding: 6px 12px; margin: 14px 0; }
.gmat-card .explanation-body { font-weight: 400; font-size: 19px; line-height: 1.55; }
.gmat-card .explanation-body b, .gmat-card .explanation-body strong { font-weight: 700; }
</style>
""".replace("__FONT__", BAUHAUS_FONT_STACK)
    .replace("__INK__", BAUHAUS_INK)
    .replace("__PAPER__", BAUHAUS_PAPER)
    .replace("__GREEN__", BAUHAUS_GREEN)
)

_CHOICE_RE = re.compile(r"^\s*([A-E])[).]\s*(.*)$")
_HR_ANSWER_RE = re.compile(r"<hr[^>]*id=[\"']?answer[\"']?[^>]*>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MARKER_STRIP_RE = re.compile(r"^\s*(?:<[^>]+>\s*)*[A-E][).]\s*", re.I)
_ANSWER_LETTER_RE = re.compile(r"Answer:\s*(?:</b>)?\s*([A-E])", re.I)
_EXPLANATION_RE = re.compile(r"Explanation:\s*(?:</b>)?\s*([\s\S]*)$", re.I)


def _transform_card(text: str) -> str | None:
    """Bauhaus card HTML for a GMAT multiple-choice card, or None if the text is
    not an A-E multiple-choice card (the caller then passes it through)."""
    parts = _HR_ANSWER_RE.split(text, maxsplit=1)
    front = parts[0]
    back = parts[1] if len(parts) > 1 else None

    lines = _BR_RE.split(front)
    first = -1
    for i, line in enumerate(lines):
        if _CHOICE_RE.match(_TAG_RE.sub("", line).strip()):
            first = i
            break
    if first == -1:
        return None

    stem_html = "<br>".join(lines[:first]).strip()
    choices: list[tuple[str, str]] = []
    for line in lines[first:]:
        m = _CHOICE_RE.match(_TAG_RE.sub("", line).strip())
        if m:
            # Keep the ORIGINAL choice HTML (minus the leading "A)" marker) so
            # entities like &lt; render literally and inline formatting survives.
            choice_html = _MARKER_STRIP_RE.sub("", line).strip()
            choices.append((m.group(1).upper(), choice_html))
    if not choices:
        return None

    correct = None
    explanation = None
    if back:
        am = _ANSWER_LETTER_RE.search(back)
        if am:
            correct = am.group(1).upper()
        em = _EXPLANATION_RE.search(back)
        if em:
            explanation = em.group(1).strip()

    out = ['<div class="gmat-card">']
    if stem_html:
        out.append(f'<div class="stem">{stem_html}</div>')
    out.append('<div class="choices">')
    for letter, choice_html in choices:
        is_correct = bool(correct and letter == correct)
        out.append(f'<div class="choice{" correct" if is_correct else ""}">')
        out.append(f'<div class="marker">{letter}</div>')
        out.append(f'<div class="choice-text">{choice_html}</div>')
        if is_correct:
            out.append('<span class="answer-flag">Answer</span>')
        out.append("</div>")
    out.append("</div>")
    if back:
        # Keep id="answer" so Anki's scroll-to-answer still works.
        out.append('<hr id="answer" class="rule">')
        out.append('<div class="explanation-tab">Explanation</div>')
        out.append(
            f'<div class="explanation-body">{explanation if explanation else back}</div>'
        )
    out.append("</div>")
    return "".join(out)


def _on_card_will_show(text: str, card: object, kind: str) -> str:
    """FILTER: reshape GMAT MC cards into the Bauhaus layout. The card CSS is
    prepended *inline* so the styling always travels with the card — the
    ``webview_will_set_content`` head injection doesn't reliably reach the
    reviewer's card view (styles get set before our hook, and cards re-render via
    JS without re-firing it). Defensive: any non-MC card or error passes the
    original text through unchanged."""
    try:
        transformed = _transform_card(text)
        return _CARD_CSS + transformed if transformed is not None else text
    except Exception:  # pragma: no cover - a theming error must not hide a card
        return text


def init() -> None:
    """Register the theme layer. Importing this module then calling init() is
    the only wiring required (done from aqt.main)."""
    # 1) whole-app QSS filter (re-runs on every apply_style rebuild).
    gui_hooks.style_did_init.append(_append_bauhaus_qss)
    # 2) per-webview CSS injection (fires for every AnkiWebView).
    gui_hooks.webview_will_set_content.append(_on_webview_will_set_content)
    # 2b) reshape GMAT multiple-choice cards into the Bauhaus card layout.
    gui_hooks.card_will_show.append(_on_card_will_show)
    # 3) real QApplication base font, set once the window is up.
    gui_hooks.main_window_did_init.append(_set_base_font)

    # apply_style() early-returns when neither night-mode nor widget-style has
    # changed, and it has already run once by the time we get here -- so merely
    # appending our filter won't repaint. Force one rebuild+setStyleSheet with
    # our filter in place. _apply_style is the exact private method that runs
    # the style_did_init hook; it's the only clean way to force an app-wide
    # re-style without a fake theme change. Defensive: no-op if mw/app is None.
    # Defensive: a theming error must NEVER break Anki startup.
    if aqt.mw is not None:
        try:
            from aqt.theme import theme_manager

            theme_manager._apply_style(aqt.mw.app)
            _set_base_font()
        except Exception as exc:  # pragma: no cover - startup safety net
            print(f"[gmat_theme] initial restyle skipped: {exc}")
