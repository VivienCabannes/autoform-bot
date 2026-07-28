from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from scripts import service_control as sc


def _review_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sc.ServiceSpec:
    project = tmp_path / "project"
    plugin = tmp_path / "plugin"
    graph = project / "graph.json"
    lean_root = tmp_path / "lean"
    server = plugin / "scripts" / "review_ui" / "serve_review.py"
    project.mkdir()
    lean_root.mkdir()
    server.parent.mkdir(parents=True)
    graph.write_text("{}")
    server.write_text("# test server")
    monkeypatch.setattr(sc.shutil, "which", lambda command: "/opt/local/bin/uv")
    return sc.review_spec(
        project=project,
        plugin_root=plugin,
        graph=graph,
        lean_root=lean_root,
        port=48765,
    )


def test_service_labels_are_stable_and_project_scoped(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert sc.service_label(first, "review") == sc.service_label(first, "review")
    assert sc.service_label(first, "review") != sc.service_label(second, "review")
    assert sc.service_label(first, "review").endswith(".review")
    assert sc.service_label(first, "blueprint").endswith(".blueprint")


def test_review_plist_is_loopback_only_keepalive_and_project_local(
    tmp_path, monkeypatch
):
    spec = _review_fixture(tmp_path, monkeypatch)
    payload = sc.launchd_plist(spec)

    assert payload["Label"] == spec.label
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["WorkingDirectory"] == str(spec.project)
    assert payload["StandardOutPath"].startswith(
        str(spec.project / ".autoform" / "logs")
    )
    arguments = payload["ProgramArguments"]
    assert arguments[-2:] == ["--port", "48765"]
    assert "0.0.0.0" not in arguments


def test_blueprint_plist_binds_loopback_and_rejects_unbuilt_directory(tmp_path):
    project = tmp_path / "project"
    web = tmp_path / "web"
    project.mkdir()
    web.mkdir()

    with pytest.raises(sc.ServiceError, match="no index.html"):
        sc.blueprint_spec(
            project=project,
            directory=web,
            port=8005,
            python=sys.executable,
        )

    (web / "index.html").write_text("<h1>blueprint</h1>")
    spec = sc.blueprint_spec(
        project=project,
        directory=web,
        port=8005,
        python=sys.executable,
    )
    arguments = list(spec.command)
    assert arguments[1:4] == ["-u", "-m", "http.server"]
    assert arguments[4] == "8005"
    assert arguments[5:7] == ["--bind", "127.0.0.1"]


def test_port_zero_reuses_previous_project_service_port(tmp_path, monkeypatch):
    spec = _review_fixture(tmp_path, monkeypatch)
    spec.state_directory.mkdir(parents=True)
    spec.plist_path.write_bytes(plistlib.dumps(sc.launchd_plist(spec)))

    reused = sc.review_spec(
        project=spec.project,
        plugin_root=tmp_path / "plugin",
        graph=spec.project / "graph.json",
        lean_root=tmp_path / "lean",
        port=0,
    )
    assert reused.port == 48765


def test_non_macos_start_fails_with_actionable_fallback(
    tmp_path, monkeypatch, capsys
):
    spec = _review_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(sc.sys, "platform", "linux")

    assert sc.main(
        [
            "start",
            "review",
            "--project",
            str(spec.project),
            "--plugin-root",
            str(tmp_path / "plugin"),
            "--graph",
            str(spec.project / "graph.json"),
            "--lean-root",
            str(tmp_path / "lean"),
            "--port",
            "48765",
        ]
    ) == 1
    assert "documented foreground command" in capsys.readouterr().err
