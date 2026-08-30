from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoform_cli.__main__ import main
from autoform_cli.claims import (
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    LEGACY_CLAIM_SCHEMA,
    author_claim_key,
    resource_claim_key,
)


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
    return repo


def _plant_message(repo: Path, key: str, message: str) -> None:
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input="", capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()
    subprocess.run(["git", "update-ref", CLAIM_REF_PREFIX + key, commit], cwd=repo, check=True)


def _article(path: Path, title: str, article_id: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"article_id: {article_id}\n" if article_id else ""
    path.write_text(f"---\n{metadata}---\n\n# {title}\n", encoding="utf-8")


def _blueprint(tmp_path: Path, *, article_id: str | None = "af_0123456789abcdef01234567") -> Path:
    blueprint = tmp_path / "blueprint"
    _article(blueprint / "roadmap/chapter/README.md", "Chapter", None)
    _article(blueprint / "roadmap/chapter/main-result.md", "Main result", article_id)
    return blueprint


def _args(repo: Path, scratch: Path, blueprint: Path, *command: str) -> list[str]:
    args = [
        "claim",
        *command,
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--session-id",
        "test-session",
        "--scratch",
        str(scratch),
    ]
    if command[0] in {"acquire", "renew", "release"}:
        args.extend(["--blueprint", str(blueprint)])
    return args


def test_claim_cli_acquire_renew_list_release_round_trip(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"

    assert main(_args(repo, scratch, blueprint, "acquire", node_id, "--ttl", "600")) == 0
    assert "acquired chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "renew", node_id, "--ttl", "600")) == 0
    assert "renewed chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert leases[0]["_key"] == author_claim_key("af_0123456789abcdef01234567")
    assert leases[0]["schema"] == CLAIM_SCHEMA
    assert leases[0]["owner"] == "worker-a"
    assert main(_args(repo, scratch, blueprint, "release", node_id)) == 0
    assert "released chapter/main-result" in capsys.readouterr().out


def test_claim_cli_refuses_live_peer_and_requires_identity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(_args(repo, first, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()

    peer = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-b",
        "--session-id",
        "peer-session",
        "--scratch",
        str(second),
        "--blueprint",
        str(blueprint),
    ]
    assert main(peer) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out

    assert main(["claim", "list", "--repo", str(repo), "--scratch", str(second)]) == 1
    assert "--worker-id" in capsys.readouterr().out


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    blueprint = _blueprint(tmp_path)
    assert main(_args(missing, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "error:" in capsys.readouterr().out


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _plant_message(repo, author_claim_key(article_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "invalid lease JSON" in capsys.readouterr().out


def test_nonexistent_article_creates_no_claim_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "missing")) == 1
    assert "does not exist" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_without_durable_id_is_actionable_and_creates_no_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path, article_id=None)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    output = capsys.readouterr().out
    assert "has no durable article_id" in output
    assert "autoform migrate article-ids" in output
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_rename_with_unchanged_id_preserves_claim_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    old_path = blueprint / "roadmap/chapter/main-result.md"
    new_path = blueprint / "roadmap/chapter/renamed-result.md"
    old_path.rename(new_path)

    assert main(_args(repo, scratch, blueprint, "renew", "chapter/renamed-result")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert [lease["_key"] for lease in leases] == [
        author_claim_key("af_0123456789abcdef01234567")
    ]


def test_article_target_rejects_path_and_article_id_ambiguity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    ambiguous = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / f"roadmap/{ambiguous}.md", "Path match", "af_bbbbbbbbbbbbbbbbbbbbbbbb")
    _article(blueprint / "roadmap/id-match.md", "ID match", ambiguous)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", ambiguous)) == 1
    assert "is ambiguous" in capsys.readouterr().out


def test_explicit_resource_uses_a_distinct_namespace_and_round_trips(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"

    assert main(_args(repo, scratch, blueprint, "acquire", "--resource", "lake-build")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert leases[0]["_key"] == resource_claim_key("lake-build")
    assert leases[0]["_key"] != author_claim_key("lake-build")
    assert main(_args(repo, scratch, blueprint, "release", "--resource", "lake-build")) == 0


def test_positional_lake_build_is_a_deprecated_resource_alias(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 0
    captured = capsys.readouterr()
    assert "deprecated" in captured.err
    assert resource_claim_key("lake-build") in captured.out


def test_article_and_resource_targets_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(
        _args(
            repo,
            tmp_path / "scratch",
            blueprint,
            "acquire",
            "chapter/main-result",
            "--resource",
            "lake-build",
        )
    ) == 1
    assert "mutually exclusive" in capsys.readouterr().out


def test_live_legacy_path_claim_blocks_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out


def test_expired_legacy_path_claim_does_not_block_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 201.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 0
    capsys.readouterr()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert CLAIM_REF_PREFIX + author_claim_key(node_id) in refs
    assert CLAIM_REF_PREFIX + author_claim_key("af_0123456789abcdef01234567") in refs


def test_malformed_legacy_path_claim_blocks_new_article_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    _plant_message(repo, author_claim_key(node_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + author_claim_key(node_id)]


def test_cli_session_environment_is_stable_across_worker_label_changes(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("AUTOFORM_CLAIM_SESSION_ID", "worktree-session")
    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-a")
    acquire = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]
    assert main(acquire) == 0
    capsys.readouterr()

    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-b")
    renew = acquire.copy()
    renew[1] = "renew"
    assert main(renew) == 0
    assert "renewed" in capsys.readouterr().out


def test_cli_derives_a_stable_session_from_the_target_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    blueprint = _blueprint(project)
    scratch = tmp_path / "scratch"
    monkeypatch.delenv("AUTOFORM_CLAIM_SESSION_ID", raising=False)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]

    assert main(args) == 0
    capsys.readouterr()
    args[1] = "renew"
    assert main(args) == 0
    assert "renewed" in capsys.readouterr().out
