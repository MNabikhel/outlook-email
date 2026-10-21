"""Settings (config.py) and setup: the .env file, paths, and the launcher's dependency check."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from controller_inbox.config import Settings


def test_azure_keys_written_in_dotenv_as_the_readme_says_are_read(tmp_path, monkeypatch):
    for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AZURE_CLIENT_ID=1111-2222\nAZURE_TENANT_ID=\nAZURE_CLIENT_SECRET=s3cret\n")
    settings = Settings()
    assert (settings.azure_client_id, settings.azure_tenant_id, settings.azure_client_secret) == ("1111-2222", "common", "s3cret")
    assert settings.graph_configured
    # The prefixed name wins over the bare one, for every key.
    (tmp_path / ".env").write_text("AZURE_TENANT_ID=contoso\nCONTROLLER_INBOX_AZURE_TENANT_ID=fabrikam\n")
    assert Settings().azure_tenant_id == "fabrikam"
    assert Settings(azure_client_id="x", _env_file=None).azure_client_id == "x"


def test_a_line_left_empty_in_dotenv_means_the_default_and_amounts_may_have_commas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CONTROLLER_INBOX_PORT=\nCONTROLLER_INBOX_WRITEBACK=\nCONTROLLER_INBOX_HIGH_AMOUNT=$25,000\n")
    settings = Settings()
    assert (settings.port, settings.writeback, settings.high_amount) == (8765, False, 25000.0)


def test_a_windows_path_in_double_quotes_is_refused_with_a_clear_message():
    with pytest.raises(ValueError, match="single quotes"):
        Settings(data_dir="C:\\Users\nancy\\CloseDesk data", _env_file=None)


ROOT = Path(__file__).resolve().parent.parent


# The launcher's dependency check notices an outdated package, not only a missing one.
def _check_deps():
    spec = importlib.util.spec_from_file_location("check_deps", ROOT / "scripts" / "check_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_deps_reports_missing_and_outdated_packages():
    deps = _check_deps()
    assert deps.problem("fastapi>=0.1") == ""
    assert deps.problem("fastapi>=999").startswith("Outdated: fastapi")
    assert deps.problem("surely-not-installed-pkg>=1").startswith("Missing")
    assert deps.problem("surely-not-installed-pkg>=1; sys_platform == 'plan9'") == ""
    assert deps.main() == 0


def test_check_deps_also_checks_the_launcher_extras_that_are_installed():
    """Raising an add-on's version brings a reinstall too, where the add-on is installed; an add-on that never
    installed here (its first package missing) is left out, so the launcher doesn't reinstall on every start."""
    deps = _check_deps()
    project = {"project": {
        "dependencies": ["fastapi>=0.1"],
        "optional-dependencies": {
            "ocr": ["fastapi>=0.1", "fastapi>=999"],
            "tables": ["surely-not-installed-pkg>=1", "fastapi>=999"],
            "dev": ["fastapi>=999"],
        },
    }}
    wanted = deps.requirements_to_check(project)
    assert wanted == ["fastapi>=0.1", "fastapi>=0.1", "fastapi>=999"], "ocr is installed; tables and dev aren't checked"
    assert [deps.problem(r) for r in wanted][-1].startswith("Outdated: fastapi")
    assert not deps.extra_installed(["surely-not-installed-pkg>=1"])
    assert not deps.extra_installed(["fastapi>=0.1; sys_platform == 'plan9'"])
    assert deps.extra_installed(["fastapi>=0.1"])
