from pathlib import Path

from slopcheck.private_registry import (
    npm_private_registry_context,
    npm_scope,
    pip_private_index_configured,
)


def test_pip_no_private_index_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is False


def test_pip_private_index_from_env_var(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://pypi.internal.example/simple")
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_directive(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("--extra-index-url https://pypi.internal.example/simple\nrequests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_pip_conf(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    pip_conf = tmp_path / "pip.conf"
    pip_conf.write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_conf))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_npm_no_private_registry_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_npm_scope_registry_from_npmrc(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npm_blanket_registry_from_env_var(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("npm_config_registry", "https://npm.internal.example/")

    blanket, _scopes = npm_private_registry_context([tmp_path])

    assert blanket is True


def test_npm_scope_helper():
    assert npm_scope("@acmecorp/widget") == "@acmecorp"
    assert npm_scope("requests") is None
    assert npm_scope("@acmecorp") is None
