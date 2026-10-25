#!/usr/bin/env bash
# Seed the workspace with the recorded copies of every cited page, so the
# case grades the skill's method and never depends on live network.
set -euo pipefail
cp -R "$(dirname "${BASH_SOURCE[0]}")/fixtures/source-cache" ./source-cache
