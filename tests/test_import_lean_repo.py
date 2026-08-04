"""Tests for scripts/import_lean_repo.py — bootstrapping a roadmap from Lean source.

Covers:
  * strip_code: block/line comments and string literals blanked, so prose that merely
    says "sorry" never reads as an incomplete proof;
  * parse_file: head -> schema kind, `example` skipped, attributes/modifiers tolerated,
    namespace-stack qualification, and per-declaration body slicing (a sorry in one
    declaration must not leak into its neighbours);
  * scan: recursive walk, .lake/.git/build skipping, --include / --exclude;
  * build_payload: tier-1 module clusters + tier-2 statements, repo-relative posix
    lean_file, origin/background, missing-vs-partial status (never in-mathlib),
    textual depends_on resolution, --limit;
  * apply_payload / main end-to-end against a real init_plan.py project, including
    check_invariants structural validity and idempotency of a second import;
  * main() exit codes and --dry-run / --json reporting.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "review_ui"))

import check_invariants as ci     # noqa: E402
import import_lean_repo as ilr    # noqa: E402
import init_plan                  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def write_lean(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def one_file(tmp_path: Path, text: str, rel: str = "Sample.lean") -> tuple[Path, Path]:
    root = tmp_path / "lean"
    root.mkdir(exist_ok=True)
    return write_lean(root, rel, text), root


def parsed(tmp_path: Path, text: str, rel: str = "Sample.lean") -> dict:
    path, root = one_file(tmp_path, text, rel)
    return {d.name: d for d in ilr.parse_file(path, root)}


def make_lean_repo(tmp_path: Path) -> Path:
    """A small two-module Lean repo: one clean module, one with a sorry."""
    root = tmp_path / "repo"
    write_lean(root, "Geo/Basic.lean", (
        "namespace Geo\n"
        "\n"
        "def dist : Nat := 0\n"
        "\n"
        "theorem dist_nonneg : True := by\n"
        "  have h := dist\n"
        "  trivial\n"
        "\n"
        "end Geo\n"
    ))
    write_lean(root, "Geo/Advanced.lean", (
        "import Geo.Basic\n"
        "\n"
        "namespace Geo\n"
        "\n"
        "theorem triangle : True := by\n"
        "  have h := dist_nonneg\n"
        "  sorry\n"
        "\n"
        "end Geo\n"
    ))
    return root


def make_project(tmp_path: Path, lean_root: Path) -> Path:
    """A dispatch project whose graph.json was created by init_plan.py."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    rc = init_plan.main(["--project", str(project), "--lean-root", str(lean_root)])
    assert rc == 0
    return project / "graph.json"


def nodes_of(graph_path: Path) -> dict:
    return json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]


# ---------------------------------------------------------------------------
# strip_code
# ---------------------------------------------------------------------------

def test_strip_code_blanks_block_line_and_string_literals():
    src = (
        "/- a block comment\n"
        "   spanning several lines -/\n"
        "theorem keep_me : True := trivial  -- a trailing line comment\n"
        '/-- a docstring -/\n'
        'def message : String := "a string literal"\n'
    )
    out = ilr.strip_code(src)
    assert "theorem keep_me" in out
    assert "def message" in out
    for gone in ("a block comment", "spanning several lines", "a trailing line comment",
                 "a docstring", "a string literal"):
        assert gone not in out, gone


def test_strip_code_leaves_real_sorry_visible():
    assert "sorry" in ilr.strip_code("theorem t : True := by\n  sorry\n")


def test_string_literal_containing_dashes_is_blanked_without_swallowing_code(tmp_path):
    """A `--` inside a string literal is not a line comment; the code after it must survive."""
    src = (
        'def flag : String := "pass -- to lake"\n'
        "\n"
        "theorem needs_work : True := by\n"
        "  sorry\n"
        "\n"
        'def other : String := "ok"\n'
    )
    out = ilr.strip_code(src)
    assert "theorem needs_work" in out
    assert "sorry" in out
    decls = parsed(tmp_path, src)
    assert set(decls) == {"flag", "needs_work", "other"}
    assert decls["needs_work"].incomplete is True


def test_prose_that_says_sorry_never_marks_a_declaration_incomplete(tmp_path):
    decls = parsed(tmp_path, (
        "/-- Historically this was closed with a sorry. -/\n"
        "theorem doc_ok : True := by\n"
        "  trivial\n"
        "\n"
        "-- TODO: the old proof was a sorry\n"
        "theorem comment_ok : True := by\n"
        "  trivial\n"
        "\n"
        '/- multi-line note:\n'
        '   sorry, sorry, admit -/\n'
        'def excuse : String := "this proof is a sorry"\n'
        "\n"
        "theorem string_ok : True := by\n"
        "  trivial\n"
    ))
    assert set(decls) == {"doc_ok", "comment_ok", "excuse", "string_ok"}
    assert not any(d.incomplete for d in decls.values()), {n: d.incomplete for n, d in decls.items()}


# ---------------------------------------------------------------------------
# parse_file: kinds, example, attributes/modifiers
# ---------------------------------------------------------------------------

def test_parse_file_maps_every_head_to_its_schema_kind(tmp_path):
    decls = parsed(tmp_path, (
        "theorem k_thm : True := trivial\n"
        "lemma k_lem : True := trivial\n"
        "proposition k_prop : True := trivial\n"
        "corollary k_cor : True := trivial\n"
        "def k_def : Nat := 0\n"
        "structure K_struct where\n"
        "  size : Nat\n"
        "inductive K_ind\n"
        "  | red\n"
        "  | blue\n"
        "class K_class (a : Type) where\n"
        "  op : a\n"
    ))
    assert {name: d.kind for name, d in decls.items()} == {
        "k_thm": "theorem",
        "k_lem": "lemma",
        "k_prop": "proposition",
        "k_cor": "corollary",
        "k_def": "definition",
        "K_struct": "definition",
        "K_ind": "definition",
        "K_class": "definition",
    }


def test_parse_file_skips_example(tmp_path):
    decls = parsed(tmp_path, (
        "example : True := trivial\n"
        "\n"
        "theorem named : True := trivial\n"
        "\n"
        "example (n : Nat) : n = n := rfl\n"
    ))
    assert set(decls) == {"named"}


def test_parse_file_tolerates_attributes_and_modifiers(tmp_path):
    decls = parsed(tmp_path, (
        "@[simp]\n"
        "private theorem m_private : True := trivial\n"
        "\n"
        "@[simp, norm_cast]\n"
        "protected noncomputable def m_protected : Nat := 0\n"
        "\n"
        "unsafe def m_unsafe : Nat := 0\n"
        "\n"
        "partial def m_partial : Nat := 0\n"
        "\n"
        "@[reducible] def m_inline : Nat := 0\n"
    ))
    assert set(decls) == {"m_private", "m_protected", "m_unsafe", "m_partial", "m_inline"}
    assert decls["m_private"].kind == "theorem"
    assert decls["m_protected"].kind == "definition"


# ---------------------------------------------------------------------------
# parse_file: namespace resolution
# ---------------------------------------------------------------------------

def test_namespace_stack_qualifies_ids_and_end_pops(tmp_path):
    decls = parsed(tmp_path, (
        "namespace Alpha\n"
        "\n"
        "theorem one : True := trivial\n"
        "\n"
        "namespace Beta\n"
        "\n"
        "theorem two : True := trivial\n"
        "\n"
        "end Beta\n"
        "\n"
        "theorem three : True := trivial\n"
        "\n"
        "end Alpha\n"
        "\n"
        "theorem four : True := trivial\n"
        "\n"
        "namespace Gamma.Delta\n"
        "theorem five : True := trivial\n"
        "end Gamma.Delta\n"
        "\n"
        "theorem six : True := trivial\n"
    ))
    assert set(decls) == {
        "Alpha.one", "Alpha.Beta.two", "Alpha.three", "four", "Gamma.Delta.five", "six",
    }


def test_anonymous_end_of_a_section_must_not_pop_the_namespace(tmp_path):
    """`section ... end` inside a namespace: Lean still resolves `Ns.after` there."""
    decls = parsed(tmp_path, (
        "namespace Ns\n"
        "\n"
        "section\n"
        "theorem inside : True := trivial\n"
        "end\n"
        "\n"
        "theorem after : True := trivial\n"
        "\n"
        "end Ns\n"
    ))
    assert set(decls) == {"Ns.inside", "Ns.after"}


# ---------------------------------------------------------------------------
# parse_file: body slicing must not bleed across declarations
# ---------------------------------------------------------------------------

def test_incomplete_body_does_not_bleed_into_neighbouring_declarations(tmp_path):
    decls = parsed(tmp_path, (
        "theorem first_sorry : True := by\n"
        "  sorry\n"
        "\n"
        "theorem second_clean : True := by\n"
        "  trivial\n"
        "\n"
        "theorem third_clean : True := by\n"
        "  exact trivial\n"
        "\n"
        "theorem fourth_admit : True := by\n"
        "  admit\n"
        "\n"
        "def fifth_clean : Nat := 0\n"
    ))
    assert {name: d.incomplete for name, d in decls.items()} == {
        "first_sorry": True,
        "second_clean": False,
        "third_clean": False,
        "fourth_admit": True,
        "fifth_clean": False,
    }


def test_final_declaration_owns_the_tail_of_the_file(tmp_path):
    decls = parsed(tmp_path, (
        "namespace Tail\n"
        "theorem head_clean : True := by\n"
        "  trivial\n"
        "\n"
        "theorem tail_sorry : True := by\n"
        "  sorry\n"
        "end Tail\n"
    ))
    assert decls["Tail.head_clean"].incomplete is False
    assert decls["Tail.tail_sorry"].incomplete is True


def test_module_and_lean_file_are_repo_relative(tmp_path):
    path, root = one_file(tmp_path, "theorem nested_thm : True := trivial\n", "Lib/Sub/Deep.lean")
    (decl,) = ilr.parse_file(path, root)
    assert decl.module == "Lib.Sub.Deep"
    assert decl.lean_file == "Lib/Sub/Deep.lean"
    assert ilr.module_of(path, root) == "Lib.Sub.Deep"


def test_parse_file_returns_empty_for_a_lean_file_without_declarations(tmp_path):
    path, root = one_file(tmp_path, "import Mathlib\n-- nothing here\n")
    assert ilr.parse_file(path, root) == []


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def scan_repo(tmp_path: Path) -> Path:
    root = tmp_path / "scanroot"
    write_lean(root, "Lib/Core.lean", "theorem core_thm : True := trivial\n")
    write_lean(root, "Lib/Sub/Deep.lean", "theorem deep_thm : True := trivial\n")
    write_lean(root, "Tests/Spec.lean", "theorem spec_thm : True := trivial\n")
    write_lean(root, ".lake/packages/Dep/Dep.lean", "theorem dep_thm : True := trivial\n")
    write_lean(root, ".git/hooks/Hook.lean", "theorem hook_thm : True := trivial\n")
    write_lean(root, "build/Generated.lean", "theorem gen_thm : True := trivial\n")
    return root


def test_scan_walks_recursively_and_skips_build_dirs(tmp_path):
    root = scan_repo(tmp_path)
    assert {d.name for d in ilr.scan(root)} == {"core_thm", "deep_thm", "spec_thm"}


def test_scan_honours_include_subdirectory(tmp_path):
    root = scan_repo(tmp_path)
    assert {d.name for d in ilr.scan(root, include="Lib")} == {"core_thm", "deep_thm"}
    assert {d.module for d in ilr.scan(root, include="Lib")} == {"Lib.Core", "Lib.Sub.Deep"}


def test_scan_honours_exclude_substrings(tmp_path):
    root = scan_repo(tmp_path)
    assert {d.name for d in ilr.scan(root, exclude=["Tests"])} == {"core_thm", "deep_thm"}
    assert {d.name for d in ilr.scan(root, exclude=["Tests", "Sub"])} == {"core_thm"}


def test_scan_returns_empty_for_a_directory_with_no_lean(tmp_path):
    empty = tmp_path / "empty"
    (empty / "docs").mkdir(parents=True)
    (empty / "docs" / "README.md").write_text("no Lean here\n", encoding="utf-8")
    assert ilr.scan(empty) == []


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

def payload_repo(tmp_path: Path) -> Path:
    root = tmp_path / "payloadroot"
    write_lean(root, "Geo/Basic.lean", (
        "namespace Geo\n"
        "\n"
        "def point : Nat := 0\n"
        "\n"
        "theorem point_pos : True := by\n"
        "  have h := point\n"
        "  trivial\n"
        "\n"
        "end Geo\n"
    ))
    write_lean(root, "Geo/Hard.lean", (
        "theorem uses_qualified : True := by\n"
        "  have h := Geo.point_pos\n"
        "  have k := Nat.add_comm\n"
        "  sorry\n"
        "\n"
        "def recursive_thing : Nat := recursive_thing\n"
    ))
    return root


def test_build_payload_emits_one_cluster_per_module_with_its_members(tmp_path):
    upsert = ilr.build_payload(ilr.scan(payload_repo(tmp_path)))["upsert"]
    clusters = {nid: rec for nid, rec in upsert.items() if rec["tier"] == 1}
    assert set(clusters) == {"Geo.Basic", "Geo.Hard"}
    for rec in clusters.values():
        assert rec["parent"] is None
        assert rec["origin"] == "background"
    assert clusters["Geo.Basic"]["provisional_members"] == ["Geo.point", "Geo.point_pos"]
    assert clusters["Geo.Hard"]["provisional_members"] == ["uses_qualified", "recursive_thing"]


def test_build_payload_statement_fields(tmp_path):
    upsert = ilr.build_payload(ilr.scan(payload_repo(tmp_path)))["upsert"]
    rec = upsert["Geo.point_pos"]
    assert rec["id"] == "Geo.point_pos"
    assert rec["tier"] == 2
    assert rec["parent"] == "Geo.Basic"
    assert rec["kind"] == "theorem"
    assert rec["origin"] == "background"
    assert rec["lean_file"] == "Geo/Basic.lean"
    assert "\\" not in rec["lean_file"]
    assert rec["mathlib_status"] == "partial"
    assert upsert["Geo.point"]["kind"] == "definition"
    assert upsert["uses_qualified"]["lean_file"] == "Geo/Hard.lean"


def test_statement_status_is_missing_iff_incomplete_and_never_in_mathlib(tmp_path):
    upsert = ilr.build_payload(ilr.scan(payload_repo(tmp_path)))["upsert"]
    statements = {nid: rec for nid, rec in upsert.items() if rec["tier"] == 2}
    assert statements["uses_qualified"]["mathlib_status"] == "missing"
    assert statements["Geo.point"]["mathlib_status"] == "partial"
    assert statements["Geo.point_pos"]["mathlib_status"] == "partial"
    assert {rec["mathlib_status"] for rec in upsert.values()} <= {"missing", "partial"}
    assert all(rec["mathlib_status"] != "in-mathlib" for rec in upsert.values())
    assert all(rec.get("mathlib_declarations") is None for rec in upsert.values())


def test_cluster_is_missing_iff_any_member_is_incomplete(tmp_path):
    upsert = ilr.build_payload(ilr.scan(payload_repo(tmp_path)))["upsert"]
    assert upsert["Geo.Basic"]["mathlib_status"] == "partial"
    assert upsert["Geo.Hard"]["mathlib_status"] == "missing"


def test_depends_on_resolves_qualified_and_bare_names_only(tmp_path):
    upsert = ilr.build_payload(ilr.scan(payload_repo(tmp_path)))["upsert"]
    # bare `point` inside `namespace Geo` resolves to the qualified sibling
    assert upsert["Geo.point_pos"]["depends_on"] == ["Geo.point"]
    # fully-qualified mention resolves; `Nat.add_comm` is not an imported node
    assert upsert["uses_qualified"]["depends_on"] == ["Geo.point_pos"]
    # a self-recursive body never depends on itself
    assert upsert["recursive_thing"]["depends_on"] == []
    known = set(upsert)
    for rec in upsert.values():
        assert set(rec["depends_on"]) <= known
        assert rec["id"] not in rec["depends_on"]
    assert upsert["Geo.Basic"]["depends_on"] == []


def test_limit_truncates_the_import(tmp_path):
    decls = ilr.scan(payload_repo(tmp_path))
    assert len(decls) == 4
    upsert = ilr.build_payload(decls, limit=2)["upsert"]
    statements = {nid for nid, rec in upsert.items() if rec["tier"] == 2}
    assert statements == {d.name for d in decls[:2]}
    for rec in upsert.values():
        assert set(rec["depends_on"]) <= set(upsert)


# ---------------------------------------------------------------------------
# apply_payload / main end-to-end
# ---------------------------------------------------------------------------

def test_apply_payload_writes_through_merge_node(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()
    payload = ilr.build_payload(ilr.scan(lean_root))
    code, output = ilr.apply_payload(payload, graph_path, ROOT)
    assert code == 0, output
    assert "merged" in output
    assert set(nodes_of(graph_path)) == set(payload["upsert"])


def test_main_imports_a_repo_into_a_real_project(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()

    rc = ilr.main([str(lean_root), "--project", str(graph_path.parent)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "imported 2 cluster(s) and 3 statement(s)" in out
    assert "DRAFT" in out

    nodes = nodes_of(graph_path)
    assert set(nodes) == {"Geo.Basic", "Geo.Advanced", "Geo.dist", "Geo.dist_nonneg", "Geo.triangle"}

    cluster = nodes["Geo.Advanced"]
    assert cluster["tier"] == 1 and cluster["parent"] is None
    assert cluster["provisional_members"] == ["Geo.triangle"]
    assert cluster["mathlib_status"] == "missing"
    assert nodes["Geo.Basic"]["mathlib_status"] == "partial"

    triangle = nodes["Geo.triangle"]
    assert triangle["tier"] == 2
    assert triangle["parent"] == "Geo.Advanced"
    assert triangle["kind"] == "theorem"
    assert triangle["origin"] == "background"
    assert triangle["lean_file"] == "Geo/Advanced.lean"
    assert triangle["mathlib_status"] == "missing"
    assert triangle["depends_on"] == ["Geo.dist_nonneg"]
    assert nodes["Geo.dist_nonneg"]["depends_on"] == ["Geo.dist"]
    assert nodes["Geo.dist"]["mathlib_status"] == "partial"
    assert all(rec["mathlib_status"] != "in-mathlib" for rec in nodes.values())

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph["metadata"]["lean_root"] == str(lean_root.resolve())


def test_imported_graph_passes_structural_invariants(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    assert ilr.main([str(lean_root), "--project", str(graph_path.parent)]) == 0
    capsys.readouterr()

    structural, _grounded = ci.check(str(graph_path))
    report = capsys.readouterr().out
    assert structural, report
    assert ci.main([str(graph_path)]) == 0
    capsys.readouterr()


def test_second_import_is_idempotent(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    assert ilr.main([str(lean_root), "--project", str(graph_path.parent)]) == 0
    first = nodes_of(graph_path)
    assert ilr.main([str(lean_root), "--project", str(graph_path.parent)]) == 0
    second = nodes_of(graph_path)
    capsys.readouterr()

    assert set(second) == set(first)
    assert len(second) == len(first) == 5
    assert second == first
    structural, _ = ci.check(str(graph_path))
    assert structural
    capsys.readouterr()


def test_import_composes_with_a_preexisting_node(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"]["pre_existing"] = {
        "id": "pre_existing", "tier": 1, "parent": None, "kind": "section",
        "mathlib_status": "in-mathlib", "depends_on": [],
    }
    graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    assert ilr.main([str(lean_root), "--project", str(graph_path.parent)]) == 0
    capsys.readouterr()
    assert "pre_existing" in nodes_of(graph_path)


# ---------------------------------------------------------------------------
# main(): dry run, json, exit codes
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_reports_counts(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    before = graph_path.read_text(encoding="utf-8")
    capsys.readouterr()

    rc = ilr.main([str(lean_root), "--project", str(graph_path.parent), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert "2 module cluster(s)" in out
    assert "3 statement(s)" in out
    assert "1 with incomplete proofs" in out
    assert graph_path.read_text(encoding="utf-8") == before
    assert nodes_of(graph_path) == {}


def test_dry_run_json_is_parseable(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()
    rc = ilr.main([str(lean_root), "--project", str(graph_path.parent), "--dry-run", "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["dry_run"] is True
    assert summary["clusters"] == 2
    assert summary["statements"] == 3
    assert summary["incomplete"] == 1
    assert summary["complete"] == 2
    assert summary["lean_root"] == str(lean_root.resolve())
    assert nodes_of(graph_path) == {}


def test_json_output_of_a_real_import_is_parseable(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()
    rc = ilr.main([str(lean_root), "--project", str(graph_path.parent), "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "dry_run" not in summary
    assert summary["clusters"] == 2 and summary["statements"] == 3
    assert len(nodes_of(graph_path)) == 5


def test_limit_flag_reaches_the_graph(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()
    rc = ilr.main([str(lean_root), "--project", str(graph_path.parent), "--limit", "1", "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["statements"] == 1
    assert len([n for n, r in nodes_of(graph_path).items() if r["tier"] == 2]) == 1


def test_main_rejects_a_nonexistent_lean_root(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    capsys.readouterr()
    rc = ilr.main([str(tmp_path / "does_not_exist"), "--project", str(graph_path.parent)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not a directory" in err
    assert nodes_of(graph_path) == {}


def test_main_reports_a_directory_with_no_lean(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    graph_path = make_project(tmp_path, lean_root)
    empty = tmp_path / "no_lean"
    empty.mkdir()
    capsys.readouterr()
    rc = ilr.main([str(empty), "--project", str(graph_path.parent)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no Lean declarations found" in err
    assert nodes_of(graph_path) == {}


def test_main_requires_an_existing_graph_json(tmp_path, capsys):
    lean_root = make_lean_repo(tmp_path)
    bare = tmp_path / "unplanned"
    bare.mkdir()
    capsys.readouterr()
    rc = ilr.main([str(lean_root), "--project", str(bare)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "run Setup first" in err
    assert not (bare / "graph.json").exists()
