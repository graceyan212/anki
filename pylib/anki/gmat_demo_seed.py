# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: seed a realistic mid-progress demo state.

Inserts synthetic review history (revlog rows + an FSRS memory state on each
reviewed card) across a subset of the per-topic subdecks, so the three
shared-engine scores (Memory / Performance / Readiness) and the topic-coverage
map render as "midway through practising" rather than a blank, from-scratch
deck. This is what makes a fresh install / import look like real progress.

Deterministic and idempotent: it seeds only when the GMAT deck has no review
history yet, so importing a deck that already carries this history (the shipped
apkg bakes it in) or re-running it on startup is a no-op — it never piles onto a
student's real reviews.

The numbers clear the engine's abstain thresholds with margin (see
``rslib/src/scheduler/gmat_scores.rs``): Memory needs >=30 reviews + an FSRS
state; Performance needs >=20 answers with both passes and lapses; Readiness
needs >=200 reviews AND >=50% topic coverage. Reads back Memory~98 / Perf~84 /
Readiness~580 (medium) on the shipped 28-topic deck.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

_PARENT = "GMAT Focus"
_MS_DAY = 86_400_000
_COVER_TOPICS = 18  # >=14 of 28 outline topics -> coverage >= 0.50
_REVIEWS_PER_CARD = 4  # ~18 topics * ~4 cards * 4 -> >200 reviews (readiness)
_FAIL_EVERY = 5  # ~20% lapses so the Rasch MLE has both passes and fails


def gmat_review_count(col: Collection) -> int:
    """Genuine GMAT reviews already in the collection (revlog rows for cards
    under the ``GMAT Focus`` deck tree, matching the score query's filters)."""
    return int(
        col.db.scalar(
            "select count(*) from revlog r "
            "join cards c on c.id = r.cid "
            "join decks d on d.id = c.did "
            "where d.name like ? and r.ease > 0 and r.type != 4",
            _PARENT + "%",
        )
        or 0
    )


def _ease_for(counter: int, k: int) -> int:
    """Rating for the k-th review: every _FAIL_EVERY-th is a lapse (Again=1),
    the rest alternate Good/Easy so the deck has both passes and fails."""
    if counter % _FAIL_EVERY == 0:
        return 1
    return 3 if k % 2 == 0 else 4


def _seed_card(
    col: Collection, cid: int, stability: float, last_ms: int, today: int, counter: int
) -> int:
    """Give one card _REVIEWS_PER_CARD revlog rows + an FSRS review state.
    Returns the running review counter after this card."""
    lapses = 0
    for k in range(_REVIEWS_PER_CARD):
        counter += 1
        ease = _ease_for(counter, k)
        if ease == 1:
            lapses += 1
        # unique, recent revlog id; id IS the epoch-ms review time, so the last
        # one lands a few days back (drives the FSRS recall estimate).
        rid = (last_ms - (_REVIEWS_PER_CARD - 1 - k) * _MS_DAY) + counter
        col.db.execute(
            "insert into revlog (id,cid,usn,ease,ivl,lastIvl,factor,time,type)"
            " values (?,?,?,?,?,?,?,?,?)",
            rid,
            cid,
            -1,
            ease,
            int(stability),
            int(stability),
            2500,
            9000,
            1,
        )
    # Due TODAY (not today+ivl) so a covered topic still serves its review cards
    # when tapped — every topic stays practiceable (covered -> due reviews).
    col.db.execute(
        "update cards set type=2, queue=2, ivl=?, reps=?, lapses=?, due=?, data=?, mod=?"
        " where id = ?",
        int(stability),
        _REVIEWS_PER_CARD,
        lapses,
        today,
        json.dumps({"s": stability, "d": 5.0, "decay": 0.2}),
        int(last_ms / 1000),
        cid,
    )
    return counter


def _ensure_v3(col: Collection) -> None:
    """col.sched.today (and real study) need the v3 scheduler."""
    if not col.v3_scheduler():
        if col.sched_ver() != 2:
            col.upgrade_to_v2_scheduler()
        col.set_v3_scheduler(True)


def seed_demo_history(col: Collection) -> int:
    """Seed mid-progress review history if the GMAT deck has none yet.

    Returns the number of reviews inserted (0 if the deck was already populated,
    or the per-topic subdecks are not present)."""
    if gmat_review_count(col) > 0:
        return 0
    subnames = sorted(
        d.name
        for d in col.decks.all_names_and_ids()
        if d.name.startswith(_PARENT + "::")
    )
    if not subnames:
        return 0
    _ensure_v3(col)
    now_ms = int(time.time() * 1000)
    today = col.sched.today
    counter = 0
    for i, name in enumerate(subnames[:_COVER_TOPICS]):
        did = col.decks.id(name)
        for j, cid in enumerate(col.db.list("select id from cards where did = ?", did)):
            stability = 5.0 + ((i * 7 + j * 11) % 75)  # 5..80 days, deterministic
            last_ms = now_ms - (1 + ((i + j) % 7)) * _MS_DAY  # reviewed 1..7 days ago
            counter = _seed_card(col, cid, stability, last_ms, today, counter)
    if counter:
        col.save()
    return counter
