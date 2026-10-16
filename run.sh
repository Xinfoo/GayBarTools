#!/bin/sh
set -eu
cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' '请先执行：python3 scripts/bootstrap.py'
    exit 1
fi
exec .venv/bin/python -B -m gaybar_tools "$@"
