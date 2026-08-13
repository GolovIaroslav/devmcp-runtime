from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ToolFailure


@dataclass
class TaskTemplate:
    id: str
    category: str
    description: str
    executable: str
    argv: list[str] = field(default_factory=list)
    arg_definitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    network_requirement: bool = False
    approval_class: str = "ALLOW"
    cwd_policy: str = "workspace_root"

    @property
    def command_args(self) -> list[str]:
        """Compatibility view; execution always consumes executable + argv."""
        return [self.executable, *self.argv]


_COMMON_ARGUMENTS = {
    "args": {"type": "string_array", "required": False},
    "path": {"type": "path", "required": False},
}


def _task(
    task_id: str,
    category: str,
    description: str,
    executable: str,
    argv: list[str] | None = None,
    *,
    network: bool = False,
    approval: str = "ALLOW",
    cwd: str = "workspace_root",
    arguments: dict[str, dict[str, Any]] | None = None,
) -> TaskTemplate:
    return TaskTemplate(
        task_id,
        category,
        description,
        executable,
        list(argv or []),
        dict(arguments or _COMMON_ARGUMENTS),
        network,
        approval,
        cwd,
    )


TASK_REGISTRY = [
    _task("test.echo", "test", "Safe echo test task.", "echo", ["hello"]),
    _task(
        "test.dummy",
        "test",
        "Safe dummy test task.",
        "python3",
        ["-c", "print('dummy_task_ok')"],
    ),
    _task(
        "project.detect",
        "project",
        "Detect project type.",
        "python3",
        [
            "-c",
            "import os; print('package.json' if os.path.exists('package.json') else 'pyproject.toml')",
        ],
    ),
    _task("project.summary", "project", "Summary of project files.", "ls", ["-la"]),
    _task("file.stat", "file", "Get file stats.", "stat"),
    _task("file.type", "file", "Get file type.", "file"),
    _task("text.head", "text", "Get first N lines of a file.", "head", ["-n"]),
    _task("text.tail", "text", "Get last N lines of a file.", "tail", ["-n"]),
    _task("text.count", "text", "Count lines, words, and bytes.", "wc"),
    _task("hash.sha256", "hash", "Calculate SHA256 hash.", "sha256sum"),
    _task("search.rg", "search", "Search using ripgrep.", "rg"),
    _task("search.files", "search", "Search for files.", "find", [".", "-name"]),
    _task("search.todo", "search", "Search for TODO.", "grep", ["-rn", "TODO", "."]),
    _task(
        "json.validate", "json", "Validate JSON syntax.", "python3", ["-m", "json.tool"]
    ),
    _task(
        "yaml.validate",
        "yaml",
        "Validate YAML syntax.",
        "python3",
        ["-c", "import yaml, sys; yaml.safe_load(open(sys.argv[1]))"],
    ),
    _task(
        "toml.validate",
        "toml",
        "Validate TOML syntax.",
        "python3",
        ["-c", "import tomllib, sys; tomllib.load(open(sys.argv[1], 'rb'))"],
    ),
    _task("npm.test", "npm", "Run npm tests.", "npm", ["test"]),
    _task("npm.test_target", "npm", "Run a specific npm test.", "npm", ["test", "--"]),
    _task("npm.build", "npm", "Run npm build.", "npm", ["run", "build"]),
    _task("npm.lint", "npm", "Run npm lint.", "npm", ["run", "lint"]),
    _task("npm.typecheck", "npm", "Run npm typecheck.", "npm", ["run", "typecheck"]),
    _task(
        "npm.format_check",
        "npm",
        "Run npm format check.",
        "npm",
        ["run", "format:check"],
    ),
    _task(
        "npm.install",
        "npm",
        "Install npm dependencies with an approved network grant.",
        "npm",
        ["install"],
        network=True,
        approval="ASK",
    ),
    _task("npm.audit", "npm", "Run npm audit.", "npm", ["audit"]),
    _task("pnpm.test", "pnpm", "Run pnpm tests.", "pnpm", ["test"]),
    _task("pnpm.build", "pnpm", "Run pnpm build.", "pnpm", ["run", "build"]),
    _task("pnpm.lint", "pnpm", "Run pnpm lint.", "pnpm", ["run", "lint"]),
    _task(
        "pnpm.install",
        "pnpm",
        "Install pnpm dependencies with an approved network grant.",
        "pnpm",
        ["install"],
        network=True,
        approval="ASK",
    ),
    _task("yarn.test", "yarn", "Run yarn tests.", "yarn", ["test"]),
    _task("yarn.build", "yarn", "Run yarn build.", "yarn", ["run", "build"]),
    _task("yarn.lint", "yarn", "Run yarn lint.", "yarn", ["run", "lint"]),
    _task(
        "yarn.install",
        "yarn",
        "Install yarn dependencies with an approved network grant.",
        "yarn",
        ["install"],
        network=True,
        approval="ASK",
    ),
    _task("bun.test", "bun", "Run bun tests.", "bun", ["test"]),
    _task("bun.build", "bun", "Run bun build.", "bun", ["run", "build"]),
    _task(
        "bun.install",
        "bun",
        "Install bun dependencies with an approved network grant.",
        "bun",
        ["install"],
        network=True,
        approval="ASK",
    ),
    _task("vitest.run", "javascript", "Run Vitest tests.", "vitest", ["run"]),
    _task("jest.run", "javascript", "Run Jest tests.", "jest"),
    _task("pytest.all", "python", "Run all pytest tests.", "python3", ["-m", "pytest"]),
    _task(
        "pytest.file",
        "python",
        "Run pytest on a file.",
        "python3",
        ["-m", "pytest"],
        arguments={
            "args": {"type": "string_array", "required": False},
            "path": {"type": "path", "required": True},
        },
    ),
    _task(
        "unittest.all",
        "python",
        "Run all unittests.",
        "python3",
        ["-m", "unittest", "discover"],
    ),
    _task("ruff.check", "python", "Run ruff check.", "ruff", ["check", "."]),
    _task(
        "ruff.format_check",
        "python",
        "Run ruff format check.",
        "ruff",
        ["format", "--check", "."],
    ),
    _task("mypy.check", "python", "Run mypy type checking.", "mypy", ["."]),
    _task("pyright.check", "python", "Run pyright.", "pyright"),
    _task(
        "python.compileall",
        "python",
        "Compile all Python files.",
        "python3",
        ["-m", "compileall", "."],
    ),
    _task(
        "python.build",
        "python",
        "Build the Python package.",
        "python3",
        ["-m", "build"],
    ),
    _task("tox.run", "python", "Run tox.", "tox"),
    _task("nox.run", "python", "Run nox.", "nox"),
    _task(
        "uv.sync",
        "python",
        "Sync uv dependencies with an approved network grant.",
        "uv",
        ["sync"],
        network=True,
        approval="ASK",
    ),
    _task("cargo.test", "rust", "Run cargo tests.", "cargo", ["test"]),
    _task("cargo.build", "rust", "Run cargo build.", "cargo", ["build"]),
    _task("cargo.check", "rust", "Run cargo check.", "cargo", ["check"]),
    _task("cargo.clippy", "rust", "Run cargo clippy.", "cargo", ["clippy"]),
    _task(
        "cargo.fmt_check",
        "rust",
        "Run cargo fmt check.",
        "cargo",
        ["fmt", "--", "--check"],
    ),
    _task("cargo.run", "rust", "Run cargo project.", "cargo", ["run"]),
    _task("go.test", "go", "Run Go tests.", "go", ["test", "./..."]),
    _task("go.build", "go", "Build Go packages.", "go", ["build", "./..."]),
    _task("go.vet", "go", "Run go vet.", "go", ["vet", "./..."]),
    _task("go.fmt", "go", "Format Go packages.", "go", ["fmt", "./..."]),
    _task(
        "go.mod_tidy",
        "go",
        "Tidy Go dependencies with an approved network grant.",
        "go",
        ["mod", "tidy"],
        network=True,
        approval="ASK",
    ),
    _task("golangci_lint.run", "go", "Run golangci-lint.", "golangci-lint", ["run"]),
    _task("mvn.test", "java", "Run Maven tests.", "mvn", ["test"]),
    _task("mvn.package", "java", "Build a Maven package.", "mvn", ["package"]),
    _task("mvn.compile", "java", "Compile with Maven.", "mvn", ["compile"]),
    _task("gradle.test", "java", "Run Gradle tests.", "./gradlew", ["test"]),
    _task("gradle.build", "java", "Build with Gradle.", "./gradlew", ["build"]),
    _task("make.all", "c", "Run make.", "make"),
    _task("make.clean", "c", "Clean make outputs.", "make", ["clean"], approval="ASK"),
    _task("make.test", "c", "Run make tests.", "make", ["test"]),
    _task(
        "cmake.configure", "c", "Configure CMake.", "cmake", ["-S", ".", "-B", "build"]
    ),
    _task("cmake.build", "c", "Build CMake project.", "cmake", ["--build", "build"]),
    _task("cmake.test", "c", "Run CTest.", "ctest", ["--test-dir", "build"]),
    _task(
        "playwright.test",
        "browser",
        "Run Playwright tests.",
        "npx",
        ["playwright", "test"],
    ),
    _task(
        "playwright.test_target",
        "browser",
        "Run a Playwright target.",
        "npx",
        ["playwright", "test"],
    ),
    _task(
        "playwright.smoke",
        "browser",
        "Run Playwright smoke tests.",
        "npx",
        ["playwright", "test", "--grep", "@smoke"],
    ),
    _task("cypress.run", "browser", "Run Cypress.", "npx", ["cypress", "run"]),
    _task(
        "prisma.generate",
        "db",
        "Generate Prisma client.",
        "npx",
        ["prisma", "generate"],
    ),
    _task(
        "prisma.db_push",
        "db",
        "Push Prisma schema.",
        "npx",
        ["prisma", "db", "push"],
        approval="ASK",
    ),
    _task(
        "alembic.upgrade",
        "db",
        "Upgrade the Alembic database.",
        "alembic",
        ["upgrade", "head"],
        approval="ASK",
    ),
    _task(
        "http.health",
        "network",
        "Check HTTP health with an approved network grant.",
        "curl",
        ["-sI"],
        network=True,
        approval="ASK",
    ),
]


class TaskRegistry:
    def __init__(self) -> None:
        self.tasks = {task.id: task for task in TASK_REGISTRY}

    def get_task(self, task_id: str) -> TaskTemplate | None:
        return self.tasks.get(task_id)

    def match_direct_argv(self, argv: list[str]) -> TaskTemplate | None:
        """Match only commands with a registered, fixed argv shape.

        This is deliberately narrower than shell-command pattern matching. A
        direct ``exec_command`` call is auto-allowed only when its argv is the
        exact argv of a non-network registered task, or a registered pytest
        file task with one validated workspace-relative path argument.
        """
        if not argv:
            return None
        for task in self.tasks.values():
            fixed = [task.executable, *task.argv]
            if argv == fixed:
                return task
            if (
                task.id == "pytest.file"
                and len(argv) == 2
                and argv[0] == task.executable
            ):
                path = argv[1]
                if path and not path.startswith("-"):
                    from pathlib import PurePosixPath

                    pure = PurePosixPath(path)
                    if not pure.is_absolute() and ".." not in pure.parts:
                        return task
        return None

    def list_tasks(
        self, category: str | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        results = []
        for task in self.tasks.values():
            if category and task.category != category:
                continue
            if (
                query
                and query.lower() not in task.id.lower()
                and query.lower() not in task.description.lower()
            ):
                continue
            results.append(
                {
                    "id": task.id,
                    "category": task.category,
                    "description": task.description,
                    "executable": task.executable,
                    "argv": list(task.argv),
                    "argument_definitions": task.arg_definitions,
                    "network_requirement": task.network_requirement,
                    "approval_class": task.approval_class,
                    "cwd_policy": task.cwd_policy,
                }
            )
        return results

    def describe_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise ToolFailure(
                "NOT_FOUND", f"Task {task_id} not found.", category="validation"
            )
        return {
            "id": task.id,
            "category": task.category,
            "description": task.description,
            "executable": task.executable,
            "argv": list(task.argv),
            "argument_definitions": task.arg_definitions,
            "command": " ".join(task.command_args),
            "network_requirement": task.network_requirement,
            "approval_class": task.approval_class,
            "cwd_policy": task.cwd_policy,
        }

    def resolve_command(
        self, task_id: str, args: list[str] | str | None = None, path: str | None = None
    ) -> list[str]:
        task = self.tasks.get(task_id)
        if task is None:
            raise ToolFailure(
                "NOT_FOUND", f"Task {task_id} not found.", category="validation"
            )
        if isinstance(args, str):
            args = [args]
        return self.build_argv(task, {"args": args, "path": path})

    def build_argv(
        self, template: TaskTemplate, args_input: dict[str, Any] | None = None
    ) -> list[str]:
        values = args_input or {}
        typed: dict[str, Any] = {}
        for name, definition in template.arg_definitions.items():
            if name not in values or values[name] is None:
                if definition.get("required"):
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        f"Task argument '{name}' is required.",
                        category="validation",
                    )
                continue
            typed[name] = self._validate_argument(name, values[name], definition)

        extras = typed.get("args")
        path = typed.get("path")
        rendered: list[str] = []
        for token in template.argv:
            if token.startswith("{") and token.endswith("}") and token[1:-1] in typed:
                value = typed[token[1:-1]]
                if isinstance(value, list):
                    rendered.extend(value)
                else:
                    rendered.append(str(value))
            else:
                rendered.append(token)

        argv = [template.executable, *rendered]
        if extras:
            argv.extend(extras)
        if path:
            argv.append(path)
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Task template produced an invalid argv.",
                category="validation",
            )
        return argv

    @staticmethod
    def _validate_argument(name: str, value: Any, definition: dict[str, Any]) -> Any:
        type_name = definition.get("type", "string")
        if type_name == "string":
            valid = isinstance(value, str)
        elif type_name == "string_array":
            valid = isinstance(value, (list, tuple)) and all(
                isinstance(item, str) and item for item in value
            )
        elif type_name == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif type_name == "boolean":
            valid = isinstance(value, bool)
        elif type_name == "path":
            valid = isinstance(value, str)
            if valid:
                from pathlib import PurePosixPath

                pure = PurePosixPath(value)
                valid = (
                    not pure.is_absolute()
                    and ".." not in pure.parts
                    and "" not in pure.parts
                )
        elif type_name == "enum":
            valid = value in definition.get("values", [])
        else:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Task argument '{name}' has unsupported type '{type_name}'.",
                category="validation",
            )
        if not valid:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Task argument '{name}' must have type '{type_name}'.",
                category="validation",
            )
        if type_name == "string_array":
            return list(value)
        return value
