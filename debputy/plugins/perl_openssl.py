import functools
import os
import subprocess
from typing import Any

from debputy.plugin.api import (
    DebputyPluginInitializer,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.util import _error


def initialize(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector(
        "perl-openssl-abi",
        detect_perl_openssl_abi,
    )


@functools.lru_cache
def _resolve_libssl_abi(cmd: str) -> str:
    try:
        return subprocess.check_output([cmd]).strip().decode("utf-8")
    except FileNotFoundError:
        _error(
            f"The perl-openssl plugin requires that perl-openssl-defaults + libssl-dev is installed"
        )
    except subprocess.CalledProcessError as e:
        _error(f"")


def detect_perl_openssl_abi(
    _unused: Any,
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    cmd = os.environ.get(
        "_PERL_SSL_DEFAULTS_TEST_PATH",
        "/usr/share/perl-openssl-defaults/get-libssl-abi",
    )
    abi = _resolve_libssl_abi(cmd)
    ctrl.substvars.add_dependency("perl:Depends", f"perl-openssl-abi-{abi}")
