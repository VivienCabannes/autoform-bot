# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Constants for Lean tooling."""

# Lean 4 declaration keywords
DECL_KINDS: frozenset[str] = frozenset({"theorem", "lemma", "def", "abbrev", "instance", "example", "opaque"})

# Sorry proof variants
SORRY_PROOFS: frozenset[str] = frozenset({"sorry"})

# Declaration kinds subsumed by noncomputable
NONCOMPUTABLE_SUBSUMES: frozenset[str] = frozenset({"theorem", "lemma"})

# Standard axioms allowed in Lean proofs
STANDARD_AXIOMS: frozenset[str] = frozenset({"propext", "Classical.choice", "Quot.sound"})

# Extended axioms — includes native_decide dependencies (trusts the compiler)
EXTENDED_AXIOMS: frozenset[str] = STANDARD_AXIOMS | {"Lean.ofReduceBool", "Lean.trustCompiler"}

# Keywords that modify the Lean compiler or define custom syntax — always forbidden.
# The only allowed macro is in the template's Unproved.lean (excluded from scanning).
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset({"elab", "macro", "syntax"})

# Import roots validated against agent-submitted code.
ALLOWED_IMPORTS: frozenset[str] = frozenset({"Mathlib", "Aesop", "Batteries", "Qq", "Std", "Init", "Lean"})

# Import roots preloaded at REPL startup. Only non-redundant imports —
# everything else is transitively available through Mathlib.
# Must NOT include ProofWidgets — importing it alongside Mathlib silently
# corrupts env 0 (REPL returns {"env": 0} with no errors, but env 0 is empty).
WARMUP_IMPORTS: frozenset[str] = frozenset({"Mathlib"})
