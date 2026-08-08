from typing import Any
import json
from dataclasses import dataclass, field
from pathlib import Path
from .errors import ToolFailure

@dataclass
class TaskTemplate:
    id: str
    category: str
    description: str
    command_args: list[str] = field(default_factory=list)

# We define 70+ default configurations spanning Python, Node, Go, Rust, Java, C/C++, Make, Playwright, DB tools.
TASK_REGISTRY = [
    # project
    TaskTemplate("project.detect", "project", "Detect project type.", ["sh", "-c", "cat package.json || cat pyproject.toml || cat Cargo.toml || cat pom.xml || cat build.gradle"]),
    TaskTemplate("project.summary", "project", "Summary of project files.", ["ls", "-la"]),

    # file and text
    TaskTemplate("file.stat", "file", "Get file stats.", ["stat"]),
    TaskTemplate("file.type", "file", "Get file type.", ["file"]),
    TaskTemplate("text.head", "text", "Get first N lines of a file.", ["head", "-n"]),
    TaskTemplate("text.tail", "text", "Get last N lines of a file.", ["tail", "-n"]),
    TaskTemplate("text.count", "text", "Count lines, words, and bytes.", ["wc"]),
    TaskTemplate("hash.sha256", "hash", "Calculate SHA256 hash.", ["sha256sum"]),
    
    # search
    TaskTemplate("search.rg", "search", "Search using ripgrep.", ["rg"]),
    TaskTemplate("search.files", "search", "Search for files.", ["find", ".", "-name"]),
    TaskTemplate("search.todo", "search", "Search for TODO.", ["grep", "-rn", "TODO", "."]),
    
    # validation
    TaskTemplate("json.validate", "json", "Validate JSON syntax.", ["python3", "-m", "json.tool"]),
    TaskTemplate("yaml.validate", "yaml", "Validate YAML syntax.", ["python3", "-c", "import yaml, sys; yaml.safe_load(open(sys.argv[1]))"]),
    TaskTemplate("toml.validate", "toml", "Validate TOML syntax.", ["python3", "-c", "import tomllib, sys; tomllib.load(open(sys.argv[1], 'rb'))"]),
    
    # git
    TaskTemplate("git.status", "git", "Show git status.", ["git", "status"]),
    TaskTemplate("git.diff", "git", "Show git diff.", ["git", "diff"]),
    TaskTemplate("git.diff_cached", "git", "Show staged git diff.", ["git", "diff", "--cached"]),
    TaskTemplate("git.log", "git", "Show git log.", ["git", "log", "-n", "10"]),
    TaskTemplate("git.show", "git", "Show a specific git commit.", ["git", "show"]),
    TaskTemplate("git.blame", "git", "Show git blame.", ["git", "blame"]),
    TaskTemplate("git.current_branch", "git", "Show current git branch.", ["git", "branch", "--show-current"]),
    TaskTemplate("git.ls_files", "git", "List tracked files.", ["git", "ls-files"]),

    # npm
    TaskTemplate("npm.test", "npm", "Run npm tests.", ["npm", "test"]),
    TaskTemplate("npm.test_target", "npm", "Run specific npm test.", ["npm", "test", "--"]),
    TaskTemplate("npm.build", "npm", "Run npm build.", ["npm", "run", "build"]),
    TaskTemplate("npm.lint", "npm", "Run npm lint.", ["npm", "run", "lint"]),
    TaskTemplate("npm.typecheck", "npm", "Run npm typecheck.", ["npm", "run", "typecheck"]),
    TaskTemplate("npm.format_check", "npm", "Run npm format check.", ["npm", "run", "format:check"]),
    TaskTemplate("npm.install", "npm", "Run npm install.", ["npm", "install"]),
    TaskTemplate("npm.audit", "npm", "Run npm audit.", ["npm", "audit"]),

    # pnpm
    TaskTemplate("pnpm.test", "pnpm", "Run pnpm tests.", ["pnpm", "test"]),
    TaskTemplate("pnpm.build", "pnpm", "Run pnpm build.", ["pnpm", "run", "build"]),
    TaskTemplate("pnpm.lint", "pnpm", "Run pnpm lint.", ["pnpm", "run", "lint"]),
    TaskTemplate("pnpm.install", "pnpm", "Run pnpm install.", ["pnpm", "install"]),

    # yarn
    TaskTemplate("yarn.test", "yarn", "Run yarn tests.", ["yarn", "test"]),
    TaskTemplate("yarn.build", "yarn", "Run yarn build.", ["yarn", "run", "build"]),
    TaskTemplate("yarn.lint", "yarn", "Run yarn lint.", ["yarn", "run", "lint"]),
    TaskTemplate("yarn.install", "yarn", "Run yarn install.", ["yarn", "install"]),

    # bun
    TaskTemplate("bun.test", "bun", "Run bun tests.", ["bun", "test"]),
    TaskTemplate("bun.build", "bun", "Run bun build.", ["bun", "run", "build"]),
    TaskTemplate("bun.install", "bun", "Run bun install.", ["bun", "install"]),

    # python
    TaskTemplate("pytest.all", "python", "Run all pytest tests.", ["pytest"]),
    TaskTemplate("pytest.file", "python", "Run pytest on a file.", ["pytest"]),
    TaskTemplate("unittest.all", "python", "Run all unittests.", ["python", "-m", "unittest", "discover"]),
    TaskTemplate("ruff.check", "python", "Run ruff check.", ["ruff", "check", "."]),
    TaskTemplate("ruff.format_check", "python", "Run ruff format check.", ["ruff", "format", "--check", "."]),
    TaskTemplate("mypy.check", "python", "Run mypy type check.", ["mypy", "."]),
    TaskTemplate("pyright.check", "python", "Run pyright type check.", ["pyright"]),
    TaskTemplate("python.compileall", "python", "Compile all python files.", ["python", "-m", "compileall", "."]),
    TaskTemplate("python.build", "python", "Build python package.", ["python", "-m", "build"]),
    TaskTemplate("tox.run", "python", "Run tox.", ["tox"]),
    TaskTemplate("nox.run", "python", "Run nox.", ["nox"]),
    TaskTemplate("uv.sync", "python", "Sync uv dependencies.", ["uv", "sync"]),

    # rust
    TaskTemplate("cargo.test", "rust", "Run cargo tests.", ["cargo", "test"]),
    TaskTemplate("cargo.build", "rust", "Run cargo build.", ["cargo", "build"]),
    TaskTemplate("cargo.check", "rust", "Run cargo check.", ["cargo", "check"]),
    TaskTemplate("cargo.clippy", "rust", "Run cargo clippy.", ["cargo", "clippy"]),
    TaskTemplate("cargo.fmt_check", "rust", "Run cargo fmt check.", ["cargo", "fmt", "--", "--check"]),
    TaskTemplate("cargo.run", "rust", "Run cargo project.", ["cargo", "run"]),

    # go
    TaskTemplate("go.test", "go", "Run go tests.", ["go", "test", "./..."]),
    TaskTemplate("go.build", "go", "Run go build.", ["go", "build", "./..."]),
    TaskTemplate("go.vet", "go", "Run go vet.", ["go", "vet", "./..."]),
    TaskTemplate("go.fmt", "go", "Run go fmt.", ["go", "fmt", "./..."]),
    TaskTemplate("go.mod_tidy", "go", "Run go mod tidy.", ["go", "mod", "tidy"]),
    TaskTemplate("golangci_lint.run", "go", "Run golangci-lint.", ["golangci-lint", "run"]),

    # java
    TaskTemplate("mvn.test", "java", "Run maven tests.", ["mvn", "test"]),
    TaskTemplate("mvn.package", "java", "Run maven package.", ["mvn", "package"]),
    TaskTemplate("mvn.compile", "java", "Run maven compile.", ["mvn", "compile"]),
    TaskTemplate("gradle.test", "java", "Run gradle tests.", ["./gradlew", "test"]),
    TaskTemplate("gradle.build", "java", "Run gradle build.", ["./gradlew", "build"]),

    # c/c++
    TaskTemplate("make.all", "c", "Run make.", ["make"]),
    TaskTemplate("make.clean", "c", "Run make clean.", ["make", "clean"]),
    TaskTemplate("make.test", "c", "Run make test.", ["make", "test"]),
    TaskTemplate("cmake.configure", "c", "Run cmake configure.", ["cmake", "-S", ".", "-B", "build"]),
    TaskTemplate("cmake.build", "c", "Run cmake build.", ["cmake", "--build", "build"]),
    TaskTemplate("cmake.test", "c", "Run cmake test.", ["ctest", "--test-dir", "build"]),

    # browser
    TaskTemplate("playwright.test", "browser", "Run playwright tests.", ["npx", "playwright", "test"]),
    TaskTemplate("playwright.test_target", "browser", "Run playwright test target.", ["npx", "playwright", "test"]),
    TaskTemplate("playwright.smoke", "browser", "Run playwright smoke tests.", ["npx", "playwright", "test", "--grep", "@smoke"]),
    TaskTemplate("cypress.run", "browser", "Run cypress tests.", ["npx", "cypress", "run"]),
    
    # db tools
    TaskTemplate("prisma.generate", "db", "Run prisma generate.", ["npx", "prisma", "generate"]),
    TaskTemplate("prisma.db_push", "db", "Run prisma db push.", ["npx", "prisma", "db", "push"]),
    TaskTemplate("alembic.upgrade", "db", "Run alembic upgrade head.", ["alembic", "upgrade", "head"]),

    # network
    TaskTemplate("http.health", "network", "Check HTTP health.", ["curl", "-sI"]),
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
        # Return string-like representation for backward compatibility with schema
        return {"id": task.id, "category": task.category, "description": task.description, "command": " ".join(task.command_args)}

    def resolve_command(self, task_id: str, args: list[str] | None = None, path: str | None = None) -> list[str]:
        task = self.tasks.get(task_id)
        if not task:
            raise ToolFailure("NOT_FOUND", f"Task {task_id} not found", category="validation")
        
        cmd_args = list(task.command_args)
        
        if args:
            if isinstance(args, str):
                cmd_args.append(args)
            elif isinstance(args, list):
                cmd_args.extend(args)
                
        if path:
            cmd_args.append(path)
            
        return cmd_args
