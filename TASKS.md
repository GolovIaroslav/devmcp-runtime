# Task Registry Specification & Supported Catalog

The ChatGPT Dev Runtime exposes structured task execution capabilities (`list_tasks`, `describe_task`, `run_task`) to replace arbitrary shell guesswork with pre-configured, safe task definitions.

## Task Architecture

Tasks are defined as `TaskTemplate` instances registered in `coding_tools_mcp/tasks.py`. Each task contains:
- `id`: Unique identifier (e.g. `npm.test`, `pytest.all`)
- `category`: Grouping category (e.g. `npm`, `python`, `git`, `project`)
- `description`: Human-readable purpose of the task
- `command`: The command string to execute in the `ExecutionSandbox`

## Pre-Configured Task Catalog

### Project Lifecycle
- `project.detect`: Detect project type and dependencies (`package.json`, `pyproject.toml`, `Cargo.toml`).
- `project.health`: Run basic project health checks.
- `project.summary`: Summarize workspace structure.

### File & Utility Tasks
- `file.stat`: Inspect file status metadata.
- `file.type`: Check file MIME type.
- `text.head`: Retrieve beginning lines of a file.
- `text.tail`: Retrieve ending lines of a file.
- `text.count`: Count lines, words, and bytes.
- `hash.sha256`: Calculate SHA256 checksums.

### Search & Validation
- `search.rg`: Execute ripgrep text search.
- `search.files`: Find files matching name patterns.
- `search.todo`: Search workspace for `TODO` comments.
- `json.validate`: Validate JSON file syntax.
- `yaml.validate`: Validate YAML syntax.
- `toml.validate`: Validate TOML syntax.

### Git Operations
- `git.status`: Display working tree status.
- `git.diff`: Display uncommitted changes.
- `git.diff_cached`: Display staged changes.
- `git.log`: Retrieve recent commit log.
- `git.show`: Inspect specific revision.
- `git.blame`: Inspect file revision history.
- `git.current_branch`: Display active branch.
- `git.ls_files`: List tracked files.

### Node / npm Tasks
- `npm.test`: Run test suite (`npm test`).
- `npm.test_target`: Run target test (`npm test -- <args>`).
- `npm.build`: Build target (`npm run build`).
- `npm.lint`: Run linter (`npm run lint`).
- `npm.typecheck`: Run TypeScript checker (`npm run typecheck`).
- `npm.format_check`: Run code formatting check (`npm run format:check`).

### Python Tasks
- `pytest.all`: Run pytest suite.
- `pytest.file`: Run pytest on a target file.
- `pytest.node`: Run pytest on a target node.
- `unittest.all`: Run python unittest runner.
- `ruff.check`: Run ruff code linter.
- `ruff.format_check`: Check ruff formatting.
- `mypy.check`: Run mypy type checker.
- `pyright.check`: Run pyright type checker.
- `python.compileall`: Verify syntax by compiling all python files.
- `python.build`: Build python distribution package.
- `tox.run`: Run tox matrix.
- `nox.run`: Run nox matrix.

### Playwright / Browser Tasks
- `playwright.test`: Run Playwright E2E tests.
- `playwright.test_target`: Run Playwright target test.
- `playwright.smoke`: Run Playwright smoke tag (`@smoke`).
