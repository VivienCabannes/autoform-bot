import Mathlib

/-!
# Convex sets (demo)

A tiny, deliberately *incomplete* file used by `make demo` to show what the
workspace scanner reports: counts of declarations, unfinished proofs, and
trusted assumptions.
-/

/-- A convex subset of the reals. -/
def ConvexSet (s : Set ℝ) : Prop := sorry

/-- The intersection of two convex sets is convex. -/
theorem convex_inter (s t : Set ℝ) :
    ConvexSet s → ConvexSet t → ConvexSet (s ∩ t) := by
  sorry

/-- A deliberately introduced axiom, so the scanner has one to count. -/
axiom choice_real : ∀ p : ℝ → Prop, (∃ x, p x) → ℝ
