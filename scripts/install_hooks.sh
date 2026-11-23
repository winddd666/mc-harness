#!/usr/bin/env bash
set -euo pipefail

# Install lightweight local pre-commit gate into the shared git hooks directory.
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
HOOK_PATH="${GIT_COMMON_DIR}/hooks/pre-commit"
mkdir -p "$(dirname "${HOOK_PATH}")"

cat > "${HOOK_PATH}" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail

# 1) Block accidental staging of secrets, tokens, or run logs
STAGED="$(git diff --cached --name-only)"
if echo "${STAGED}" | grep -Eq '(^|/)\.env($|\.(?!example))|tokens\.json|(^|/)\.state|^runs/|^backups/'; then
  echo "[pre-commit] BLOCKED: sensitive or runtime artifact file staged:"
  echo "${STAGED}" | grep -E '(^|/)\.env($|\.(?!example))|tokens\.json|(^|/)\.state|^runs/|^backups/'
  exit 1
fi

PY="/home/xzq/projects/mc/.venv/bin/python"
if [ -x "${PY}" ]; then
  # 2) Enforce file-size ratchet on this worktree
  if [ -f "scripts/check_file_sizes.py" ]; then
    "${PY}" scripts/check_file_sizes.py
  fi
  # 3) Fast syntax / undefined-name check on staged Python files via ruff
  STAGED_PY="$(echo "${STAGED}" | grep -E '\.py$' | while read -r f; do [ -f "$f" ] && echo "$f"; done || true)"
  if [ -n "${STAGED_PY}" ] && "${PY}" -m ruff --version >/dev/null 2>&1; then
    echo "${STAGED_PY}" | xargs "${PY}" -m ruff check --select E9,F63,F7,F82 --quiet
  fi
fi
HOOK

chmod +x "${HOOK_PATH}"
echo "Installed pre-commit hook at ${HOOK_PATH}"
