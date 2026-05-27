# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean metaprogram template for dependency graph extraction."""

# The Lean script is a template with {import_module} and {module_prefix}
# placeholders replaced via str.replace() (NOT str.format(), since Lean
# code contains braces).
#
# Output format (pipe-delimited, one line per declaration):
#   NAME|KIND|IS_CLASS|TYPE_HEAD|HAS_SORRY|BODY_TAGS|DEP1,DEP2,...|FIELD_DEP1,FIELD_DEP2,...|IS_UNPROVED

LEAN_SCRIPT = """\
import {import_module}
import Lean

open Lean in

partial def _dg_collectConsts (e : Expr) (acc : NameSet := {}) : NameSet :=
  match e with
  | .const name _     => acc.insert name
  | .app fn arg       => _dg_collectConsts arg (_dg_collectConsts fn acc)
  | .lam _ ty body _  => _dg_collectConsts body (_dg_collectConsts ty acc)
  | .forallE _ ty body _ => _dg_collectConsts body (_dg_collectConsts ty acc)
  | .letE _ ty val body _ =>
      _dg_collectConsts body (_dg_collectConsts val (_dg_collectConsts ty acc))
  | .mdata _ e        => _dg_collectConsts e acc
  | .proj _ _ e       => _dg_collectConsts e acc
  | _                 => acc

def _dg_constKind (ci : Lean.ConstantInfo) : String :=
  match ci with
  | .axiomInfo _  => "axiom"
  | .defnInfo _   => "def"
  | .thmInfo _    => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _   => "quot"
  | .inductInfo _ => "inductive"
  | .ctorInfo _   => "constructor"
  | .recInfo _    => "recursor"

-- Get the head constant of the type's conclusion (strip all foralls)
partial def _dg_typeHead (e : Lean.Expr) : String :=
  if e.isForall then _dg_typeHead e.bindingBody!
  else match e.getAppFn with
    | .const n _ => n.toString
    | _ => ""

-- Strip all leading lambdas from an expression
partial def _dg_stripLambdas (e : Lean.Expr) (count : Nat := 0) : Lean.Expr × Nat :=
  match e with
  | .lam _ _ body _ => _dg_stripLambdas body (count + 1)
  | _ => (e, count)

-- Check if an expression references any bvar with index < depth
partial def _dg_hasBVar (e : Lean.Expr) (depth : Nat) : Bool :=
  match e with
  | .bvar idx       => idx < depth
  | .app fn arg     => _dg_hasBVar fn depth || _dg_hasBVar arg depth
  | .lam _ ty b _   => _dg_hasBVar ty depth || _dg_hasBVar b (depth + 1)
  | .forallE _ ty b _ => _dg_hasBVar ty depth || _dg_hasBVar b (depth + 1)
  | .letE _ ty v b _ => _dg_hasBVar ty depth || _dg_hasBVar v depth || _dg_hasBVar b (depth + 1)
  | .mdata _ e      => _dg_hasBVar e depth
  | .proj _ _ e     => _dg_hasBVar e depth
  | _               => false

-- Detect body-level tags
open Lean in
def _dg_bodyTags (env : Environment) (ci : ConstantInfo)
    (isProjectDecl : Name → Bool) (isHelper : Name → Bool) : List String :=
  match ci.value? with
  | none => []
  | some val =>
    let (inner, lamCount) := _dg_stripLambdas val
    let headConst := inner.getAppFn
    let bodyConsts := _dg_collectConsts val

    -- 1. vacuous_body
    let t1 := match inner with
      | .const n _ => if n == ``True || n == ``PUnit.unit || n == ``Unit.unit
          || n == ``True.intro || n == ``trivial then ["vacuous_body"] else []
      | .app (.const n _) _ => if n == ``True.intro || n == ``trivial then ["vacuous_body"] else []
      | .lit _ => ["vacuous_body"]
      | _ => []

    -- 2. ignores_params
    let t2 := if lamCount > 0 && !_dg_hasBVar inner lamCount then ["ignores_params"] else []

    -- 3. proof_by_exfalso
    let t3 := match headConst with
      | .const n _ => if n == ``False.elim || n == ``absurd || n == ``False.casesOn || n == ``Empty.elim
                       then ["proof_by_exfalso"] else []
      | _ => []

    -- 4. proof_by_subsingleton
    let t4 := if bodyConsts.contains ``Subsingleton.elim then ["proof_by_subsingleton"] else []

    -- 5. returns_assumption: body is a bvar (returns hypothesis directly)
    --    or body is a class/struct field projection applied to a bvar
    --    (only fires when the projected name is an actual structure field
    --    projection, not a separately proved theorem in the same namespace)
    let t5 := match inner with
      | .bvar _ => ["returns_assumption"]
      | .app (.const n _) (.bvar _) =>
        if isProjectDecl n && env.isProjectionFn n then
          ["returns_assumption"]
        else []
      | _ => []

    -- 6. field_projection_body
    let t6 := match headConst with
      | .const n _ =>
        if isProjectDecl n && env.isProjectionFn n then ["field_projection_body"] else []
      | _ => []

    -- 7. custom_hypothesis_in_type: only fire for instImplicit binders ([inst : MyClass X])
    let rec checkTypeForCustomHyp (e : Expr) (fuel : Nat) : Bool :=
      match fuel with
      | 0 => false
      | fuel + 1 =>
        if e.isForall then
          let bindTy := e.bindingDomain!
          let binderInfo := e.binderInfo
          let head := bindTy.getAppFn
          let found := match head with
            | .const n _ =>
              (binderInfo == BinderInfo.instImplicit) && isProjectDecl n && Lean.isClass env n
            | _ => false
          found || checkTypeForCustomHyp e.bindingBody! fuel
        else false
    let t7 := if checkTypeForCustomHyp ci.type 100 then ["custom_hypothesis_in_type"] else []

    -- 8. trivial_constructor: body is Foo.mk applied to args where none of the
    --    args reference project-local declarations.
    --    Only fires for constructors of project-defined types (not Exists.intro,
    --    And.intro, Sigma.mk, etc. which are legitimate proof constructions).
    let t8 := match headConst with
      | .const ctorName _ =>
        match env.find? ctorName with
        | some (.ctorInfo ci) =>
          if isProjectDecl ci.induct then
            let args := inner.getAppArgs
            if args.size > 0 then
              let argConsts := args.foldl (init := ({} : NameSet)) fun acc a =>
                _dg_collectConsts a acc
              let ctorParent := ctorName.getPrefix
              let hasProjectRef := argConsts.toList.any fun n =>
                isProjectDecl n && !isHelper n
                  && n != ctorName
                  && !(ctorParent.isPrefixOf n)
              if !hasProjectRef then ["trivial_constructor"] else []
            else []
          else []
        | _ => []
      | _ => []

    t1 ++ t2 ++ t3 ++ t4 ++ t5 ++ t6 ++ t7 ++ t8

open Lean Elab Command in
#eval! show CommandElabM Unit from do
  let env ← getEnv
  let modulePrefix := `{module_prefix}
  let isProjectDecl (name : Name) : Bool :=
    match env.getModuleIdxFor? name with
    | some idx =>
      let modName := env.header.moduleNames[idx.toNat]!
      modulePrefix.isPrefixOf modName
    | none => true
  let isHelper (name : Name) : Bool :=
    (`_dg_collectConsts).isPrefixOf name
      || (`_dg_constKind).isPrefixOf name
      || (`_dg_typeHead).isPrefixOf name
      || (`_dg_stripLambdas).isPrefixOf name
      || (`_dg_hasBVar).isPrefixOf name
      || (`_dg_bodyTags).isPrefixOf name
  let decls := env.constants.fold (init := #[]) fun acc name ci =>
    if !name.isInternal && isProjectDecl name && !isHelper name
    then acc.push (name, ci) else acc
  for (name, ci) in decls do
    let typeConsts := _dg_collectConsts ci.type
    let valueConsts := match ci.value? with
      | some val => _dg_collectConsts val
      | none     => {}
    -- For inductives, also collect consts from constructor types
    -- (structure field types live in the constructor, not the inductive type)
    let ctorConsts := match ci with
      | .inductInfo info =>
        info.ctors.foldl (init := ({} : NameSet)) fun acc ctorName =>
          match env.find? ctorName with
          | some ctorCi => _dg_collectConsts ctorCi.type acc
          | none => acc
      | _ => {}
    let allConsts := typeConsts.union valueConsts |>.union ctorConsts
    -- Resolve internal project constants to capture their transitive refs
    -- (e.g. _private proof helpers that reference project axioms)
    let mut expanded := allConsts
    let mut toResolve := allConsts.toList.filter fun n =>
      n.isInternal && isProjectDecl n && !isHelper n
    while !toResolve.isEmpty do
      match toResolve with
      | [] => break
      | n :: rest =>
        toResolve := rest
        if expanded.contains n then
          match env.find? n with
          | some ci2 =>
            let newConsts := _dg_collectConsts ci2.type
            let newConsts := match ci2.value? with
              | some val => _dg_collectConsts val newConsts
              | none => newConsts
            for c in newConsts.toList do
              if !expanded.contains c then
                expanded := expanded.insert c
                if c.isInternal && isProjectDecl c && !isHelper c then
                  toResolve := c :: toResolve
          | none => pure ()
    let projectDeps := expanded.toList.filter fun n =>
      !n.isInternal && n != name && isProjectDecl n && !isHelper n
    let fieldDeps := projectDeps.filter fun n => env.isProjectionFn n
    let bodyTags := _dg_bodyTags env ci isProjectDecl isHelper
    let hasSorry := if expanded.contains ``sorryAx then "true" else "false"
    let depsStr := ",".intercalate (projectDeps.map fun n => n.toString)
    let fieldDepsStr := ",".intercalate (fieldDeps.map fun n => n.toString)
    let bodyTagsStr := ";".intercalate bodyTags
    let isCls := if isClass env name then "true" else "false"
    let typeHead := _dg_typeHead ci.type
    let isUnproved := {unproved_check}
    IO.println s!"{name}|{_dg_constKind ci}|{isCls}|{typeHead}|{hasSorry}|{bodyTagsStr}|{depsStr}|{fieldDepsStr}|{isUnproved}"
"""
