// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::cmp::Ordering;
use std::collections::HashMap;
use std::hash::Hasher;

use fnv::FnvHasher;

use super::DueCard;
use super::NewCard;
use super::NewCardSortOrder;
use super::QueueBuilder;
use crate::notes::NoteId;

impl QueueBuilder {
    /// GMAT fork (T2): "points-at-stake" review ordering.
    ///
    /// Review cards are normally left in the order the DB returned them (the
    /// configured `review_order`). Here we stably reorder the already-gathered
    /// `self.review` list in memory by a precomputed per-note weight
    /// (topic_weight × student-weakness; see
    /// `scheduler::topic_mastery::points_at_stake_weights`): the highest-weighted
    /// cards float to the front — `topic_weight` is uniform (1.0) by default, so
    /// in practice this orders the student's weakest topics first. This is purely
    /// in-memory — no `card.due` is
    /// mutated, so there is nothing for the undo system to track.
    ///
    /// `weights` is empty when the feature has no data (e.g. untagged
    /// collections), in which case the original order is preserved.
    pub(super) fn sort_review(&mut self, weights: &HashMap<NoteId, f32>) {
        reorder_reviews_by_weight(&mut self.review, weights);
    }

    pub(super) fn sort_new(&mut self) {
        match self.context.sort_options.new_order {
            // preserve gather order
            NewCardSortOrder::NoSort => (),
            NewCardSortOrder::Template => {
                // stable sort to preserve gather order
                self.new
                    .sort_by(|a, b| a.template_index.cmp(&b.template_index))
            }
            NewCardSortOrder::TemplateThenRandom => {
                self.hash_new_cards_by_id();
                self.new.sort_unstable_by(cmp_template_then_hash);
            }
            NewCardSortOrder::RandomNoteThenTemplate => {
                self.hash_new_cards_by_note_id();
                self.new.sort_unstable_by(cmp_hash_then_template);
            }
            NewCardSortOrder::RandomCard => {
                self.hash_new_cards_by_id();
                self.new.sort_unstable_by(cmp_hash)
            }
        }
    }

    fn hash_new_cards_by_id(&mut self) {
        self.new
            .iter_mut()
            .for_each(|card| card.hash_id_with_salt(self.context.timing.days_elapsed as i64));
    }

    fn hash_new_cards_by_note_id(&mut self) {
        self.new
            .iter_mut()
            .for_each(|card| card.hash_note_id_with_salt(self.context.timing.days_elapsed as i64));
    }
}

/// GMAT fork (T2): stable, in-memory reorder of review cards by descending
/// per-note points-at-stake weight. Factored out of `sort_review` so it can be
/// unit-tested without constructing a full `QueueBuilder`/`Context`.
fn reorder_reviews_by_weight(review: &mut [DueCard], weights: &HashMap<NoteId, f32>) {
    if weights.is_empty() {
        // No weighting data (e.g. an untagged collection): keep DB order.
        return;
    }
    // Stable sort so cards of equal weight keep their incoming DB order
    // (e.g. due-date / relative overdueness from the configured review_order).
    review.sort_by(|a, b| {
        let wa = weights.get(&a.note_id).copied().unwrap_or(0.0);
        let wb = weights.get(&b.note_id).copied().unwrap_or(0.0);
        // Descending by weight; NaN-safe (NaN treated as equal/lowest).
        wb.partial_cmp(&wa).unwrap_or(Ordering::Equal)
    });
}

fn cmp_hash(a: &NewCard, b: &NewCard) -> Ordering {
    a.hash.cmp(&b.hash)
}

fn cmp_template_then_hash(a: &NewCard, b: &NewCard) -> Ordering {
    (a.template_index, a.hash).cmp(&(b.template_index, b.hash))
}

fn cmp_hash_then_template(a: &NewCard, b: &NewCard) -> Ordering {
    (a.hash, a.template_index).cmp(&(b.hash, b.template_index))
}

// We sort based on a hash so that if the queue is rebuilt, remaining
// cards come back in the same approximate order (mixing + due learning cards
// may still result in a different card)

impl NewCard {
    fn hash_id_with_salt(&mut self, salt: i64) {
        let mut hasher = FnvHasher::default();
        hasher.write_i64(self.id.0);
        hasher.write_i64(salt);
        self.hash = hasher.finish();
    }

    fn hash_note_id_with_salt(&mut self, salt: i64) {
        let mut hasher = FnvHasher::default();
        hasher.write_i64(self.note_id.0);
        hasher.write_i64(salt);
        self.hash = hasher.finish();
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::CardId;
    use crate::decks::DeckId;
    use crate::scheduler::queue::DueCardKind;
    use crate::timestamp::TimestampSecs;

    fn due_card(id: i64, note_id: i64) -> DueCard {
        DueCard {
            id: CardId(id),
            note_id: NoteId(note_id),
            mtime: TimestampSecs(0),
            due: 0,
            current_deck_id: DeckId(1),
            original_deck_id: DeckId(0),
            kind: DueCardKind::Review,
            reps: 0,
        }
    }

    #[test]
    fn points_at_stake_orders_by_descending_weight() {
        // note 10 (low), 20 (high), 30 (mid) in DB order.
        let mut review = vec![due_card(1, 10), due_card(2, 20), due_card(3, 30)];
        let weights = HashMap::from([(NoteId(10), 0.1), (NoteId(20), 0.9), (NoteId(30), 0.5)]);
        reorder_reviews_by_weight(&mut review, &weights);
        let order: Vec<i64> = review.iter().map(|c| c.note_id.0).collect();
        assert_eq!(order, vec![20, 30, 10], "highest weight first");
    }

    #[test]
    fn points_at_stake_is_stable_for_equal_weights() {
        // Equal weights must preserve incoming DB order (stable sort).
        let mut review = vec![due_card(1, 10), due_card(2, 20), due_card(3, 30)];
        let weights = HashMap::from([(NoteId(10), 0.5), (NoteId(20), 0.5), (NoteId(30), 0.5)]);
        reorder_reviews_by_weight(&mut review, &weights);
        let order: Vec<i64> = review.iter().map(|c| c.id.0).collect();
        assert_eq!(order, vec![1, 2, 3], "ties keep DB order");
    }

    #[test]
    fn empty_weights_leaves_order_untouched() {
        let mut review = vec![due_card(1, 10), due_card(2, 20)];
        let before: Vec<i64> = review.iter().map(|c| c.id.0).collect();
        reorder_reviews_by_weight(&mut review, &HashMap::new());
        let after: Vec<i64> = review.iter().map(|c| c.id.0).collect();
        assert_eq!(before, after, "no weights => no reorder");
    }
}
