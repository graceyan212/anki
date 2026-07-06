# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork performance benchmark (rubric 7h / PRD section 10).

Generates a realistic ~50,000-card GMAT collection (28 ``Section::Topic::Subtopic``
topics, coarse ``difficulty::`` bands, a ``split::`` tag, and a body of revlog
history) then times each key engine action over N>=30 iterations, printing the
MEDIAN (p50), 95th PERCENTILE (p95) and WORST CASE per action in milliseconds.

One hand-picked number does not count: every action reports p50 / p95 / max so
the tail (p95, worst) is visible next to the section-10 budgets.

Actions timed (each on the same 50k deck):

  - review queue build .......... the points-at-stake reorder. Forces a fresh
                                  build (set_current invalidates the cached
                                  queue) then fetches, so build_queues +
                                  points_at_stake_weights + sort_review run.
  - get_gmat_scores ............. the three-score summary RPC (T3).
  - get_topic_mastery_stats ..... per-topic mastery aggregate RPC (T2).
  - get_topic_breakdown ......... per-topic x difficulty-band RPC (T4),
                                  topic_depth=3 (what the dashboard uses).
  - next-card fetch + answer .... get the next queued card and answer it Good.

Run it via the ``just bench`` recipe (which builds pylib and invokes this with
the project's Python). The heavy 50k collection is generated once into a temp
directory and reused on subsequent runs (delete it or pass --regen to rebuild).
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Match tools/run.py so pylib + the built Rust backend (out/pylib) are importable
# whether invoked from the repo root (the just recipe's cwd) or elsewhere.
_REPO = Path(__file__).resolve().parent.parent
for _p in ("pylib", "qt", "out/pylib", "out/qt"):
    sys.path.insert(0, str(_REPO / _p))

from anki.collection import Collection  # noqa: E402
from anki.decks import DeckId  # noqa: E402

# ---------------------------------------------------------------------------
# The 28-topic taxonomy (content/taxonomy.md, the T1 contract). Kept inline so
# the benchmark is self-contained and does not depend on the sibling content
# repo being checked out next to the anki fork.
# ---------------------------------------------------------------------------
TOPICS = [
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
    "DataInsights::DataSufficiency",
    "DataInsights::MultiSourceReasoning",
    "DataInsights::TableAnalysis",
    "DataInsights::GraphicsInterpretation",
    "DataInsights::TwoPartAnalysis",
]
assert len(TOPICS) == 28, "taxonomy must have exactly 28 topics"

DIFFICULTIES = ["easy", "medium", "hard"]
GMAT_DECK_NAME = "GMAT Focus"

TARGET_CARDS = 50_000
# Fraction of cards that get revlog history. Enough to clear every give-up
# threshold in gmat_scores.rs (>=200 reviews, >=50% coverage) so the score
# queries exercise the full computation, not an early abstain.
REVIEWED_FRACTION = 0.60
# How many times each reviewed card is answered (spread of history depth).
MAX_REVIEWS_PER_CARD = 4

ITERATIONS = 30
# A GMAT-plausible seed so the generated deck (and thus the numbers) is stable.
SEED = 20260705


def _cache_dir(regen: bool) -> Path:
    """Stable temp location for the generated collection, reused across runs."""
    base = Path(tempfile.gettempdir()) / "anki-gmat-bench"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _collection_path(regen: bool) -> tuple[Path, bool]:
    """Return (path, already_generated). already_generated is True when a cached
    50k collection is present and --regen was not passed."""
    base = _cache_dir(regen)
    path = base / f"gmat_{TARGET_CARDS}.anki2"
    marker = base / f"gmat_{TARGET_CARDS}.done"
    if regen:
        for p in (path, marker):
            if p.exists():
                p.unlink()
    return path, (path.exists() and marker.exists())


def generate_collection(path: Path) -> None:
    """Create a ~50k-card GMAT collection at ``path`` with realistic tags and
    revlog history. Idempotent from the caller's side (caller deletes first)."""
    rng = random.Random(SEED)
    if path.exists():
        path.unlink()

    print(f"generating ~{TARGET_CARDS:,}-card GMAT collection at {path} ...")
    t0 = time.perf_counter()
    col = Collection(str(path))
    try:
        basic = col.models.by_name("Basic")
        assert basic is not None, "Basic notetype missing"
        did = DeckId(col.decks.add_normal_deck_with_name(GMAT_DECK_NAME).id)

        # --- add ~50k notes across the 28 topics -----------------------------
        for i in range(TARGET_CARDS):
            topic = TOPICS[i % len(TOPICS)]
            difficulty = DIFFICULTIES[i % len(DIFFICULTIES)]
            split = "train" if i % 10 else "holdout"
            note = col.new_note(basic)
            note["Front"] = f"[{topic}] GMAT practice item #{i}"
            note["Back"] = f"Worked solution for item #{i}"
            note.tags = [topic, f"difficulty::{difficulty}", f"split::{split}"]
            col.add_note(note, did)
            if (i + 1) % 10_000 == 0:
                print(f"  added {i + 1:,} notes ...")

        # --- realistic revlog history ----------------------------------------
        # Answer a large subset of cards a few times each so the mastery/score
        # queries have real revlog rows to aggregate (mix of right/wrong so the
        # Rasch performance estimate and per-band accuracy are meaningful).
        # Enable FSRS so each answer computes an FSRS memory_state; without it
        # the memory score has no retrievability to read and always abstains.
        col.set_config("fsrs", True)
        col.decks.set_current(did)
        card_ids = col.find_cards(f'deck:"{GMAT_DECK_NAME}"')
        reviewed = card_ids[: int(len(card_ids) * REVIEWED_FRACTION)]
        print(f"  building revlog history on {len(reviewed):,} cards ...")
        total_reviews = 0
        for idx, cid in enumerate(reviewed):
            reps = rng.randint(1, MAX_REVIEWS_PER_CARD)
            for _ in range(reps):
                card = col.get_card(cid)
                # answerCard reads card.time_taken(), which needs a started
                # timer (getCard() normally sets it; we load cards directly).
                card.start_timer()
                # ~78% Good, ~10% Easy, ~12% Again -> mix of pass/fail so the
                # revlog-backed accuracy/ability numbers are not degenerate.
                roll = rng.random()
                ease = 3 if roll < 0.78 else (4 if roll < 0.88 else 1)
                col.sched.answerCard(card, ease)  # type: ignore[arg-type]
                total_reviews += 1
            if (idx + 1) % 10_000 == 0:
                print(f"  reviewed {idx + 1:,} cards ({total_reviews:,} reviews) ...")

        col.save()
        print(
            f"  done: {len(card_ids):,} cards, {total_reviews:,} revlog entries "
            f"in {time.perf_counter() - t0:.1f}s"
        )
    finally:
        col.close()


def _percentiles(samples_ms: list[float]) -> tuple[float, float, float]:
    """Return (p50, p95, max) in ms. p95 uses nearest-rank on sorted samples."""
    s = sorted(samples_ms)
    p50 = statistics.median(s)
    # nearest-rank 95th percentile
    rank = max(1, int(round(0.95 * len(s))))
    p95 = s[min(rank, len(s)) - 1]
    return p50, p95, s[-1]


def _time_action(name: str, fn, iterations: int) -> tuple[str, float, float, float]:
    """Time ``fn`` over ``iterations``. If ``fn`` sets a ``_last`` attribute
    (ms), that self-reported figure is used instead of wall time -- for actions
    that must run untimed setup (e.g. cache invalidation) before the measured
    work."""
    samples = []
    self_timed = hasattr(fn, "_last")
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - t0) * 1000.0
        samples.append(fn._last if self_timed else elapsed)
    p50, p95, worst = _percentiles(samples)
    return name, p50, p95, worst


def run_benchmark(col: Collection, iterations: int) -> list[tuple[str, float, float, float]]:
    did = DeckId(col.decks.id_for_name(GMAT_DECK_NAME))
    other_did = DeckId(1)  # the built-in Default deck, used only to invalidate

    def build_review_queue() -> None:
        # A fresh build must be forced each iteration or we'd re-time the cached
        # queue (~0.01ms). set_current is Op::SetCurrentDeck, which invalidates
        # the cached study queue (requires_study_queue_rebuild). We first switch
        # to the empty Default deck (untimed) to drop the cache, then TIME the
        # switch back to the GMAT deck plus the fetch -- so every measured build
        # is the full 50k GMAT build: build_queues -> gather_cards ->
        # points_at_stake_weights -> sort_review.
        col.decks.set_current(other_did)
        # build the (untimed) Default queue to drop the cache before we time
        col.sched.get_queued_cards(fetch_limit=1)  # type: ignore[union-attr]
        _t0 = time.perf_counter()
        col.decks.set_current(did)
        col.sched.get_queued_cards(fetch_limit=1)  # type: ignore[union-attr]
        build_review_queue._last = (time.perf_counter() - _t0) * 1000.0  # type: ignore[attr-defined]

    # Flag the action as self-timed (see _time_action) before it first runs.
    build_review_queue._last = 0.0  # type: ignore[attr-defined]

    def gmat_scores() -> None:
        col._backend.get_gmat_scores(deck_name=GMAT_DECK_NAME)

    def mastery_stats() -> None:
        col._backend.get_topic_mastery_stats(
            topic_depth=2, mastered_interval_days=21, mastered_retrievability=0.9
        )

    def topic_breakdown() -> None:
        col._backend.get_topic_breakdown(topic_depth=3)

    def next_card_answer() -> None:
        card = col.sched.getCard()
        if card is not None:
            col.sched.answerCard(card, 3)

    actions = [
        ("review queue build (points-at-stake reorder)", build_review_queue),
        ("get_gmat_scores", gmat_scores),
        ("get_topic_mastery_stats", mastery_stats),
        ("get_topic_breakdown (topic_depth=3)", topic_breakdown),
        ("next-card fetch + answer", next_card_answer),
    ]

    # Warm up each action once (JIT of SQL query plans, first queue build) so the
    # first measured iteration is not an outlier that dominates the worst case.
    for _name, fn in actions:
        fn()

    results = []
    for name, fn in actions:
        results.append(_time_action(name, fn, iterations))
    return results


def _print_table(results, iterations: int, abstained: dict) -> None:
    name_w = max(len(r[0]) for r in results) + 2
    header = f"{'action'.ljust(name_w)}{'p50 (ms)':>12}{'p95 (ms)':>12}{'worst (ms)':>12}"
    print()
    print(f"GMAT engine benchmark  ({TARGET_CARDS:,} cards, N={iterations} per action)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for name, p50, p95, worst in results:
        print(f"{name.ljust(name_w)}{p50:>12.2f}{p95:>12.2f}{worst:>12.2f}")
    print("=" * len(header))
    print()
    print("Score give-up state on the generated deck (non-abstaining = full path timed):")
    for k, v in abstained.items():
        print(f"  {k}: {'ABSTAINED (needs more data)' if v else 'computed a number'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="GMAT engine benchmark (rubric 7h)")
    parser.add_argument(
        "--regen", action="store_true", help="regenerate the 50k collection from scratch"
    )
    parser.add_argument(
        "--iterations", type=int, default=ITERATIONS, help="samples per action (>=30)"
    )
    args = parser.parse_args()
    iterations = max(30, args.iterations)

    path, already = _collection_path(args.regen)
    if already:
        print(f"reusing cached 50k collection at {path} (pass --regen to rebuild)")
    else:
        generate_collection(path)
        # touch the done-marker so subsequent runs reuse it
        path.with_suffix(".done").write_text("ok\n")

    col = Collection(str(path))
    try:
        card_count = len(col.find_cards(f'deck:"{GMAT_DECK_NAME}"'))
        print(f"loaded collection: {card_count:,} cards in the GMAT deck")

        # Record whether each score abstains so the doc can state the full
        # computation (not an early give-up return) is what was timed.
        scores = col._backend.get_gmat_scores(deck_name=GMAT_DECK_NAME)
        abstained = {
            "memory": scores.memory.abstained,
            "performance": scores.performance.abstained,
            "readiness": scores.readiness.abstained,
        }

        results = run_benchmark(col, iterations)
        _print_table(results, iterations, abstained)
    finally:
        col.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
