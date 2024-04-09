from typing import List

from debputy import elf_util
from debputy.elf_util import ELF_LINKING_TYPE_DYNAMIC
from debputy.plugin.api import (
    VirtualPath,
    PackageProcessingContext,
)
from debputy.plugin.api.impl import BinaryCtrlAccessorProvider

SKIPPED_DEBUG_DIRS = [
    "lib",
    "lib64",
    "usr",
    "bin",
    "sbin",
    "opt",
    "dev",
    "emul",
    ".build-id",
]

SKIP_DIRS = {f"./usr/lib/debug/{subdir}" for subdir in SKIPPED_DEBUG_DIRS}


def _walk_filter(fs_path: VirtualPath, children: List[VirtualPath]) -> bool:
    if fs_path.path in SKIP_DIRS:
        children.clear()
        return False
    return True


def detect_shlibdeps(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessorProvider,
    _context: PackageProcessingContext,
) -> None:
    elf_files_to_process = elf_util.find_all_elf_files(
        fs_root,
        walk_filter=_walk_filter,
        with_linking_type=ELF_LINKING_TYPE_DYNAMIC,
    )

    if not elf_files_to_process:
        return

    ctrl.dpkg_shlibdeps(elf_files_to_process)
