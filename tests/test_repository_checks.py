"""Publication guards must reject damaged text without exposing secrets."""

from scripts.check_repository import findings


def test_valid_chinese_source_is_publishable():
    assert findings("app/example.py", 'title = "论文修改"'.encode()) == []


def test_invalid_encoding_and_replacement_characters_are_rejected():
    assert "UTF-8" in findings("app/example.py", b"\xff")[0]
    assert "replacement character" in findings("app/example.py", "\ufffd".encode())[0]


def test_secret_values_are_redacted_in_findings():
    secret = "sk-" + "x" * 30
    result = findings("app/example.py", secret.encode())
    assert len(result) == 1
    assert secret not in result[0]


def test_env_and_runtime_paths_are_rejected_even_without_secrets():
    assert findings("deploy/.env", b"DEBUG=1")
    assert findings("data/runs/example/state.json", b"{}")
    assert findings("deploy/.env.example", b"DEBUG=0") == []
