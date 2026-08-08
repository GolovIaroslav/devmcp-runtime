#!/usr/bin/env bash
set -euo pipefail

# A public-release gate must never silently downgrade to a small regex scan.
# `git --log-opts=--all` covers every reachable ref; `dir` checks the current
# tree too, including untracked release artifacts during a local dry run.
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks is required for the release secret audit; install the pinned CI tool or run the CI gate" >&2
  exit 2
fi

gitleaks git --no-banner --redact --exit-code 1 --log-opts="--all" .
gitleaks dir --no-banner --redact --exit-code 1 .
echo "Gitleaks completed: current tree and all reachable Git history scanned."
