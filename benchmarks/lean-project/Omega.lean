import Mathlib

theorem add_zero (n : ℕ) : n + 0 = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]
