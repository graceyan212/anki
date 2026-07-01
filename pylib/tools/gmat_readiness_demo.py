#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Runnable demo for GMAT readiness (track T3) against the real content deck.

Loads content/gmat_focus.apkg into a throwaway collection and exercises BOTH
states the dashboard can show, printing what the panel would display:

  (a) fresh import (0 reviews)          -> ABSTAIN + "what's missing"
  (b) enough graded reviews + coverage  -> SCORE + likely range + coverage %
  (c) a narrow subdeck                  -> ABSTAIN on the coverage branch

The GUI (aqt.gmat_dashboard) is a thin wrapper over the same
anki.gmat_readiness.compute_readiness call used here, so this reproduces the
dashboard's content headless. A self-contained pytest lives at
pylib/tests/test_gmat_readiness.py.

Run with the project's built python and PYTHONPATH on the build outputs:

    cd <repo root>
    PYTHONPATH=out/pylib:out/qt out/pyenv/bin/python pylib/tools/gmat_readiness_demo.py
"""

from __future__ import annotations

import os
import tempfile
import time

from anki import import_export_pb2 as iepb
from anki.collection import Collection, ImportAnkiPackageRequest
from anki.gmat_readiness import compute_readiness

APKG = os.path.expanduser("~/Desktop/alpha/speedrun/content/gmat_focus.apkg")
DECK = "GMAT Focus"


def fresh_collection() -> Collection:
    tmp = tempfile.mkdtemp()
    col = Collection(os.path.join(tmp, "col.anki2"))
    col.import_anki_package(
        ImportAnkiPackageRequest(
            package_path=APKG,
            options=iepb.ImportAnkiPackageOptions(
                with_scheduling=True, with_deck_configs=True
            ),
        )
    )
    return col


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def simulate_reviews(col: Collection, *, passes: int = 3) -> int:
    """Answer cards 'Good' until the queue drains, repeated `passes` times. Each
    answer writes a revlog row and (with FSRS on) an FSRS memory state, exactly
    like a study session. We raise the deck's per-day limits AND extend today's
    allowance so every card in the GMAT subdecks surfaces."""
    col.set_config("fsrs", True)
    gmat = col.decks.id_for_name(DECK)
    assert gmat is not None
    col.decks.select(gmat)
    conf = col.decks.config_dict_for_deck_id(gmat)
    conf["new"]["perDay"] = 9999
    conf["rev"]["perDay"] = 9999
    col.decks.update_config(conf)
    col.sched.extend_limits(9999, 9999)
    answered = 0
    for _ in range(passes):
        while True:
            card = col.sched.getCard()
            if card is None:
                break
            col.sched.answerCard(card, 3)  # Good
            answered += 1
    return answered


def main() -> None:
    # ---- STATE (a): fresh import, 0 graded reviews -> ABSTAIN -----------
    banner("STATE (a)  FRESH IMPORT - expect ABSTAIN")
    col = fresh_collection()
    res_a = compute_readiness(col, deck_name=DECK)
    print("\n".join(res_a.summary_lines()))
    assert res_a.abstained, "fresh import should abstain"
    assert res_a.score is None
    assert res_a.graded_reviews == 0
    assert any("reviews to go" in m for m in res_a.missing)
    print("\n[OK] abstained on fresh import; 0 reviews; lists missing reviews.")

    # ---- STATE (b): enough reviews + coverage -> SCORE + range ----------
    banner("STATE (b)  AFTER SIMULATED STUDY - expect SCORE + RANGE")
    answered = simulate_reviews(col)
    # Evaluate a week out so the point estimate reflects real forgetting.
    exam_day = int(time.time()) + 7 * 86400
    res_b = compute_readiness(col, deck_name=DECK, as_of=exam_day)
    revlog = col.db.scalar("select count() from revlog")
    print(
        f"(simulated {answered} answers; revlog has {revlog} rows; "
        f"readiness evaluated 7 days post-study)\n"
    )
    print("\n".join(res_b.summary_lines()))
    assert not res_b.abstained, res_b.missing
    assert res_b.score is not None
    assert res_b.score_low is not None and res_b.score_high is not None
    assert res_b.score_low <= res_b.score <= res_b.score_high
    assert res_b.score_high - res_b.score_low >= 5.0, "range should be wide"
    assert res_b.graded_reviews >= 200
    assert res_b.coverage_fraction >= 0.50
    print(
        f"\n[OK] showed {res_b.score:.0f}/100 "
        f"(range {res_b.score_low:.0f}-{res_b.score_high:.0f}, "
        f"width {res_b.score_high - res_b.score_low:.0f} pts); "
        f"reviews={res_b.graded_reviews}; coverage={res_b.coverage_fraction * 100:.0f}%."
    )

    # ---- STATE (c): narrow subdeck -> ABSTAIN on coverage --------------
    banner("STATE (c)  NARROW SUBDECK - expect ABSTAIN on coverage")
    narrow = "GMAT Focus::Memory"  # only ~36% of the outline topics
    res_c = compute_readiness(col, deck_name=narrow, as_of=exam_day)
    print(
        f"(scoped to {narrow!r}; covers "
        f"{res_c.coverage_fraction * 100:.0f}% of topics)\n"
    )
    print("\n".join(res_c.summary_lines()))
    assert res_c.abstained
    assert res_c.coverage_fraction < 0.50
    assert any("exam topic" in m for m in res_c.missing)
    print("\n[OK] coverage branch of the give-up rule abstains as expected.")

    col.close()
    banner("ALL ASSERTIONS PASSED - both abstain and show-score paths exercised.")


if __name__ == "__main__":
    main()
