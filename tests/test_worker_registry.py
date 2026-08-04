"""Agent-role registry tests — the plugin's extension point.

Everything is offline and confined to tmp_path: synthetic plugin roots hold the
role Markdown, the dispatch project is synthesized, and the only real-repo test
reads the shipped ``agents/*.md`` files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_worker import agent_work, registry
from autoform_worker.config import resolve_config
from autoform_worker.constants import MAX_AGENT_ATTEMPTS
from autoform_worker.counters import Counters
from autoform_worker.registry import ENGINE_KINDS, Registry
from autoform_worker.survey import Survey

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The kinds the shipped plugin must expose today (8 role files + 2 engine kinds).
SHIPPED_KINDS = (
    "reviewer", "worker", "planner", "mathcheck", "graphreview",
    "contentreview", "holistic", "escalation", "counterexample", "priorart",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient role dirs and no ambient project may leak into discovery."""
    monkeypatch.delenv("AUTOFORM_AGENT_PATH", raising=False)
    monkeypatch.delenv("AUTOFORM_DISPATCH_PROJECT", raising=False)


def write_role(directory: Path, filename: str, front: str, body: str = "# Do the work\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(f"---\n{front.strip()}\n---\n{body}", encoding="utf-8")
    return path


def make_project(tmp_path, monkeypatch, tasks=None, raw_queue=None, worker_id="w1"):
    """A minimal dispatch project (graph + optional queue) plus its resolved config."""
    lean = tmp_path / "lean"
    lean.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "graph.json").write_text(json.dumps({
        "version": 2,
        "metadata": {"lean_root": str(lean), "sources": [{"file": "book.pdf"}]},
        "nodes": {"n1": {"tier": 1, "depends_on": []}},
    }), encoding="utf-8")
    if raw_queue is not None:
        (proj / "task_queue.json").write_text(raw_queue, encoding="utf-8")
    elif tasks is not None:
        (proj / "task_queue.json").write_text(json.dumps(tasks), encoding="utf-8")
    monkeypatch.setenv("AUTOFORM_DISPATCH_PROJECT", str(proj))
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "autoform-config.json"))
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(tmp_path / "remotes"))
    monkeypatch.delenv("AUTOFORM_RESPECT_CLAIMS", raising=False)
    return resolve_config(worker_id=worker_id)


# -- _parse_frontmatter ------------------------------------------------------

def test_parse_frontmatter_simple_keys():
    fm = registry._parse_frontmatter("---\nname: planner\nicon: ◷\napplies: tier1\n---\nbody\n")
    assert fm == {"name": "planner", "icon": "◷", "applies": "tier1"}


def test_parse_frontmatter_folded_block_spans_lines():
    text = (
        "---\n"
        "name: counterexample-hunter\n"
        "description: >\n"
        "  Tries to REFUTE a node's statement\n"
        "  before anyone spends compute.\n"
        "kind: counterexample\n"
        "---\n"
        "body\n"
    )
    fm = registry._parse_frontmatter(text)
    assert fm["description"] == "Tries to REFUTE a node's statement before anyone spends compute."
    assert fm["kind"] == "counterexample"
    assert fm["name"] == "counterexample-hunter"


def test_parse_frontmatter_pipe_block_and_dash_variants():
    text = "---\nblurb: |-\n  line one\n  line two\nkind: k\n---\nbody\n"
    fm = registry._parse_frontmatter(text)
    assert fm == {"blurb": "line one line two", "kind": "k"}


def test_parse_frontmatter_missing_frontmatter_is_empty():
    assert registry._parse_frontmatter("# just markdown\n\nno frontmatter here\n") == {}


def test_parse_frontmatter_unterminated_block_is_empty():
    assert registry._parse_frontmatter("---\nname: planner\nkind: planner\n") == {}


def test_parse_frontmatter_value_keeps_its_colon():
    fm = registry._parse_frontmatter("---\nblurb: split: check: wire\n---\nbody\n")
    assert fm["blurb"] == "split: check: wire"


# -- discover() over a synthetic plugin root ---------------------------------

def test_discover_uses_declared_kind_not_filename(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "counterexample-hunter.md", """
name: counterexample-hunter
description: refute before proving
kind: counterexample
label: Counterexample
icon: ⚂
blurb: try to refute this statement
applies: tier2
drained_by: agent
writes: graph
""")
    roles = registry.discover(tmp_path / "plugin")
    assert "counterexample" in roles
    assert "counterexample-hunter" not in roles
    role = roles["counterexample"]
    assert (role.name, role.kind, role.label, role.icon) == (
        "counterexample-hunter", "counterexample", "Counterexample", "⚂")
    assert (role.blurb, role.applies, role.drained_by, role.writes) == (
        "try to refute this statement", "tier2", "agent", "graph")
    assert role.source == "plugin" and role.queueable is True


def test_discover_defaults_kind_to_stem_and_drained_by_agent(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "tidier.md", "name: tidier\ndescription: tidies things up")
    roles = registry.discover(tmp_path / "plugin")
    role = roles["tidier"]
    assert role.kind == "tidier"
    assert role.drained_by == "agent"
    assert role.icon == "◆" and role.applies == "any" and role.writes == "none"
    assert role.label == "" and role.blurb == ""
    assert registry.agent_kinds(tmp_path / "plugin") == ("tidier",)


def test_discover_excludes_drained_by_none(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "helper.md", "name: helper\ndescription: a sub-step\ndrained_by: none")
    write_role(agents_dir, "tidier.md", "name: tidier\ndescription: tidies")
    roles = registry.discover(tmp_path / "plugin")
    assert "helper" not in roles
    assert "tidier" in roles


def test_discover_excludes_non_queue_subroles(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    for name in sorted(registry._NON_QUEUE_ROLES):
        write_role(agents_dir, f"{name}.md", f"name: {name}\ndescription: a sub-step")
    roles = registry.discover(tmp_path / "plugin")
    assert set(roles) == set(ENGINE_KINDS)


def test_discover_includes_subrole_when_it_declares_kind_or_drained_by(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "splitter.md", "name: splitter\ndescription: splits\nkind: split")
    write_role(agents_dir, "source-searcher.md",
               "name: source-searcher\ndescription: searches sources\ndrained_by: agent")
    roles = registry.discover(tmp_path / "plugin")
    assert "split" in roles and roles["split"].name == "splitter"
    assert "source-searcher" in roles
    assert roles["source-searcher"].drained_by == "agent"


def test_engine_kinds_exist_with_empty_agents_dir(tmp_path):
    (tmp_path / "plugin" / "agents").mkdir(parents=True)
    roles = registry.discover(tmp_path / "plugin")
    assert set(roles) == set(ENGINE_KINDS)
    for kind in ENGINE_KINDS:
        assert roles[kind].drained_by == "engine"
        assert roles[kind].source == "builtin"
        assert roles[kind].blurb
    assert registry.agent_kinds(tmp_path / "plugin") == ()


def test_engine_kinds_exist_with_no_agents_dir_at_all(tmp_path):
    roles = registry.discover(tmp_path / "plugin")
    assert set(roles) == set(ENGINE_KINDS)


def test_role_file_may_override_a_builtin_engine_kind(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "autoform-worker.md",
               "name: autoform-worker\ndescription: prover\nkind: worker\nlabel: Prover")
    roles = registry.discover(tmp_path / "plugin")
    assert roles["worker"].name == "autoform-worker"
    assert roles["worker"].label == "Prover"
    assert roles["worker"].drained_by == "engine"      # implied by the engine kind
    assert roles["worker"].source == "plugin"


def test_body_returns_markdown_after_frontmatter_verbatim(tmp_path):
    body = "# Mission\n\nDo X, then Y:\n\n- step one\n- step two\n"
    path = write_role(tmp_path / "plugin" / "agents", "tidier.md",
                      "name: tidier\ndescription: tidies", body=body)
    roles = registry.discover(tmp_path / "plugin")
    assert roles["tidier"].body() == body
    assert roles["tidier"].path == path


def test_discover_skips_an_unreadable_role_file(tmp_path):
    """One bad role file must not take down discovery for every other role.

    ``discover()`` guards the read with ``except OSError: continue`` — but a role
    file saved in a non-UTF-8 encoding (cp1252 is easy to hit: these files are full
    of ⚂/🔭/— characters) raises UnicodeDecodeError, which that guard misses.
    """
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "good.md", "name: good\ndescription: fine\nkind: good")
    agents_dir.joinpath("bad.md").write_bytes(
        b"---\nname: bad\ndescription: caf\xe9 role\nkind: bad\n---\nbody\n")
    roles = registry.discover(tmp_path / "plugin")
    assert "good" in roles


def test_body_of_file_without_frontmatter_is_whole_file(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "bare.md").write_text("just instructions\n", encoding="utf-8")
    roles = registry.discover(tmp_path / "plugin")
    assert roles["bare"].body() == "just instructions\n"


# -- project-local overrides -------------------------------------------------

def test_project_role_overrides_plugin_role_of_same_kind(tmp_path):
    write_role(tmp_path / "plugin" / "agents", "holistic-reviewer.md",
               "name: holistic-reviewer\ndescription: shipped\nkind: holistic\nlabel: Holistic")
    write_role(tmp_path / "proj" / ".autoform" / "agents", "holistic-reviewer.md",
               "name: strict-holistic\ndescription: stricter\nkind: holistic\nlabel: Strict holistic")
    roles = registry.discover(tmp_path / "plugin", tmp_path / "proj")
    assert roles["holistic"].source == "project"
    assert roles["holistic"].name == "strict-holistic"
    assert roles["holistic"].label == "Strict holistic"
    # ...and without the project the shipped role still wins.
    assert registry.discover(tmp_path / "plugin")["holistic"].source == "plugin"


def test_project_local_new_kind_adds_to_the_set(tmp_path):
    write_role(tmp_path / "plugin" / "agents", "planner.md", "name: planner\ndescription: plans")
    write_role(tmp_path / "proj" / ".autoform" / "agents", "numerics.md",
               "name: numerics-auditor\ndescription: audits numerics\nkind: numerics")
    roles = registry.discover(tmp_path / "plugin", tmp_path / "proj")
    assert roles["numerics"].source == "project"
    assert registry.agent_kinds(tmp_path / "plugin", tmp_path / "proj") == ("numerics", "planner")


def test_agent_path_env_adds_role_dirs(tmp_path, monkeypatch):
    write_role(tmp_path / "plugin" / "agents", "planner.md", "name: planner\ndescription: plans")
    extra = tmp_path / "extra-roles"
    write_role(extra, "sitewide.md", "name: sitewide\ndescription: site-wide role")
    assert "sitewide" not in registry.discover(tmp_path / "plugin")
    monkeypatch.setenv("AUTOFORM_AGENT_PATH", str(extra))
    roles = registry.discover(tmp_path / "plugin")
    assert roles["sitewide"].source == "project"
    assert set(registry.agent_kinds(tmp_path / "plugin")) == {"planner", "sitewide"}


# -- Registry / palette ------------------------------------------------------

def test_registry_get_and_agent_kinds(tmp_path):
    write_role(tmp_path / "plugin" / "agents", "planner.md", "name: planner\ndescription: plans")
    reg = Registry(tmp_path / "plugin")
    assert reg.get("planner").name == "planner"
    assert reg.get("nope") is None
    assert reg.agent_kinds() == ("planner",)
    assert set(reg.roles) == {"planner", *ENGINE_KINDS}


def test_palette_shape_order_and_unique_ids(tmp_path):
    agents_dir = tmp_path / "plugin" / "agents"
    write_role(agents_dir, "zeta.md", "name: zeta\ndescription: z role\nkind: zeta")
    write_role(agents_dir, "alpha.md", "name: alpha-role\ndescription: a role\nkind: alpha")
    entries = Registry(tmp_path / "plugin").palette()
    ids = [e["id"] for e in entries]
    assert ids == ["reviewer", "worker", "alpha", "zeta"]     # engine kinds sort first
    assert len(ids) == len(set(ids))
    for entry in entries:
        assert {"id", "label", "icon", "blurb", "applies"} <= set(entry)
        assert all(entry[key] for key in ("id", "label", "icon", "blurb", "applies"))
    by_id = {e["id"]: e for e in entries}
    assert by_id["alpha"]["label"] == "Alpha role"            # derived from name
    assert by_id["alpha"]["blurb"] == "a role"                # derived from description


def test_palette_of_real_plugin_is_well_formed():
    entries = Registry(REPO_ROOT).palette()
    ids = [e["id"] for e in entries]
    assert ids[:2] == sorted(ENGINE_KINDS)
    assert len(ids) == len(set(ids))
    assert set(SHIPPED_KINDS) <= set(ids)
    assert all(e["applies"] in {"any", "tier1", "tier2"} for e in entries)


# -- the real shipped role files --------------------------------------------

def test_shipped_kinds_are_all_discovered():
    roles = registry.discover(REPO_ROOT)
    assert set(SHIPPED_KINDS) <= set(roles)
    for kind in ENGINE_KINDS:
        assert roles[kind].drained_by == "engine"
    for kind in set(SHIPPED_KINDS) - set(ENGINE_KINDS):
        assert roles[kind].drained_by == "agent"
    assert set(registry.agent_kinds(REPO_ROOT)) == set(SHIPPED_KINDS) - set(ENGINE_KINDS)


def test_every_shipped_role_declaring_a_kind_has_a_body():
    roles = registry.discover(REPO_ROOT)
    declared = 0
    for path in sorted((REPO_ROOT / "agents").glob("*.md")):
        fm = registry._parse_frontmatter(path.read_text(encoding="utf-8"))
        if "kind" not in fm:
            continue
        declared += 1
        kind = fm["kind"]
        assert kind in roles, f"{path.name} declares kind {kind!r} but it is not discovered"
        assert roles[kind].body().strip(), f"{path.name}: empty role body"
    assert declared >= 8


def test_shipped_sub_roles_are_not_queue_kinds():
    roles = registry.discover(REPO_ROOT)
    assert "splitter" not in roles
    assert "source-searcher" not in roles
    assert "proof-strategy-researcher" not in roles


def test_recovery_outcome_reads_final_marker(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "considered RECOVERY: RETRY in the instructions\n"
        "RECOVERY: RETRY - provisional\n"
        "RECOVERY: PARK - no defensible route\n",
        encoding="utf-8",
    )
    assert agent_work._recovery_outcome(log) == "PARK"


# -- agent_work.queued_agent_tasks ------------------------------------------

def queue_task(task_id, agent, node, status="queued", note=""):
    return {"id": task_id, "agent": agent, "node": node, "node_label": f"{node} label",
            "status": status, "note": note}


def test_queued_agent_tasks_filters_and_orders(tmp_path, monkeypatch):
    tasks = [
        queue_task("holistic:g", "holistic", "g"),
        queue_task("worker:n1", "worker", "n1"),                     # engine kind
        queue_task("reviewer:n1", "reviewer", "n1"),                 # engine kind
        queue_task("planner:c2", "planner", "c2"),
        queue_task("escalation:n9", "escalation", "n9", note="prover hit a wall"),
        queue_task("planner:c1", "planner", "c1"),
        queue_task("mathcheck:n3", "mathcheck", "n3", status="done"),
        queue_task("graphreview:t1", "graphreview", "t1", status="running"),
        queue_task("bogus:n4", "bogus", "n4"),                       # unknown kind
    ]
    cfg = make_project(tmp_path, monkeypatch, tasks=tasks)
    out = agent_work.queued_agent_tasks(cfg, Registry(REPO_ROOT))
    assert [(t.kind, t.node) for t in out] == [
        ("escalation", "n9"), ("planner", "c1"), ("planner", "c2"), ("holistic", "g")]
    assert out[0].task_id == "escalation:n9"
    assert out[0].note == "prover hit a wall"
    assert out[0].node_label == "n9 label"


def test_queued_agent_tasks_unknown_kinds_sort_last(tmp_path, monkeypatch):
    tasks = [
        queue_task("numerics:n1", "numerics", "n1"),
        queue_task("holistic:g", "holistic", "g"),
        queue_task("escalation:n2", "escalation", "n2"),
    ]
    cfg = make_project(tmp_path, monkeypatch, tasks=tasks)
    write_role(cfg.project / ".autoform" / "agents", "numerics.md",
               "name: numerics\ndescription: audits numerics\nkind: numerics")
    out = agent_work.queued_agent_tasks(cfg, Registry(REPO_ROOT, cfg.project))
    assert [t.kind for t in out] == ["escalation", "holistic", "numerics"]


def test_queued_agent_tasks_missing_queue_is_empty(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch)
    assert agent_work.queued_agent_tasks(cfg, Registry(REPO_ROOT)) == []


def test_queued_agent_tasks_corrupt_queue_is_empty(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, raw_queue="{not json at all")
    assert agent_work.queued_agent_tasks(cfg, Registry(REPO_ROOT)) == []


def test_queued_agent_tasks_non_array_queue_is_empty(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, raw_queue=json.dumps({"id": "x"}))
    assert agent_work.queued_agent_tasks(cfg, Registry(REPO_ROOT)) == []


# -- agent_work.agent_candidates --------------------------------------------

def test_agent_candidates_ready(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, tasks=[queue_task("planner:c1", "planner", "c1")])
    ready, held = agent_work.agent_candidates(cfg, Registry(REPO_ROOT), Counters(cfg.counters_path), {})
    assert held == []
    assert len(ready) == 1
    assert ready[0].kind == "planner" and ready[0].node == "c1"
    assert ready[0].task.task_id == "planner:c1"
    assert ready[0].reason == "queued planner task"


def test_agent_candidates_suppressed_by_live_peer_claim(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, tasks=[queue_task("planner:c1", "planner", "c1")])
    live_foreign = {"task/planner/planner-c1": {"worker": "other"}}
    ready, held = agent_work.agent_candidates(
        cfg, Registry(REPO_ROOT), Counters(cfg.counters_path), live_foreign)
    assert ready == []
    assert [c.reason for c in held] == ["claimed by peer"]


def test_agent_candidates_claim_key_colon_normalized(tmp_path, monkeypatch):
    """A re-enqueued id (``planner:c1:2``) claims under a colon-free ref name."""
    cfg = make_project(tmp_path, monkeypatch, tasks=[queue_task("planner:c1:2", "planner", "c1")])
    reg, counters = Registry(REPO_ROOT), Counters(cfg.counters_path)
    ready, held = agent_work.agent_candidates(cfg, reg, counters, {"task/planner/planner-c1-2": {}})
    assert ready == [] and [c.reason for c in held] == ["claimed by peer"]
    ready, held = agent_work.agent_candidates(cfg, reg, counters, {"task/planner/planner:c1:2": {}})
    assert held == [] and len(ready) == 1


def test_agent_candidates_suppressed_by_attempt_budget(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, tasks=[
        queue_task("planner:c1", "planner", "c1"),
        queue_task("holistic:g", "holistic", "g"),
    ])
    counters = Counters(cfg.counters_path)
    for _ in range(MAX_AGENT_ATTEMPTS):
        counters.bump("agent-planner-c1")
    ready, held = agent_work.agent_candidates(cfg, Registry(REPO_ROOT), counters, {})
    assert [c.kind for c in ready] == ["holistic"]
    assert [(c.kind, c.reason) for c in held] == [("planner", "attempt budget spent")]


def test_agent_candidates_one_below_budget_is_still_ready(tmp_path, monkeypatch):
    cfg = make_project(tmp_path, monkeypatch, tasks=[queue_task("planner:c1", "planner", "c1")])
    counters = Counters(cfg.counters_path)
    for _ in range(MAX_AGENT_ATTEMPTS - 1):
        counters.bump("agent-planner-c1")
    ready, held = agent_work.agent_candidates(cfg, Registry(REPO_ROOT), counters, {})
    assert len(ready) == 1 and held == []


# -- agent_work.build_prompt -------------------------------------------------

def make_prompt(tmp_path, monkeypatch, body, note=""):
    cfg = make_project(tmp_path, monkeypatch)
    write_role(tmp_path / "plugin" / "agents", "auditor.md",
               "name: numerics-auditor\ndescription: audits\nkind: numerics", body=body)
    role = registry.discover(tmp_path / "plugin")["numerics"]
    task = agent_work.QueuedTask(task_id="numerics:n1", kind="numerics", node="n1",
                                 node_label="Node one", note=note)
    survey = Survey(canonical="o/r", default_branch="main", me="me")
    return cfg, role, agent_work.build_prompt(role, task, cfg, survey)


def test_build_prompt_carries_role_body_and_context(tmp_path, monkeypatch):
    body = "# Numerics audit\n\nCheck every constant: twice.\n"
    cfg, role, prompt = make_prompt(tmp_path, monkeypatch, body)
    assert prompt.startswith(body.strip())
    assert body.strip() in prompt
    assert "`numerics-auditor` (queue kind `numerics`)" in prompt
    assert "`n1` (Node one)" in prompt
    assert str(cfg.graph_path) in prompt
    assert str(cfg.lean_root) in prompt
    assert str(cfg.project / "informal_content") in prompt
    assert "- sources:" in prompt and "book.pdf" in prompt


def test_build_prompt_states_the_write_protocol(tmp_path, monkeypatch):
    cfg, _role, prompt = make_prompt(tmp_path, monkeypatch, "# Role\n")
    assert f"python {cfg.plugin_root}/scripts/merge_node.py {cfg.graph_path} --payload <file>" in prompt
    assert "only writer of graph.json" in prompt
    assert "Do NOT run `git push`" in prompt
    assert "do NOT open PRs" in prompt
    assert "FAILED:" in prompt


def test_build_prompt_includes_task_note_verbatim(tmp_path, monkeypatch):
    _cfg, _role, prompt = make_prompt(tmp_path, monkeypatch, "# Role\n", note="rw_pow failed: no lemma")
    assert "rw_pow failed: no lemma" in prompt
    assert "Task note" in prompt


def test_build_prompt_omits_note_section_when_empty(tmp_path, monkeypatch):
    _cfg, _role, prompt = make_prompt(tmp_path, monkeypatch, "# Role\n")
    assert "Task note" not in prompt
