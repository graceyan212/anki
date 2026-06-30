# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for anki.gmat_readiness (track T3).

Self-contained: builds a tiny tagged deck in-memory rather than depending on
the content repo's gmat_focus.apkg, then exercises BOTH required states:

  (a) fresh deck, 0 graded reviews  -> ABSTAIN with a "what's missing" list
  (b) enough graded reviews + >=50% coverage -> SCORE + honest range + coverage %

It also unit-tests the FSRS forgetting curve, the tag/coverage matching, and the
two branches of the give-up rule independently.
"""

from __future__ import annotations

import time

from anki.gmat_readiness import (
    MIN_GRADED_REVIEWS,
    _all_outline_topics,
    _recall_probability,
    compute_readiness,
    covered_outline_tag_from_tags,
    difficulty_from_tags,
)
from tests.shared import getEmptyCol

# A spread of topics that comfortably clears the 50% coverage gate (>=14 of 28).
# Quant/Verbal entries are full 3-segment tags; DataInsights are 2-segment.
_TOPIC_TAGS = [
    "Quant::Arithmetic::Percents",
    "Quant::Arithmetic::PropertiesOfIntegers",
    "Quant::Arithmetic::FractionsDecimals",
    "Quant::Algebra::LinearEquations",
    "Quant::Algebra::Quadratics",
    "Quant::WordProblems::RateWorkMixtureInterest",
    "Verbal::CriticalReasoning::Assumption",
    "Verbal::CriticalReasoning::Weaken",
    "Verbal::CriticalReasoning::Strengthen",
    "Verbal::ReadingComprehension::MainIdea",
    "Verbal::ReadingComprehension::Inference",
    "DataInsights::DataSufficiency",
    "DataInsights::TableAnalysis",
    "DataInsights::TwoPartAnalysis",
    "DataInsights::GraphicsInterpretation",
    "DataInsights::MultiSourceReasoning",
]


def _build_deck(col, *, cards_per_topic: int = 3) -> None:
    """Add Basic notes carrying GMAT topic + difficulty tags."""
    difficulties = ["easy", "medium", "hard"]
    for i, topic in enumerate(_TOPIC_TAGS):
        for j in range(cards_per_topic):
            note = col.newNote()
            note["Front"] = f"{topic} q{j}"
            note["Back"] = f"answer {j}"
            note.tags = [
                topic,
                f"difficulty::{difficulties[(i + j) % 3]}",
                "split::train",
            ]
            col.addNote(note)


def _simulate_reviews(col) -> int:
    """Answer every due card 'Good' until the queue drains; returns answers
    given. Each new card walks through its learning steps, so a single drain
    produces several revlog rows per card."""
    col.set_config("fsrs", True)
    # Lift today's new/review caps so every card surfaces (mirrors the
    # "increase today's limit" study action).
    col.sched.extend_limits(99999, 99999)
    answered = 0
    while True:
        card = col.sched.getCard()
        if card is None:
            break
        col.sched.answerCard(card, 3)  # Good
        answered += 1
    return answered


# --------------------------------------------------------------------------
# Unit tests for the pieces.
# --------------------------------------------------------------------------


def test_recall_probability_matches_fsrs_reference():
    # From fsrs-5.2.0 inference.rs::current_retrievability test vectors
    # (stability=1, decay=0.2): R(0)=1, R(1)=0.9, R(2)=~0.8403, R(3)=~0.7985.
    assert _recall_probability(1.0, 0.0, 0.2) == 1.0
    assert abs(_recall_probability(1.0, 1.0, 0.2) - 0.9) < 1e-6
    assert abs(_recall_probability(1.0, 2.0, 0.2) - 0.84028935) < 1e-5
    assert abs(_recall_probability(1.0, 3.0, 0.2) - 0.7985001) < 1e-5
    # zero/negative stability is non-recallable
    assert _recall_probability(0.0, 1.0, 0.2) == 0.0


def test_outline_has_28_topics():
    assert len(_all_outline_topics()) == 28


def test_tag_matching():
    # full 3-segment Quant tag matches its outline entry
    assert (
        covered_outline_tag_from_tags(
            ["Quant::Arithmetic::Percents", "difficulty::easy"]
        )
        == "Quant::Arithmetic::Percents"
    )
    # DataInsights matches on the 2-segment prefix even with an extra subtopic
    assert (
        covered_outline_tag_from_tags(["DataInsights::DataSufficiency::Whatever"])
        == "DataInsights::DataSufficiency"
    )
    # auxiliary-only tags match nothing
    assert covered_outline_tag_from_tags(["difficulty::hard", "split::train"]) is None
    # an unknown topic matches nothing
    assert covered_outline_tag_from_tags(["Quant::Geometry::Circles"]) is None


def test_difficulty_parsing():
    assert difficulty_from_tags(["difficulty::medium"]) == "medium"
    assert difficulty_from_tags(["split::train"]) == "unknown"


# --------------------------------------------------------------------------
# The two acceptance states.
# --------------------------------------------------------------------------


def test_fresh_deck_abstains_on_reviews():
    """State (a): coverage is fine but there are 0 graded reviews -> ABSTAIN."""
    col = getEmptyCol()
    _build_deck(col)
    res = compute_readiness(col)
    assert res.abstained
    assert res.score is None
    assert res.score_low is None and res.score_high is None
    assert res.graded_reviews == 0
    assert res.coverage_fraction >= 0.50  # the deck itself is broad enough
    # the missing list must call out the review shortfall
    assert any("graded review" in m for m in res.missing)
    # the rule must be stated for the UI
    assert str(MIN_GRADED_REVIEWS) in res.rule_text


def test_narrow_deck_abstains_on_coverage():
    """Coverage branch of the give-up rule: a 1-topic deck abstains even with
    plenty of reviews."""
    col = getEmptyCol()
    # one topic only -> 1/28 coverage
    for j in range(120):
        note = col.newNote()
        note["Front"] = f"q{j}"
        note["Back"] = "a"
        note.tags = ["Quant::Arithmetic::Percents", "difficulty::easy"]
        col.addNote(note)
    answered = _simulate_reviews(col)
    assert answered >= MIN_GRADED_REVIEWS
    res = compute_readiness(col)
    assert res.abstained
    assert res.coverage_fraction < 0.50
    assert any("coverage" in m for m in res.missing)


def test_studied_deck_shows_score_and_wide_range():
    """State (b): enough graded reviews + >=50% coverage -> SCORE + range."""
    col = getEmptyCol()
    _build_deck(col, cards_per_topic=10)  # 16 topics x 10 = 160 cards
    answered = _simulate_reviews(col)
    assert answered >= MIN_GRADED_REVIEWS

    # Evaluate a week out so the point estimate isn't a trivial 1.0.
    exam_day = int(time.time()) + 7 * 86400
    res = compute_readiness(col, as_of=exam_day)

    assert not res.abstained, res.missing
    assert res.score is not None
    assert res.score_low is not None and res.score_high is not None
    # the score sits inside its band
    assert res.score_low <= res.score <= res.score_high
    # the band is honestly wide (>= ~5 points), not a false-precision number
    assert res.score_high - res.score_low >= 5.0
    # evidence is populated
    assert res.graded_reviews >= MIN_GRADED_REVIEWS
    assert res.coverage_fraction >= 0.50
    assert res.scored_cards > 0
    # summary text never shows a bare number; it always pairs score with range
    text = "\n".join(res.summary_lines())
    assert "honest range" in text
    assert "Topic coverage" in text
