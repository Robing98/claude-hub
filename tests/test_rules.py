from pathlib import Path

from claude_hub.collector.rulescan import scan_repo, scan_user
from claude_hub.rules import content_sha, describe, split_frontmatter, split_rules


def test_frontmatter_shapes():
    text = (
        "---\n"
        "name: qa-run\n"
        'description: "Manual QA: plan, test, report"\n'
        "paths:\n"
        '  - "src/**/*.php"\n'
        "  - tests/**\n"
        "globs: [a/*.ts, b/*.ts]\n"
        "long: >-\n"
        "  first part\n"
        "  second part\n"
        "---\n\n# Body\ntext\n"
    )
    fields, body = split_frontmatter(text)
    assert fields["name"] == "qa-run"
    assert fields["description"] == "Manual QA: plan, test, report"
    assert fields["paths"] == "src/**/*.php, tests/**"
    assert fields["globs"] == "a/*.ts, b/*.ts"
    assert fields["long"] == "first part second part"
    assert body == "# Body\ntext"


def test_text_without_frontmatter_is_untouched():
    assert split_frontmatter("# Title\n---\nnot frontmatter") == ({}, "# Title\n---\nnot frontmatter")
    assert split_frontmatter("---\nnever closed\n")[0] == {}


def test_split_rules_by_heading_and_skip_code_blocks():
    body = "Intro line\n\n# One\ntext one\n\n```bash\n# not a heading\n```\n\n## Two ##\ntext two\n"
    items = split_rules(body)
    assert [(i["heading"], i["level"]) for i in items] == [(None, 0), ("One", 1), ("Two", 2)]
    assert "# not a heading" in items[1]["content"]
    assert items[2]["content"] == "text two"


def test_reformatting_is_not_drift():
    assert content_sha("Use  tabs.\n\nAlways.") == content_sha("Use tabs. Always.")
    assert content_sha("Use tabs.") != content_sha("Use spaces.")


def test_describe_names():
    assert describe("skill", "skills/qa-run/SKILL.md", "body")["name"] == "qa-run"
    assert describe("skill", "skills/x/SKILL.md", "---\nname: real-name\n---\nbody")["name"] == "real-name"
    assert describe("rule", ".claude/rules/php/style.md", "# A\n# B")["name"] == "style"
    assert len(describe("rule", "r.md", "# A\nx\n# B\ny")["items"]) == 2
    # A skill is one unit, whatever headings it contains.
    assert len(describe("skill", "skills/x/SKILL.md", "# A\nx\n# B\ny")["items"]) == 1


def make(path: Path, text: str = "# Rule\ntext\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_scan_user_reads_only_instruction_files(tmp_path: Path):
    make(tmp_path / "CLAUDE.md")
    make(tmp_path / "rules" / "style" / "commits.md")
    make(tmp_path / "skills" / "qa-run" / "SKILL.md")
    make(tmp_path / "skills" / "qa-run" / "reference.md")
    make(tmp_path / "agents" / "reviewer.md")
    make(tmp_path / "commands" / "ship.md")
    make(tmp_path / "settings.json", '{"env": {"TOKEN": "secret"}}')
    make(tmp_path / ".credentials.json", '{"token": "secret"}')
    make(tmp_path / "projects" / "-x" / "notes.md")
    found = {(f["kind"], f["rel_path"]) for f in scan_user(tmp_path)}
    assert found == {
        ("claude_md", "CLAUDE.md"), ("rule", "rules/style/commits.md"),
        ("skill", "skills/qa-run/SKILL.md"), ("agent", "agents/reviewer.md"),
        ("command", "commands/ship.md"),
    }
    assert not any("secret" in f["content"] for f in scan_user(tmp_path))


def test_scan_repo(tmp_path: Path):
    make(tmp_path / "CLAUDE.md")
    make(tmp_path / "CLAUDE.local.md")
    make(tmp_path / ".claude" / "rules" / "tests.md", "---\npaths: tests/**\n---\n# T\n")
    make(tmp_path / ".claude" / "skills" / "release" / "SKILL.md")
    make(tmp_path / ".claude" / "settings.local.json", "{}")
    make(tmp_path / "frontend" / "CLAUDE.md")
    make(tmp_path / "node_modules" / "pkg" / "CLAUDE.md")
    # A linked worktree holds a second checkout of the same files.
    make(tmp_path / ".claude" / "worktrees" / "feature" / "CLAUDE.md")
    make(tmp_path / "nested-repo" / "CLAUDE.md")
    make(tmp_path / "nested-repo" / ".git", "gitdir: elsewhere")
    make(tmp_path / "big" / "CLAUDE.md", "x" * (600 * 1024))
    found = {(f["kind"], f["rel_path"]) for f in scan_repo(tmp_path)}
    assert found == {
        ("claude_md", "CLAUDE.md"), ("claude_md", "CLAUDE.local.md"),
        ("rule", ".claude/rules/tests.md"), ("skill", ".claude/skills/release/SKILL.md"),
        ("claude_md", "frontend/CLAUDE.md"),
    }
