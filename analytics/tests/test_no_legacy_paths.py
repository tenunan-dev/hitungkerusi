"""Assert that repository code contains no legacy absolute-path literals.

PLAN.md §7.4: after cutover, exactly ONE legacy path literal may exist in
repository code — the SOURCE_ROOT constant in the delivery publisher — and
after Phase 4 even that one points at the repository root instead.

This test enforces the invariant the plan asserts but never implemented.
Scope: executable code under this repository's automation directory. Data
files, reports and vendored content are excluded — they may legitimately
mention the legacy project by name in prose.
"""

import re
from pathlib import Path

import pytest

def _automation_root_from_test_file(test_file: Path) -> Path:
    """Return automation/ from a file located at automation/tests/."""
    return test_file.parent.parent


AUTOMATION_ROOT = _automation_root_from_test_file(Path(__file__).resolve())
REPOSITORY_ROOT = AUTOMATION_ROOT.parent

# The legacy project root, spelled exactly as it appears on disk.
LEGACY_LITERAL = "HermesWorkFolder/Malaysia General Election"

# Directories whose *code* must be free of legacy path literals.
CODE_ROOTS = [AUTOMATION_ROOT]

SKIP_DIR_PARTS = {
    "__pycache__", ".git", ".venv", "node_modules", ".vercel",
    ".claude", ".pytest_cache",
}

CODE_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".applescript"}


@pytest.mark.parametrize("repository_name", ["HERMES", "2_ANALYTICS"])
def test_automation_root_derivation_is_repository_name_independent(
    tmp_path, repository_name
):
    """The test location, rather than the repository name, identifies automation."""
    test_file = (
        tmp_path / repository_name / "automation" / "tests" / "test_no_legacy_paths.py"
    )
    test_file.parent.mkdir(parents=True)
    test_file.touch()

    assert _automation_root_from_test_file(test_file) == tmp_path / repository_name / "automation"


def _iter_code_files():
    for root in CODE_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in CODE_SUFFIXES:
                continue
            if SKIP_DIR_PARTS & set(p.parts):
                continue
            # This file necessarily contains the literal it searches for.
            if p.name == "test_no_legacy_paths.py":
                continue
            # Generated bundles are built artifacts, not authored code.
            if p.name in {"data.js", "report.js", "app-bundle.js",
                          "data.generated.js", "report.generated.js",
                          "dun-data.js"}:
                continue
            yield p


def test_code_files_exist_to_scan():
    """Guard: if the glob breaks, the other test would vacuously pass."""
    files = list(_iter_code_files())
    assert len(files) > 3, f"expected to scan several code files, got {len(files)}"


def test_no_legacy_path_literal_in_v2_code():
    """No V2 code file may hardcode the legacy project root.

    The V2 root string legitimately contains the legacy string as a prefix
    ('Malaysia General Election v2'), so a match only counts when it is NOT
    immediately followed by ' v2'.
    """
    offenders = []
    pattern = re.compile(re.escape(LEGACY_LITERAL) + r"(?! v2)")

    for path in _iter_code_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{lineno}: {line.strip()[:120]}"
                )

    assert not offenders, (
        "Repository code must not reference the legacy project root "
        f"(PLAN.md §7.4). Found {len(offenders)} occurrence(s):\n"
        + "\n".join(offenders)
    )
