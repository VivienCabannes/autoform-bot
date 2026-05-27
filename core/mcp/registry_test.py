# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for SkillRegistry."""

from pathlib import Path

import pytest

from core.mcp.registry import SkillEntry, SkillRegistry

ALL_SKILLS = ["coding-style", "git/clean-branch", "empty-desc"]


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a temporary skills directory with sample .md files."""
    # Top-level skill
    (tmp_path / "coding-style.md").write_text(
        "# Coding Style\n\nUse consistent formatting throughout.\n\n## Details\n\nMore info here.\n"
    )
    # Nested skill
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "clean-branch.md").write_text("# Clean Branch\n\nClean up after squash-merge.\n")
    # Skill with no description (only heading)
    (tmp_path / "empty-desc.md").write_text("# Just a Title\n")
    return tmp_path


class TestPopulate:
    def test_loads_specified_skills(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        assert set(reg.skills.keys()) == {"coding-style", "git/clean-branch", "empty-desc"}

    def test_loads_subset(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ["coding-style"])
        assert set(reg.skills.keys()) == {"coding-style"}

    def test_entries_are_frozen(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        entry = reg.skills["coding-style"]
        assert isinstance(entry, SkillEntry)
        with pytest.raises(AttributeError):
            entry.name = "changed"  # type: ignore[misc]

    def test_extracts_description(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        assert reg.skills["coding-style"].description == "Use consistent formatting throughout."
        assert reg.skills["git/clean-branch"].description == "Clean up after squash-merge."

    def test_empty_description_for_heading_only(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        assert reg.skills["empty-desc"].description == ""

    def test_skips_missing_files(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ["coding-style", "nonexistent"])
        assert set(reg.skills.keys()) == {"coding-style"}

    def test_empty_names_list(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, [])
        assert reg.skills == {}

    def test_repopulate_clears_old(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        assert len(reg.skills) == 3

        reg.populate(skills_dir, [])
        assert reg.skills == {}


class TestFormatting:
    def test_format_skill_list_empty(self) -> None:
        reg = SkillRegistry()
        assert reg.format_skill_list() == "No skills registered."

    def test_format_skill_list(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.format_skill_list()
        assert "# Available Skills" in result
        assert "**coding-style**" in result
        assert "**git/clean-branch**" in result
        assert "check_skills(name)" in result

    def test_format_skill_detail(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.format_skill_detail("coding-style")
        assert result.startswith("# Skill: coding-style")
        assert "Use consistent formatting throughout." in result

    def test_format_skill_detail_unknown(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        assert "Unknown skill" in reg.format_skill_detail("nope")

    def test_format_compact_summary(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.format_compact_summary()
        lines = result.strip().splitlines()
        assert len(lines) == 3
        assert all(line.startswith("- **") for line in lines)


class TestLookup:
    def test_exact_match(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.lookup("git/clean-branch")
        assert "# Skill: git/clean-branch" in result

    def test_fuzzy_suggestion(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.lookup("clean")
        assert "Did you mean" in result
        assert "git/clean-branch" in result

    def test_no_match(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.lookup("zzzzz")
        assert "Unknown skill" in result
        assert "list_skills()" in result

    def test_empty_name(self, skills_dir: Path) -> None:
        reg = SkillRegistry()
        reg.populate(skills_dir, ALL_SKILLS)
        result = reg.lookup("")
        assert "Unknown skill" in result
