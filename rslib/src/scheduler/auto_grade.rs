// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork: auto-grade a tapped multiple-choice answer into an Anki `Rating`.
//!
//! Instead of asking the student to self-rate Again/Hard/Good/Easy, the engine
//! derives the rating from signals already in the app: whether the tapped
//! choice was correct, how long they took vs the item's target time, and how
//! hard the item is *for them* — its difficulty `b` (logits, see
//! `gmat_scores::difficulty_logit`) vs their ability `theta` (see
//! `adaptive::estimate_ability`). It is a pure, offline function so the desktop
//! app and the phone (via the C-ABI bridge) grade identically.

use crate::scheduler::answering::Rating;

/// Correct + very fast (≤ 50% of the expected time) → Easy.
const EASY_MAX_RATIO: f64 = 0.5;
/// Correct + within the expected time (≤ 100%) → Good; slower → Hard.
const GOOD_MAX_RATIO: f64 = 1.0;
/// How strongly difficulty-vs-ability stretches the expected time. An item one
/// logit harder than the learner gets `exp(0.5) ≈ 1.65×` the time budget.
const TIME_DIFFICULTY_K: f64 = 0.5;
/// Clamp the difficulty stretch so a single item can't distort the budget.
const MIN_TIME_FACTOR: f64 = 0.5;
const MAX_TIME_FACTOR: f64 = 2.0;
/// Fallback target when the item declares none.
const DEFAULT_TARGET_SECS: f64 = 60.0;

/// Map a tapped multiple-choice answer to a `Rating` from offline signals only.
///
/// * `correct` — did the tapped choice match the answer key?
/// * `elapsed_secs` — seconds from card shown to choice tapped.
/// * `target_secs` — the item's intended time (`target_seconds`); ≤ 0 = unknown.
/// * `difficulty_b` — item difficulty in logits (`gmat_scores::difficulty_logit`).
/// * `theta` — the learner's Rasch ability (`adaptive::estimate_ability`).
///
/// A wrong answer is always `Again`. A correct answer is graded by speed against
/// a difficulty-adjusted target: items that are hard *for this learner* get a
/// longer budget, so being slow on a genuinely hard question isn't over-penalised.
pub(crate) fn grade_answer(
    correct: bool,
    elapsed_secs: f64,
    target_secs: f64,
    difficulty_b: f64,
    theta: f64,
) -> Rating {
    if !correct {
        return Rating::Again;
    }
    let target = if target_secs > 0.0 {
        target_secs
    } else {
        DEFAULT_TARGET_SECS
    };
    let factor = (TIME_DIFFICULTY_K * (difficulty_b - theta))
        .exp()
        .clamp(MIN_TIME_FACTOR, MAX_TIME_FACTOR);
    let effective_target = target * factor;
    // No usable timer → don't guess Easy/Hard; call it Good.
    if !elapsed_secs.is_finite() || elapsed_secs <= 0.0 || effective_target <= 0.0 {
        return Rating::Good;
    }
    let ratio = elapsed_secs / effective_target;
    if ratio <= EASY_MAX_RATIO {
        Rating::Easy
    } else if ratio <= GOOD_MAX_RATIO {
        Rating::Good
    } else {
        Rating::Hard
    }
}

/// Anki's 1–4 ease buttons for a `Rating` (Again=1 … Easy=4). Used by the bridge
/// and the desktop reviewer to answer the card.
pub(crate) fn rating_to_ease(rating: Rating) -> u8 {
    match rating {
        Rating::Again => 1,
        Rating::Hard => 2,
        Rating::Good => 3,
        Rating::Easy => 4,
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn ease(correct: bool, elapsed: f64, target: f64, b: f64, theta: f64) -> u8 {
        rating_to_ease(grade_answer(correct, elapsed, target, b, theta))
    }

    #[test]
    fn wrong_is_always_again() {
        assert_eq!(ease(false, 5.0, 90.0, 0.0, 0.0), 1);
        assert_eq!(ease(false, 200.0, 90.0, 2.0, -2.0), 1);
    }

    #[test]
    fn correct_speed_maps_to_easy_good_hard() {
        // Same ability and difficulty (factor = 1), target 90s.
        assert_eq!(ease(true, 30.0, 90.0, 0.0, 0.0), 4); // 0.33 → Easy
        assert_eq!(ease(true, 70.0, 90.0, 0.0, 0.0), 3); // 0.78 → Good
        assert_eq!(ease(true, 150.0, 90.0, 0.0, 0.0), 2); // 1.67 → Hard
    }

    #[test]
    fn ratio_boundaries() {
        assert_eq!(ease(true, 45.0, 90.0, 0.0, 0.0), 4); // 0.50 → Easy (≤)
        assert_eq!(ease(true, 90.0, 90.0, 0.0, 0.0), 3); // 1.00 → Good (≤)
        assert_eq!(ease(true, 90.1, 90.0, 0.0, 0.0), 2); // just over → Hard
    }

    #[test]
    fn difficulty_adjusts_the_time_budget() {
        // Item 2 logits ABOVE ability → budget ×2 (eff target 180s): 120/180 =
        // 0.67 → Good, whereas at factor 1 it would be 1.33 → Hard.
        assert_eq!(ease(true, 120.0, 90.0, 2.0, 0.0), 3);
        // Item 2 logits BELOW ability → budget ×0.5 (eff target 45s): 60/45 =
        // 1.33 → Hard, whereas at factor 1 it would be 0.67 → Good.
        assert_eq!(ease(true, 60.0, 90.0, -2.0, 0.0), 2);
    }

    #[test]
    fn missing_timer_defaults_to_good() {
        assert_eq!(ease(true, 0.0, 90.0, 0.0, 0.0), 3);
        assert_eq!(ease(true, -1.0, 90.0, 0.0, 0.0), 3);
        assert_eq!(ease(true, f64::NAN, 90.0, 0.0, 0.0), 3);
    }

    #[test]
    fn missing_target_uses_default() {
        // target ≤ 0 → DEFAULT_TARGET_SECS (60): 20/60 = 0.33 → Easy.
        assert_eq!(ease(true, 20.0, 0.0, 0.0, 0.0), 4);
    }
}
