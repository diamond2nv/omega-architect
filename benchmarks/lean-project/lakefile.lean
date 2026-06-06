import Lake
open Lake DSL

package «omega» where
  version := "0.1.0"

require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git" @
  "v4.19.0"

@[default_target]
lean_lib «Omega» where
  -- library configuration
