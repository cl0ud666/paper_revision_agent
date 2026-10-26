"""Check publishable files (and optionally Git history) without printing secrets.

Run from any directory: python scripts/check_repository.py [--history].
Ignored local files are deliberately not read. This is a focused publication
guard, not a substitute for a dedicated secret scanner or manual review.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".json",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".conf",
    ".example",
    ".sh",
    ".ps1",
}
SECRET_PATTERNS = {
    "API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "cloud access key": re.compile(r"\b(?:AKIA[0-9A-Z]{16}|LTAI[A-Za-z0-9]{16,})\b"),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        ((path.name == ".env" or path.name.startswith(".env.")) and path.name != ".env.example")
        or any(part in {"artifacts", "node_modules", ".venv", "__pycache__"} for part in path.parts)
        or (
            name.startswith(
                ("data/runs/", "data/checkpoints/", "data/web-storage/", "data/verification-")
            )
        )
        or path.suffix.lower()
        in {".pem", ".key", ".zip", ".tgz", ".sqlite", ".sqlite3", ".db", ".sql"}
    )


def findings(name: str, raw: bytes) -> list[str]:
    results = (
        [f"{name}: private/runtime file must not be published"] if forbidden_path(name) else []
    )
    if b"\0" in raw or len(raw) > 2_000_000:
        return results
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return results + [f"{name}: text is not valid UTF-8"]
    for number, line in enumerate(source.splitlines(), 1):
        if "\ufffd" in line:
            results.append(f"{name}:{number}: damaged text contains a replacement character")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                results.append(f"{name}:{number}: possible {label} (value redacted)")
    return results


def worktree_findings() -> list[str]:
    names = (
        git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        .decode("utf-8")
        .split("\0")
    )
    results = []
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if path.is_file():
            results.extend(findings(name, path.read_bytes()))
    return results


def history_findings() -> list[str]:
    # Only public branches/tags are publishable. Local editor recovery refs may
    # contain ignored artifacts and must not be included with `git push --mirror`.
    objects = (
        git("rev-list", "--objects", "HEAD", "--branches", "--tags").decode("utf-8").splitlines()
    )
    candidates = []
    for line in objects:
        oid, _, name = line.partition(" ")
        if name and (forbidden_path(name) or PurePosixPath(name).suffix in TEXT_SUFFIXES):
            candidates.append((oid, name))
    if not candidates:
        return []
    # A single process reads immutable objects, including old versions of files.
    output = subprocess.check_output(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input="".join(f"{oid}\n" for oid, _ in candidates).encode(),
    )
    results, offset = [], 0
    for oid, name in candidates:
        end = output.index(b"\n", offset)
        header = output[offset:end].split()
        size = int(header[2])
        raw = output[end + 1 : end + 1 + size]
        offset = end + size + 2
        if header[1] == b"blob":
            results.extend(findings(f"history:{oid[:12]}:{name}", raw))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also scan reachable Git history")
    args = parser.parse_args()
    results = worktree_findings()
    if args.history:
        results.extend(history_findings())
    for result in results:
        print(result)
    print(f"Publication checks: {len(results)} finding(s).")
    return 1 if results else 0


if __name__ == "__main__":
    raise SystemExit(main())
