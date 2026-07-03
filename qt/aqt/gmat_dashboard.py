# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT readiness dashboard (track T3).

A small dialog that surfaces the honest readiness summary computed by
``anki.gmat_readiness.compute_readiness``. It is deliberately a thin
presentation layer: all of the logic (point estimate, wide range, coverage %,
give-up rule) lives in pylib so it can be exercised headless.

Two visual states:

  * ABSTAIN -- when the give-up rule fires (< 200 graded reviews OR < 50% topic
    coverage). No number is shown; instead the panel states the rule and lists
    exactly what is missing.
  * SCORE -- a point estimate out of 100, an honest +/- range, the coverage
    percentage, and the give-up rule that was satisfied.

The panel registers itself on ``gui_hooks.main_window_did_init`` (so importing
this module is the only wiring needed) and adds a single "GMAT Readiness" entry
to the Tools menu.

Presentation is the "Bauhaus" language shared with the iOS app: Futura, warm
paper, the primary palette, hard edges, and square coverage markers. The body
is Qt rich text (QTextBrowser), which supports only a subset of HTML/CSS
(tables + basic inline styles; no flexbox/grid/border-radius), so the layout is
built from ``<table>`` elements and coloured cells use both the ``bgcolor``
attribute and ``background-color`` for reliable fills.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import aqt
from anki.gmat_readiness import ReadinessResult, compute_readiness
from aqt.qt import (
    QAction,
    QDialog,
    QDialogButtonBox,
    QFont,
    QFrame,
    QLabel,
    Qt,
    QTextBrowser,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import disable_help_button, restoreGeom, saveGeom

if TYPE_CHECKING:
    from aqt.main import AnkiQt
    from aqt.toolbar import Toolbar

# The deck the readiness summary scopes to. None => whole collection.
GMAT_DECK_NAME = "GMAT Focus"

# --- Shared Bauhaus tokens (must match the iOS app + approved dashboard mockup) ---
BAUHAUS_RED = "#E2231A"
BAUHAUS_YELLOW = "#F2C200"
BAUHAUS_GREEN = "#2E9E4F"
BAUHAUS_BLUE = "#1E52A8"
BAUHAUS_INK = "#1A1A1A"
BAUHAUS_PAPER = "#F5F1E6"
BAUHAUS_MUTED = "#8a8577"
# a hollow-square border colour lifted from the mockup (.topic.no .m)
BAUHAUS_HOLLOW = "#c9c3b2"
# Qt QFont uses the "Futura" family directly.
BAUHAUS_FONT_FAMILY = "Futura"


class GmatReadinessDialog(QDialog):
    def __init__(self, mw: AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("GMAT Memory Readiness")
        self.setMinimumWidth(560)
        disable_help_button(self)

        # Dialog-level Bauhaus QSS: paper background, ink text, Futura, flat.
        self.setFont(QFont(BAUHAUS_FONT_FAMILY))
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {BAUHAUS_PAPER};
                color: {BAUHAUS_INK};
            }}
            QLabel {{
                color: {BAUHAUS_INK};
                font-family: {BAUHAUS_FONT_FAMILY};
                background-color: transparent;
            }}
            QTextBrowser {{
                background-color: {BAUHAUS_PAPER};
                color: {BAUHAUS_INK};
                border: none;
                font-family: {BAUHAUS_FONT_FAMILY};
            }}
            QPushButton {{
                background-color: {BAUHAUS_INK};
                color: #ffffff;
                font-family: {BAUHAUS_FONT_FAMILY};
                font-weight: bold;
                padding: 8px 22px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: {BAUHAUS_BLUE};
            }}
            """
        )

        layout = QVBoxLayout(self)

        # In-pane Bauhaus brand header: geometric mark + wordmark + 3px ink rule,
        # matching the approved mockup (and the iOS app header).
        self._brand = QLabel(self)
        # Per-glyph sizes so the three shapes read as visually equal — the ▲
        # glyph is drawn heavier than ● / ■ at a common size, so it's dialed down.
        self._brand.setText(
            f"<span style='color:{BAUHAUS_RED}; font-size:15px;'>&#9679;</span>"
            f"<span style='color:{BAUHAUS_BLUE}; font-size:14px;'>&nbsp;&#9632;</span>"
            f"<span style='color:{BAUHAUS_YELLOW}; font-size:11px;'>&nbsp;&#9650;</span>"
            f"<span style='color:{BAUHAUS_INK}; font-weight:bold;'>"
            f"&nbsp;&nbsp;GMAT READINESS</span>"
        )
        brand_font = QFont(BAUHAUS_FONT_FAMILY)
        brand_font.setPointSize(14)
        self._brand.setFont(brand_font)
        layout.addWidget(self._brand)

        rule = QFrame(self)
        rule.setFixedHeight(3)
        rule.setStyleSheet(f"background-color: {BAUHAUS_INK}; border: none;")
        layout.addWidget(rule)

        # Headline: either the score+range or the abstain marker.
        self._headline = QLabel(self)
        headline_font = QFont(BAUHAUS_FONT_FAMILY)
        headline_font.setPointSize(30)
        headline_font.setBold(True)
        self._headline.setFont(headline_font)
        self._headline.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._headline.setWordWrap(True)
        layout.addWidget(self._headline)

        # Sub-headline: the one-line interpretation.
        self._subhead = QLabel(self)
        subhead_font = QFont(BAUHAUS_FONT_FAMILY)
        subhead_font.setPointSize(11)
        self._subhead.setFont(subhead_font)
        self._subhead.setWordWrap(True)
        self._subhead.setStyleSheet(f"color: {BAUHAUS_MUTED};")
        layout.addWidget(self._subhead)

        # Body: coverage, evidence, give-up rule, missing list.
        self._body = QTextBrowser(self)
        self._body.setOpenExternalLinks(False)
        layout.addWidget(self._body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        qconnect(buttons.rejected, self.reject)
        qconnect(buttons.accepted, self.accept)
        layout.addWidget(buttons)

        restoreGeom(self, "gmatReadiness")
        qconnect(self.finished, lambda _: saveGeom(self, "gmatReadiness"))

        self.refresh()

    def refresh(self) -> None:
        col = self.mw.col
        if col is None:
            self._headline.setText("No collection open")
            self._subhead.setText("")
            self._body.setHtml("")
            return

        # Scope to the GMAT deck if present, else the whole collection.
        deck_name = GMAT_DECK_NAME
        if col.decks.id_for_name(deck_name) is None:
            deck_name = None

        # The three shared-engine scores (memory / performance / readiness) come
        # from the Rust GetGmatScores RPC, so desktop and phone render identical
        # numbers. The detailed coverage/give-up body below still uses the Python
        # readiness computation.
        try:
            self._scores = col._backend.get_gmat_scores(deck_name=deck_name or "")
        except Exception:
            self._scores = None

        result = compute_readiness(col, deck_name=deck_name)
        self._render(result, deck_name)

    def _render(self, result: ReadinessResult, deck_name: str | None) -> None:
        # The three shared-engine scores (memory / performance / readiness) are
        # now the hero, rendered as three separate blocks in the body below. The
        # old single-readiness headline duplicated the readiness row, wasn't
        # labelled (so you couldn't tell which score it was), and its yellow
        # "abstain" fill clashed with the yellow performance accent — so it's
        # retired. Keeping the widgets but hidden avoids reworking the layout.
        self._headline.setVisible(False)
        self._subhead.setVisible(False)
        self._body.setHtml(self._body_html(result, deck_name))

    def _body_html(self, result: ReadinessResult, deck_name: str | None) -> str:
        """Emit the Bauhaus body as Qt rich text.

        QTextBrowser renders only a subset of HTML/CSS (Qt rich text). Flexbox,
        grid, CSS variables and border-radius are NOT supported, so the layout
        is built entirely from ``<table>`` elements and basic inline styles:
        the three evidence stats and the coverage-map columns are tables, the
        square markers are fixed-size table cells with a background/border, and
        the score bar is approximated by a two-cell coloured table row. Coloured
        cells set both ``bgcolor`` and ``background-color`` for reliable fills.
        """
        scope = html.escape(deck_name) if deck_name else "whole collection"
        parts: list[str] = []

        # Scope kicker (bold, uppercase label).
        parts.append(
            f"<p style='margin:0 0 12px 0; font-size:10px; font-weight:bold;"
            f" color:{BAUHAUS_MUTED};'>"
            f"DECK &nbsp;·&nbsp; {scope.upper()}</p>"
        )

        # The three shared-engine scores (memory / performance / readiness),
        # identical to what the phone shows. This is the canonical readiness
        # display; the separate 0-100 readiness bar was removed so the panel
        # never shows two different "readiness" figures at once.
        parts.append(self._three_scores_html())

        # Evidence stats: three big tabular numbers with uppercase labels under
        # them, laid out as a bordered table (ink hairlines between cells).
        parts.append(self._stats_html(result))

        # ABSTAIN: "what's left" checklist with red square markers.
        if result.abstained and result.missing:
            parts.append(self._whats_left_html(result))

        # Coverage map: every exam topic, marked covered/not, always shown.
        parts.append(self._coverage_html(result))

        # Give-up rule: a left-accented note, italic + muted.
        parts.append(self._rule_note_html(result))

        return "".join(parts)

    def _three_scores_html(self) -> str:
        """Render the three shared-engine scores (memory / performance /
        readiness) as a compact Bauhaus table. Each row shows the number + range
        (with confidence for readiness), or the give-up state and what's still
        missing. Data comes from the Rust GetGmatScores RPC (self._scores)."""
        scores = getattr(self, "_scores", None)
        if scores is None:
            return ""
        rows = [
            ("MEMORY", BAUHAUS_GREEN, scores.memory),
            ("PERFORMANCE", BAUHAUS_YELLOW, scores.performance),
            ("READINESS", BAUHAUS_BLUE, scores.readiness),
        ]
        blocks: list[str] = []
        for label, accent, sv in rows:
            if sv.abstained:
                value_html = (
                    f"<span style='font-size:16px; font-weight:bold;"
                    f" color:{BAUHAUS_MUTED};'>NOT ENOUGH DATA YET</span>"
                )
                detail = html.escape("; ".join(sv.missing))
            else:
                n = int(round(sv.score))
                unit = "" if sv.unit == "gmat" else " / 100"
                detail = f"range {int(round(sv.low))}&#8211;{int(round(sv.high))}"
                if sv.confidence:
                    detail += f" &nbsp;·&nbsp; confidence {html.escape(sv.confidence)}"
                value_html = (
                    f"<span style='font-size:32px; font-weight:bold;"
                    f" color:{BAUHAUS_INK};'>{n}</span>"
                    f"<span style='font-size:15px; color:{BAUHAUS_MUTED};'>{unit}</span>"
                )
            # Each score is its OWN bordered block with a colour accent stripe,
            # separated by margin — breathing room instead of a cramped stack.
            blocks.append(
                f"<table cellspacing='0' cellpadding='0' width='100%'"
                f" style='border-collapse:collapse; border:2px solid {BAUHAUS_INK};"
                f" margin:0 0 10px 0;'><tr>"
                f"<td width='10' bgcolor='{accent}'"
                f" style='background-color:{accent};'>&nbsp;</td>"
                f"<td style='padding:11px 14px;'>"
                f"<div style='font-size:10px; font-weight:bold; letter-spacing:1px;"
                f" color:{BAUHAUS_MUTED};'>{label}</div>"
                f"{value_html}"
                f"<div style='font-size:11px; color:{BAUHAUS_INK};'>{detail}</div>"
                f"</td></tr></table>"
            )
        return "".join(blocks)

    def _score_bar_html(self, result: ReadinessResult) -> str:
        """Approximate the score bar with a two-cell coloured table row.

        A blue-filled portion (0 -> score) against a paper remainder, both
        boxed by an ink border. Reduced fidelity vs the mockup's positioned
        band/tick, which Qt rich text cannot render; the exact range is stated
        in bold below the bar so the number is never lost.
        """
        assert result.score is not None
        fill = max(0, min(100, int(round(result.score))))
        rest = 100 - fill
        # Guard against zero-width cells (Qt may drop them).
        fill = max(fill, 1)
        rest = max(rest, 1)
        return (
            f"<p style='margin:0 0 4px 0; font-size:13px; color:{BAUHAUS_INK};'>"
            f"Likely range "
            f"<b>{result.score_low:.0f}&#8211;{result.score_high:.0f}</b>"
            f" &nbsp;out of 100</p>"
            f"<table cellspacing='0' cellpadding='0' width='100%'"
            f" style='border-collapse:collapse; border:2px solid {BAUHAUS_INK};"
            f" margin:0 0 4px 0;'>"
            f"<tr>"
            f"<td width='{fill}%' height='18' bgcolor='{BAUHAUS_BLUE}'"
            f" style='background-color:{BAUHAUS_BLUE};'>&nbsp;</td>"
            f"<td width='{rest}%' height='18' bgcolor='{BAUHAUS_PAPER}'"
            f" style='background-color:{BAUHAUS_PAPER};'>&nbsp;</td>"
            f"</tr></table>"
            f"<table cellspacing='0' cellpadding='0' width='100%'"
            f" style='margin:0 0 4px 0;'><tr>"
            f"<td align='left' style='font-size:9px; font-weight:bold;"
            f" color:{BAUHAUS_MUTED};'>0</td>"
            f"<td align='right' style='font-size:9px; font-weight:bold;"
            f" color:{BAUHAUS_MUTED};'>100</td>"
            f"</tr></table>"
        )

    def _stats_html(self, result: ReadinessResult) -> str:
        cov_pct = f"{result.coverage_fraction * 100:.0f}%"
        # In ABSTAIN, the two threshold stats are flagged red.
        cov_flag = result.abstained and result.coverage_fraction < 0.5
        rev_flag = result.abstained and result.graded_reviews < 200

        def cell(value: str, label: str, flagged: bool) -> str:
            vcolor = BAUHAUS_RED if flagged else BAUHAUS_INK
            return (
                f"<td width='33%' valign='top' bgcolor='{BAUHAUS_PAPER}'"
                f" style='background-color:{BAUHAUS_PAPER}; padding:10px 8px;"
                f" border:2px solid {BAUHAUS_INK};'>"
                f"<div style='font-size:24px; font-weight:bold; color:{vcolor};'>"
                f"{value}</div>"
                f"<div style='font-size:9px; font-weight:bold;"
                f" color:{BAUHAUS_MUTED};'>{label}</div>"
                f"</td>"
            )

        return (
            "<table cellspacing='0' cellpadding='0' width='100%'"
            " style='border-collapse:collapse; margin:12px 0 6px 0;'><tr>"
            + cell(cov_pct, "TOPIC COVERAGE", cov_flag)
            + cell(str(result.graded_reviews), "REVIEWS DONE", rev_flag)
            + cell(
                f"{result.scored_cards}"
                f"<small style='font-size:13px; color:{BAUHAUS_MUTED};'>"
                f" / {result.total_exam_cards}</small>",
                "SCORABLE CARDS",
                False,
            )
            + "</tr></table>"
        )

    def _whats_left_html(self, result: ReadinessResult) -> str:
        rows: list[str] = []
        for m in result.missing:
            rows.append(
                f"<tr>"
                f"<td width='16' valign='middle' style='padding:3px 0;'>"
                f"<table cellspacing='0' cellpadding='0'><tr>"
                f"<td width='11' height='11'"
                f" style='border:2px solid {BAUHAUS_RED};'></td>"
                f"</tr></table></td>"
                f"<td valign='middle'"
                f" style='padding:3px 0 3px 8px; font-size:12px;"
                f" color:{BAUHAUS_INK};'>{html.escape(m)}</td>"
                f"</tr>"
            )
        return (
            f"<p style='margin:14px 0 6px 0; font-size:11px; font-weight:bold;"
            f" color:{BAUHAUS_INK};'>WHAT'S LEFT</p>"
            f"<table cellspacing='0' cellpadding='0'>"
            + "".join(rows)
            + "</table>"
        )

    def _coverage_html(self, result: ReadinessResult) -> str:
        parts: list[str] = []
        # Ink divider above the coverage map: a 1-row full-width table (Qt rich
        # text does not render border-top on a <p>).
        parts.append(
            f"<table cellspacing='0' cellpadding='0' width='100%'"
            f" style='margin:16px 0 0 0;'><tr>"
            f"<td height='2' bgcolor='{BAUHAUS_INK}'"
            f" style='background-color:{BAUHAUS_INK};'></td></tr></table>"
        )
        parts.append(
            f"<p style='margin:10px 0 2px 0; font-size:11px;"
            f" font-weight:bold; color:{BAUHAUS_INK};'>"
            f"EXAM COVERAGE &#8212; {len(result.covered_topics)} / "
            f"{result.total_topics} TOPICS</p>"
        )
        for section_name, rows in result.coverage_map():
            parts.append(
                f"<p style='margin:12px 0 4px 0; font-size:12px;"
                f" font-weight:bold; color:{BAUHAUS_INK};'>"
                f"{html.escape(section_name).upper()}</p>"
            )
            # Two-column topic grid via a table (grid/flex unsupported here).
            cells = [self._topic_cell(name, cov) for name, cov in rows]
            parts.append(
                "<table cellspacing='0' cellpadding='0' width='100%'"
                " style='border-collapse:collapse;'>"
            )
            for i in range(0, len(cells), 2):
                left = cells[i]
                right = cells[i + 1] if i + 1 < len(cells) else "<td width='50%'></td>"
                parts.append(f"<tr>{left}{right}</tr>")
            parts.append("</table>")
        return "".join(parts)

    def _topic_cell(self, topic_name: str, is_covered: bool) -> str:
        label = html.escape(topic_name)
        if is_covered:
            # Green filled square marker; ink label.
            marker = (
                f"<td width='13' height='13' bgcolor='{BAUHAUS_GREEN}'"
                f" style='background-color:{BAUHAUS_GREEN};"
                f" border:2px solid {BAUHAUS_GREEN};'></td>"
            )
            label_color = BAUHAUS_INK
        else:
            # Hollow square with a muted border; muted label.
            marker = (
                f"<td width='13' height='13' bgcolor='{BAUHAUS_PAPER}'"
                f" style='background-color:{BAUHAUS_PAPER};"
                f" border:2px solid {BAUHAUS_HOLLOW};'></td>"
            )
            label_color = BAUHAUS_MUTED
        return (
            f"<td width='50%' valign='middle' style='padding:3px 0;'>"
            f"<table cellspacing='0' cellpadding='0'><tr>"
            f"{marker}"
            f"<td valign='middle'"
            f" style='padding-left:8px; font-size:12px; color:{label_color};'>"
            f"{label}</td>"
            f"</tr></table></td>"
        )

    def _rule_note_html(self, result: ReadinessResult) -> str:
        # Left-ruled note: a narrow yellow accent cell + italic muted text.
        return (
            f"<table cellspacing='0' cellpadding='0' width='100%'"
            f" style='border-collapse:collapse; margin-top:16px;'><tr>"
            f"<td width='4' bgcolor='{BAUHAUS_YELLOW}'"
            f" style='background-color:{BAUHAUS_YELLOW};'></td>"
            f"<td bgcolor='{BAUHAUS_PAPER}'"
            f" style='padding:9px 12px; font-size:12px;"
            f" color:{BAUHAUS_MUTED}; background-color:{BAUHAUS_PAPER};'>"
            f"<i>{html.escape(result.rule_text)}</i></td>"
            f"</tr></table>"
        )


def show_gmat_readiness(mw: AnkiQt) -> None:
    dialog = GmatReadinessDialog(mw)
    dialog.show()


def _on_main_window_did_init() -> None:
    """Add a single 'GMAT Readiness' action to the Tools menu."""
    mw = aqt.mw
    if mw is None:
        return
    action = QAction("GMAT Readiness", mw)
    qconnect(action.triggered, lambda: show_gmat_readiness(mw))
    mw.form.menuTools.addAction(action)


def _on_top_toolbar_did_init_links(links: list[str], toolbar: Toolbar) -> None:
    """Add a 'Readiness' link to the top toolbar so the dashboard opens from
    inside the main window, not only the Tools menu / macOS menu bar."""
    mw = aqt.mw
    if mw is None:
        return
    links.append(
        toolbar.create_link(
            "gmat_readiness",
            "Readiness",
            lambda: show_gmat_readiness(mw),
            tip="GMAT Readiness",
            id="gmat_readiness",
        )
    )


def init() -> None:
    """Register the hooks. Importing this module then calling init() is the only
    wiring required (done from aqt.main)."""
    aqt.gui_hooks.main_window_did_init.append(_on_main_window_did_init)
    aqt.gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_did_init_links)
