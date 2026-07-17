"""Fail checks when repository files appear to contain real credentials."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

_SECRET_PATTERNS = (
    (
        "provider API token",
        re.compile(r"\b(?:sk|ds)-[A-Za-z0-9]{24,}\b"),
    ),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
        ),
    ),
)


def detect_secret_kinds(text: str) -> set[str]:
    """Return credential categories without ever returning matched values."""
    return {name for name, pattern in _SECRET_PATTERNS if pattern.search(text)}


def _repository_files(root: Path) -> list[Path]:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("Git is required to run the repository secret scan.")

    result = subprocess.run(  # noqa: S603
        [
            git_executable,
            "-c",
            "core.excludesFile=NUL",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def scan_repository(root: Path) -> list[tuple[Path, set[str]]]:
    """Scan Git-visible text files and return paths plus non-sensitive findings."""
    findings: list[tuple[Path, set[str]]] = []
    for path in _repository_files(root):
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        kinds = detect_secret_kinds(text)
        if kinds:
            findings.append((path.relative_to(root), kinds))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan_repository(root)
    if not findings:
        print("Secret scan passed.")
        return 0

    print("Potential credentials found; values are intentionally hidden:", file=sys.stderr)
    for path, kinds in findings:
        print(f"- {path}: {', '.join(sorted(kinds))}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
