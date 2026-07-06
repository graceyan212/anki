# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork (T4): exercise the new GetTopicBreakdown backend RPC from Python.

Proves the per-topic x per-difficulty-band engine change is reachable
end-to-end through the generated protobuf backend and returns real, banded
data (totals + revlog-derived attempted/correct/accuracy + a reviewed_cards
"have you hit this topic" flag).
"""

from tests.shared import getEmptyCol


def _add_note(col, tags: list[str]):
    """Add one Basic note with the given tags and turn its card into a
    review-eligible (non-new) card, so it is picked up by the aggregate query
    (which scopes to non-new, non-suspended cards). Returns the note."""
    note = col.newNote()
    note["Front"] = "front"
    note["Back"] = "back"
    note.tags = list(tags)
    col.addNote(note)

    card = note.cards()[0]
    card.type = 2  # CARD_TYPE_REV
    card.queue = 2  # QUEUE_TYPE_REV
    card.ivl = 5
    card.due = 0
    col.update_card(card)
    return note


def _answer_note(col, note, ratings: list[int]) -> None:
    """Answer the note's card the given sequence of ratings, generating real
    revlog rows. Rating 1 = Again (wrong, ease 1); 3 = Good (correct, ease > 1).
    Forces the card due before each answer so it re-surfaces in the queue."""
    cid = note.cards()[0].id
    col.sched.extend_limits(99999, 99999)
    for rating in ratings:
        col.db.execute(
            "update cards set due = 0, queue = 2, type = 2 where id = ?", cid
        )
        col.sched.reset()
        card = col.sched.getCard()
        assert card is not None and card.id == cid, "expected the target card"
        col.sched.answerCard(card, rating)


def test_get_topic_breakdown():
    col = getEmptyCol()

    # Quant::Arithmetic, one card per band, with controlled revlog answers.
    # Easy (aidiff::10): 2 correct + 1 wrong -> accuracy 2/3.
    easy = _add_note(col, ["Quant::Arithmetic::Percents", "aidiff::10"])
    _answer_note(col, easy, [3, 3, 1])
    # Medium (coarse difficulty::medium -> 50): 1 correct + 1 wrong.
    medium = _add_note(
        col, ["Quant::Arithmetic::FractionsDecimals", "difficulty::medium"]
    )
    _answer_note(col, medium, [3, 1])
    # Hard (aidiff::90): 2 correct.
    hard = _add_note(col, ["Quant::Arithmetic::Roots", "aidiff::90"])
    _answer_note(col, hard, [3, 3])
    # A second hard card, never reviewed -> counts in band total only.
    _add_note(col, ["Quant::Arithmetic::Roots", "aidiff::80"])
    # A card with a topic but NO difficulty tag -> excluded from every band.
    _add_note(col, ["Quant::Arithmetic::Ratios"])

    # The generated backend returns the repeated `topics` field directly.
    topics = col._backend.get_topic_breakdown(topic_depth=2)
    by_topic = {t.topic: t for t in topics}
    assert set(by_topic) == {"Quant::Arithmetic"}

    t = by_topic["Quant::Arithmetic"]
    # Three distinct cards were reviewed (easy, medium, one hard).
    assert t.reviewed_cards == 3

    assert t.easy.total == 1
    assert t.easy.attempted == 1
    assert t.easy.correct == 2
    assert abs(t.easy.accuracy - (2 / 3)) < 1e-9

    assert t.medium.total == 1
    assert t.medium.attempted == 1
    assert t.medium.correct == 1
    assert abs(t.medium.accuracy - 0.5) < 1e-9

    assert t.hard.total == 2  # aidiff 90 (reviewed) + aidiff 80 (not)
    assert t.hard.attempted == 1
    assert t.hard.correct == 2
    assert abs(t.hard.accuracy - 1.0) < 1e-9


def test_get_topic_breakdown_zero_reviews():
    col = getEmptyCol()
    # Two banded cards, neither reviewed.
    _add_note(col, ["Verbal::CriticalReasoning::Assumption", "aidiff::20"])
    _add_note(col, ["Verbal::CriticalReasoning::Weaken", "difficulty::hard"])

    topics = {t.topic: t for t in col._backend.get_topic_breakdown(topic_depth=0)}
    t = topics["Verbal::CriticalReasoning"]

    # Nothing reviewed -> the "have you hit this topic" flag stays 0.
    assert t.reviewed_cards == 0
    # Cards are still counted in their bands...
    assert t.easy.total == 1
    assert t.hard.total == 1
    # ...but nothing is attempted and accuracy is 0.
    for band in (t.easy, t.medium, t.hard):
        assert band.attempted == 0
        assert band.correct == 0
        assert band.accuracy == 0.0
