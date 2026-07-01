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
    QLabel,
    Qt,
    QTextBrowser,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import disable_help_button, restoreGeom, saveGeom

if TYPE_CHECKING:
    from aqt.main import AnkiQt

# The deck the readiness summary scopes to. None => whole collection.
GMAT_DECK_NAME = "GMAT Focus"


class GmatReadinessDialog(QDialog):
    def __init__(self, mw: AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("GMAT Memory Readiness")
        self.setMinimumWidth(560)
        disable_help_button(self)

        layout = QVBoxLayout(self)

        # Headline: either the score+range or the abstain marker.
        self._headline = QLabel(self)
        headline_font = QFont()
        headline_font.setPointSize(20)
        headline_font.setBold(True)
        self._headline.setFont(headline_font)
        self._headline.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._headline.setWordWrap(True)
        layout.addWidget(self._headline)

        # Sub-headline: the one-line interpretation.
        self._subhead = QLabel(self)
        self._subhead.setWordWrap(True)
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

        result = compute_readiness(col, deck_name=deck_name)
        self._render(result, deck_name)

    def _render(self, result: ReadinessResult, deck_name: str | None) -> None:
        if result.abstained:
            self._headline.setText("Readiness — not enough data yet")
            self._headline.setStyleSheet("color: #b58900;")  # amber: deliberate
            self._subhead.setText(
                "Not enough review history yet to show a number you can trust. "
                "Here's what's left:"
            )
        else:
            assert result.score is not None
            self._headline.setText(f"Readiness: {result.score:.0f} / 100")
            self._headline.setStyleSheet("")
            self._subhead.setText(
                f"Likely range {result.score_low:.0f}–{result.score_high:.0f}, "
                f"based on your recall across {result.scored_cards} exam cards."
            )

        self._body.setHtml(self._body_html(result, deck_name))

    def _body_html(self, result: ReadinessResult, deck_name: str | None) -> str:
        scope = html.escape(deck_name) if deck_name else "whole collection"
        cov_pct = f"{result.coverage_fraction * 100:.0f}%"
        parts: list[str] = []
        parts.append(f"<p style='margin:2px 0'><b>Deck:</b> {scope}</p>")

        # Evidence block.
        parts.append("<table cellpadding='3' style='border-collapse:collapse'>")
        parts.append(
            f"<tr><td><b>Topic coverage</b></td>"
            f"<td>{cov_pct} &nbsp;"
            f"({len(result.covered_topics)} of {result.total_topics} exam topics in your deck)</td></tr>"
        )
        parts.append(
            f"<tr><td><b>Reviews done</b></td><td>{result.graded_reviews}</td></tr>"
        )
        parts.append(
            f"<tr><td><b>Cards with enough data to score</b></td>"
            f"<td>{result.scored_cards} of {result.total_exam_cards}</td></tr>"
        )
        parts.append("</table>")

        if result.abstained:
            parts.append(
                "<p style='margin:8px 0 2px 0'><b>What's left before a score shows:</b></p>"
            )
            parts.append("<ul style='margin-top:2px'>")
            for m in result.missing:
                parts.append(f"<li>{html.escape(m)}</li>")
            parts.append("</ul>")

        # §7c coverage map: every exam topic, marked covered/not, always shown.
        parts.append(
            f"<p style='margin:10px 0 2px 0'><b>Exam coverage</b> — "
            f"{len(result.covered_topics)} of {result.total_topics} topics in your deck</p>"
        )
        for section_name, rows in result.coverage_map():
            parts.append(
                f"<p style='margin:6px 0 1px 0'><b>{html.escape(section_name)}</b></p>"
            )
            parts.append("<div style='margin-left:8px'>")
            for topic_name, is_covered in rows:
                label = html.escape(topic_name)
                if is_covered:
                    parts.append(
                        f"<div><span style='color:#2e7d32'>&#10003;</span> {label}</div>"
                    )
                else:
                    parts.append(
                        f"<div><span style='color:#c0392b'>&#10007;</span> "
                        f"<span style='color:#999'>{label}</span></div>"
                    )
            parts.append("</div>")

        # The rule, verbatim.
        parts.append(
            f"<p style='margin-top:10px;color:#888'><i>{html.escape(result.rule_text)}</i></p>"
        )
        return "".join(parts)


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


def init() -> None:
    """Register the hook. Importing this module then calling init() is the only
    wiring required (done from aqt.main)."""
    aqt.gui_hooks.main_window_did_init.append(_on_main_window_did_init)
