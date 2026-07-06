# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: keep the collection organised into the per-topic subdeck layout.

The deck ships as 28 per-topic subdecks under "GMAT Focus" (studying the parent
is the full exam). But cards can arrive in the OLD flat layout several ways:
a collection created before the split, a manual ``File > Import`` of the apkg,
or a sync — and Anki's importer keeps existing cards in their current deck
rather than moving them into the new subdecks. When that happens the topic
decks look empty and practising a topic hits "session complete".

So on every profile open we reconcile: find the cards carrying a GMAT topic tag
that are NOT already in their correct subdeck (the same tag -> subdeck mapping
the deck ships with, ``anki.gmat_readiness``) and move them there via
``col.set_deck``, resetting them to new so every topic serves problems now (a
fresh drill; matches the phone). This is self-healing — a late import or sync
that drops cards in flat is tidied on the next launch — and a no-op once the
collection is already organised, so it never disturbs an ordinary launch.

Only cards that carry a GMAT section tag are ever inspected, so this stays cheap
even on a large unrelated collection. Defensive: any error is swallowed so a
migration hiccup can never block startup.

NOTE: imported during ``aqt`` init (via ``aqt.main``); reference ``aqt.mw``
lazily, never ``from aqt import mw`` at module load (circular import).
"""

from __future__ import annotations

import aqt
from aqt import gui_hooks

_PARENT = "GMAT Focus"
_LAYOUT_VERSION = 2  # 2 = 28 per-topic subdecks + exam parent
_CONFIG_KEY = "gmatDeckLayoutVersion"  # record of the last applied layout


def _migrate() -> None:
    col = aqt.mw.col if aqt.mw else None
    if col is None:
        return
    try:
        from anki.cards import CardId
        from anki.decks import DeckId
        from anki.gmat_readiness import (
            COVERAGE_OUTLINE,
            _all_outline_topics,
            _prettify_topic,
            covered_outline_tag_from_tags,
        )

        dest_name = {
            tag: f"{_PARENT}::{_prettify_topic(tag)}" for tag in _all_outline_topics()
        }

        # Only inspect cards that actually carry a GMAT section tag — cheap even on
        # a large unrelated collection.
        query = " or ".join(f'"tag:{section}::*"' for section in COVERAGE_OUTLINE)
        candidates = col.find_cards(query) if query else []

        # Collect the MISPLACED ones (not already in their correct subdeck), so a
        # re-import/sync that drops cards flat is healed on the next launch and an
        # already-tidy collection is a no-op.
        dest_did: dict[str, DeckId] = {}
        by_dest: dict[DeckId, list[CardId]] = {}
        for cid in candidates:
            card = col.get_card(cid)
            outline = covered_outline_tag_from_tags(card.note().tags)
            name = dest_name.get(outline) if outline else None
            if name is None:
                continue
            did = dest_did.get(name)
            if did is None:
                did = col.decks.id(name)  # get-or-create the subdeck
                dest_did[name] = did
            if card.did != did:
                by_dest.setdefault(did, []).append(cid)

        if not by_dest:
            return  # already organised — nothing to do this launch

        # col.set_deck / schedule_cards_as_new require the v3 scheduler.
        if not col.v3_scheduler():
            if col.sched_ver() != 2:
                col.upgrade_to_v2_scheduler()
            col.set_v3_scheduler(True)

        moved: list[CardId] = []
        for did, cids in by_dest.items():
            col.set_deck(cids, did)
            moved.extend(cids)
        # Fresh drill: reset the moved cards to new so every topic serves problems
        # now (history rebuilds as the student practises; matches the phone).
        col.sched.schedule_cards_as_new(moved)

        col.set_config(_CONFIG_KEY, _LAYOUT_VERSION)
        col.save()
        aqt.mw.reset()  # refresh the deck list / study screen with the new layout
    except Exception as exc:  # pragma: no cover - never block startup
        print(f"gmat_deck_migrate: skipped ({exc!r})")


def init() -> None:
    # Runs when a profile/collection opens. If one is already open at register
    # time (edge case), run once immediately too.
    gui_hooks.profile_did_open.append(_migrate)
    if aqt.mw and aqt.mw.col is not None:
        _migrate()
