#!/bin/sh

DEBPUTY_PATH="$(dirname "$(readlink -f "$0")")/src"
if [ -z "${PYTHONPATH}" ]; then
  PYTHONPATH="${DEBPUTY_PATH}"
else
  PYTHONPATH="${DEBPUTY_PATH}:${PYTHONPATH}"
fi

export PYTHONPATH
python3 -m debputy.commands.debputy_cmd "$@"
