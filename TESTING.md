# Testing & Verification Guide

This project includes automated unit and compliance test suites.

## Running Tests

To run the full test suite using `uv`:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```

Alternatively, if using standard python in a virtual environment:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Compliance Suite Overview

The compliance test suite covers:
- Workspace path isolation (`..` traversal, absolute path denial, `.env` access blocks).
- Ephemeral `ExecutionSandbox` path translation.
- `ApprovalEngine` command evaluation.
- `TaskRegistry` lookup and parameter substitution.
- Patch engine verification (allowing additions/updates, blocking `Delete File` and `Move to`).
