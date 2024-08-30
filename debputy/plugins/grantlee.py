import functools
import os
import re
import subprocess
from typing import Any, Optional

from debputy.plugin.api import (
    DebputyPluginInitializer,
    BinaryCtrlAccessor,
    PackageProcessingContext,
    VirtualPath,
)
from debputy.util import _error


_RE_GRANTLEE_VERSION = re.compile(r"^\d\.\d$")


def initialize(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector(
        "detect-grantlee-dependencies",
        detect_grantlee_dependencies,
    )


def detect_grantlee_dependencies(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    binary_package = context.binary_package
    if binary_package.is_arch_all:
        # Delta from dh_grantlee, but the MULTIARCH paths should not
        # exist in arch:all packages
        return
    ma = binary_package.package_deb_architecture_variable("MULTIARCH")
    grantlee_root_dirs = [
        f"usr/lib/{ma}/grantlee",
        f"usr/lib/{ma}/qt5/plugins/grantlee",
    ]
    grantee_version: Optional[str] = None
    for grantlee_root_dir in grantlee_root_dirs:
        grantlee_root_path = fs_root.lookup(grantlee_root_dir)
        if grantlee_root_path is None or not grantlee_root_path.is_dir:
            continue
        # Delta: The original code recurses and then checks for "grantee/VERSION".
        # Code here assumes that was just File::Find being used as a dir iterator.
        for child in grantlee_root_path.iterdir:
            if not _RE_GRANTLEE_VERSION.fullmatch(child.name):
                continue
            version = child.name
            if grantee_version is not None and version != grantee_version:
                _error(
                    f"Package {binary_package.name} contains plugins for different grantlee versions"
                )
            grantee_version = version

    if grantee_version is None:
        return
    dep_version = grantee_version.replace(".", "-")
    grantee_dep = f"grantlee5-templates-{dep_version}"
    ctrl.substvars["grantlee:Depends"] = grantee_dep
