# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork (T2): exercise the new GetTopicMasteryStats backend RPC from Python.

This proves the Rust engine change is reachable end-to-end through the
generated protobuf backend interface and returns real, aggregated data.
"""

from tests.shared import getEmptyCol


def _add_review_card(col, tags: list[str], interval: int) -> None:
    """Add one Basic note with the given tags and turn its card into a review
    card with the given interval (in days)."""
    note = col.newNote()
    note["Front"] = "front"
    note["Back"] = "back"
    note.tags = list(tags)
    col.addNote(note)

    card = note.cards()[0]
    card.type = 2  # CARD_TYPE_REV
    card.queue = 2  # QUEUE_TYPE_REV
    card.ivl = interval
    card.due = 0
    col.update_card(card)


def test_get_topic_mastery_stats():
    col = getEmptyCol()

    # Quant::Arithmetic: one mature/mastered (ivl 40), one young (ivl 5).
    _add_review_card(col, ["Quant::Arithmetic::Percents", "split::train"], 40)
    _add_review_card(col, ["Quant::Arithmetic::FractionsDecimals"], 5)
    # Verbal::CriticalReasoning: a single young card.
    _add_review_card(col, ["Verbal::CriticalReasoning::Assumption"], 3)
    # Untagged card must be ignored by the topic grouping.
    _add_review_card(col, [], 99)

    # The generated backend method returns the repeated `topics` field directly,
    # i.e. a Sequence[TopicMasteryStat].
    topics = col._backend.get_topic_mastery_stats(
        topic_depth=2,
        mastered_interval_days=21,
        mastered_retrievability=0.9,
    )

    by_topic = {t.topic: t for t in topics}
    # Two topic groups; the untagged card is excluded.
    assert set(by_topic) == {"Quant::Arithmetic", "Verbal::CriticalReasoning"}

    quant = by_topic["Quant::Arithmetic"]
    assert quant.total_cards == 2
    assert quant.mastered_cards == 1  # only the ivl=40 card
    assert abs(quant.mastery - 0.5) < 1e-6

    verbal = by_topic["Verbal::CriticalReasoning"]
    assert verbal.total_cards == 1
    assert verbal.mastered_cards == 0

    # Defaults kick in when zero is passed (depth -> 2, interval -> 21, retr -> 0.9).
    default_topics = col._backend.get_topic_mastery_stats(
        topic_depth=0,
        mastered_interval_days=0,
        mastered_retrievability=0.0,
    )
    assert {t.topic for t in default_topics} == {
        "Quant::Arithmetic",
        "Verbal::CriticalReasoning",
    }
