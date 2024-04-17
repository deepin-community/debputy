import functools
import os
from typing import Any, Tuple

from debputy.plugin.api import (
    DebputyPluginInitializer,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.util import _error


def initialize(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector(
        "numpy-depends",
        numpy3_versions,
        # Probably not necessary, but this is the most faithful conversion
        package_type=["deb", "udeb"],
    )


@functools.lru_cache
def _parse_numpy3_versions(versions_file: str) -> Tuple[str, str]:
    attributes = {}
    try:
        with open(versions_file, "rt", encoding="utf-8") as fd:
            for line in fd:
                if line.startswith("#") or line.isspace():
                    continue
                k, v = line.split()
                attributes[k] = v
    except FileNotFoundError:
        _error(
            f"Missing Build-Dependency on python3-numpy to ensure {versions_file}"
            " is present."
        )

    try:
        api_min_version = attributes["api-min-version"]
        abi_version = attributes["abi"]
    except KeyError as e:
        k = e.args[0]
        _error(f'Expected {versions_file} to contain the key "{k}"')
        assert False

    return api_min_version, abi_version


def numpy3_versions(
    _unused: Any,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    if context.binary_package.is_arch_all:
        dep = "python3-numpy"
    else:
        # Note we do not support --strict; codesearch.d.n suggests it is not used
        # anywhere and this saves us figuring out how to support it here.
        versions_file = os.environ.get("_NUMPY_TEST_PATH", "/usr/share/numpy3/versions")
        api_min_version, abi_version = _parse_numpy3_versions(versions_file)
        dep = f"python3-numpy (>= {api_min_version}), python3-numpy-abi{abi_version}"
    ctrl.substvars.add_dependency("python3:Depends", dep)
