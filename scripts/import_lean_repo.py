#!/usr/bin/env python3
"""Bootstrap a roadmap FROM an existing Lean formalization.

AutoformBot's graph normally grows from informal sources. That leaves out the
case that matters for adoption: a repository that *already* contains a
formalization — complete or half-finished — and no AutoformBot state. Pointed
at such a repo, Setup would give you a working dashboard over an empty graph,
so nothing would render and nothing would be prove-eligible.

This importer reads the Lean itself and emits a graph:

* one **tier-1 cluster per module** (the file's module path is the natural
  coarse grouping a Lean author already chose);
* one **tier-2 node per declaration** (`theorem`/`lemma`/`def`/…), carrying
  ``lean_file`` so prove-eligibility, the sorry scan, and the static
  dashboard's proof status all work immediately;
* ``depends_on`` seeded from declarations this one actually *mentions* within
  the same tier — a high-recall, deliberately imperfect first draft;
* ``mathlib_status: missing`` for anything whose proof is incomplete
  (``sorry``/``admit``), otherwise ``partial`` — a landed proof is real work,
  but the importer has not reviewed it, so it never claims ``in-mathlib``;
* ``origin: background``, because imported statements are recovered from code
  rather than from a cited source, and no citation may be invented.

Everything is written through ``merge_node.py`` — the only legal writer of
``graph.json`` — so an import composes with an existing graph instead of
replacing it.

**The result is a draft, not a roadmap.** It has the shape of the code, not the
shape of the mathematics: a module is not always a concept, and the dependency
edges are textual. Run the graph/content reviewers and the audit over it
afterwards, and attach sources when you have them. That honest starting point
is still worth far more than an empty graph.

Usage::

  import_lean_repo.py <lean-root> --project <dispatch-project> [--dry-run]
                      [--include Sub/Dir] [--exclude Pattern] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE / "review_ui") not in sys.path:
    sys.path.insert(0, str(_HERE / "review_ui"))

#: Declaration heads we import as nodes. `example` is deliberately absent (it is
#: anonymous), as are `instance`/`abbrev` (rarely roadmap-worthy on their own).
_KINDS = {
    "theorem": "theorem",
    "lemma": "lemma",
    "proposition": "proposition",
    "corollary": "corollary",
    "def": "definition",
    "structure": "definition",
    "inductive": "definition",
    "class": "definition",
}

_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+)*"
    r"(" + "|".join(sorted(_KINDS, key=len, reverse=True)) + r")\s+"
    r"([A-Za-z_][A-Za-z0-9_.'!?]*)",
    re.MULTILINE,
)
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_.']*)", re.MULTILINE)
_END_RE = re.compile(r"^\s*end\b\s*([A-Za-z_][A-Za-z0-9_.']*)?", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/-.*?-/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_INCOMPLETE_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b")

_SKIP_DIRS = {".lake", ".git", "build", "lakefile", ".autoform"}


def strip_code(src: str) -> str:
    """Source with comments and string literals blanked — prose that merely
    mentions ``sorry`` must never be read as an incomplete proof.

    Deliberately a single left-to-right pass rather than three chained regex
    substitutions: run separately, a line-comment pattern matches the ``--``
    *inside* a string literal, truncating it so its closing quote then pairs
    with the next literal's opening quote and everything between is blanked.
    A file containing ``"pass -- to lake"`` lost every declaration after it.
    Lengths are preserved so byte offsets stay usable by the caller.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == '"':                                   # string literal
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                j += 1
            out.append('""'.ljust(j - i))
            i = j
        elif src.startswith("/-", i):                   # block comment (nesting)
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/-", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("-/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append("".join(c if c == "\n" else " " for c in src[i:j]))
            i = j
        elif src.startswith("--", i):                   # line comment
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass
class Decl:
    name: str            # fully qualified where namespaces are open
    kind: str            # schema kind
    lean_file: str       # repo-relative
    module: str          # dotted module path (the tier-1 cluster)
    incomplete: bool     # its proof body contains sorry/admit
    mentions: set = field(default_factory=set)   # other declaration names in its body


def module_of(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _namespace_at(stripped: str, offset: int) -> str:
    """The namespace stack open at ``offset`` — so declaration ids match the
    names Lean actually resolves, not the bare local head.

    ``section`` must push a sentinel even though it contributes no name: a
    bare ``end`` closing a section would otherwise pop the enclosing
    *namespace*, and since sections are ubiquitous in real Lean the ids would
    drift from what Lean resolves — mis-resolving `depends_on` and even
    colliding a declaration id with a module cluster id.
    """
    stack: list[str | None] = []
    for match in re.finditer(r"^\s*(namespace|section|end)\b[ \t]*([A-Za-z_][A-Za-z0-9_.']*)?",
                             stripped[:offset], re.MULTILINE):
        head, name = match.group(1), match.group(2)
        if head == "namespace":
            if name:
                stack.append(name)
        elif head == "section":
            stack.append(None)                  # anonymous or named: contributes no id
        elif stack:
            if name is None:
                stack.pop()                     # closes the innermost open scope
            else:
                # `end Foo` closes back through Foo when it is open; a named
                # section end is otherwise just the innermost scope.
                if name in [s for s in stack if s]:
                    while stack:
                        top = stack.pop()
                        if top == name:
                            break
                else:
                    stack.pop()
    return ".".join(s for s in stack if s)


def parse_file(path: Path, root: Path) -> list[Decl]:
    """Every importable declaration in one Lean file, with its body's mentions."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stripped = strip_code(raw)
    module = module_of(path, root)
    rel = path.relative_to(root).as_posix()

    hits = list(_DECL_RE.finditer(stripped))
    out: list[Decl] = []
    for index, match in enumerate(hits):
        head, local = match.group(1), match.group(2)
        end = hits[index + 1].start() if index + 1 < len(hits) else len(stripped)
        body = stripped[match.end():end]
        namespace = _namespace_at(stripped, match.start())
        name = f"{namespace}.{local}" if namespace else local
        mentions = set(re.findall(r"[A-Za-z_][A-Za-z0-9_.']*", body))
        out.append(Decl(
            name=name,
            kind=_KINDS[head],
            lean_file=rel,
            module=module,
            incomplete=bool(_INCOMPLETE_RE.search(body)),
            mentions=mentions,
        ))
    return out


def scan(root: Path, include: str | None = None, exclude: list[str] | None = None) -> list[Decl]:
    decls: list[Decl] = []
    base = root / include if include else root
    for path in sorted(base.rglob("*.lean")):
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_DIRS:
            continue
        if any(pattern in path.relative_to(root).as_posix() for pattern in (exclude or [])):
            continue
        decls.extend(parse_file(path, root))
    return decls


def build_payload(decls: list[Decl], limit: int = 0) -> dict:
    """Graph records for the scanned declarations: module clusters + statements."""
    if limit:
        decls = decls[:limit]
    by_name = {d.name: d for d in decls}
    # Local names too: a body usually cites `foo`, not `Ns.foo`.
    local_index: dict[str, str] = {}
    for name in by_name:
        local_index.setdefault(name.rsplit(".", 1)[-1], name)

    upsert: dict[str, dict] = {}
    modules: dict[str, list[Decl]] = {}
    for decl in decls:
        modules.setdefault(decl.module, []).append(decl)

    for module, members in sorted(modules.items()):
        upsert[module] = {
            "id": module,
            "tier": 1,
            "parent": None,
            "kind": "definition",
            "description": (f"Declarations in the Lean module `{module}` "
                            f"({len(members)} imported)."),
            "provisional_members": [d.name for d in members],
            "statement_depends_on": [],
            "proof_depends_on": [],
            "depends_on": [],
            "related": [],
            "mathlib_status": "missing" if any(d.incomplete for d in members) else "partial",
            "origin": "background",
            "content": None,
        }

    for decl in decls:
        deps = set()
        for token in decl.mentions:
            target = token if token in by_name else local_index.get(token)
            if target and target != decl.name:
                deps.add(target)
        upsert[decl.name] = {
            "id": decl.name,
            "tier": 2,
            "parent": decl.module,
            "kind": decl.kind,
            "description": (f"{decl.kind.capitalize()} `{decl.name}` imported from "
                            f"`{decl.lean_file}`."
                            + (" Its proof is incomplete (sorry/admit)." if decl.incomplete else "")),
            # The lightweight lexer sees a declaration as one span and cannot
            # soundly separate type mentions from proof-body mentions. Preserve
            # scheduling by classifying imported edges as proof dependencies;
            # the graph-review wave reclassifies statement-level edges.
            "statement_depends_on": [],
            "proof_depends_on": sorted(deps),
            "depends_on": sorted(deps),
            "related": [],
            # An imported proof is real work, but nobody has reviewed it and it
            # is not Mathlib: `partial` when it compiles-as-written, `missing`
            # when a sorry remains. Never `in-mathlib` — that status is trusted
            # by construction and must be earned by a verified check.
            "mathlib_status": "missing" if decl.incomplete else "partial",
            "origin": "background",
            "lean_file": decl.lean_file,
            "content": None,
        }
    return {"upsert": upsert}


def apply_payload(payload: dict, graph_path: Path, plugin_root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(plugin_root / "scripts" / "merge_node.py"), str(graph_path)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Bootstrap a roadmap from an existing Lean formalization.")
    ap.add_argument("lean_root", type=Path)
    ap.add_argument("--project", type=Path, required=True,
                    help="dispatch project that owns graph.json")
    ap.add_argument("--include", default=None, help="only scan this subdirectory")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip paths containing this substring (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="import at most N declarations")
    ap.add_argument("--dry-run", action="store_true", help="report what would be imported")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = args.lean_root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    decls = scan(root, args.include, args.exclude)
    if not decls:
        print(f"no Lean declarations found under {root}", file=sys.stderr)
        return 1

    payload = build_payload(decls, args.limit)
    clusters = sum(1 for r in payload["upsert"].values() if r["tier"] == 1)
    statements = sum(1 for r in payload["upsert"].values() if r["tier"] == 2)
    incomplete = sum(1 for r in payload["upsert"].values()
                     if r["tier"] == 2 and r["mathlib_status"] == "missing")
    summary = {
        "lean_root": str(root),
        "clusters": clusters,
        "statements": statements,
        "incomplete": incomplete,
        "complete": statements - incomplete,
    }

    if args.dry_run:
        if args.json:
            print(json.dumps({**summary, "dry_run": True}, indent=2))
        else:
            print(f"[dry-run] {clusters} module cluster(s), {statements} statement(s) "
                  f"({incomplete} with incomplete proofs) would be imported from {root}")
        return 0

    graph_path = (args.project / "graph.json").resolve()
    if not graph_path.exists():
        print(f"no graph.json at {graph_path} — run Setup first", file=sys.stderr)
        return 2
    code, output = apply_payload(payload, graph_path, _HERE.parent)
    if code != 0:
        print(f"merge_node rejected the import: {output}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"imported {clusters} cluster(s) and {statements} statement(s) "
              f"({incomplete} with incomplete proofs) into {graph_path}")
        print("This is a DRAFT with the shape of the code, not of the mathematics: "
              "run the graph/content reviewers and `roadmap_audit.py` over it, and "
              "attach sources before treating its structure as authoritative.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
