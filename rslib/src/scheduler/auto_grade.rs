// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork: turn a tapped multiple-choice answer + the student's confidence
//! into an Anki `Rating`.
//!
//! Calibration — not speed — is the trainable GMAT skill (see the Brainlift), so
//! the rating comes from *correctness × the student's own confidence*, never from
//! response time. Time is confounded by reading load, computation weight, and (on
//! an adaptive test) item position, so it is a poor mastery signal; the apps use
//! it only for pacing feedback. The same confidence tap doubles as the
//! "keep working vs. skip/guess" judgment the exam actually rewards. Pure and
//! offline, so the desktop app and the phone grade identically.

use crate::scheduler::answering::Rating;

/// The student's self-reported confidence in the answer they picked, captured
/// before the answer is revealed — the brief self-evaluation step the
/// calibration literature (Osterhage 2019; Nietfeld 2005) credits with reducing
/// overconfidence.
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub(crate) enum Confidence {
    Guessing,
    FairlySure,
    Confident,
}

impl Confidence {
    /// Map the wire value (0/1/2) to a level; anything else is treated as a guess.
    pub(crate) fn from_u32(v: u32) -> Self {
        match v {
            2 => Confidence::Confident,
            1 => Confidence::FairlySure,
            _ => Confidence::Guessing,
        }
    }
}

/// Rating from correctness and confidence. A wrong answer is always `Again`; a
/// correct answer is `Hard`/`Good`/`Easy` by how sure the student was.
pub(crate) fn grade_answer(correct: bool, confidence: Confidence) -> Rating {
    if !correct {
        return Rating::Again;
    }
    match confidence {
        Confidence::Guessing => Rating::Hard, // right, but a coin-flip → resurface sooner
        Confidence::FairlySure => Rating::Good,
        Confidence::Confident => Rating::Easy,
    }
}

/// A miscalibrated answer: the student was confident (not guessing) but wrong —
/// the overconfident miss that hurts most on an adaptive test. Surfaced to the
/// UI so the student can see *where* their sense of certainty betrayed them.
pub(crate) fn is_overconfident_miss(correct: bool, confidence: Confidence) -> bool {
    !correct && confidence != Confidence::Guessing
}

/// Anki's 1–4 ease buttons for a `Rating` (Again=1 … Easy=4).
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

    fn ease(correct: bool, c: Confidence) -> u8 {
        rating_to_ease(grade_answer(correct, c))
    }

    #[test]
    fn wrong_is_always_again_regardless_of_confidence() {
        assert_eq!(ease(false, Confidence::Guessing), 1);
        assert_eq!(ease(false, Confidence::FairlySure), 1);
        assert_eq!(ease(false, Confidence::Confident), 1);
    }

    #[test]
    fn correct_maps_confidence_to_hard_good_easy() {
        assert_eq!(ease(true, Confidence::Guessing), 2); // Hard
        assert_eq!(ease(true, Confidence::FairlySure), 3); // Good
        assert_eq!(ease(true, Confidence::Confident), 4); // Easy
    }

    #[test]
    fn overconfident_miss_is_wrong_and_not_guessing() {
        assert!(is_overconfident_miss(false, Confidence::Confident));
        assert!(is_overconfident_miss(false, Confidence::FairlySure));
        // An honest guess that misses is not overconfidence.
        assert!(!is_overconfident_miss(false, Confidence::Guessing));
        // Correct answers are never miscalibrated misses.
        assert!(!is_overconfident_miss(true, Confidence::Confident));
    }

    #[test]
    fn confidence_from_wire_value() {
        assert_eq!(Confidence::from_u32(0), Confidence::Guessing);
        assert_eq!(Confidence::from_u32(1), Confidence::FairlySure);
        assert_eq!(Confidence::from_u32(2), Confidence::Confident);
        assert_eq!(Confidence::from_u32(99), Confidence::Guessing); // fallback
    }
}
