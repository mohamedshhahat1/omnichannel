"""Guardrail tests for ``.github/workflows/ci.yml``.

The CI workflow is validated here for the properties that matter: correct
paths, the supported Python version, the expected per-check commands and
working directories, lock-based dependency installation, and the PostgreSQL /
Redis service configuration. Plain-text assertions keep the suite hermetic and
add no dependencies; YAML parsing is covered by CI itself when the workflow
runs.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCK_PATH = REPO_ROOT / "backend" / "requirements.lock"
DOCKERFILE_PATH = REPO_ROOT / "backend" / "Dockerfile"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ROLES_SQL_PATH = REPO_ROOT / "infra" / "postgres" / "init" / "10-roles.sql"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_ci_workflow_file_and_referenced_paths_exist() -> None:
    for path in (WORKFLOW_PATH, LOCK_PATH, DOCKERFILE_PATH, COMPOSE_PATH, ROLES_SQL_PATH):
        assert path.is_file(), f"missing referenced path: {path.relative_to(REPO_ROOT)}"


def test_ci_triggers_cover_pull_requests_and_tracked_branches() -> None:
    workflow = _workflow()
    assert "pull_request:" in workflow
    assert "- main" in workflow
    assert "- phase-2-infrastructure" in workflow


def test_ci_uses_the_supported_python_version() -> None:
    # backend/pyproject.toml declares requires-python = ">=3.12,<3.14" and
    # backend/Dockerfile builds on python:3.13-slim; CI must use 3.13.
    assert 'python-version: "3.13"' in _workflow()


def test_ci_defines_each_quality_gate_as_its_own_job() -> None:
    workflow = _workflow()
    for job in (
        "ruff-lint:",
        "ruff-format:",
        "mypy:",
        "pytest-unit:",
        "pytest-integration:",
        "docker-build:",
        "compose-config:",
    ):
        assert job in workflow, f"missing CI job: {job}"


def test_ci_runs_the_expected_commands_from_the_backend_directory() -> None:
    workflow = _workflow()
    assert "working-directory: backend" in workflow
    for command in (
        "ruff check .",
        "ruff format --check .",
        "mypy",
        'pytest -m "not integration"',
        'pytest -m "integration"',
        "docker build -t omnichannel-backend:ci .",
        "docker compose config",
    ):
        assert command in workflow, f"missing CI command: {command}"


def test_ci_installs_dependencies_from_the_committed_lock() -> None:
    workflow = _workflow()
    assert "pip install -r requirements.lock" in workflow
    # The lint/format jobs derive the pinned tool version from the lock
    # instead of duplicating it in the workflow.
    assert "grep '^ruff==' requirements.lock" in workflow


def test_ci_provisions_real_postgresql_and_redis_services() -> None:
    workflow = _workflow()
    # Same images as docker-compose.yml; no SQLite, no fake Redis.
    assert "image: pgvector/pgvector:pg16" in workflow
    assert "image: redis:7-alpine" in workflow
    assert "5432:5432" in workflow
    assert "6379:6379" in workflow
    # The project's own configuration contract drives the integration tests.
    assert "OC_TEST_DATABASE_URL" in workflow
    assert "OC_TEST_REDIS_URL" in workflow
    assert "OC_DATABASE__MIGRATION_URL" in workflow


def test_ci_has_least_privilege_and_no_soft_failures() -> None:
    workflow = _workflow()
    assert "permissions:" in workflow
    assert "contents: read" in workflow
    assert "continue-on-error" not in workflow
