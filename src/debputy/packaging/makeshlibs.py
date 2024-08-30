import collections
import dataclasses
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import suppress
from typing import Optional, Set, List, Tuple, TYPE_CHECKING, Dict, IO

from debputy import elf_util
from debputy.elf_util import ELF_LINKING_TYPE_DYNAMIC
from debputy.exceptions import DebputyDpkgGensymbolsError
from debputy.packager_provided_files import PackagerProvidedFile
from debputy.packages import BinaryPackage
from debputy.plugin.api import VirtualPath, PackageProcessingContext, BinaryCtrlAccessor
from debputy.util import (
    print_command,
    escape_shell,
    assume_not_none,
    _normalize_link_target,
    _warn,
    _error,
)

if TYPE_CHECKING:
    from debputy.highlevel_manifest import HighLevelManifest


HAS_SONAME = re.compile(r"\s+SONAME\s+(\S+)")
SHLIBS_LINE_READER = re.compile(r"^(?:(\S*):)?\s*(\S+)\s*(\S+)\s*(\S.+)$")
SONAME_FORMATS = [
    re.compile(r"\s+SONAME\s+((.*)[.]so[.](.*))"),
    re.compile(r"\s+SONAME\s+((.*)-(\d.*)[.]so)"),
]


@dataclasses.dataclass
class SONAMEInfo:
    path: VirtualPath
    full_soname: str
    library: str
    major_version: Optional[str]


class ShlibsContent:
    def __init__(self) -> None:
        self._deb_lines: List[str] = []
        self._udeb_lines: List[str] = []
        self._seen: Set[Tuple[str, str, str]] = set()

    def add_library(
        self,
        library: str,
        major_version: str,
        dependency: str,
        *,
        udeb_dependency: Optional[str] = None,
    ) -> None:
        line = f"{library} {major_version} {dependency}\n"
        seen_key = ("deb", library, major_version)
        if seen_key not in self._seen:
            self._deb_lines.append(line)
            self._seen.add(seen_key)
        if udeb_dependency is not None:
            seen_key = ("udeb", library, major_version)
            udeb_line = f"udeb: {library} {major_version} {udeb_dependency}\n"
            if seen_key not in self._seen:
                self._udeb_lines.append(udeb_line)
                self._seen.add(seen_key)

    def __bool__(self) -> bool:
        return bool(self._deb_lines) or bool(self._udeb_lines)

    def add_entries_from_shlibs_file(self, fd: IO[str]) -> None:
        for line in fd:
            if line.startswith("#") or line.isspace():
                continue
            m = SHLIBS_LINE_READER.match(line)
            if not m:
                continue
            shtype, library, major_version, dependency = m.groups()
            if shtype is None or shtype == "":
                shtype = "deb"
            seen_key = (shtype, library, major_version)
            if seen_key in self._seen:
                continue
            self._seen.add(seen_key)
            if shtype == "udeb":
                self._udeb_lines.append(line)
            else:
                self._deb_lines.append(line)

    def write_to(self, fd: IO[str]) -> None:
        fd.writelines(self._deb_lines)
        fd.writelines(self._udeb_lines)


def extract_so_name(
    binary_package: BinaryPackage,
    path: VirtualPath,
) -> Optional[SONAMEInfo]:
    objdump = binary_package.cross_command("objdump")
    output = subprocess.check_output([objdump, "-p", path.fs_path], encoding="utf-8")
    for r in SONAME_FORMATS:
        m = r.search(output)
        if m:
            full_soname, library, major_version = m.groups()
            return SONAMEInfo(path, full_soname, library, major_version)
    m = HAS_SONAME.search(output)
    if not m:
        return None
    full_soname = m.group(1)
    return SONAMEInfo(path, full_soname, full_soname, None)


def extract_soname_info(
    binary_package: BinaryPackage,
    fs_root: VirtualPath,
) -> List[SONAMEInfo]:
    so_files = elf_util.find_all_elf_files(
        fs_root,
        with_linking_type=ELF_LINKING_TYPE_DYNAMIC,
    )
    result = []
    for so_file in so_files:
        soname_info = extract_so_name(binary_package, so_file)
        if not soname_info:
            continue
        result.append(soname_info)
    return result


def _compute_shlibs_content(
    binary_package: BinaryPackage,
    manifest: "HighLevelManifest",
    soname_info_list: List[SONAMEInfo],
    udeb_package_name: Optional[str],
    combined_shlibs: ShlibsContent,
) -> Tuple[ShlibsContent, bool]:
    shlibs_file_contents = ShlibsContent()
    unversioned_so_seen = False
    strict_version = manifest.package_state_for(binary_package.name).binary_version
    if strict_version is not None:
        upstream_version = re.sub(r"-[^-]+$", "", strict_version)
    else:
        strict_version = manifest.substitution.substitute(
            "{{DEB_VERSION}}", "<internal-usage>"
        )
        upstream_version = manifest.substitution.substitute(
            "{{DEB_VERSION_EPOCH_UPSTREAM}}", "<internal-usage>"
        )

    dependency = f"{binary_package.name} (>= {upstream_version})"
    strict_dependency = f"{binary_package.name} (= {strict_version})"
    udeb_dependency = None

    if udeb_package_name is not None:
        udeb_dependency = f"{udeb_package_name} (>= {upstream_version})"

    for soname_info in soname_info_list:
        if soname_info.major_version is None:
            unversioned_so_seen = True
            continue
        shlibs_file_contents.add_library(
            soname_info.library,
            soname_info.major_version,
            dependency,
            udeb_dependency=udeb_dependency,
        )
        combined_shlibs.add_library(
            soname_info.library,
            soname_info.major_version,
            strict_dependency,
            udeb_dependency=udeb_dependency,
        )

    return shlibs_file_contents, unversioned_so_seen


def resolve_reserved_provided_file(
    basename: str,
    reserved_packager_provided_files: Dict[str, List[PackagerProvidedFile]],
) -> Optional[VirtualPath]:
    matches = reserved_packager_provided_files.get(basename)
    if matches is None:
        return None
    assert len(matches) < 2
    if matches:
        return matches[0].path
    return None


def generate_shlib_dirs(
    pkg: BinaryPackage,
    root_dir: str,
    soname_info_list: List[SONAMEInfo],
    materialized_dirs: List[str],
) -> None:
    dir_scanned: Dict[str, Dict[str, Set[str]]] = {}
    dirs: Dict[str, str] = {}
    warn_dirs = {
        "/usr/lib",
        "/lib",
        f"/usr/lib/{pkg.deb_multiarch}",
        f"/lib/{pkg.deb_multiarch}",
    }

    for soname_info in soname_info_list:
        elf_binary = soname_info.path
        p = assume_not_none(elf_binary.parent_dir)
        abs_parent_path = p.absolute
        matches = dir_scanned.get(abs_parent_path)
        materialized_dir = dirs.get(abs_parent_path)
        if matches is None:
            matches = collections.defaultdict(set)
            for child in p.iterdir:
                if not child.is_symlink:
                    continue
                target = _normalize_link_target(child.readlink())
                if "/" in target:
                    # The shlib symlinks (we are interested in) are relative to the same folder
                    continue
                matches[target].add(child.name)
            dir_scanned[abs_parent_path] = matches
        symlinks = matches.get(elf_binary.name)
        if not symlinks:
            if abs_parent_path in warn_dirs:
                _warn(
                    f"Could not find any SO symlinks pointing to {elf_binary.absolute} in {pkg.name} !?"
                )
            continue
        if materialized_dir is None:
            materialized_dir = tempfile.mkdtemp(prefix=f"{pkg.name}_", dir=root_dir)
            materialized_dirs.append(materialized_dir)
            dirs[abs_parent_path] = materialized_dir

        os.symlink(elf_binary.fs_path, os.path.join(materialized_dir, elf_binary.name))
        for link in symlinks:
            os.symlink(elf_binary.name, os.path.join(materialized_dir, link))


def compute_shlibs(
    binary_package: BinaryPackage,
    control_output_dir: str,
    fs_root: VirtualPath,
    manifest: "HighLevelManifest",
    udeb_package_name: Optional[str],
    ctrl: BinaryCtrlAccessor,
    reserved_packager_provided_files: Dict[str, List[PackagerProvidedFile]],
    combined_shlibs: ShlibsContent,
) -> List[SONAMEInfo]:
    assert not binary_package.is_udeb
    shlibs_file = os.path.join(control_output_dir, "shlibs")
    need_ldconfig = False
    so_files = elf_util.find_all_elf_files(
        fs_root,
        with_linking_type=ELF_LINKING_TYPE_DYNAMIC,
    )
    sonames = extract_soname_info(binary_package, fs_root)
    provided_shlibs_file = resolve_reserved_provided_file(
        "shlibs",
        reserved_packager_provided_files,
    )
    symbols_template_file = resolve_reserved_provided_file(
        "symbols",
        reserved_packager_provided_files,
    )

    if provided_shlibs_file:
        need_ldconfig = True
        unversioned_so_seen = False
        shutil.copyfile(provided_shlibs_file.fs_path, shlibs_file)
        with open(shlibs_file) as fd:
            combined_shlibs.add_entries_from_shlibs_file(fd)
    else:
        shlibs_file_contents, unversioned_so_seen = _compute_shlibs_content(
            binary_package,
            manifest,
            sonames,
            udeb_package_name,
            combined_shlibs,
        )

        if shlibs_file_contents:
            need_ldconfig = True
            with open(shlibs_file, "wt", encoding="utf-8") as fd:
                shlibs_file_contents.write_to(fd)

    if symbols_template_file:
        symbols_file = os.path.join(control_output_dir, "symbols")
        symbols_cmd = [
            "dpkg-gensymbols",
            f"-p{binary_package.name}",
            f"-I{symbols_template_file.fs_path}",
            f"-P{control_output_dir}",
            f"-O{symbols_file}",
        ]

        if so_files:
            symbols_cmd.extend(f"-e{x.fs_path}" for x in so_files)
            print_command(*symbols_cmd)
            try:
                subprocess.check_call(symbols_cmd)
            except subprocess.CalledProcessError as e:
                # Wrap in a special error, so debputy can run the other packages.
                # The kde symbols helper relies on this behaviour
                raise DebputyDpkgGensymbolsError(
                    f"Error while running command for {binary_package.name}: {escape_shell(*symbols_cmd)}"
                ) from e

        with suppress(FileNotFoundError):
            st = os.stat(symbols_file)
            if stat.S_ISREG(st.st_mode) and st.st_size == 0:
                os.unlink(symbols_file)
            elif unversioned_so_seen:
                need_ldconfig = True

    if need_ldconfig:
        ctrl.dpkg_trigger("activate-noawait", "ldconfig")
    return sonames
