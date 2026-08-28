#!/usr/bin/env bash
# Provision the dev environment after the container is created.
set -euo pipefail

export PATH="$HOME/.local/share/mise/shims:$PATH"

# Docker creates volume mount points owned by root. Non-recursive on purpose:
# ~/.claude/skills is a read-only bind mount nested inside ~/.claude.
echo "==> Fixing ownership of mounted directories"
sudo chown "$(id -u):$(id -g)" \
  "$HOME/.cache" "$HOME/.cache/rattler" "$HOME/.claude" .pixi

echo "==> Installing tools from mise.toml (pixi, uv, prek)"
mise trust
mise install
mise reshim

echo "==> Installing pixi environments (default + dev + docs)"
pixi install -e default -e dev -e docs

if [ -d .git ]; then
  echo "==> Installing git hooks"
  prek install
else
  echo "==> Skipping git hooks (repo is not git-initialized yet)"
fi

# Only the parent: leave creating the store itself to Icechunk, so open-vs-create
# logic still sees a fresh location.
echo "==> Preparing local store parent for ${OISST_LOCAL_STORE:-local-store/oisst}"
mkdir -p "$(dirname "${OISST_LOCAL_STORE:-local-store/oisst}")"

cat <<'EOF'

Ready. Common entry points:

  pixi run -e dev pytest        # tests
  uvx nox -s lint               # prek / ruff / mypy, as CI runs it
  prek run -a                   # same hooks, directly
  claude                        # Claude Code, with host skills mounted read-only

The local Icechunk store lives at $OISST_LOCAL_STORE (inside the workspace,
git-ignored), so it is usable from the host outside the container too.

EOF
