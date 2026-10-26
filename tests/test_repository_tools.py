"""Publication safeguards and reproducible update contents."""

import json
import zipfile
from hashlib import sha256

import pytest

from scripts.check_repository import findings, forbidden_path
from scripts.package_agent_update import REQUIRED_FILES, build_bundle


def test_private_paths_are_blocked_but_templates_are_allowed():
    for name in (
        ".env",
        "frontend/.env.local",
        "deploy/.env",
        "artifacts/release.zip",
        "data/runs/private/doc.docx",
    ):
        assert forbidden_path(name)
    for name in (".env.example", "deploy/.env.example", "app/web/api.py"):
        assert not forbidden_path(name)


def test_secret_findings_never_print_the_value():
    token = "sk-" + "A1b2" * 12
    result = findings("example.py", f"KEY='{token}'".encode())
    assert result and "API key" in result[0]
    assert token not in "".join(result)


def test_update_includes_migrations_and_build_inputs_without_credentials(tmp_path):
    for name in REQUIRED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    migration = tmp_path / "migrations/versions/0003_example.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("upgrade = None", encoding="utf-8")
    for name in (".env", "deploy/.env", "frontend/src/.env.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    output = tmp_path / "release.zip"
    build_bundle(tmp_path, output)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert set(REQUIRED_FILES) <= names
        assert "migrations/versions/0003_example.py" in names
        assert not any(".env" in name for name in names)
        manifest = json.loads(archive.read("agent-update-manifest.json"))
        assert all(
            sha256(archive.read(name)).hexdigest() == digest for name, digest in manifest.items()
        )
    with pytest.raises(FileExistsError):
        build_bundle(tmp_path, output)


def test_update_fails_before_packaging_when_required_files_are_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Missing required"):
        build_bundle(tmp_path, tmp_path / "release.zip")
