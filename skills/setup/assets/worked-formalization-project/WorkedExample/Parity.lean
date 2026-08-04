import Mathlib

namespace WorkedExample

def IsEven (n : Int) : Prop := ∃ k, n = 2 * k

def LinearCombination (a b x y : Int) : Int := a * x + b * y

theorem isEven_add {x y : Int} (hx : IsEven x) (hy : IsEven y) :
    IsEven (x + y) := by
  rcases hx with ⟨m, rfl⟩
  rcases hy with ⟨n, rfl⟩
  exact ⟨m + n, by ring⟩

theorem isEven_mul_left (a : Int) {x : Int} (hx : IsEven x) :
    IsEven (a * x) := by
  rcases hx with ⟨m, rfl⟩
  exact ⟨a * m, by ring⟩

theorem linearCombination_isEven (a b : Int) {x y : Int}
    (hx : IsEven x) (hy : IsEven y) :
    IsEven (LinearCombination a b x y) := by
  simpa [LinearCombination] using isEven_add (isEven_mul_left a hx) (isEven_mul_left b hy)

end WorkedExample
