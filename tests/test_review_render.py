"""Regression tests for the node-packet renderer's tolerance of source_refs shapes.

source_refs are verbatim citations: typically plain strings, but a structured
{file, location} dict must also render. A bad shape must never crash the packet.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm   # noqa: E402
import serve_review as sv   # noqa: E402


class _FakeProj:
    """Minimal Project stand-in: the packet meta only calls kernel_evidence()."""
    def kernel_evidence(self, node_id):
        return None


def _meta(node: dict) -> str:
    scorecard = rm.node_scorecard("n", rm.empty_sidecar())
    return sv._node_meta_html(_FakeProj(), "n", node, scorecard)


def test_string_source_refs_render():
    html = _meta({"kind": "theorem", "source_refs": ["High-Dim Stats, Thm 1.6"]})
    assert "High-Dim Stats, Thm 1.6" in html


def test_dict_source_refs_render():
    html = _meta({"kind": "theorem",
                  "source_refs": [{"file": "Foo.lean", "location": "line 12"}]})
    assert "Foo.lean" in html and "line 12" in html


def test_mixed_source_refs_do_not_crash():
    html = _meta({"kind": "theorem",
                  "source_refs": ["verbatim citation", {"file": "B.lean"}]})
    assert "verbatim citation" in html and "B.lean" in html


def test_no_source_refs_is_fine():
    html = _meta({"kind": "definition"})
    assert isinstance(html, str)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
