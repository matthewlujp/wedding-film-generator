from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from wedding_film.openai_narrative_adapter import API_URL, DEFAULT_MODEL, DEFAULT_STORE

SECRET = "sk-test-do-not-leak-0123456789"


def run_cli(
    *args: str, env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    merged = {
        **os.environ,
        "WEDDING_FILM_OPENAI_STUB_TRANSPORT": "1",
        "OPENAI_API_KEY": SECRET,
        **(env or {}),
    }
    resolved_env = {key: value for key, value in merged.items() if value is not None}
    return subprocess.run(
        [str(executable), *args], check=False, capture_output=True, text=True, env=resolved_env
    )


def configured_workspace(tmp_path: Path, *, model: str) -> Path:
    workspace = tmp_path / "openai-narrative-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["narrative"] = {"name": "openai", "model": model, "prompt_version": "v1"}
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    return workspace


def test_default_settings_match_the_specified_launch_configuration() -> None:
    assert DEFAULT_MODEL == "gpt-5.6-luna"
    assert DEFAULT_STORE is False
    assert API_URL == "https://api.openai.com/v1/responses"


def test_success_generates_a_reviewable_candidate_with_no_secrets_leaked(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="stub-success")

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-ready"
    candidate_text = Path(payload["candidate"]).read_text(encoding="utf-8")
    assert SECRET not in candidate_text


def test_missing_api_key_maps_to_authentication_failure(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="stub-success")

    result = run_cli(
        "--project", str(workspace), "story", "generate", "--json",
        env={"OPENAI_API_KEY": None},
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_ADAPTER_FAILURE"


def test_refusal_maps_to_narrative_adapter_refusal(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="stub-refusal")

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_ADAPTER_REFUSAL"
