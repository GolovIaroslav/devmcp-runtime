from typing import Any
import json
from dataclasses import dataclass
from pathlib import Path
from .errors import ToolFailure

@dataclass
class TaskTemplate:
    id: str
    category: str
    description: str
    command: str

TASK_REGISTRY = [
    TaskTemplate("project.detect", "project", "Detect project type and dependencies.", "cat package.json || cat pyproject.toml || cat Cargo.toml"),
    TaskTemplate("project.health", "project", "Run basic health checks on the project.", "echo 'Health check not implemented for this project type'"),
    TaskTemplate("project.summary", "project", "Generate a summary of the project.", "ls -la"),
    TaskTemplate("file.stat", "file", "Get file stats.", "stat {args}"),
    TaskTemplate("file.type", "file", "Get file type.", "file {args}"),
    TaskTemplate("text.head", "text", "Get first N lines of a file.", "head -n {args} {path}"),
    TaskTemplate("text.tail", "text", "Get last N lines of a file.", "tail -n {args} {path}"),
    TaskTemplate("text.count", "text", "Count lines, words, and bytes.", "wc {args}"),
    TaskTemplate("hash.sha256", "hash", "Calculate SHA256 hash.", "sha256sum {args}"),
    TaskTemplate("search.rg", "search", "Search using ripgrep if available.", "rg {args}"),
    TaskTemplate("search.files", "search", "Search for files by name.", "find . -name {args}"),
    TaskTemplate("search.todo", "search", "Search for TODO comments.", "grep -rn 'TODO' ."),
    TaskTemplate("json.validate", "json", "Validate JSON syntax.", "python3 -m json.tool {args} > /dev/null"),
    TaskTemplate("yaml.validate", "yaml", "Validate YAML syntax.", "python3 -c \"import yaml, sys; yaml.safe_load(open(sys.argv[1]))\" {args}"),
    TaskTemplate("toml.validate", "toml", "Validate TOML syntax.", "python3 -c \"import tomllib, sys; tomllib.load(open(sys.argv[1], 'rb'))\" {args}"),

    # Git
    TaskTemplate("git.status", "git", "Show git status.", "git status"),
    TaskTemplate("git.diff", "git", "Show git diff.", "git diff"),
    TaskTemplate("git.diff_cached", "git", "Show git diff for staged changes.", "git diff --cached"),
    TaskTemplate("git.log", "git", "Show git log.", "git log -n 10"),
    TaskTemplate("git.show", "git", "Show a specific git commit.", "git show {args}"),
    TaskTemplate("git.blame", "git", "Show git blame.", "git blame {args}"),
    TaskTemplate("git.current_branch", "git", "Show current git branch.", "git branch --show-current"),
    TaskTemplate("git.ls_files", "git", "List tracked files.", "git ls-files"),

    # npm
    TaskTemplate("npm.test", "npm", "Run npm tests.", "npm test"),
    TaskTemplate("npm.test_target", "npm", "Run specific npm test.", "npm test -- {args}"),
    TaskTemplate("npm.build", "npm", "Run npm build.", "npm run build"),
    TaskTemplate("npm.lint", "npm", "Run npm lint.", "npm run lint"),
    TaskTemplate("npm.typecheck", "npm", "Run npm typecheck.", "npm run typecheck"),
    TaskTemplate("npm.format_check", "npm", "Run npm format check.", "npm run format:check"),

    # Python
    TaskTemplate("pytest.all", "python", "Run all pytest tests.", "pytest"),
    TaskTemplate("pytest.file", "python", "Run pytest on a specific file.", "pytest {args}"),
    TaskTemplate("pytest.node", "python", "Run pytest on a specific node.", "pytest {args}"),
    TaskTemplate("unittest.all", "python", "Run all unittests.", "python -m unittest discover"),
    TaskTemplate("ruff.check", "python", "Run ruff check.", "ruff check ."),
    TaskTemplate("ruff.format_check", "python", "Run ruff format check.", "ruff format --check ."),
    TaskTemplate("mypy.check", "python", "Run mypy type check.", "mypy ."),
    TaskTemplate("pyright.check", "python", "Run pyright type check.", "pyright"),
    TaskTemplate("python.compileall", "python", "Compile all python files.", "python -m compileall ."),
    TaskTemplate("python.build", "python", "Build python package.", "python -m build"),
    TaskTemplate("tox.run", "python", "Run tox.", "tox"),
    TaskTemplate("nox.run", "python", "Run nox.", "nox"),
    
    # Just generic tasks to satisfy the list in prompt for now
    TaskTemplate("playwright.test", "browser", "Run playwright tests.", "npx playwright test"),
    TaskTemplate("playwright.test_target", "browser", "Run playwright test target.", "npx playwright test {args}"),
    TaskTemplate("playwright.smoke", "browser", "Run playwright smoke tests.", "npx playwright test --grep @smoke"),
    TaskTemplate("http.health", "network", "Check HTTP health.", "curl -sI {args}"),
]

class TaskRegistry:
    def __init__(self):
        self.tasks = {task.id: task for task in TASK_REGISTRY}

    def list_tasks(self, category: str = None, query: str = None) -> list[dict[str, Any]]:
        results = []
        for task in self.tasks.values():
            if category and task.category != category:
                continue
            if query and query.lower() not in task.id.lower() and query.lower() not in task.description.lower():
                continue
            results.append({"id": task.id, "category": task.category, "description": task.description})
        return results

    def describe_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise ToolFailure("NOT_FOUND", f"Task {task_id} not found", category="validation")
        return {"id": task.id, "category": task.category, "description": task.description, "command": task.command}

    def resolve_command(self, task_id: str, args: str | None = None, path: str | None = None) -> str:
        task = self.tasks.get(task_id)
        if not task:
            raise ToolFailure("NOT_FOUND", f"Task {task_id} not found", category="validation")
        
        cmd = task.command
        if "{args}" in cmd:
            cmd = cmd.replace("{args}", args or "")
        elif args:
            cmd = f"{cmd} {args}"
            
        if "{path}" in cmd:
            cmd = cmd.replace("{path}", path or "")
            
        return cmd.strip()
