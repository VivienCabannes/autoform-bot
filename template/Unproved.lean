-- Copyright (c) Meta Platforms, Inc. and affiliates.
-- All rights reserved.
--
-- This source code is licensed under the license found in the
-- LICENSE file in the root directory of this source tree.

import Lean

/-- Tag attribute marking a declaration as intentionally unproved.

Use this when the book does not provide a proof for a statement (e.g. "proof
omitted", references another source, or states without proof). The dependency
graph metaprogram detects this tag and surfaces it as `is_unproved` so that
evaluation judges can distinguish justified gaps from failures. -/
initialize unprovedAttr : Lean.TagAttribute ←
  Lean.registerTagAttribute `unproved
    "Marks a declaration as intentionally unproved (book does not provide a proof)"

/-- Convenience macro: `unproved theorem foo : P` expands to
`@[unproved] axiom foo : P`. -/
macro "unproved " id:ident sig:Lean.Parser.Command.declSig : command =>
  `(@[unproved] axiom $id $sig)
