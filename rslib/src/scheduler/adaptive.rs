// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork: computer-adaptive card selection (Rasch / 1PL).
//!
//! This adds a secondary "difficulty-fit" sort key that extends the existing
//! points-at-stake review reorder (see `scheduler::topic_mastery`). After
//! estimating the student's ability **θ** from their answer history joined with
//! each card's difficulty, cards whose difficulty is nearest θ float to the
//! front *within* the existing weakest-topic ordering (θ is the tie-break, never
//! the primary key). Gated behind `BoolKey::GmatAdaptiveEnabled`.
//!
//! Like `points_at_stake_weights`, this is a PLAIN READ: it does a single
//! aggregate storage read and never opens a transaction or writes a card, so it
//! records no undo step and leaves the existing undo history intact (building a
//! queue must not cost the student their pending undo).
//!
//! ## The model (Rasch / 1PL)
//! - Each note's difficulty comes from a tag. We prefer `aidiff::NN`
//!   (NN = 0–100, AI-rated); when absent we fall back to coarse
//!   `difficulty::easy|medium|hard` → 20 / 50 / 80. A note with neither tag has
//!   no difficulty and is simply absent from the fit map (the sort treats a
//!   missing note as an infinite distance, so it lands last within a weight
//!   tie).
//! - A 0–100 difficulty maps to a logit `b = (difficulty/100 - 0.5) * SCALE`.
//! - `P(correct) = σ(θ − b)`, σ = logistic.
//! - θ maximises the Rasch log-likelihood over *answered* notes (`total > 0`):
//!   `Σ [passed·ln σ(θ−b) + (total−passed)·ln(1−σ(θ−b))]`, solved with Newton's
//!   method and θ clamped to `[−4, 4]`. With zero observations θ = 0.
//! - `fit_distance(note) = |b_note − θ|`; smaller = closer to ability =
//!   preferred.

use std::collections::HashMap;

use crate::prelude::*;

/// Logit scale mapping a 0–100 difficulty onto the ability axis: difficulty 0
/// → −SCALE/2, 50 → 0, 100 → +SCALE/2. 4.0 spreads easy/medium/hard (20/50/80)
/// across roughly [−1.2, +1.2] logits, a sensible band within the [−4, 4] θ
/// clamp.
const SCALE: f32 = 4.0;
/// θ is clamped to this symmetric band. Beyond ~4 logits the logistic is
/// saturated (P within 0.018 of 0/1) so further movement is meaningless and
/// only invites numerical blow-up from all-correct / all-wrong histories.
const THETA_BOUND: f32 = 4.0;
/// Newton iterations. The Rasch log-likelihood is concave, so a handful of
/// steps from θ = 0 converge to well within display precision.
const NEWTON_ITERS: usize = 10;

/// Coarse `difficulty::*` levels mapped onto the 0–100 scale used by `aidiff`.
const COARSE_EASY: f32 = 20.0;
const COARSE_MEDIUM: f32 = 50.0;
const COARSE_HARD: f32 = 80.0;

impl Collection {
    /// Read-only per-note difficulty-fit distances for the adaptive review
    /// tie-break. See module docs.
    ///
    /// Plain read: this runs while a queue is being built (possibly inside an
    /// outer transaction), so — exactly like `points_at_stake_weights` — it must
    /// not open/commit its own transaction or clear study queues. A direct
    /// storage read is correct and side-effect-free.
    ///
    /// Returns a map of `fit_distance` for every note that has a difficulty
    /// (`aidiff` or coarse). Notes with no difficulty tag are absent from the
    /// map; the sort treats absence as an infinite distance (sorts last within
    /// a weight tie).
    pub(crate) fn adaptive_difficulty_fit(&mut self) -> Result<HashMap<NoteId, f32>> {
        let rows = self.storage.all_topic_card_rows()?;

        // First pass: gather the answered observations that drive θ.
        let mut obs: Vec<(f32, u32, u32)> = Vec::new();
        for row in &rows {
            if row.total == 0 {
                continue;
            }
            if let Some(difficulty) = note_difficulty(&row.tags) {
                obs.push((difficulty_to_logit(difficulty), row.passed, row.total));
            }
        }
        let theta = estimate_theta(&obs);

        // Second pass: fit distance for every note that has a difficulty.
        let mut fit = HashMap::with_capacity(rows.len());
        for row in &rows {
            if let Some(difficulty) = note_difficulty(&row.tags) {
                let b = difficulty_to_logit(difficulty);
                fit.insert(row.note_id, (b - theta).abs());
            }
        }
        Ok(fit)
    }
}

/// Map a 0–100 difficulty onto a Rasch logit `b = (d/100 - 0.5) * SCALE`.
fn difficulty_to_logit(difficulty: f32) -> f32 {
    (difficulty / 100.0 - 0.5) * SCALE
}

/// The logistic (sigmoid) function σ(x) = 1 / (1 + e^{-x}).
fn sigmoid(x: f32) -> f32 {
    1.0 / (1.0 + (-x).exp())
}

/// Estimate the student's Rasch ability θ by maximising the 1PL log-likelihood
/// over answered observations `(b, passed, total)` with Newton's method.
///
/// The gradient and Hessian of the Rasch log-likelihood have closed forms:
///   `L'(θ)  = Σ [passed − total·p]`         where `p = σ(θ − b)`
///   `L''(θ) = Σ [−total·p·(1 − p)]`          (≤ 0, so L is concave)
/// so `θ ← θ − L'/L''`. Starts from θ = 0, clamps each step into
/// `[−THETA_BOUND, THETA_BOUND]`, and returns 0.0 when there are no
/// observations. Factored out as a free function so it is unit-testable with
/// synthetic data.
fn estimate_theta(obs: &[(f32, u32, u32)]) -> f32 {
    if obs.is_empty() {
        return 0.0;
    }
    let mut theta = 0.0_f32;
    for _ in 0..NEWTON_ITERS {
        let mut grad = 0.0_f32;
        let mut hess = 0.0_f32;
        for &(b, passed, total) in obs {
            let p = sigmoid(theta - b);
            grad += passed as f32 - total as f32 * p;
            hess -= total as f32 * p * (1.0 - p);
        }
        // Concave objective: the Hessian is ≤ 0. If it is ~0 the surface is
        // flat here (e.g. saturated probabilities), so stop rather than divide.
        if hess.abs() < 1e-6 {
            break;
        }
        let step = grad / hess;
        theta = (theta - step).clamp(-THETA_BOUND, THETA_BOUND);
        if step.abs() < 1e-5 {
            break;
        }
    }
    theta.clamp(-THETA_BOUND, THETA_BOUND)
}

/// A note's 0–100 difficulty: prefer the AI-rated `aidiff::NN` tag, else the
/// coarse `difficulty::easy|medium|hard` level, else `None` (no difficulty).
fn note_difficulty(tags: &str) -> Option<f32> {
    parse_aidiff(tags).or_else(|| parse_coarse(tags))
}

/// Parse an `aidiff::NN` tag (case-insensitive namespace) into a 0–100
/// difficulty, clamped to the valid range. Returns the first parseable value;
/// `None` if no well-formed `aidiff::` tag is present.
fn parse_aidiff(tags: &str) -> Option<f32> {
    tags.split_whitespace()
        .filter_map(|tag| tag.split_once("::"))
        .filter(|(head, _)| head.eq_ignore_ascii_case("aidiff"))
        .find_map(|(_, value)| value.parse::<f32>().ok())
        .map(|d| d.clamp(0.0, 100.0))
}

/// Parse a coarse `difficulty::easy|medium|hard` tag (case-insensitive) into a
/// 0–100 difficulty (20 / 50 / 80). Returns the first recognised level; `None`
/// if no recognised `difficulty::` tag is present.
fn parse_coarse(tags: &str) -> Option<f32> {
    tags.split_whitespace()
        .filter_map(|tag| tag.split_once("::"))
        .filter(|(head, _)| head.eq_ignore_ascii_case("difficulty"))
        .find_map(|(_, level)| match level.to_ascii_lowercase().as_str() {
            "easy" => Some(COARSE_EASY),
            "medium" => Some(COARSE_MEDIUM),
            "hard" => Some(COARSE_HARD),
            _ => None,
        })
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;

    // --- pure-math / tag-parsing unit tests -----------------------------------

    #[test]
    fn parses_aidiff_then_falls_back_to_coarse() {
        // aidiff wins when both are present.
        assert_eq!(
            note_difficulty("Quant::Arithmetic aidiff::73 difficulty::easy"),
            Some(73.0)
        );
        // coarse levels map to 20/50/80.
        assert_eq!(note_difficulty("difficulty::easy"), Some(COARSE_EASY));
        assert_eq!(note_difficulty("difficulty::medium"), Some(COARSE_MEDIUM));
        assert_eq!(note_difficulty("difficulty::hard"), Some(COARSE_HARD));
        // namespace is case-insensitive; out-of-range aidiff is clamped.
        assert_eq!(note_difficulty("AiDiff::120"), Some(100.0));
        // no difficulty tag at all -> None.
        assert_eq!(note_difficulty("Quant::Arithmetic split::train"), None);
        assert_eq!(note_difficulty("difficulty::trivial"), None);
    }

    #[test]
    fn ability_rises_on_correct_falls_on_wrong() {
        // A single hard item (difficulty 80 -> positive logit).
        let b_hard = difficulty_to_logit(80.0);
        // Passing the hard item most of the time yields a higher θ than failing
        // it most of the time.
        let theta_pass = estimate_theta(&[(b_hard, 9, 10)]);
        let theta_fail = estimate_theta(&[(b_hard, 1, 10)]);
        assert!(
            theta_pass > theta_fail,
            "passing hard items should raise ability above failing them: pass={theta_pass} fail={theta_fail}"
        );
        // Passing hard items should push ability above the item difficulty;
        // failing them should push it below.
        assert!(theta_pass > b_hard, "pass θ above item b: {theta_pass}");
        assert!(theta_fail < b_hard, "fail θ below item b: {theta_fail}");
    }

    #[test]
    fn theta_is_finite_and_bounded() {
        let cases: Vec<Vec<(f32, u32, u32)>> = vec![
            vec![],                                     // no observations -> 0
            vec![(difficulty_to_logit(50.0), 0, 5)],    // all wrong
            vec![(difficulty_to_logit(50.0), 5, 5)],    // all correct
            vec![(difficulty_to_logit(0.0), 100, 100)], // trivial, all correct
            vec![(difficulty_to_logit(100.0), 0, 100)], // hardest, all wrong
            vec![
                (difficulty_to_logit(20.0), 8, 10),
                (difficulty_to_logit(50.0), 5, 10),
                (difficulty_to_logit(80.0), 2, 10),
            ],
        ];
        for obs in cases {
            let theta = estimate_theta(&obs);
            assert!(theta.is_finite(), "θ must be finite for {obs:?}: {theta}");
            assert!(
                (-THETA_BOUND..=THETA_BOUND).contains(&theta),
                "θ must stay within the clamp for {obs:?}: {theta}"
            );
        }
        assert_eq!(estimate_theta(&[]), 0.0, "no observations -> θ = 0");
    }

    // --- end-to-end tests against a real Collection ---------------------------

    /// Add a single review card with the given tags and interval to the default
    /// deck, returning its note id.
    fn add_review_card(col: &mut Collection, tags: &[&str], interval: u32) -> NoteId {
        let nt = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "front").unwrap();
        note.tags = tags.iter().map(|s| s.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();

        let mut card = col
            .storage
            .get_card_by_ordinal(note.id, 0)
            .unwrap()
            .unwrap();
        card.interval = interval;
        card.due = 0;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
        note.id
    }

    /// Record genuine revlog entries for a note's card so the aggregate query
    /// sees `passed`/`total`. A "pass" is ease >= 2 (Good), a "fail" is ease 1
    /// (Again); both count toward `total`. This is a plain revlog write, not a
    /// card write, and not undoable — it mirrors how the read path counts
    /// answers without needing to drive the full reviewer for a specific card.
    fn record_reviews(col: &mut Collection, nid: NoteId, passed: u32, failed: u32) {
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();
        for i in 0..(passed + failed) {
            let ease = if i < passed { 3 } else { 1 };
            let entry = RevlogEntry {
                id: crate::revlog::RevlogId(TimestampMillis::now().0 + i as i64),
                cid: card.id,
                usn: Usn(-1),
                button_chosen: ease,
                interval: 10,
                last_interval: 5,
                ease_factor: 2500,
                taken_millis: 1000,
                review_kind: RevlogReviewKind::Review,
            };
            col.storage.add_revlog_entry(&entry, true).unwrap();
        }
    }

    #[test]
    fn missing_aidiff_falls_back_to_coarse() {
        let mut col = Collection::new();
        // Note tagged only with a coarse difficulty -> present in the map,
        // computed from 80.
        let coarse = add_review_card(&mut col, &["Quant::Arithmetic", "difficulty::hard"], 5);
        // Note with an explicit aidiff -> uses it, not the coarse level.
        let ai = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::30"], 5);
        // Note with no difficulty tag at all -> absent from the map.
        let none = add_review_card(&mut col, &["Quant::Arithmetic"], 5);

        let fit = col.adaptive_difficulty_fit().unwrap();

        // With no answered observations θ = 0, so fit = |b - 0| = |b|.
        let expected_coarse = difficulty_to_logit(COARSE_HARD).abs();
        let expected_ai = difficulty_to_logit(30.0).abs();
        assert!(
            (fit[&coarse] - expected_coarse).abs() < 1e-5,
            "coarse note uses difficulty 80: {}",
            fit[&coarse]
        );
        assert!(
            (fit[&ai] - expected_ai).abs() < 1e-5,
            "aidiff note uses 30: {}",
            fit[&ai]
        );
        assert!(
            !fit.contains_key(&none),
            "note with no difficulty tag is absent from the map"
        );
    }

    #[test]
    fn next_card_picks_nearest_difficulty() {
        use crate::scheduler::queue::builder::sorting::reorder_reviews_by_weight_then_fit;
        use crate::scheduler::queue::builder::DueCard;
        use crate::scheduler::queue::DueCardKind;

        let mut col = Collection::new();
        // Answered history that pins ability high (θ near the hard end): the
        // student keeps getting an aidiff::80 item right.
        let anchor = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::80"], 5);
        record_reviews(&mut col, anchor, 20, 0);

        // Three candidate notes at spread difficulties.
        let easy = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::20"], 5);
        let mid = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::50"], 5);
        let hard = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::80"], 5);

        let fit = col.adaptive_difficulty_fit().unwrap();
        // With ability pinned high, the hard item is closest to θ.
        assert!(
            fit[&hard] < fit[&mid] && fit[&mid] < fit[&easy],
            "nearest-to-ability ordering: hard={} mid={} easy={}",
            fit[&hard],
            fit[&mid],
            fit[&easy]
        );

        // Within an equal-weight group, sort_review_adaptive floats the
        // nearest-difficulty note (hard) first, then mid, then easy.
        fn due(note_id: NoteId) -> DueCard {
            DueCard {
                id: CardId(note_id.0),
                note_id,
                mtime: TimestampSecs(0),
                due: 0,
                current_deck_id: DeckId(1),
                original_deck_id: DeckId(0),
                kind: DueCardKind::Review,
                reps: 0,
            }
        }
        // Incoming DB order deliberately worst-first.
        let mut review = vec![due(easy), due(mid), due(hard)];
        let equal_weights: HashMap<NoteId, f32> =
            [easy, mid, hard].iter().map(|&n| (n, 1.0)).collect();
        reorder_reviews_by_weight_then_fit(&mut review, &equal_weights, &fit);
        let order: Vec<NoteId> = review.iter().map(|c| c.note_id).collect();
        assert_eq!(
            order,
            vec![hard, mid, easy],
            "nearest-difficulty note first within an equal-weight group"
        );
    }

    #[test]
    fn adaptive_selection_leaves_undo_intact() {
        let mut col = Collection::new();
        let nid = add_review_card(&mut col, &["Quant::Arithmetic", "aidiff::50"], 10);
        record_reviews(&mut col, nid, 3, 2);

        // Perform a genuine undoable op so there is an undo entry to protect.
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();
        col.transact(Op::UpdateCard, |col| {
            col.get_and_update_card(card.id, |c| {
                c.interval = 12;
                Ok(())
            })
            .unwrap();
            Ok(())
        })
        .unwrap();
        assert_eq!(col.can_undo(), Some(&Op::UpdateCard));

        // Enabling adaptive selection and building the queue runs
        // adaptive_difficulty_fit + sort_review_adaptive, which mutate no cards
        // and open no transaction, so the pending undo must survive intact.
        col.set_config_bool(BoolKey::GmatAdaptiveEnabled, true, false)
            .unwrap();
        col.build_queues(DeckId(1)).unwrap();
        assert_eq!(
            col.can_undo(),
            Some(&Op::UpdateCard),
            "adaptive queue build must not clear undo"
        );
        col.undo().unwrap();
        assert_eq!(
            col.storage.get_card(card.id).unwrap().unwrap().interval,
            10,
            "undo restored the pre-op interval after the adaptive reorder"
        );
    }
}
