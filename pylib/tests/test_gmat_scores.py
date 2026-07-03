# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork (T3): exercise the new GetGmatScores backend RPC from Python.

Mirrors test_topic_mastery.py — proves the three-score RPC is reachable
end-to-end through the generated protobuf backend and returns a
scored-or-correctly-abstaining ScoreValue for each of memory / performance /
readiness (the per-score give-up rule).
"""

from tests.shared import getEmptyCol


def test_get_gmat_scores_returns_three_scores():
    col = getEmptyCol()
    res = col._backend.get_gmat_scores(deck_name="")

    # All three distinct scores are always present in the response.
    for sv in (res.memory, res.performance, res.readiness):
        if sv.abstained:
            # Give-up rule: no number, but it must say what data is still needed.
            assert len(sv.missing) >= 1
        else:
            # A scored value must bracket its point estimate with a range.
            assert sv.low <= sv.score <= sv.high
