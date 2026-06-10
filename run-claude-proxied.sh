#!/usr/bin/env bash
# run-claude-proxied.sh -- macOS/Linux equivalent of run-claude-proxied.ps1.
# Launch Claude Code routed through the local memory-inject proxy, in one step.
#
# The proxy (proxy.py) must already be running in its own window:
#     python3 proxy.py
# (On some systems the interpreter is `python` rather than `python3` -- use
#  whichever resolves to Python 3.10+. `python3 --version` to check.)
#
# Then, instead of `claude`, run:
#     ./run-claude-proxied.sh            (or pass claude args: ./run-claude-proxied.sh --resume)
#
# This sets ANTHROPIC_BASE_URL only for the Claude Code process it spawns --
# it does not leak to your shell. Close Claude Code and the routing is gone.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
port_file="$here/proxy_port.txt"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }

if [ ! -f "$port_file" ]; then
    red "No proxy_port.txt found -- start the proxy first:  python3 proxy.py"
    exit 1
fi
port="$(tr -d '[:space:]' < "$port_file")"

# Verify the proxy is actually listening before handing Claude Code to it --
# otherwise every request would fail with no obvious cause.
if ! { exec 3<>"/dev/tcp/127.0.0.1/$port"; } 2>/dev/null; then
    red "Proxy is not reachable on 127.0.0.1:$port"
    red "Start it first (in its own window):  python3 proxy.py"
    exit 1
fi
exec 3>&- 3<&- 2>/dev/null || true

# Resolve the project name from the CURRENT working directory and hand it to
# the proxy via a sidecar file. The proxy reads this on every request, so the
# DB tags rows with the right project even though the proxy process itself was
# started before this script ran and doesn't inherit our env.
#
# Convention: project = the first path segment of pwd under $HOME
# (e.g. ~/my-app/api -> "my-app"). Falls back to the current directory's
# basename, then 'misc'. Override with MEMORY_INJECT_PROJECT.
pwd_path="$(pwd -P)"
project="${MEMORY_INJECT_PROJECT:-}"
if [ -z "$project" ]; then
    case "$pwd_path/" in
        "$HOME"/*)
            rel="${pwd_path#"$HOME"/}"
            project="${rel%%/*}"
            ;;
    esac
    [ -z "$project" ] && project="$(basename "$pwd_path")"
    [ -z "$project" ] && project="misc"
fi
printf '%s' "$project" > "$here/current_project.txt"
cyan "memory-inject project = $project"

export ANTHROPIC_BASE_URL="http://127.0.0.1:$port"
green "Routing Claude Code through memory-inject proxy at $ANTHROPIC_BASE_URL"
exec claude "$@"
