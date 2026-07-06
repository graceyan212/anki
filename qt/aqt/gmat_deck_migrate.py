# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT fork: migrate an existing collection to the per-topic subdeck layout.

The deck ships as 28 per-topic subdecks under "GMAT Focus" (studying the parent
is the full exam). A collection imported before that split has all cards under
the old layout, and re-importing the apkg does NOT move existing cards into the
new subdecks (Anki keeps existing cards in their current deck). So on startup we
reorganise the cards ourselves — the same tag -> subdeck mapping the deck ships
with (``anki.gmat_readiness``), applied in place via ``col.set_deck`` — so the
28 topic decks appear and each is practiceable. Moved cards are reset to new so
every topic serves problems immediately (a fresh drill; matches the phone).

Idempotent + gated by a collection-config version, so it runs exactly once per
layout bump, never on an ordinary launch. Defensive: any error is swallowed so a
migration hiccup can never block startup.

NOTE: imported during ``aqt`` init (via ``aqt.main``); reference ``aqt.mw``
lazily, never ``from aqt import mw`` at module load (circular import).
"""

from __future__ import annotations

import aqt
from aqt import gui_hooks

_PARENT = "GMAT Focus"
_LAYOUT_VERSION = 2  # 2 = 28 per-topic subdecks + exam parent
_CONFIG_KEY = "gmatDeckLayoutVersion"


def _migrate() -> None:
    col = aqt.mw.col if aqt.mw else None
    if col is None:
        return
    try:
        if int(col.get_config(_CONFIG_KEY, 0) or 0) >= _LAYOUT_VERSION:
            return

        from anki.cards import CardId
        from anki.gmat_readiness import (
            _all_outline_topics,
            _prettify_topic,
            covered_outline_tag_from_tags,
        )

        dest_map = {
            tag: f"{_PARENT}::{_prettify_topic(tag)}" for tag in _all_outline_topics()
        }

        # col.set_deck / schedule_cards_as_new require the v3 scheduler.
        if not col.v3_scheduler():
            if col.sched_ver() != 2:
                col.upgrade_to_v2_scheduler()
            col.set_v3_scheduler(True)

        by_dest: dict[str, list[CardId]] = {}
        for row in col.db.execute("select id from cards"):
            cid = CardId(row[0])
            card = col.get_card(cid)
            outline = covered_outline_tag_from_tags(card.note().tags)
            dest = dest_map.get(outline) if outline else None
            if dest is not None:
                by_dest.setdefault(dest, []).append(cid)

        if by_dest:
            moved: list[CardId] = []
            for deck_name, cids in by_dest.items():
                did = col.decks.id(deck_name)  # get-or-create the subdeck
                if did is not None:
                    col.set_deck(cids, did)
                    moved.extend(cids)
            if moved:
                # Fresh drill: reset the moved cards to new so every topic serves
                # problems now (history rebuilds as the student practises).
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
