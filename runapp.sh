#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="$script_dir/.venv"

if [[ ! -f "$venv_dir/bin/activate" ]]; then
        echo "Virtual environment not found: $venv_dir" >&2
        echo "Create it with: python3 -m venv .venv" >&2
        exit 1
fi

# shellcheck disable=SC1091
source "$venv_dir/bin/activate"

echo "Initializing PlainWatch database..."
python "$script_dir/db.py" --initialize

echo "Starting PlainWatch Poller..."
python "$script_dir/poller.py" &
poller_pid=$!

echo "Starting PlainWatch Executor..."
python "$script_dir/executor.py" &
executor_pid=$!

cleanup() {
        kill "$poller_pid" "$executor_pid" 2>/dev/null || true
        wait "$poller_pid" "$executor_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

# Keep this launcher alive while both services run.
wait "$poller_pid" "$executor_pid"

echo