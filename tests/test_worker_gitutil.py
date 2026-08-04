"""Tests for autoform_worker.gitutil — slug parsing and the CAS safe_push safety layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_worker import gitutil
from autoform_worker.errors import Die


def _commit(repo: Path, msg: str) -> str:
    gitutil.run_git(
        ["-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "--allow-empty", "-m", msg],
        cwd=repo,
    )
    return gitutil.head_oid(repo)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    gitutil.run_git(["init", "-b", "main"], cwd=path)
    (path / "a.txt").write_text("one\n")
    gitutil.run_git(["add", "."], cwd=path)
    gitutil.run_git(
        ["-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-m", "c1"],
        cwd=path,
    )
    return path


def _make_bare(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    gitutil.run_git(["init", "--bare", str(path)])
    return str(path)


# ---------------------------------------------------------------- parse_slug


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:o/r.git", "o/r"),
        ("git@github.com:o/r", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("https://github.com/o/r.git", "o/r"),
        ("http://github.com/o/r", "o/r"),
        ("ssh://git@github.com/o/r", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
        ("https://github.com/o/r/", "o/r"),
        ("  https://github.com/o/r  ", "o/r"),
        # non-github hosts
        ("https://gitlab.com/o/r", None),
        ("git@bitbucket.org:o/r.git", None),
        # bad shapes
        ("https://github.com/o", None),
        ("https://github.com/o/r/extra", None),
        ("https://github.com/", None),
        ("https://github.com//r", None),
        ("git@github.com:", None),
        ("", None),
        ("not a url at all", None),
    ],
)
def test_parse_slug(url, expected):
    assert gitutil.parse_slug(url) == expected


# ------------------------------------------------------------------ slug_url


def test_slug_url_default(monkeypatch):
    monkeypatch.delenv("AUTOFORM_GIT_BASE_URL", raising=False)
    assert gitutil.slug_url("o/r") == "https://github.com/o/r"


def test_slug_url_env_override(monkeypatch, tmp_path):
    base = tmp_path / "remotes"
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(base))
    assert gitutil.slug_url("o/r") == f"{base}/o/r"


def test_slug_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", "https://ghe.example.com/")
    assert gitutil.slug_url("o/r") == "https://ghe.example.com/o/r"


# ------------------------------------------- repo introspection helpers


def test_clean_tree_true_and_false(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    assert gitutil.clean_tree(repo) is True
    # an untracked file counts as dirty
    (repo / "junk.txt").write_text("x\n")
    assert gitutil.clean_tree(repo) is False
    (repo / "junk.txt").unlink()
    assert gitutil.clean_tree(repo) is True
    # a modified tracked file counts as dirty
    (repo / "a.txt").write_text("changed\n")
    assert gitutil.clean_tree(repo) is False


def test_current_branch_on_branch_and_detached(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    assert gitutil.current_branch(repo) == "main"
    gitutil.run_git(["checkout", "--detach"], cwd=repo)
    assert gitutil.current_branch(repo) == "HEAD"


def test_head_oid(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    oid = gitutil.head_oid(repo)
    assert len(oid) == 40 and all(c in "0123456789abcdef" for c in oid)
    assert oid == gitutil.run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    oid2 = _commit(repo, "c2")
    assert gitutil.head_oid(repo) == oid2 != oid


def test_remote_ref_oid_present_and_absent(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    oid = gitutil.head_oid(repo)
    gitutil.run_git(["push", remote, "HEAD:refs/heads/topic"], cwd=repo)
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == oid
    assert gitutil.remote_ref_oid(remote, "refs/heads/nope") is None


def test_remote_ref_oid_transport_error_raises_die(tmp_path):
    with pytest.raises(Die):
        gitutil.remote_ref_oid(str(tmp_path / "no-such-remote"), "refs/heads/topic")


# ----------------------------------------------------------------- safe_push


def test_safe_push_create_only_succeeds_when_absent(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    oid = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is True
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == oid


def test_safe_push_create_only_refused_when_ref_exists(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    a = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is True
    _commit(repo, "c2")
    # ref already exists on the remote: create-only must lose the CAS
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is False
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == a


def test_safe_push_cas_with_correct_expect(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    a = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is True
    b = _commit(repo, "c2")
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=a) is True
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == b


def test_safe_push_cas_stale_expect_loses_and_remote_unchanged(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    a = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is True
    b = _commit(repo, "c2")  # someone else advances the remote to b
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=a) is True
    _commit(repo, "c3")  # our new local work
    # lease still claims a, but the remote moved to b: CAS must lose
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=a) is False
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == b


def test_safe_push_noop_when_remote_equals_local(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    a = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=None) is True
    # remote already equals local: a no-op push is not progress
    assert gitutil.safe_push(repo, "topic", remote=remote, expect=a) is False
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == a


def test_safe_push_transport_error_raises_die(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(Die):
        gitutil.safe_push(repo, "topic", remote=str(tmp_path / "no-such-remote"), expect=None)


def test_safe_push_accepts_full_ref_name(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    remote = _make_bare(tmp_path / "remote.git")
    oid = gitutil.head_oid(repo)
    assert gitutil.safe_push(repo, "refs/heads/topic", remote=remote, expect=None) is True
    assert gitutil.remote_ref_oid(remote, "refs/heads/topic") == oid
