"""Architecture guard tests for R1-R5 and R10.

These complement the import-linter contracts in pyproject.toml.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def _python_files() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_env_access_only_in_config():
    offenders = []
    for path in _python_files():
        if path.name == "config.py" and path.parent.name == "core":
            continue
        if "os.environ" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def test_agents_never_import_other_agents():
    agent_root = APP / "agents"
    agent_packages = [p for p in agent_root.iterdir() if p.is_dir() and p.name != "prompts"]
    for package in agent_packages:
        for path in package.rglob("*.py"):
            for module in _imports(path):
                for other in agent_packages:
                    if other.name != package.name and module.startswith(f"app.agents.{other.name}"):
                        pytest.fail(f"{path} imports another agent: {module}")


def test_agents_never_import_db_layer():
    for path in (APP / "agents").rglob("*.py"):
        for module in _imports(path):
            assert not module.startswith("app.db"), f"{path} imports {module}"


def test_llm_provider_import_is_single_entry():
    llm_factory = APP / "utils" / "llm_factory.py"
    if not llm_factory.exists():
        pytest.skip("llm_factory.py arrives in Phase 2")
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        if "langchain_google_genai" in text and path != llm_factory:
            pytest.fail(f"{path} imports the Gemini provider directly")


def test_routes_do_not_import_models_directly():
    """Routes may use schemas and services; ORM models belong to services.

    deps.py is the one allowed exception: it loads the authenticated User.
    """
    for path in (APP / "api").rglob("*.py"):
        if path.name == "deps.py":
            continue
        for module in _imports(path):
            assert not module.startswith("app.db.models"), f"{path} imports {module}"
