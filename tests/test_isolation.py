"""Static checks that keep apps from stepping on each other inside one process.

1. Bare imports of an app's own sub-packages (`from db import x`): every project has a `db`,
   `config`, `auth_utils`… and `sys.modules` is shared, so the second app would silently get the
   first app's module. Inside apps/<name>/ use relative imports or `apps.<name>.…`.
2. Importing another app (`apps.other…`).
3. Reading the environment directly (os.environ / os.getenv): settings come only through the
   AppSettings that router/settings.py builds, with the app's prefix.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"


def _app_dirs():
    return [p for p in APPS.iterdir() if p.is_dir() and (p / "__init__.py").exists()]


def _own_top_names(app_dir: Path) -> set[str]:
    names = set()
    for child in app_dir.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            names.add(child.name)
        elif child.suffix == ".py" and child.stem != "__init__":
            names.add(child.stem)
    return names


def _problems(app_dir: Path) -> list[str]:
    own = _own_top_names(app_dir)
    problems = []
    for path in app_dir.rglob("*.py"):
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for mod in modules:
                top = mod.split(".")[0]
                if top in own:
                    problems.append(f"{rel}:{node.lineno} bare import of own module '{mod}'")
                if top == "apps" and not mod.startswith(f"apps.{app_dir.name}"):
                    problems.append(f"{rel}:{node.lineno} imports another app '{mod}'")
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    problems.append(f"{rel}:{node.lineno} reads os.{node.attr}; use AppSettings")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                if any(a.name in ("environ", "getenv") for a in node.names):
                    problems.append(f"{rel}:{node.lineno} imports os.environ/getenv; use AppSettings")
    return problems


def test_there_is_at_least_one_app():
    assert _app_dirs()


def test_apps_import_only_themselves_and_read_no_environment():
    problems = [p for d in _app_dirs() for p in _problems(d)]
    assert problems == [], "\n".join(problems)


def test_the_checker_catches_each_mistake(tmp_path, monkeypatch):
    """Guard the guard: a planted bad app must produce all three problems."""
    bad = tmp_path / "apps" / "bad"
    (bad / "db").mkdir(parents=True)
    (bad / "__init__.py").write_text("")
    (bad / "db" / "__init__.py").write_text("")
    (bad / "app.py").write_text(
        "import os\nfrom db import connection\nimport apps.diary.app\nx = os.environ['X']\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    found = _problems(bad)
    assert any("bare import" in p for p in found)
    assert any("another app" in p for p in found)
    assert any("os.environ" in p for p in found)
