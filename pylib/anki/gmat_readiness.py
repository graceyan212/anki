# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT memory-readiness scoring (track T3).

Produces an *honest* readiness summary for a GMAT deck:

  * a point estimate (mean FSRS recall probability across the deck's exam cards),
  * a deliberately WIDE uncertainty band (difficulty tags are coarse, not
    IRT-calibrated -- the band reflects that),
  * a topic-coverage percentage (distinct topics in the deck / topics in the
    GMAT Focus coverage outline),
  * and a *give-up rule* that abstains -- shows NO score and instead lists what
    is missing -- when the data is too thin to be honest about.

No AI / no model inference of difficulty: every number is computed directly from
data already exposed by the collection (``card.memory_state``, the FSRS decay /
last-review-time fields, the scheduler day, and ``notes.tags``). The Rust FSRS
forgetting curve is reproduced here exactly (see ``_recall_probability``) so we
do not depend on any new backend RPC.

This module is pure-Python and importable headless; the Qt dashboard
(``aqt.gmat_dashboard``) is a thin presentation layer over ``compute_readiness``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.cards import CardId
    from anki.collection import Collection

# --------------------------------------------------------------------------
# Give-up rule thresholds (stated verbatim in the UI).
# --------------------------------------------------------------------------

#: Minimum number of graded reviews (revlog entries) before we will show a
#: score. Below this we have not seen the learner answer enough cards to say
#: anything honest about memory.
MIN_GRADED_REVIEWS = 200

#: Minimum fraction of outline topics that must be present in the deck before we
#: will show a score. Below this the deck is too narrow to summarise as one
#: readiness number.
MIN_COVERAGE_FRACTION = 0.50

# --------------------------------------------------------------------------
# Coverage outline -- the 28 GMAT Focus topics, keyed by their
# Section::Topic tag prefix (see content/taxonomy.md, the T1 tag contract).
# Data Insights topics are matched on the 2-segment Section::Topic prefix
# (their Subtopic is implicit), so every outline entry is a 2-segment prefix.
# --------------------------------------------------------------------------

#: The frozen 1:1 map of coverage-outline entries onto their tag prefix.
#: This mirrors content/taxonomy.md; it is embedded so the dashboard works
#: without the content repo present. ``load_outline_from_items_json`` can
#: override it from content/items.json when that file is available.
COVERAGE_OUTLINE: dict[str, tuple[str, ...]] = {
    "Quant": (
        "Quant::Arithmetic::PropertiesOfIntegers",
        "Quant::Arithmetic::FractionsDecimals",
        "Quant::Arithmetic::Percents",
        "Quant::Arithmetic::RatiosProportions",
        "Quant::Arithmetic::PowersRoots",
        "Quant::Arithmetic::Statistics",
        "Quant::Algebra::LinearEquations",
        "Quant::Algebra::Quadratics",
        "Quant::Algebra::Inequalities",
        "Quant::Algebra::FunctionsExponents",
        "Quant::WordProblems::RateWorkMixtureInterest",
    ),
    "Verbal": (
        "Verbal::CriticalReasoning::Assumption",
        "Verbal::CriticalReasoning::Strengthen",
        "Verbal::CriticalReasoning::Weaken",
        "Verbal::CriticalReasoning::Inference",
        "Verbal::CriticalReasoning::Evaluate",
        "Verbal::CriticalReasoning::Paradox",
        "Verbal::CriticalReasoning::Boldface",
        "Verbal::ReadingComprehension::MainIdea",
        "Verbal::ReadingComprehension::Detail",
        "Verbal::ReadingComprehension::Inference",
        "Verbal::ReadingComprehension::Function",
        "Verbal::ReadingComprehension::Tone",
    ),
    "DataInsights": (
        "DataInsights::DataSufficiency",
        "DataInsights::MultiSourceReasoning",
        "DataInsights::TableAnalysis",
        "DataInsights::GraphicsInterpretation",
        "DataInsights::TwoPartAnalysis",
    ),
}

#: Fallback FSRS decay, used only when a card has no explicit per-card ``decay``
#: recorded (legacy / FSRS-5 cards). FSRS-6 cards store their own decay, which we
#: read from ``card.decay`` and prefer; this constant is just the legacy default.
#: Matches fsrs::FSRS5_DEFAULT_DECAY in the Rust crate and Anki's own convention
#: (``card.decay ?? FSRS5_DEFAULT_DECAY`` at every core call site, e.g.
#: rslib/src/stats/card.rs, browser_table.rs, storage/sqlite.rs).
_DEFAULT_DECAY = 0.5


def _all_outline_topics() -> list[str]:
    topics: list[str] = []
    for section_topics in COVERAGE_OUTLINE.values():
        topics.extend(section_topics)
    return topics


#: Human-readable section names for the coverage map.
_SECTION_DISPLAY = {
    "Quant": "Quantitative Reasoning",
    "Verbal": "Verbal Reasoning",
    "DataInsights": "Data Insights",
}


def _prettify_topic(tag: str) -> str:
    """Turn an outline tag into a readable label: drop the section prefix and
    split CamelCase subtopics. 'Quant::Arithmetic::Percents' -> 'Arithmetic ·
    Percents'; 'DataInsights::DataSufficiency' -> 'Data Sufficiency'."""
    parts = tag.split("::")[1:]
    pretty = [re.sub(r"(?<!^)(?=[A-Z])", " ", p) for p in parts]
    return " · ".join(pretty)


# --------------------------------------------------------------------------
# FSRS forgetting curve.
# --------------------------------------------------------------------------


def _recall_probability(stability: float, days_elapsed: float, decay: float) -> float:
    """Current FSRS recall probability (retrievability).

    Reproduces ``fsrs::inference::current_retrievability`` exactly::

        factor = 0.9 ** (1 / -decay) - 1
        R      = (days_elapsed / stability * factor + 1) ** (-decay)

    ``decay`` is the POSITIVE value stored on the card (verified empirically:
    ``card.decay`` comes through positive, e.g. 0.5 for the FSRS-5 fallback or
    ~0.1542 for an FSRS-6 card), and the crate applies ``-decay`` internally, so
    the curve is decreasing. At ``days_elapsed == stability`` this returns the
    desired retention (0.9). This is the same formula and sign convention Anki's
    Rust scheduler uses everywhere; do not introduce a variant here.
    """
    if stability <= 0:
        return 0.0
    days_elapsed = max(0.0, days_elapsed)
    factor = 0.9 ** (1.0 / -decay) - 1.0
    return (days_elapsed / stability * factor + 1.0) ** (-decay)


# --------------------------------------------------------------------------
# Per-card view of the data we need (kept independent of anki.cards.Card so the
# scoring math is unit-testable in isolation).
# --------------------------------------------------------------------------


@dataclass
class _CardDatum:
    stability: float
    decay: float
    days_elapsed: float
    difficulty_tag: str  # "easy" | "medium" | "hard" | "unknown"


#: Per-difficulty-band half-width of the recall uncertainty interval, in
#: probability units. These are intentionally LARGE: the difficulty tags are
#: coarse (easy/medium/hard, NOT IRT-calibrated -- see content/taxonomy.md
#: stability rule #4), so a single card's "true" recall could be well above or
#: below the FSRS point estimate. Harder + un-calibrated => wider.
_DIFFICULTY_UNCERTAINTY = {
    "easy": 0.08,
    "medium": 0.12,
    "hard": 0.18,
    "unknown": 0.15,
}


def _card_recall_band(card: _CardDatum) -> tuple[float, float, float]:
    """Return (low, point, high) recall probability for one card.

    The point estimate is the FSRS curve. The band is the point +/- a coarse,
    difficulty-driven half-width, clamped to [0, 1].
    """
    point = _recall_probability(card.stability, card.days_elapsed, card.decay)
    half = _DIFFICULTY_UNCERTAINTY.get(card.difficulty_tag, 0.15)
    low = max(0.0, point - half)
    high = min(1.0, point + half)
    return low, point, high


# --------------------------------------------------------------------------
# Result object.
# --------------------------------------------------------------------------


@dataclass
class ReadinessResult:
    """The full, honest readiness summary.

    When ``abstained`` is True, ``score`` / ``score_low`` / ``score_high`` are
    None and ``missing`` explains what is needed. The UI MUST NOT show a bare
    number in that case.
    """

    abstained: bool
    # what's missing (only populated when abstained, but always informative)
    missing: list[str] = field(default_factory=list)

    # core numbers (None when abstained)
    score: float | None = None  # point estimate, 0-100
    score_low: float | None = None  # honest band lower bound, 0-100
    score_high: float | None = None  # honest band upper bound, 0-100

    # supporting evidence (always populated)
    graded_reviews: int = 0
    coverage_fraction: float = 0.0
    covered_topics: list[str] = field(default_factory=list)
    missing_topics: list[str] = field(default_factory=list)
    total_topics: int = 0
    scored_cards: int = 0  # exam cards with an FSRS memory state
    total_exam_cards: int = 0

    # human-readable statement of the rule that was applied
    rule_text: str = ""

    def summary_lines(self) -> list[str]:
        """Plain-text rendering, used by the demo script and (escaped) the GUI."""
        lines: list[str] = []
        if self.abstained:
            lines.append("MEMORY READINESS: — (not enough data yet)")
            lines.append("")
            lines.append("What's left before a score shows:")
            for m in self.missing:
                lines.append(f"  - {m}")
        else:
            assert self.score is not None
            lines.append(
                f"MEMORY READINESS: {self.score:.0f} / 100   "
                f"(likely range {self.score_low:.0f}-{self.score_high:.0f})"
            )
            lines.append(
                f"  based on your recall across {self.scored_cards} exam cards"
            )
        lines.append("")
        lines.append(
            f"Topic coverage: {self.coverage_fraction * 100:.0f}%  "
            f"({len(self.covered_topics)} of {self.total_topics} exam topics in your deck)"
        )
        lines.append(f"Reviews done: {self.graded_reviews}")
        if self.missing_topics:
            lines.append(f"Topics not yet in deck ({len(self.missing_topics)}):")
            for t in self.missing_topics:
                lines.append(f"  - {t}")
        lines.append("")
        lines.append(self.rule_text)
        return lines

    def coverage_map(self) -> list[tuple[str, list[tuple[str, bool]]]]:
        """Full §7c coverage map: every exam topic, grouped by section, each
        marked covered/not. Returns
        [(section_name, [(topic_name, covered), ...]), ...] over ALL outline
        topics — independent of whether any are missing, so it's always shown."""
        covered = set(self.covered_topics)
        out: list[tuple[str, list[tuple[str, bool]]]] = []
        for section, topics in COVERAGE_OUTLINE.items():
            name = _SECTION_DISPLAY.get(section, section)
            rows = [(_prettify_topic(t), t in covered) for t in topics]
            out.append((name, rows))
        return out


# --------------------------------------------------------------------------
# Tag parsing.
# --------------------------------------------------------------------------


#: Sections whose outline entries are matched on the 2-segment Section::Topic
#: prefix (their Subtopic is implicit, per content/taxonomy.md). Everything else
#: is matched on the full 3-segment Section::Topic::Subtopic tag.
_TWO_SEGMENT_SECTIONS = {"DataInsights"}


def covered_outline_tag_from_tags(tags: list[str]) -> str | None:
    """Return the coverage-outline entry a note's topic tag belongs to, or None.

    A topic tag is ``Section::Topic`` or ``Section::Topic::Subtopic`` whose first
    segment is a known section code. For DataInsights we match on the 2-segment
    prefix; for Quant/Verbal we match on the full 3-segment tag (so each
    Arithmetic/Algebra/CR/RC subtopic counts as its own outline topic). Auxiliary
    tags (``difficulty::*``, ``split::*``, ``type::*``, ``id::*``, ``kind::*``,
    ``of::*``) are ignored because their first segment is not a section code.
    """
    sections = set(COVERAGE_OUTLINE.keys())
    outline = set(_all_outline_topics())
    for tag in tags:
        segs = tag.split("::")
        if len(segs) < 2 or segs[0] not in sections:
            continue
        if segs[0] in _TWO_SEGMENT_SECTIONS:
            # outline entries here are 2-segment; ignore any extra subtopic.
            candidate = f"{segs[0]}::{segs[1]}"
            if candidate in outline:
                return candidate
        elif len(segs) >= 3:
            candidate = "::".join(segs[:3])
            if candidate in outline:
                return candidate
    return None


def difficulty_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("difficulty::"):
            val = tag.split("::", 1)[1]
            if val in ("easy", "medium", "hard"):
                return val
    return "unknown"


def _is_exam_card(tags: list[str]) -> bool:
    """Memory/recall cards carry ``type::Memory``; exam items omit it.

    The readiness score is about *exam-item* recall, so memory cards are
    excluded from the FSRS aggregate (but their tags still count toward
    coverage -- see compute_readiness)."""
    return "type::Memory" not in tags


# --------------------------------------------------------------------------
# Main entry point.
# --------------------------------------------------------------------------


def compute_readiness(
    col: Collection,
    *,
    deck_name: str | None = None,
    min_graded_reviews: int = MIN_GRADED_REVIEWS,
    min_coverage_fraction: float = MIN_COVERAGE_FRACTION,
    as_of: int | None = None,
) -> ReadinessResult:
    """Compute the honest readiness summary for a deck.

    Args:
        col: an open collection.
        deck_name: restrict to this deck (and its subdecks); None = whole
            collection. The graded-review count and card aggregate are scoped to
            the matching cards.
        min_graded_reviews / min_coverage_fraction: give-up-rule thresholds
            (exposed for testing; default to the module constants).
        as_of: Unix timestamp at which to evaluate recall. Defaults to "now".
            Pass a future timestamp to ask "how ready will I be on exam day?"
            -- memory decays between the last review and ``as_of``.
    """
    # ---- scope: which cards/notes are we summarising? -------------------
    if deck_name:
        # `deck:"X"` in Anki matches X and its subdecks.
        search = f'deck:"{deck_name}"'
        card_ids = col.find_cards(search)
    else:
        card_ids = col.find_cards("")

    # ---- collect per-card data + per-note tags --------------------------
    exam_cards: list[_CardDatum] = []
    covered_prefixes: set[str] = set()
    total_exam_cards = 0
    now = as_of if as_of is not None else int(time.time())

    for cid in card_ids:
        card = col.get_card(cid)
        note = card.note()
        tags = note.tags

        # coverage: a topic present on ANY card (exam or memory) counts.
        outline_match = covered_outline_tag_from_tags(tags)
        if outline_match is not None:
            covered_prefixes.add(outline_match)

        # FSRS aggregate: exam cards with a memory state only.
        if not _is_exam_card(tags):
            continue
        total_exam_cards += 1
        ms = card.memory_state
        if ms is None or ms.stability <= 0:
            continue

        # Prefer the card's own stored (positive) decay; fall back to the FSRS-5
        # default only when absent -- exactly Anki's `card.decay ??
        # FSRS5_DEFAULT_DECAY` convention.
        decay = card.decay if card.decay is not None else _DEFAULT_DECAY
        if card.last_review_time:
            days_elapsed = max(0.0, (now - card.last_review_time) / 86400.0)
        else:
            # Reviewed but no timestamp: treat as just-reviewed (days_elapsed 0)
            # so we never overstate forgetting we can't measure.
            days_elapsed = 0.0
        exam_cards.append(
            _CardDatum(
                stability=ms.stability,
                decay=decay,
                days_elapsed=days_elapsed,
                difficulty_tag=difficulty_from_tags(tags),
            )
        )

    # ---- graded reviews (revlog), scoped to these notes/cards ----------
    graded_reviews = _count_graded_reviews(col, card_ids)

    # ---- coverage -------------------------------------------------------
    all_topics = _all_outline_topics()
    total_topics = len(all_topics)
    covered = sorted(covered_prefixes)
    missing_topics = sorted(t for t in all_topics if t not in covered_prefixes)
    coverage_fraction = len(covered) / total_topics if total_topics else 0.0

    rule_text = (
        f"You'll see a readiness score once you've done {min_graded_reviews} "
        f"reviews and your deck covers at least {min_coverage_fraction * 100:.0f}% "
        f"of the exam topics."
    )

    result = ReadinessResult(
        abstained=False,
        graded_reviews=graded_reviews,
        coverage_fraction=coverage_fraction,
        covered_topics=covered,
        missing_topics=missing_topics,
        total_topics=total_topics,
        scored_cards=len(exam_cards),
        total_exam_cards=total_exam_cards,
        rule_text=rule_text,
    )

    # ---- give-up rule ---------------------------------------------------
    missing: list[str] = []
    if graded_reviews < min_graded_reviews:
        missing.append(
            f"{min_graded_reviews - graded_reviews} more reviews to go "
            f"({graded_reviews} of {min_graded_reviews} done)."
        )
    if coverage_fraction < min_coverage_fraction:
        need = int(round(min_coverage_fraction * total_topics)) - len(covered)
        missing.append(
            f"Add cards for about {max(need, 1)} more exam topic(s) — your deck "
            f"covers {coverage_fraction * 100:.0f}% now, and we need "
            f"{min_coverage_fraction * 100:.0f}%."
        )
    if not exam_cards and not missing:
        # Edge case: enough reviews + coverage recorded but no scorable FSRS
        # states (e.g. FSRS disabled). Be honest rather than divide by zero.
        missing.append(
            "None of your exam cards have FSRS data yet — turn on FSRS in Deck "
            "Options and review some cards so recall can be estimated."
        )

    if missing:
        result.abstained = True
        result.missing = missing
        return result

    # ---- aggregate: mean recall + honest band ---------------------------
    lows: list[float] = []
    points: list[float] = []
    highs: list[float] = []
    for c in exam_cards:
        lo, pt, hi = _card_recall_band(c)
        lows.append(lo)
        points.append(pt)
        highs.append(hi)

    n = len(points)
    mean_point = sum(points) / n
    mean_low = sum(lows) / n
    mean_high = sum(highs) / n

    # Add a small fixed deck-level floor to the band so it never collapses to a
    # falsely-precise interval even if every card happens to agree -- again,
    # because the inputs are coarse. +/- 3 points minimum half-width.
    floor = 0.03
    mean_low = min(mean_low, mean_point - floor)
    mean_high = max(mean_high, mean_point + floor)

    result.score = round(mean_point * 100, 1)
    result.score_low = round(max(0.0, mean_low) * 100, 1)
    result.score_high = round(min(1.0, mean_high) * 100, 1)
    return result


def _count_graded_reviews(col: Collection, card_ids: Sequence[CardId]) -> int:
    """Count revlog entries that represent a *graded* review of the scoped cards.

    Excludes manual/rescheduling entries (revlog.type == 4) which are not a
    learner grading a card. Scoped to the given card ids so a deck filter is
    honoured.
    """
    if not card_ids:
        return 0
    # Chunk the IN-clause to stay well under SQLite's variable limit.
    total = 0
    chunk_size = 900
    ids = [int(c) for c in card_ids]
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        total += col.db.scalar(
            f"select count() from revlog where type != 4 and cid in ({placeholders})",
            *chunk,
        )
    return total


def load_outline_from_items_json(path: str) -> None:
    """Optional: override COVERAGE_OUTLINE from content/items.json.

    The embedded outline already mirrors the taxonomy, so this is only needed if
    the content seed changes. It maps each outline entry's human topic name to
    its tag via the same Section::Topic::Subtopic convention. If parsing fails
    we keep the embedded outline rather than raise.
    """
    import json

    try:
        with open(path, encoding="utf8") as fh:
            data = json.load(fh)
        outline = data.get("coverage_outline")
        if not isinstance(outline, dict):
            return
        # We can only reliably recover the count/sections from items.json; the
        # exact tags come from the items' own tags. Rebuild prefixes from the
        # items list, which carries authoritative Section::Topic::Subtopic tags.
        items = data.get("items", []) + data.get("memory_cards", [])
        by_section: dict[str, set[str]] = {}
        for it in items:
            for tag in it.get("tags", []):
                segs = tag.split("::")
                if len(segs) >= 3 and segs[0] in COVERAGE_OUTLINE:
                    by_section.setdefault(segs[0], set()).add("::".join(segs[:3]))
        # Only override if we found a plausible, non-shrinking outline.
        rebuilt = {sec: tuple(sorted(v)) for sec, v in by_section.items()}
        if rebuilt:
            for sec, tags in rebuilt.items():
                COVERAGE_OUTLINE[sec] = tags
    except (OSError, ValueError):
        return
