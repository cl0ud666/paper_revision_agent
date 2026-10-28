"""Build a source-only update with a SHA-256 manifest.

Usage: python scripts/package_agent_update.py [--output artifacts/update.zip]
Production configuration, user uploads and storage volumes are never included.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("app", "migrations", "frontend/src", "frontend/public")
REQUIRED_FILES = (
    "main.py",
    "alembic.ini",
    "requirements.txt",
    "requirements-web.txt",
    "deploy/Dockerfile",
    "deploy/frontend.Dockerfile",
    "deploy/minio.Dockerfile",
    "deploy/nginx.conf",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/index.html",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
)
EXCLUDED = {
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    "test-results",
    "playwright-report",
    ".git",
    ".venv",
}
SOURCE_SUFFIXES = {".py", ".tsx", ".ts", ".css", ".json", ".svg", ".html", ".png", ".ico"}


def source_files(root: Path) -> list[Path]:
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing required source files: " + ", ".join(missing))
    selected = {root / name for name in REQUIRED_FILES}
    for directory in SOURCE_DIRS:
        folder = root / directory
        for path in folder.rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and path.suffix in SOURCE_SUFFIXES
                and not EXCLUDED.intersection(relative.parts)
                and not any(part.startswith(".") for part in relative.parts)
            ):
                selected.add(path)
    return sorted(selected)


def build_bundle(root: Path, output: Path) -> dict:
    paths = source_files(root)
    manifest = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing an earlier release.
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            name = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            manifest[name] = sha256(raw).hexdigest()
            bundle.writestr(name, raw)
        bundle.writestr("agent-update-manifest.json", json.dumps(manifest, indent=2))
    with zipfile.ZipFile(output) as check:
        if check.testzip() is not None:
            raise RuntimeError("Update archive failed its CRC check")
    return {
        "path": str(output),
        "files": len(manifest),
        "bytes": output.stat().st_size,
        "sha256": sha256(output.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "artifacts"
        / f"paper_revision_agent-update-{datetime.now():%Y%m%d-%H%M%S}.zip",
    )
    args = parser.parse_args()
    print(json.dumps(build_bundle(ROOT, args.output.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
