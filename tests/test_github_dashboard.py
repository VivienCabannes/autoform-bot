from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import configure_github_pages as pages
from scripts import export_github_dashboard as exporter


COMMIT = "1" * 40


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / ".autoform"
    content = project / "informal_content"
    kernel = project / "kernel"
    content.mkdir(parents=True)
    kernel.mkdir()
    graph = {
        "metadata": {
            "title": "Safe <Dashboard>",
            "lean_root": "/Users/private/SECRET_LOCAL_PATH",
            "backend": "SECRET_PROVIDER",
        },
        "nodes": [
            {
                "id": "cluster",
                "tier": 1,
                "kind": "section",
                "name": "Main cluster",
                "depends_on": [],
            },
            {
                "id": "theorem-one",
                "tier": 2,
                "parent": "cluster",
                "kind": "theorem",
                "name": "Theorem <script>alert(1)</script>",
                "content": "informal_content/theorem-one.md",
                "lean_file": "Proofs/TheoremOne.lean",
                "depends_on": [],
                "mathlib_status": "missing",
                "mathlib_declarations": ["Nat.add_comm"],
                "source_refs": [{"file": "/private/SECRET_SOURCE", "location": "p. 1"}],
            },
            {
                "id": "missing-content",
                "tier": 2,
                "parent": "cluster",
                "kind": "lemma",
                "name": "Missing content",
                "depends_on": ["theorem-one"],
                "mathlib_status": "partial",
            },
            {
                "id": "escape-content",
                "tier": 2,
                "parent": "cluster",
                "kind": "lemma",
                "name": "Traversal attempt",
                "content": "../../outside-secret.md",
                "lean_file": "../../outside-secret.lean",
                "depends_on": [],
                "mathlib_status": "missing",
            },
        ],
    }
    (project / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (content / "theorem-one.md").write_text(
        "# Statement\n\nFor $n < 3$, prove it.\n\n<script>SECRET_HTML_PAYLOAD</script>\n",
        encoding="utf-8",
    )
    (kernel / "theorem-one.txt").write_text("axioms: [propext]\n", encoding="utf-8")
    (tmp_path / "Proofs").mkdir()
    (tmp_path / "Proofs" / "TheoremOne.lean").write_text(
        "theorem one : True := by sorry\n",
        encoding="utf-8",
    )
    (tmp_path.parent / "outside-secret.md").write_text("SECRET_OUTSIDE_CONTENT", encoding="utf-8")
    (tmp_path.parent / "outside-secret.lean").write_text("SECRET_OUTSIDE_LEAN", encoding="utf-8")
    sidecar = {
        "version": 1,
        "updated_at": "SECRET_TIMESTAMP",
        "settings": {"dial": "full"},
        "reviews": {
            "theorem-one": {
                "ai": {
                    "faithfulness": 5,
                    "proof_integrity": 4,
                    "code_quality": 4,
                    "verdict": "clean",
                    "at": "SECRET_AI_TIMESTAMP",
                    "reasoning": "SECRET_REASONING",
                },
                "human": {
                    "verdict": "clean",
                    "score": 5,
                    "note": "SECRET_REVIEW_NOTE",
                    "by": "SECRET_REVIEWER",
                    "at": "SECRET_HUMAN_TIMESTAMP",
                },
            }
        },
    }
    (project / "review_status.json").write_text(json.dumps(sidecar), encoding="utf-8")
    for name in ("agents_status.json", "task_queue.json", "provider-settings.json"):
        (project / name).write_text(json.dumps({"secret": f"SECRET_{name}"}), encoding="utf-8")
    (project / "dispatch.log").write_text("SECRET_DISPATCH_LOG", encoding="utf-8")
    return project / "graph.json", project / "site"


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_static_export_is_deterministic_and_allowlisted(tmp_path):
    graph, site = _project(tmp_path)
    exporter.export_site(graph, site, tmp_path, git_commit=COMMIT)
    first = _files(site)
    exporter.export_site(graph, site, tmp_path, git_commit=COMMIT)
    second = _files(site)
    assert first == second

    state = json.loads((site / "data/state.json").read_text())
    assert state["publication"]["git_commit"] == COMMIT
    assert len(state["publication"]["graph_revision"]) == 64
    assert state["publication"]["published_categories"] == list(exporter.PUBLISHED_CATEGORIES)
    assert "metadata" not in state
    assert "dial" not in state
    assert {node["id"]: node for node in state["nodes"]}["theorem-one"]["proof_status"] == "incomplete"

    artifact = b"\n".join(first.values()).decode("utf-8", errors="replace")
    assert "/api/" not in artifact
    assert "save human verdict" not in artifact
    assert "X-Review-Token" not in artifact
    assert "rel='icon' type='image/svg+xml' href='assets/autoform-small.svg'" in artifact
    assert "class='af-brand-mark' src='assets/autoform-small.svg'" in artifact
    for secret in (
        "SECRET_LOCAL_PATH",
        "SECRET_PROVIDER",
        "SECRET_SOURCE",
        "SECRET_TIMESTAMP",
        "SECRET_REASONING",
        "SECRET_REVIEW_NOTE",
        "SECRET_REVIEWER",
        "SECRET_DISPATCH_LOG",
        "SECRET_agents_status.json",
        "SECRET_task_queue.json",
        "SECRET_provider-settings.json",
        "SECRET_OUTSIDE_CONTENT",
        "SECRET_OUTSIDE_LEAN",
    ):
        assert secret not in artifact


def test_export_escapes_html_and_handles_missing_or_traversal_content(tmp_path):
    graph, site = _project(tmp_path)
    exporter.export_site(graph, site, tmp_path, git_commit=COMMIT)
    state = json.loads((site / "data/state.json").read_text())
    by_id = {node["id"]: node for node in state["nodes"]}

    theorem_page = (site / by_id["theorem-one"]["path"] / "index.html").read_text()
    assert "<script>SECRET_HTML_PAYLOAD</script>" not in theorem_page
    assert "&lt;script&gt;SECRET_HTML_PAYLOAD&lt;/script&gt;" in theorem_page
    assert "Theorem &lt;script&gt;alert(1)&lt;/script&gt;" in theorem_page
    assert "axioms: [propext]" in theorem_page

    missing_page = (site / by_id["missing-content"]["path"] / "index.html").read_text()
    traversal_page = (site / by_id["escape-content"]["path"] / "index.html").read_text()
    assert "No theorem content was committed" in missing_page
    assert "No theorem content was committed" in traversal_page

    with pytest.raises(exporter.ExportError, match="inside"):
        exporter.export_site(graph, tmp_path.parent / "escaped-site", tmp_path, git_commit=COMMIT)


def test_export_rejects_destructive_or_overlapping_output_paths(tmp_path):
    graph, site = _project(tmp_path)
    marker = tmp_path / "keep-me"
    marker.write_text("safe", encoding="utf-8")

    for output in (tmp_path, graph.parent):
        with pytest.raises(exporter.ExportError, match="dedicated"):
            exporter.export_site(graph, output, tmp_path, git_commit=COMMIT)
    alias = tmp_path / "site-alias"
    alias.symlink_to(graph.parent, target_is_directory=True)
    with pytest.raises(exporter.ExportError, match="symlink"):
        exporter.export_site(graph, alias, tmp_path, git_commit=COMMIT)
    nested_alias = tmp_path / "nested-alias"
    nested_alias.symlink_to(graph.parent, target_is_directory=True)
    with pytest.raises(exporter.ExportError, match="symlink"):
        exporter.export_site(graph, nested_alias / "site", tmp_path, git_commit=COMMIT)

    assert marker.read_text(encoding="utf-8") == "safe"
    assert graph.is_file()
    assert not site.exists()


def test_export_remains_complete_after_plugin_cache_is_deleted(tmp_path):
    graph, site = _project(tmp_path)
    plugin_cache = tmp_path / "plugin-cache"
    plugin_cache.mkdir()
    (plugin_cache / "sentinel").write_text("plugin")
    exporter.export_site(graph, site, tmp_path, git_commit=COMMIT)
    shutil.rmtree(plugin_cache)

    for page in site.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r"(?:href|src)='([^']+)'", text):
            if target.startswith(("https://", "http://", "#")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved == site.resolve() or site.resolve() in resolved.parents
            assert resolved.exists(), (page, target)
    assert (site / "data/state.json").is_file()
    assert (site / "assets/autoform-small.svg").read_bytes() == exporter.BRAND_ASSET.read_bytes()
    assert (site / "assets/static_dashboard.js").is_file()
    assert "/api/" not in (site / "assets/static_dashboard.js").read_text()


def test_pages_configuration_fails_closed_and_generates_pinned_workflow(tmp_path):
    graph, _site = _project(tmp_path)
    relative_graph = graph.relative_to(tmp_path)
    kwargs = {
        "repository": "owner/project",
        "graph": relative_graph,
        "site": Path(".autoform/site"),
        "autoform_repository": "facebookresearch/autoform-bot",
        "autoform_revision": "a" * 40,
    }
    with pytest.raises(pages.PagesConfigError, match="approval"):
        pages.install_configuration(tmp_path, visibility="public", approved=False, **kwargs)
    with pytest.raises(pages.PagesConfigError, match="unclear"):
        pages.install_configuration(tmp_path, visibility="unclear", approved=True, **kwargs)
    with pytest.raises(pages.PagesConfigError, match="invalid"):
        pages.install_configuration(tmp_path, visibility="unknown", approved=True, **kwargs)
    with pytest.raises(pages.PagesConfigError, match="must be verified"):
        pages.install_configuration(tmp_path, visibility="private", approved=True, **kwargs)

    config_path, workflow_path = pages.install_configuration(
        tmp_path,
        visibility="public",
        approved=True,
        **kwargs,
    )
    config = json.loads(config_path.read_text())
    assert config["published_categories"] == list(pages.PUBLISHED_CATEGORIES)
    assert config["excluded_categories"] == list(pages.EXCLUDED_CATEGORIES)
    workflow = workflow_path.read_text()
    assert "facebookresearch/autoform-bot" in workflow
    assert "a" * 40 in workflow
    assert "export_github_dashboard.py" in workflow
    assert "blueprint/web/**" in workflow
    assert "blueprint/web/index.html" in workflow
    assert "apt-get" not in workflow
    assert "pip install" not in workflow
    assert "leanblueprint web" not in workflow
    assert "texlive-full" not in workflow
    assert "__" not in workflow
    action_refs = re.findall(r"uses:\s+actions/[^@\s]+@([^\s]+)", workflow)
    assert len(action_refs) == 5
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_verify_workflow_is_incremental_and_pins_elan_installer():
    workflow = (
        Path(__file__).parents[1] / "templates/github/autoform-verify.yml"
    ).read_text(encoding="utf-8")

    assert "fetch-depth: 0" in workflow
    assert "f81c2e48c1588d4612cd2c8851947898a45ac8d72748a07dff3a5694f1cf589b" in workflow
    assert "sha256sum --check --strict" in workflow
    assert "BASE_SHA:" in workflow
    assert '"diff", "--name-status", "-z", "--find-renames"' in workflow
    assert "New forbidden Lean constructs found" in workflow
    assert ".rglob(\"*.lean\")" not in workflow
    assert "Audit axioms" not in workflow
    assert not re.search(r"curl[^\n]*\|\s*tar", workflow)

    action_refs = re.findall(r"uses:\s+actions/[^@\s]+@([^\s]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_incremental_verify_script_handles_existing_debt_renames_and_comments(tmp_path):
    workflow = (
        Path(__file__).parents[1] / "templates/github/autoform-verify.yml"
    ).read_text(encoding="utf-8")
    script = workflow.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    script = "\n".join(line.removeprefix("          ") for line in script.splitlines())

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Autoform Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "autoform@example.invalid"], cwd=tmp_path, check=True
    )
    old = tmp_path / "Old.lean"
    old.write_text("theorem old : True := by sorry\n", encoding="utf-8")
    subprocess.run(["git", "add", "Old.lean"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "existing debt"], cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    subprocess.run(["git", "mv", "Old.lean", "Renamed.lean"], cwd=tmp_path, check=True)
    (tmp_path / "Safe.lean").write_text(
        "/- outer /- nested sorry -/ axiom hidden : Prop -/\n"
        "theorem safe : True := by trivial\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "safe changes"], cwd=tmp_path, check=True)
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "BASE_SHA": base},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    safe_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    (tmp_path / "Unsafe.lean").write_text("private axiom shortcut : Prop\n", encoding="utf-8")
    subprocess.run(["git", "add", "Unsafe.lean"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "unsafe change"], cwd=tmp_path, check=True)
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "BASE_SHA": safe_head},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Unsafe.lean: adds 1 raw axiom occurrence(s)" in result.stderr


def test_private_pages_install_requires_and_records_verification(tmp_path):
    graph, _site = _project(tmp_path)
    config_path, _workflow_path = pages.install_configuration(
        tmp_path,
        repository="enterprise/private-project",
        visibility="private",
        graph=graph.relative_to(tmp_path),
        site=Path(".autoform/site"),
        autoform_repository="facebookresearch/autoform-bot",
        autoform_revision="b" * 40,
        approved=True,
        private_pages_verified=True,
    )
    config = json.loads(config_path.read_text())
    assert config["repository_visibility"] == "private"
    assert config["private_pages_verified"] is True


def test_cli_publication_boundary_requires_committed_durable_inputs(tmp_path):
    graph, _site = _project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Autoform Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "autoform@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    exporter._require_committed(tmp_path, graph)

    content = tmp_path / ".autoform/informal_content/theorem-one.md"
    content.write_text(content.read_text() + "\nUncommitted change.\n")
    with pytest.raises(exporter.ExportError, match="must be committed"):
        exporter._require_committed(tmp_path, graph)
