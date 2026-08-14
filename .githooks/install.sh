#!/usr/bin/env bash
# Point this clone's hooks at .githooks/. Run once per clone - git hook
# configuration is per-clone, so a fresh clone has NO hooks until this
# is run, and the guard is silently absent rather than failing loudly.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit
echo "hooksPath -> $(git config core.hooksPath)"
echo
echo "Site-specific strings do NOT go in the hook: it is version"
echo "controlled and would publish them. Put one regex per line in"
echo "    $(git rev-parse --git-dir)/private-patterns"
echo "which is inside .git and therefore never committed."
