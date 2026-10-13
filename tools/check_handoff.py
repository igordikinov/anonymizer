"""Validate portable Markdown navigation without running any setup."""
import re
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check(root):
    docs = [root / "AGENTS.md", root / "CLAUDE.md", root / "README.md",
            root / "docs/handoff-glm.md", root / "docs/start-glm.md",
            *sorted((root / ".agents/roles").glob("*.md")),
            *sorted((root / "docs/tasks").glob("GLM-*.md"))]
    checked = 0
    for file in docs:
        require(file.is_file(), f"Missing document: {file.relative_to(root)}")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", file.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "#")):
                continue
            target = target.split("#", 1)[0]
            resolved = (file.parent / target).resolve()
            require(resolved.is_relative_to(root), f"Nonportable link in {file.relative_to(root)}: {target}")
            require(resolved.exists(), f"Broken link in {file.relative_to(root)}: {target}")
            checked += 1
    require({p.stem for p in (root / ".agents/roles").glob("*.md")} == {"orchestrator", "planner", "worker", "code-reviewer", "verifier"}, "Canonical role set is incomplete")
    entry = (root / "CLAUDE.md").read_text(encoding="utf-8")
    require("AGENTS.md" in entry and "docs/handoff-glm.md" in entry and "docs/start-glm.md" in entry, "Shell entry does not point to canonical instructions")
    print(f"Handoff navigation: {len(docs)} documents, {checked} portable local links; canonical roles present")


if __name__ == "__main__":
    check(Path(__file__).resolve().parent.parent)
