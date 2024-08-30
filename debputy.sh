#!/bin/sh

DEBPUTY_ROOT="$(dirname "$(readlink -f "$0")")"
DEBPUTY_PATH="${DEBPUTY_ROOT}/src"
DEBPUTY_DH_LIB="${DEBPUTY_ROOT}/lib"
if [ -z "${PYTHONPATH}" ]; then
  PYTHONPATH="${DEBPUTY_PATH}"
else
  PYTHONPATH="${DEBPUTY_PATH}:${PYTHONPATH}"
fi
if [ -z "${PERL5LIB}" ]; then
  PERL5LIB="${DEBPUTY_DH_LIB}"
else
  PERL5LIB="${DEBPUTY_DH_LIB}:${PERL5LIB}"
fi

export PYTHONPATH PERL5LIB
python3 -m debputy.commands.debputy_cmd "$@"
