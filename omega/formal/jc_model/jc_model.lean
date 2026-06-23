/-
  omega-architect — Jaynes-Cummings Model Formalization
  Core publishable theorems: Fock Space, Rabi Splitting, Dressed States
-/
import Mathlib
open Matrix
open Complex

set_option linter.unusedVariables false
noncomputable section

/-! ## 1. Truncated Fock Space (Theorem 1) -/

def basisVector (N : ℕ) (i : Fin (N+1)) : Matrix (Fin (N+1)) (Fin 1) ℂ :=
  λ j _ => if j = i then 1 else 0

def numberOp (N : ℕ) : Matrix (Fin (N+1)) (Fin (N+1)) ℂ :=
  λ i j => if i = j then (i.val : ℂ) else 0

theorem fock_dim (N : ℕ) : Fintype.card (Fin (N+1)) = N + 1 := by simp

theorem numberOp_diagonal (N : ℕ) (i j : Fin (N+1)) (h : i ≠ j) : numberOp N i j = 0 := by
  simp [numberOp, h]

/-! ## 2. Creation and Annihilation Operators -/

noncomputable def creationOp (N : ℕ) : Matrix (Fin (N+1)) (Fin (N+1)) ℂ :=
  λ i j => if h : j.val + 1 = i.val ∧ (j.val < N) then (Real.sqrt (j.val.succ : ℝ) : ℂ) else 0

noncomputable def annihilOp (N : ℕ) : Matrix (Fin (N+1)) (Fin (N+1)) ℂ :=
  λ i j => if h : i.val + 1 = j.val ∧ (i.val < N) then (Real.sqrt (i.val.succ : ℝ) : ℂ) else 0

/-! ## 3. JC Hamiltonian and Rabi Splitting (Theorem 3) -/

noncomputable def jcBlock (n : ℕ) (g : ℂ) : Matrix (Fin 2) (Fin 2) ℂ :=
  !![0, g * (Real.sqrt (n.succ : ℝ) : ℂ);
    g * (Real.sqrt (n.succ : ℝ) : ℂ), 0]

theorem plus_eigenvalue (n : ℕ) (g : ℂ) :
    jcBlock n g * (!![(1 : ℂ); (1 : ℂ)]) = ((g : ℂ) * (Real.sqrt (n.succ : ℝ) : ℂ)) • (!![(1 : ℂ); (1 : ℂ)]) := by
  ext i j
  fin_cases i <;> fin_cases j <;>
  simp [jcBlock, Matrix.mul_apply, Matrix.vecCons, Matrix.vecEmpty, Fin.sum_univ_two]

theorem minus_eigenvalue (n : ℕ) (g : ℂ) :
    jcBlock n g * (!![(1 : ℂ); (-1 : ℂ)]) = (-(g : ℂ) * (Real.sqrt (n.succ : ℝ) : ℂ)) • (!![(1 : ℂ); (-1 : ℂ)]) := by
  ext i j
  fin_cases i <;> fin_cases j <;>
  simp [jcBlock, Matrix.mul_apply, Matrix.vecCons, Matrix.vecEmpty, Fin.sum_univ_two]

theorem rabi_splitting_formula (n : ℕ) (g : ℂ) :
    ((g : ℂ) * (Real.sqrt (n.succ : ℝ) : ℂ)) - (-(g : ℂ) * (Real.sqrt (n.succ : ℝ) : ℂ)) =
    2 * (g : ℂ) * (Real.sqrt (n.succ : ℝ) : ℂ) := by ring

example : 2 * (1 : ℂ) * (Real.sqrt 1 : ℂ) = (2 : ℂ) := by norm_num
example : 2 * (1 : ℂ) * (Real.sqrt 2 : ℂ) = (2 * Real.sqrt 2 : ℂ) := by ring

/-! ## 4. Dressed States Orthogonality (Theorem 4) -/

theorem dressed_orthogonal : ((!![(1 : ℂ); (1 : ℂ)])ᴴ * (!![(1 : ℂ); (-1 : ℂ)])) (0 : Fin 1) (0 : Fin 1) = 0 := by
  simp [Matrix.mul_apply, Matrix.conjTranspose, Matrix.vecCons, Matrix.vecEmpty, Fin.sum_univ_two]

theorem dressed_norm_sq : ((!![(1 : ℂ); (1 : ℂ)])ᴴ * (!![(1 : ℂ); (1 : ℂ)])) (0 : Fin 1) (0 : Fin 1) = 2 := by
  simp [Matrix.mul_apply, Matrix.conjTranspose, Matrix.vecCons, Matrix.vecEmpty, Fin.sum_univ_two]; norm_num

end
