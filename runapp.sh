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
# TODO: replace this with main.py once it becomes the process orchestrator.
exec python "$script_dir/poller.py"