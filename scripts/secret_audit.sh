#!/usr/bin/env bash
set -euo pipefail

# Never print matching lines: the caller only needs the exit status.
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --no-banner --redact --exit-code 1
  exit 0
fi

if rg -l --hidden --glob '!.git/**' --glob '!tests/compliance/outside-secret.txt' --regexp '(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]+|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer[[:space:]]+[A-Za-z0-9._~-]{24,})' . >/dev/null; then
  echo "secret-like material detected in the current tree" >&2
  exit 1
fi

history_found=0
known_fixture_prefix='sk-'
known_fixture_suffix='test-secret-value'
while read -r commit; do
  matching_files=$(git grep -I -l -E '(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]+|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer[[:space:]]+[A-Za-z0-9._~-]{24,})' "$commit" -- ':!tests/compliance/outside-secret.txt' || true)
  while read -r file; do
    [[ -z "$file" ]] && continue
    if [[ "$file" == *:tests/compliance/test_runtime_helpers.py ]] && git grep -q -F -- "${known_fixture_prefix}${known_fixture_suffix}" "$commit" -- tests/compliance/test_runtime_helpers.py; then
      continue
    fi
    history_found=1
    break 2
  done <<< "$matching_files"
  if [[ "$history_found" -eq 1 ]]; then
    break
  fi
done < <(git rev-list --all)
if [[ "$history_found" -eq 1 ]]; then
  echo "secret-like material detected in Git history" >&2
  exit 1
fi

echo "Secret audit found no configured high-confidence patterns."
