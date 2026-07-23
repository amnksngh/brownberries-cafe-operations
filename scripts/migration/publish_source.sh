#!/usr/bin/env bash
set -euo pipefail

# Push source code only. Runtime data stays on the Windows production machine.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -d instance ]] && ! git check-ignore -q instance; then
  echo "ERROR: instance/ is not ignored. Refusing to publish runtime data." >&2
  exit 1
fi

for path in instance/brownberries.db instance/deployment_config.json instance/uploads static/uploads; do
  if ! git check-ignore -q "$path"; then
    echo "ERROR: $path is not ignored. Refusing to publish runtime data." >&2
    exit 1
  fi
done

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked source changes are ready to publish:"
  git status --short --untracked-files=no
fi

git add -u
git add app templates scripts requirements.txt README.md .gitignore android 2>/dev/null || true

if git diff --cached --name-only | grep -E '^(instance/|static/uploads/|.*\.db$)' >/dev/null; then
  echo "ERROR: runtime data was staged. Unstage it and investigate before pushing." >&2
  exit 1
fi

if git diff --cached --quiet; then
  echo "No source changes to commit. Nothing was pushed."
  exit 0
fi

MESSAGE="${1:-Update Brownberries Cafe source}"
git commit -m "$MESSAGE"
git push origin main
echo "Source published. Windows production data was not included."
