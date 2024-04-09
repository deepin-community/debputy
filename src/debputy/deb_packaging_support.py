import collections
import contextlib
import dataclasses
import datetime
import functools
import hashlib
import itertools
import operator
import os
import re
import subprocess
import tempfile
import textwrap
from contextlib import ExitStack
from tempfile import mkstemp
from typing import (
    Iterable,
    List,
    Optional,
    Set,
    Dict,
    Sequence,
    Tuple,
    Iterator,
    Literal,
    TypeVar,
    FrozenSet,
    cast,
    Any,
    Union,
    Mapping,
)

import debian.deb822
from debian.changelog import Changelog
from debian.deb822 import Deb822

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.debhelper_emulation import (
    dhe_install_pkg_file_as_ctrl_file_if_present,
    dhe_dbgsym_root_dir,
)
from debputy.elf_util import find_all_elf_files, ELF_MAGIC
from debputy.exceptions import DebputyDpkgGensymbolsError
from debputy.filesystem_scan import FSPath, FSROOverlay
from debputy.highlevel_manifest import (
    HighLevelManifest,
    PackageTransformationDefinition,
    BinaryPackageData,
)
from debputy.maintscript_snippet import (
    ALL_CONTROL_SCRIPTS,
    MaintscriptSnippetContainer,
    STD_CONTROL_SCRIPTS,
)
from debputy.packages import BinaryPackage, SourcePackage
from debputy.packaging.alternatives import process_alternatives
from debputy.packaging.debconf_templates import process_debconf_templates
from debputy.packaging.makeshlibs import (
    compute_shlibs,
    ShlibsContent,
    generate_shlib_dirs,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl import ServiceRegistryImpl
from debputy.plugin.api.impl_types import (
    MetadataOrMaintscriptDetector,
    PackageDataTable,
    ServiceManagerDetails,
)
from debputy.plugin.api.spec import (
    FlushableSubstvars,
    VirtualPath,
    PackageProcessingContext,
    ServiceDefinition,
)
from debputy.plugin.debputy.binary_package_rules import ServiceRule
from debputy.util import (
    _error,
    ensure_dir,
    assume_not_none,
    perl_module_dirs,
    perlxs_api_dependency,
    detect_fakeroot,
    grouper,
    _info,
    xargs,
    escape_shell,
    generated_content_dir,
    print_command,
    _warn,
)

VP = TypeVar("VP", bound=VirtualPath, covariant=True)

_T64_REGEX = re.compile("^lib.*t64(?:-nss)?$")
_T64_PROVIDES = "t64:Provides"


def generate_md5sums_file(control_output_dir: str, fs_root: VirtualPath) -> None:
    conffiles = os.path.join(control_output_dir, "conffiles")
    md5sums = os.path.join(control_output_dir, "md5sums")
    exclude = set()
    if os.path.isfile(conffiles):
        with open(conffiles, "rt") as fd:
            for line in fd:
                if not line.startswith("/"):
                    continue
                exclude.add("." + line.rstrip("\n"))
    had_content = False
    files = sorted(
        (
            path
            for path in fs_root.all_paths()
            if path.is_file and path.path not in exclude
        ),
        # Sort in the same order as dh_md5sums, which is not quite the same as dpkg/`all_paths()`
        # Compare `.../doc/...` vs `.../doc-base/...` if you want to see the difference between
        # the two approaches.
        key=lambda p: p.path,
    )
    with open(md5sums, "wt") as md5fd:
        for member in files:
            path = member.path
            assert path.startswith("./")
            path = path[2:]
            with member.open(byte_io=True) as f:
                file_hash = hashlib.md5()
                while chunk := f.read(8192):
                    file_hash.update(chunk)
            had_content = True
            md5fd.write(f"{file_hash.hexdigest()}  {path}\n")
    if not had_content:
        os.unlink(md5sums)


def install_or_generate_conffiles(
    binary_package: BinaryPackage,
    root_dir: str,
    fs_root: VirtualPath,
    debian_dir: VirtualPath,
) -> None:
    conffiles_dest = os.path.join(root_dir, "conffiles")
    dhe_install_pkg_file_as_ctrl_file_if_present(
        debian_dir,
        binary_package,
        "conffiles",
        root_dir,
        0o0644,
    )
    etc_dir = fs_root.lookup("etc")
    if etc_dir:
        _add_conffiles(conffiles_dest, (p for p in etc_dir.all_paths() if p.is_file))
    if os.path.isfile(conffiles_dest):
        os.chmod(conffiles_dest, 0o0644)


PERL_DEP_PROGRAM = 1
PERL_DEP_INDEP_PM_MODULE = 2
PERL_DEP_XS_MODULE = 4
PERL_DEP_ARCH_PM_MODULE = 8
PERL_DEP_MA_ANY_INCOMPATIBLE_TYPES = ~(PERL_DEP_PROGRAM | PERL_DEP_INDEP_PM_MODULE)


@functools.lru_cache(2)  # In practice, param will be "perl" or "perl-base"
def _dpkg_perl_version(package: str) -> str:
    dpkg_version = None
    lines = (
        subprocess.check_output(["dpkg", "-s", package])
        .decode("utf-8")
        .splitlines(keepends=False)
    )
    for line in lines:
        if line.startswith("Version: "):
            dpkg_version = line[8:].strip()
            break
    assert dpkg_version is not None
    return dpkg_version


def handle_perl_code(
    dctrl_bin: BinaryPackage,
    dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
    fs_root: FSPath,
    substvars: FlushableSubstvars,
) -> None:
    known_perl_inc_dirs = perl_module_dirs(dpkg_architecture_variables, dctrl_bin)
    detected_dep_requirements = 0

    # MakeMaker always makes lib and share dirs, but typically only one directory is actually used.
    for perl_inc_dir in known_perl_inc_dirs:
        p = fs_root.lookup(perl_inc_dir)
        if p and p.is_dir:
            p.prune_if_empty_dir()

    # FIXME: 80% of this belongs in a metadata detector, but that requires us to expose .walk() in the public API,
    #  which will not be today.
    for d, pm_mode in [
        (known_perl_inc_dirs.vendorlib, PERL_DEP_INDEP_PM_MODULE),
        (known_perl_inc_dirs.vendorarch, PERL_DEP_ARCH_PM_MODULE),
    ]:
        inc_dir = fs_root.lookup(d)
        if not inc_dir:
            continue
        for path in inc_dir.all_paths():
            if not path.is_file:
                continue
            if path.name.endswith(".so"):
                detected_dep_requirements |= PERL_DEP_XS_MODULE
            elif path.name.endswith(".pm"):
                detected_dep_requirements |= pm_mode

    for path, children in fs_root.walk():
        if path.path == "./usr/share/doc":
            children.clear()
            continue
        if (
            not path.is_file
            or not path.has_fs_path
            or not (path.is_executable or path.name.endswith(".pl"))
        ):
            continue

        interpreter = path.interpreter()
        if interpreter is not None and interpreter.command_full_basename == "perl":
            detected_dep_requirements |= PERL_DEP_PROGRAM

    if not detected_dep_requirements:
        return
    dpackage = "perl"
    # FIXME: Currently, dh_perl supports perl-base via manual toggle.

    dependency = dpackage
    if not (detected_dep_requirements & PERL_DEP_MA_ANY_INCOMPATIBLE_TYPES):
        dependency += ":any"

    if detected_dep_requirements & PERL_DEP_XS_MODULE:
        dpkg_version = _dpkg_perl_version(dpackage)
        dependency += f" (>= {dpkg_version})"
    substvars.add_dependency("perl:Depends", dependency)

    if detected_dep_requirements & (PERL_DEP_XS_MODULE | PERL_DEP_ARCH_PM_MODULE):
        substvars.add_dependency("perl:Depends", perlxs_api_dependency())


def usr_local_transformation(dctrl: BinaryPackage, fs_root: VirtualPath) -> None:
    path = fs_root.lookup("./usr/local")
    if path and any(path.iterdir):
        # There are two key issues:
        #  1) Getting the generated maintscript carried on to the final maintscript
        #  2) Making sure that manifest created directories do not trigger the "unused error".
        _error(
            f"Replacement of /usr/local paths is currently not supported in debputy (triggered by: {dctrl.name})."
        )


def _find_and_analyze_systemd_service_files(
    fs_root: VirtualPath,
    systemd_service_dir: Literal["system", "user"],
) -> Iterable[VirtualPath]:
    service_dirs = [
        f"./usr/lib/systemd/{systemd_service_dir}",
        f"./lib/systemd/{systemd_service_dir}",
    ]
    aliases: Dict[str, List[str]] = collections.defaultdict(list)
    seen = set()
    all_files = []

    for d in service_dirs:
        system_dir = fs_root.lookup(d)
        if not system_dir:
            continue
        for child in system_dir.iterdir:
            if child.is_symlink:
                dest = os.path.basename(child.readlink())
                aliases[dest].append(child.name)
            elif child.is_file and child.name not in seen:
                seen.add(child.name)
                all_files.append(child)

    return all_files


def detect_systemd_user_service_files(
    dctrl: BinaryPackage,
    fs_root: VirtualPath,
) -> None:
    for service_file in _find_and_analyze_systemd_service_files(fs_root, "user"):
        _error(
            f'Sorry, systemd user services files are not supported at the moment (saw "{service_file.path}"'
            f" in {dctrl.name})"
        )


# Generally, this should match the release date of oldstable or oldoldstable
_DCH_PRUNE_CUT_OFF_DATE = datetime.date(2019, 7, 6)
_DCH_MIN_NUM_OF_ENTRIES = 4


def _prune_dch_file(
    package: BinaryPackage,
    path: VirtualPath,
    is_changelog: bool,
    keep_versions: Optional[Set[str]],
    *,
    trim: bool = True,
) -> Tuple[bool, Optional[Set[str]]]:
    # TODO: Process `d/changelog` once
    # Note we cannot assume that changelog_file is always `d/changelog` as you can have
    # per-package changelogs.
    with path.open() as fd:
        dch = Changelog(fd)
    shortened = False
    important_entries = 0
    binnmu_entries = []
    if is_changelog:
        kept_entries = []
        for block in dch:
            if block.other_pairs.get("binary-only", "no") == "yes":
                # Always keep binNMU entries (they are always in the top) and they do not count
                # towards our kept_entries limit
                binnmu_entries.append(block)
                continue
            block_date = block.date
            if block_date is None:
                _error(f"The Debian changelog was missing date in sign off line")
            entry_date = datetime.datetime.strptime(
                block_date, "%a, %d %b %Y %H:%M:%S %z"
            ).date()
            if (
                trim
                and entry_date < _DCH_PRUNE_CUT_OFF_DATE
                and important_entries >= _DCH_MIN_NUM_OF_ENTRIES
            ):
                shortened = True
                break
            # Match debhelper in incrementing after the check.
            important_entries += 1
            kept_entries.append(block)
    else:
        assert keep_versions is not None
        # The NEWS files should match the version for the dch to avoid lintian warnings.
        # If that means we remove all entries in the NEWS file, then we delete the NEWS
        # file (see #1021607)
        kept_entries = [b for b in dch if b.version in keep_versions]
        shortened = len(dch) > len(kept_entries)
        if shortened and not kept_entries:
            path.unlink()
            return True, None

    if not shortened and not binnmu_entries:
        return False, None

    parent_dir = assume_not_none(path.parent_dir)

    with path.replace_fs_path_content() as fs_path, open(
        fs_path, "wt", encoding="utf-8"
    ) as fd:
        for entry in kept_entries:
            fd.write(str(entry))

        if is_changelog and shortened:
            # For changelog (rather than NEWS) files, add a note about how to
            # get the full version.
            msg = textwrap.dedent(
                f"""\
                # Older entries have been removed from this changelog.
                # To read the complete changelog use `apt changelog {package.name}`.
                """
            )
            fd.write(msg)

    if binnmu_entries:
        if package.is_arch_all:
            _error(
                f"The package {package.name} is architecture all, but it is built during a binNMU. A binNMU build"
                " must not include architecture all packages"
            )

        with parent_dir.add_file(
            f"{path.name}.{package.resolved_architecture}"
        ) as binnmu_changelog, open(
            binnmu_changelog.fs_path,
            "wt",
            encoding="utf-8",
        ) as binnmu_fd:
            for entry in binnmu_entries:
                binnmu_fd.write(str(entry))

    if not shortened:
        return False, None
    return True, {b.version for b in kept_entries}


def fixup_debian_changelog_and_news_file(
    dctrl: BinaryPackage,
    fs_root: VirtualPath,
    is_native: bool,
    build_env: DebBuildOptionsAndProfiles,
) -> None:
    doc_dir = fs_root.lookup(f"./usr/share/doc/{dctrl.name}")
    if not doc_dir:
        return
    changelog = doc_dir.get("changelog.Debian")
    if changelog and is_native:
        changelog.name = "changelog"
    elif is_native:
        changelog = doc_dir.get("changelog")

    trim = False if "notrimdch" in build_env.deb_build_options else True

    kept_entries = None
    pruned_changelog = False
    if changelog and changelog.has_fs_path:
        pruned_changelog, kept_entries = _prune_dch_file(
            dctrl, changelog, True, None, trim=trim
        )

    if not trim:
        return

    news_file = doc_dir.get("NEWS.Debian")
    if news_file and news_file.has_fs_path and pruned_changelog:
        _prune_dch_file(dctrl, news_file, False, kept_entries)


_UPSTREAM_CHANGELOG_SOURCE_DIRS = [
    ".",
    "doc",
    "docs",
]
_UPSTREAM_CHANGELOG_NAMES = {
    # The value is a priority to match the debhelper order.
    #  - The suffix weights heavier than the basename (because that is what debhelper did)
    #
    # We list the name/suffix in order of priority in the code. That makes it easier to
    # see the priority directly, but it gives the "lowest" value to the most important items
    f"{n}{s}": (sw, nw)
    for (nw, n), (sw, s) in itertools.product(
        enumerate(["changelog", "changes", "history"], start=1),
        enumerate(["", ".txt", ".md", ".rst"], start=1),
    )
}
_NONE_TUPLE = (None, (0, 0))


def _detect_upstream_changelog(names: Iterable[str]) -> Optional[str]:
    matches = []
    for name in names:
        match_priority = _UPSTREAM_CHANGELOG_NAMES.get(name.lower())
        if match_priority is not None:
            matches.append((name, match_priority))
    return min(matches, default=_NONE_TUPLE, key=operator.itemgetter(1))[0]


def install_upstream_changelog(
    dctrl_bin: BinaryPackage,
    fs_root: FSPath,
    source_fs_root: VirtualPath,
) -> None:
    doc_dir = f"./usr/share/doc/{dctrl_bin.name}"
    bdir = fs_root.lookup(doc_dir)
    if bdir and not bdir.is_dir:
        # "/usr/share/doc/foo -> bar" symlink. Avoid croaking on those per:
        # https://salsa.debian.org/debian/debputy/-/issues/49
        return

    if bdir:
        if bdir.get("changelog") or bdir.get("changelog.gz"):
            # Upstream's build system already provided the changelog with the correct name.
            # Accept that as the canonical one.
            return
        upstream_changelog = _detect_upstream_changelog(
            p.name for p in bdir.iterdir if p.is_file and p.has_fs_path and p.size > 0
        )
        if upstream_changelog:
            p = bdir.lookup(upstream_changelog)
            assert p is not None  # Mostly as a typing hint
            p.name = "changelog"
            return
    for dirname in _UPSTREAM_CHANGELOG_SOURCE_DIRS:
        dir_path = source_fs_root.lookup(dirname)
        if not dir_path or not dir_path.is_dir:
            continue
        changelog_name = _detect_upstream_changelog(
            p.name
            for p in dir_path.iterdir
            if p.is_file and p.has_fs_path and p.size > 0
        )
        if changelog_name:
            if bdir is None:
                bdir = fs_root.mkdirs(doc_dir)
            bdir.insert_file_from_fs_path(
                "changelog",
                dir_path[changelog_name].fs_path,
            )
            break


@dataclasses.dataclass(slots=True)
class _ElfInfo:
    path: VirtualPath
    fs_path: str
    is_stripped: Optional[bool] = None
    build_id: Optional[str] = None
    dbgsym: Optional[FSPath] = None


def _elf_static_lib_walk_filter(
    fs_path: VirtualPath,
    children: List[VP],
) -> bool:
    if (
        fs_path.name == ".build-id"
        and assume_not_none(fs_path.parent_dir).name == "debug"
    ):
        children.clear()
        return False
    # Deal with some special cases, where certain files are not supposed to be stripped in a given directory
    if "debug/" in fs_path.path or fs_path.name.endswith("debug/"):
        # FIXME: We need a way to opt out of this per #468333/#1016122
        for so_file in (f for f in list(children) if f.name.endswith(".so")):
            children.remove(so_file)
    if "/guile/" in fs_path.path or fs_path.name == "guile":
        for go_file in (f for f in list(children) if f.name.endswith(".go")):
            children.remove(go_file)
    return True


@contextlib.contextmanager
def _all_elf_files(fs_root: VirtualPath) -> Iterator[Dict[str, _ElfInfo]]:
    all_elf_files = find_all_elf_files(
        fs_root,
        walk_filter=_elf_static_lib_walk_filter,
    )
    if not all_elf_files:
        yield {}
        return
    with ExitStack() as cm_stack:
        resolved = (
            (p, cm_stack.enter_context(p.replace_fs_path_content()))
            for p in all_elf_files
        )
        elf_info = {
            fs_path: _ElfInfo(
                path=assume_not_none(fs_root.lookup(detached_path.path)),
                fs_path=fs_path,
            )
            for detached_path, fs_path in resolved
        }
        _resolve_build_ids(elf_info)
        yield elf_info


def _find_all_static_libs(
    fs_root: FSPath,
) -> Iterator[FSPath]:
    for path, children in fs_root.walk():
        # Matching the logic of dh_strip for now.
        if not _elf_static_lib_walk_filter(path, children):
            continue
        if not path.is_file:
            continue
        if path.name.startswith("lib") and path.name.endswith("_g.a"):
            # _g.a are historically ignored. I do not remember why, but guessing the "_g" is
            # an encoding of gcc's -g parameter into the filename (with -g meaning "I want debug
            # symbols")
            continue
        if not path.has_fs_path:
            continue
        with path.open(byte_io=True) as fd:
            magic = fd.read(8)
            if magic not in (b"!<arch>\n", b"!<thin>\n"):
                continue
            # Maybe we should see if the first file looks like an index file.
            # Three random .a samples suggests the index file is named "/"
            # Not sure if we should skip past it and then do the ELF check or just assume
            # that "index => static lib".
            data = fd.read(1024 * 1024)
            if b"\0" not in data and ELF_MAGIC not in data:
                continue
        yield path


@contextlib.contextmanager
def _all_static_libs(fs_root: FSPath) -> Iterator[List[str]]:
    all_static_libs = list(_find_all_static_libs(fs_root))
    if not all_static_libs:
        yield []
        return
    with ExitStack() as cm_stack:
        resolved: List[str] = [
            cm_stack.enter_context(p.replace_fs_path_content()) for p in all_static_libs
        ]
        yield resolved


_FILE_BUILD_ID_RE = re.compile(rb"BuildID(?:\[\S+\])?=([A-Fa-f0-9]+)")


def _resolve_build_ids(elf_info: Dict[str, _ElfInfo]) -> None:
    static_cmd = ["file", "-00", "-N"]
    if detect_fakeroot():
        static_cmd.append("--no-sandbox")

    for cmd in xargs(static_cmd, (i.fs_path for i in elf_info.values())):
        _info(f"Looking up build-ids via: {escape_shell(*cmd)}")
        output = subprocess.check_output(cmd)

        # Trailing "\0" gives an empty element in the end when splitting, so strip it out
        lines = output.rstrip(b"\0").split(b"\0")

        for fs_path_b, verdict in grouper(lines, 2, incomplete="strict"):
            fs_path = fs_path_b.decode("utf-8")
            info = elf_info[fs_path]
            info.is_stripped = b"not stripped" not in verdict
            m = _FILE_BUILD_ID_RE.search(verdict)
            if m:
                info.build_id = m.group(1).decode("utf-8")


def _make_debug_file(
    objcopy: str, fs_path: str, build_id: str, dbgsym_fs_root: FSPath
) -> FSPath:
    dbgsym_dirname = f"./usr/lib/debug/.build-id/{build_id[0:2]}/"
    dbgsym_basename = f"{build_id[2:]}.debug"
    dbgsym_dir = dbgsym_fs_root.mkdirs(dbgsym_dirname)
    if dbgsym_basename in dbgsym_dir:
        return dbgsym_dir[dbgsym_basename]
    # objcopy is a pain and includes the basename verbatim when you do `--add-gnu-debuglink` without having an option
    # to overwrite the physical basename.  So we have to ensure that the physical basename matches the installed
    # basename.
    with dbgsym_dir.add_file(
        dbgsym_basename,
        unlink_if_exists=False,
        fs_basename_matters=True,
        subdir_key="dbgsym-build-ids",
    ) as dbgsym:
        try:
            subprocess.check_call(
                [
                    objcopy,
                    "--only-keep-debug",
                    "--compress-debug-sections",
                    fs_path,
                    dbgsym.fs_path,
                ]
            )
        except subprocess.CalledProcessError:
            full_command = (
                f"{objcopy} --only-keep-debug --compress-debug-sections"
                f" {escape_shell(fs_path, dbgsym.fs_path)}"
            )
            _error(
                f"Attempting to create a .debug file failed. Please review the error message from {objcopy} to"
                f" understand what went wrong.  Full command was: {full_command}"
            )
    return dbgsym


def _strip_binary(strip: str, options: List[str], paths: Iterable[str]) -> None:
    # We assume the paths are obtained via `p.replace_fs_path_content()`,
    # which is the case at the time of written and should remain so forever.
    it = iter(paths)
    first = next(it, None)
    if first is None:
        return
    static_cmd = [strip]
    static_cmd.extend(options)

    for cmd in xargs(static_cmd, itertools.chain((first,), (f for f in it))):
        _info(f"Removing unnecessary ELF debug info via: {escape_shell(*cmd)}")
        try:
            subprocess.check_call(
                cmd,
                stdin=subprocess.DEVNULL,
                restore_signals=True,
            )
        except subprocess.CalledProcessError:
            _error(
                f"Attempting to remove ELF debug info failed. Please review the error from {strip} above"
                f" understand what went wrong."
            )


def _attach_debug(objcopy: str, elf_binary: VirtualPath, dbgsym: FSPath) -> None:
    dbgsym_fs_path: str
    with dbgsym.replace_fs_path_content() as dbgsym_fs_path:
        cmd = [objcopy, "--add-gnu-debuglink", dbgsym_fs_path, elf_binary.fs_path]
        print_command(*cmd)
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError:
            _error(
                f"Attempting to attach ELF debug link to ELF binary failed. Please review the error from {objcopy}"
                f" above understand what went wrong."
            )


def _run_dwz(
    dctrl: BinaryPackage,
    dbgsym_fs_root: FSPath,
    unstripped_elf_info: List[_ElfInfo],
) -> None:
    if not unstripped_elf_info or dctrl.is_udeb:
        return
    dwz_cmd = ["dwz"]
    dwz_ma_dir_name = f"usr/lib/debug/.dwz/{dctrl.deb_multiarch}"
    dwz_ma_basename = f"{dctrl.name}.debug"
    multifile = f"{dwz_ma_dir_name}/{dwz_ma_basename}"
    build_time_multifile = None
    if len(unstripped_elf_info) > 1:
        fs_content_dir = generated_content_dir()
        fd, build_time_multifile = mkstemp(suffix=dwz_ma_basename, dir=fs_content_dir)
        os.close(fd)
        dwz_cmd.append(f"-m{build_time_multifile}")
        dwz_cmd.append(f"-M/{multifile}")

    # TODO: configuration for disabling multi-file and tweaking memory limits

    dwz_cmd.extend(e.fs_path for e in unstripped_elf_info)

    _info(f"Deduplicating ELF debug info via: {escape_shell(*dwz_cmd)}")
    try:
        subprocess.check_call(dwz_cmd)
    except subprocess.CalledProcessError:
        _error(
            "Attempting to deduplicate ELF info via dwz failed. Please review the output from dwz above"
            " to understand what went wrong."
        )
    if build_time_multifile is not None and os.stat(build_time_multifile).st_size > 0:
        dwz_dir = dbgsym_fs_root.mkdirs(dwz_ma_dir_name)
        dwz_dir.insert_file_from_fs_path(
            dwz_ma_basename,
            build_time_multifile,
            mode=0o644,
            require_copy_on_write=False,
            follow_symlinks=False,
        )


def relocate_dwarves_into_dbgsym_packages(
    dctrl: BinaryPackage,
    package_fs_root: FSPath,
    dbgsym_fs_root: VirtualPath,
) -> List[str]:
    # FIXME: hardlinks
    with _all_static_libs(package_fs_root) as all_static_files:
        if all_static_files:
            strip = dctrl.cross_command("strip")
            _strip_binary(
                strip,
                [
                    "--strip-debug",
                    "--remove-section=.comment",
                    "--remove-section=.note",
                    "--enable-deterministic-archives",
                    "-R",
                    ".gnu.lto_*",
                    "-R",
                    ".gnu.debuglto_*",
                    "-N",
                    "__gnu_lto_slim",
                    "-N",
                    "__gnu_lto_v1",
                ],
                all_static_files,
            )

    with _all_elf_files(package_fs_root) as all_elf_files:
        if not all_elf_files:
            return []
        objcopy = dctrl.cross_command("objcopy")
        strip = dctrl.cross_command("strip")
        unstripped_elf_info = list(
            e for e in all_elf_files.values() if not e.is_stripped
        )

        _run_dwz(dctrl, dbgsym_fs_root, unstripped_elf_info)

        for elf_info in unstripped_elf_info:
            elf_info.dbgsym = _make_debug_file(
                objcopy,
                elf_info.fs_path,
                assume_not_none(elf_info.build_id),
                dbgsym_fs_root,
            )

        # Note: When run strip, we do so also on already stripped ELF binaries because that is what debhelper does!
        # Executables (defined by mode)
        _strip_binary(
            strip,
            ["--remove-section=.comment", "--remove-section=.note"],
            (i.fs_path for i in all_elf_files.values() if i.path.is_executable),
        )

        # Libraries (defined by mode)
        _strip_binary(
            strip,
            ["--remove-section=.comment", "--remove-section=.note", "--strip-unneeded"],
            (i.fs_path for i in all_elf_files.values() if not i.path.is_executable),
        )

        for elf_info in unstripped_elf_info:
            _attach_debug(
                objcopy,
                assume_not_none(elf_info.path),
                assume_not_none(elf_info.dbgsym),
            )

        # Set for uniqueness
        all_debug_info = sorted(
            {assume_not_none(i.build_id) for i in unstripped_elf_info}
        )

    dbgsym_doc_dir = dbgsym_fs_root.mkdirs("./usr/share/doc/")
    dbgsym_doc_dir.add_symlink(f"{dctrl.name}-dbgsym", dctrl.name)
    return all_debug_info


def run_package_processors(
    manifest: HighLevelManifest,
    package_metadata_context: PackageProcessingContext,
    fs_root: VirtualPath,
) -> None:
    pppps = manifest.plugin_provided_feature_set.package_processors_in_order()
    binary_package = package_metadata_context.binary_package
    for pppp in pppps:
        if not pppp.applies_to(binary_package):
            continue
        pppp.run_package_processor(fs_root, None, package_metadata_context)


def cross_package_control_files(
    package_data_table: PackageDataTable,
    manifest: HighLevelManifest,
) -> None:
    errors = []
    combined_shlibs = ShlibsContent()
    shlibs_dir = None
    shlib_dirs: List[str] = []
    shlibs_local = manifest.debian_dir.get("shlibs.local")
    if shlibs_local and shlibs_local.is_file:
        with shlibs_local.open() as fd:
            combined_shlibs.add_entries_from_shlibs_file(fd)

    debputy_plugin_metadata = manifest.plugin_provided_feature_set.plugin_data[
        "debputy"
    ]

    for binary_package_data in package_data_table:
        binary_package = binary_package_data.binary_package
        if binary_package.is_arch_all or not binary_package.should_be_acted_on:
            continue
        control_output_dir = assume_not_none(binary_package_data.control_output_dir)
        fs_root = binary_package_data.fs_root
        package_state = manifest.package_state_for(binary_package.name)
        related_udeb_package = (
            binary_package_data.package_metadata_context.related_udeb_package
        )

        udeb_package_name = related_udeb_package.name if related_udeb_package else None
        ctrl = binary_package_data.ctrl_creator.for_plugin(
            debputy_plugin_metadata,
            "compute_shlibs",
        )
        try:
            soname_info_list = compute_shlibs(
                binary_package,
                control_output_dir,
                fs_root,
                manifest,
                udeb_package_name,
                ctrl,
                package_state.reserved_packager_provided_files,
                combined_shlibs,
            )
        except DebputyDpkgGensymbolsError as e:
            errors.append(e.message)
        else:
            if soname_info_list:
                if shlibs_dir is None:
                    shlibs_dir = generated_content_dir(
                        subdir_key="_shlibs_materialization_dir"
                    )
                generate_shlib_dirs(
                    binary_package,
                    shlibs_dir,
                    soname_info_list,
                    shlib_dirs,
                )
    if errors:
        for error in errors:
            _warn(error)
        _error("Stopping due to the errors above")

    generated_shlibs_local = None
    if combined_shlibs:
        if shlibs_dir is None:
            shlibs_dir = generated_content_dir(subdir_key="_shlibs_materialization_dir")
        generated_shlibs_local = os.path.join(shlibs_dir, "shlibs.local")
        with open(generated_shlibs_local, "wt", encoding="utf-8") as fd:
            combined_shlibs.write_to(fd)
        _info(f"Generated {generated_shlibs_local} for dpkg-shlibdeps")

    for binary_package_data in package_data_table:
        binary_package = binary_package_data.binary_package
        if binary_package.is_arch_all or not binary_package.should_be_acted_on:
            continue
        binary_package_data.ctrl_creator.shlibs_details = (
            generated_shlibs_local,
            shlib_dirs,
        )


def _relevant_service_definitions(
    service_rule: ServiceRule,
    service_managers: Union[List[str], FrozenSet[str]],
    by_service_manager_key: Mapping[
        Tuple[str, str, str, str], Tuple[ServiceManagerDetails, ServiceDefinition[Any]]
    ],
    aliases: Mapping[str, Sequence[Tuple[str, str, str, str]]],
) -> Iterable[Tuple[Tuple[str, str, str, str], ServiceDefinition[Any]]]:
    as_keys = (key for key in aliases[service_rule.service])

    pending_queue = {
        key
        for key in as_keys
        if key in by_service_manager_key
        and service_rule.applies_to_service_manager(key[-1])
    }
    relevant_names = {}
    seen_keys = set()

    if not pending_queue:
        service_manager_names = ", ".join(sorted(service_managers))
        _error(
            f"No none of the service managers ({service_manager_names}) detected a service named"
            f" {service_rule.service} (type: {service_rule.type_of_service}, scope: {service_rule.service_scope}),"
            f" but the manifest definition at {service_rule.definition_source} requested that."
        )

    while pending_queue:
        next_key = pending_queue.pop()
        seen_keys.add(next_key)
        _, definition = by_service_manager_key[next_key]
        yield next_key, definition
        for name in definition.names:
            for target_key in aliases[name]:
                if (
                    target_key not in seen_keys
                    and service_rule.applies_to_service_manager(target_key[-1])
                ):
                    pending_queue.add(target_key)

    return relevant_names


def handle_service_management(
    binary_package_data: BinaryPackageData,
    manifest: HighLevelManifest,
    package_metadata_context: PackageProcessingContext,
    fs_root: VirtualPath,
    feature_set: PluginProvidedFeatureSet,
) -> None:

    by_service_manager_key = {}
    aliases_by_name = collections.defaultdict(list)

    state = manifest.package_state_for(binary_package_data.binary_package.name)
    all_service_managers = list(feature_set.service_managers)
    requested_service_rules = state.requested_service_rules
    for requested_service_rule in requested_service_rules:
        if not requested_service_rule.service_managers:
            continue
        for manager in requested_service_rule.service_managers:
            if manager not in feature_set.service_managers:
                # FIXME: Missing definition source; move to parsing.
                _error(
                    f"Unknown service manager {manager} used at {requested_service_rule.definition_source}"
                )

    for service_manager_details in feature_set.service_managers.values():
        service_registry = ServiceRegistryImpl(service_manager_details)
        service_manager_details.service_detector(
            fs_root,
            service_registry,
            package_metadata_context,
        )

        service_definitions = service_registry.detected_services
        if not service_definitions:
            continue

        for plugin_provided_definition in service_definitions:
            key = (
                plugin_provided_definition.name,
                plugin_provided_definition.type_of_service,
                plugin_provided_definition.service_scope,
                service_manager_details.service_manager,
            )
            by_service_manager_key[key] = (
                service_manager_details,
                plugin_provided_definition,
            )

            for name in plugin_provided_definition.names:
                aliases_by_name[name].append(key)

    for requested_service_rule in requested_service_rules:
        explicit_service_managers = requested_service_rule.service_managers is not None
        related_service_managers = (
            requested_service_rule.service_managers or all_service_managers
        )
        seen_service_managers = set()
        for service_key, service_definition in _relevant_service_definitions(
            requested_service_rule,
            related_service_managers,
            by_service_manager_key,
            aliases_by_name,
        ):
            sm = service_key[-1]
            seen_service_managers.add(sm)
            by_service_manager_key[service_key] = (
                by_service_manager_key[service_key][0],
                requested_service_rule.apply_to_service_definition(service_definition),
            )
        if (
            explicit_service_managers
            and seen_service_managers != related_service_managers
        ):
            missing_sms = ", ".join(
                sorted(related_service_managers - seen_service_managers)
            )
            _error(
                f"The rule {requested_service_rule.definition_source} explicitly requested which service managers"
                f" it should apply to. However, the following service managers did not provide a service of that"
                f" name, type and scope: {missing_sms}. Please check the rule is correct and either provide the"
                f" missing service or update the definition match the relevant services."
            )

    per_service_manager = {}

    for (
        service_manager_details,
        plugin_provided_definition,
    ) in by_service_manager_key.values():
        service_manager = service_manager_details.service_manager
        if service_manager not in per_service_manager:
            per_service_manager[service_manager] = (
                service_manager_details,
                [plugin_provided_definition],
            )
        else:
            per_service_manager[service_manager][1].append(plugin_provided_definition)

    for (
        service_manager_details,
        final_service_definitions,
    ) in per_service_manager.values():
        ctrl = binary_package_data.ctrl_creator.for_plugin(
            service_manager_details.plugin_metadata,
            service_manager_details.service_manager,
            default_snippet_order="service",
        )
        _info(f"Applying {final_service_definitions}")
        service_manager_details.service_integrator(
            final_service_definitions,
            ctrl,
            package_metadata_context,
        )


def setup_control_files(
    binary_package_data: BinaryPackageData,
    manifest: HighLevelManifest,
    dbgsym_fs_root: VirtualPath,
    dbgsym_ids: List[str],
    package_metadata_context: PackageProcessingContext,
    *,
    allow_ctrl_file_management: bool = True,
) -> None:
    binary_package = package_metadata_context.binary_package
    control_output_dir = assume_not_none(binary_package_data.control_output_dir)
    fs_root = binary_package_data.fs_root
    package_state = manifest.package_state_for(binary_package.name)

    feature_set: PluginProvidedFeatureSet = manifest.plugin_provided_feature_set
    metadata_maintscript_detectors = feature_set.metadata_maintscript_detectors
    substvars = binary_package_data.substvars

    snippets = STD_CONTROL_SCRIPTS
    generated_triggers = list(binary_package_data.ctrl_creator.generated_triggers())

    if binary_package.is_udeb:
        # FIXME: Add missing udeb scripts
        snippets = ["postinst"]

    if allow_ctrl_file_management:
        process_alternatives(
            binary_package,
            fs_root,
            package_state.reserved_packager_provided_files,
            package_state.maintscript_snippets,
        )
        process_debconf_templates(
            binary_package,
            package_state.reserved_packager_provided_files,
            package_state.maintscript_snippets,
            substvars,
            control_output_dir,
        )

        handle_service_management(
            binary_package_data,
            manifest,
            package_metadata_context,
            fs_root,
            feature_set,
        )

        plugin_detector_definition: MetadataOrMaintscriptDetector
        for plugin_detector_definition in itertools.chain.from_iterable(
            metadata_maintscript_detectors.values()
        ):
            if not plugin_detector_definition.applies_to(binary_package):
                continue
            ctrl = binary_package_data.ctrl_creator.for_plugin(
                plugin_detector_definition.plugin_metadata,
                plugin_detector_definition.detector_id,
            )
            plugin_detector_definition.run_detector(
                fs_root, ctrl, package_metadata_context
            )

        for script in snippets:
            _generate_snippet(
                control_output_dir,
                script,
                package_state.maintscript_snippets,
            )

    else:
        state = manifest.package_state_for(binary_package_data.binary_package.name)
        if state.requested_service_rules:
            service_source = state.requested_service_rules[0].definition_source
            _error(
                f"Use of service definitions (such as {service_source}) is not supported in this integration mode"
            )
        for script, snippet_container in package_state.maintscript_snippets.items():
            for snippet in snippet_container.all_snippets():
                source = snippet.definition_source
                _error(
                    f"This integration mode cannot use maintscript snippets"
                    f' (since dh_installdeb has already been called). However, "{source}" triggered'
                    f" a snippet for {script}. Please remove the offending definition if it is from"
                    f" the manifest or file a bug if it is caused by a built-in rule."
                )

        for trigger in generated_triggers:
            source = f"{trigger.provider.plugin_name}:{trigger.provider_source_id}"
            _error(
                f"This integration mode must not generate triggers"
                f' (since dh_installdeb has already been called). However, "{source}" created'
                f" a trigger. Please remove the offending definition if it is from"
                f" the manifest or file a bug if it is caused by a built-in rule."
            )

        shlibdeps_definition = [
            d
            for d in metadata_maintscript_detectors["debputy"]
            if d.detector_id == "dpkg-shlibdeps"
        ][0]

        ctrl = binary_package_data.ctrl_creator.for_plugin(
            shlibdeps_definition.plugin_metadata,
            shlibdeps_definition.detector_id,
        )
        shlibdeps_definition.run_detector(fs_root, ctrl, package_metadata_context)

        dh_staging_dir = os.path.join("debian", binary_package.name, "DEBIAN")
        try:
            with os.scandir(dh_staging_dir) as it:
                existing_control_files = [
                    f.path
                    for f in it
                    if f.is_file(follow_symlinks=False)
                    and f.name not in ("control", "md5sums")
                ]
        except FileNotFoundError:
            existing_control_files = []

        if existing_control_files:
            cmd = ["cp", "-a"]
            cmd.extend(existing_control_files)
            cmd.append(control_output_dir)
            print_command(*cmd)
            subprocess.check_call(cmd)

    if binary_package.is_udeb:
        _generate_control_files(
            binary_package_data.source_package,
            binary_package,
            package_state,
            control_output_dir,
            fs_root,
            substvars,
            # We never built udebs due to #797391, so skip over this information,
            # when creating the udeb
            None,
            None,
        )
        return

    if generated_triggers:
        assert not allow_ctrl_file_management
        dest_file = os.path.join(control_output_dir, "triggers")
        with open(dest_file, "at", encoding="utf-8") as fd:
            fd.writelines(
                textwrap.dedent(
                    f"""\
                # Added by {t.provider_source_id} from {t.provider.plugin_name}
                {t.dpkg_trigger_type} {t.dpkg_trigger_target}
            """
                )
                for t in generated_triggers
            )
            os.chmod(fd.fileno(), 0o644)

    if allow_ctrl_file_management:
        install_or_generate_conffiles(
            binary_package,
            control_output_dir,
            fs_root,
            manifest.debian_dir,
        )

    _generate_control_files(
        binary_package_data.source_package,
        binary_package,
        package_state,
        control_output_dir,
        fs_root,
        substvars,
        dbgsym_fs_root,
        dbgsym_ids,
    )


def _generate_snippet(
    control_output_dir: str,
    script: str,
    maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
) -> None:
    debputy_snippets = maintscript_snippets.get(script)
    if debputy_snippets is None:
        return
    reverse = script in ("prerm", "postrm")
    snippets = [
        debputy_snippets.generate_snippet(reverse=reverse),
        debputy_snippets.generate_snippet(snippet_order="service", reverse=reverse),
    ]
    if reverse:
        snippets = reversed(snippets)
    full_content = "".join(f"{s}\n" for s in filter(None, snippets))
    if not full_content:
        return
    filename = os.path.join(control_output_dir, script)
    with open(filename, "wt") as fd:
        fd.write("#!/bin/sh\nset -e\n\n")
        fd.write(full_content)
        os.chmod(fd.fileno(), 0o755)


def _add_conffiles(
    conffiles_dest: str,
    conffile_matches: Iterable[VirtualPath],
) -> None:
    with open(conffiles_dest, "at") as fd:
        for conffile_match in conffile_matches:
            conffile = conffile_match.absolute
            assert conffile_match.is_file
            fd.write(f"{conffile}\n")
    if os.stat(conffiles_dest).st_size == 0:
        os.unlink(conffiles_dest)


def _ensure_base_substvars_defined(substvars: FlushableSubstvars) -> None:
    for substvar in ("misc:Depends", "misc:Pre-Depends"):
        if substvar not in substvars:
            substvars[substvar] = ""


def _compute_installed_size(fs_root: VirtualPath) -> int:
    """Emulate dpkg-gencontrol's code for computing the default Installed-Size"""
    size_in_kb = 0
    hard_links = set()
    for path in fs_root.all_paths():
        if not path.is_dir and path.has_fs_path:
            st = path.stat()
            if st.st_nlink > 1:
                hl_key = (st.st_dev, st.st_ino)
                if hl_key in hard_links:
                    continue
                hard_links.add(hl_key)
            path_size = (st.st_size + 1023) // 1024
        elif path.is_symlink:
            path_size = (len(path.readlink()) + 1023) // 1024
        else:
            path_size = 1
        size_in_kb += path_size
    return size_in_kb


def _generate_dbgsym_control_file_if_relevant(
    binary_package: BinaryPackage,
    dbgsym_fs_root: VirtualPath,
    dbgsym_root_dir: str,
    dbgsym_ids: str,
    multi_arch: Optional[str],
    dctrl: str,
    extra_common_params: Sequence[str],
) -> None:
    section = binary_package.archive_section
    component = ""
    extra_params = []
    if section is not None and "/" in section and not section.startswith("main/"):
        component = section.split("/", 1)[1] + "/"
    if multi_arch != "same":
        extra_params.append("-UMulti-Arch")
    extra_params.append("-UReplaces")
    extra_params.append("-UBreaks")
    dbgsym_control_dir = os.path.join(dbgsym_root_dir, "DEBIAN")
    ensure_dir(dbgsym_control_dir)
    # Pass it via cmd-line to make it more visible that we are providing the
    # value.  It also prevents the dbgsym package from picking up this value.
    ctrl_fs_root = FSROOverlay.create_root_dir("DEBIAN", dbgsym_control_dir)
    total_size = _compute_installed_size(dbgsym_fs_root) + _compute_installed_size(
        ctrl_fs_root
    )
    extra_params.append(f"-VInstalled-Size={total_size}")
    extra_params.extend(extra_common_params)

    package = binary_package.name
    package_selector = (
        binary_package.name
        if dctrl == "debian/control"
        else f"{binary_package.name}-dbgsym"
    )
    dpkg_cmd = [
        "dpkg-gencontrol",
        f"-p{package_selector}",
        # FIXME: Support d/<pkg>.changelog at some point.
        "-ldebian/changelog",
        "-T/dev/null",
        f"-c{dctrl}",
        f"-P{dbgsym_root_dir}",
        f"-DPackage={package}-dbgsym",
        "-DDepends=" + package + " (= ${binary:Version})",
        f"-DDescription=debug symbols for {package}",
        f"-DSection={component}debug",
        f"-DBuild-Ids={dbgsym_ids}",
        "-UPre-Depends",
        "-URecommends",
        "-USuggests",
        "-UEnhances",
        "-UProvides",
        "-UEssential",
        "-UConflicts",
        "-DPriority=optional",
        "-UHomepage",
        "-UImportant",
        "-UBuilt-Using",
        "-UStatic-Built-Using",
        "-DAuto-Built-Package=debug-symbols",
        "-UProtected",
        *extra_params,
    ]
    print_command(*dpkg_cmd)
    try:
        subprocess.check_call(dpkg_cmd)
    except subprocess.CalledProcessError:
        _error(
            f"Attempting to generate DEBIAN/control file for {package}-dbgsym failed. Please review the output from "
            " dpkg-gencontrol above to understand what went wrong."
        )
    os.chmod(os.path.join(dbgsym_root_dir, "DEBIAN", "control"), 0o644)


def _all_parent_directories_of(directories: Iterable[str]) -> Set[str]:
    result = {"."}
    for path in directories:
        current = os.path.dirname(path)
        while current and current not in result:
            result.add(current)
            current = os.path.dirname(current)
    return result


def _auto_compute_multi_arch(
    binary_package: BinaryPackage,
    control_output_dir: str,
    fs_root: FSPath,
) -> Optional[str]:
    resolved_arch = binary_package.resolved_architecture
    if resolved_arch == "all":
        return None
    if any(
        script
        for script in ALL_CONTROL_SCRIPTS
        if os.path.isfile(os.path.join(control_output_dir, script))
    ):
        return None

    resolved_multiarch = binary_package.deb_multiarch
    assert resolved_arch != "all"
    acceptable_no_descend_paths = {
        f"./usr/lib/{resolved_multiarch}",
        f"./usr/include/{resolved_multiarch}",
    }
    acceptable_files = {
        f"./usr/share/doc/{binary_package.name}/{basename}"
        for basename in (
            "copyright",
            "changelog.gz",
            "changelog.Debian.gz",
            f"changelog.Debian.{resolved_arch}.gz",
            "NEWS.Debian",
            "NEWS.Debian.gz",
            "README.Debian",
            "README.Debian.gz",
        )
    }
    acceptable_intermediate_dirs = _all_parent_directories_of(
        itertools.chain(acceptable_no_descend_paths, acceptable_files)
    )

    for fs_path, children in fs_root.walk():
        path = fs_path.path
        if path in acceptable_no_descend_paths:
            children.clear()
            continue
        if path in acceptable_intermediate_dirs or path in acceptable_files:
            continue
        return None

    return "same"


@functools.lru_cache()
def _has_t64_enabled() -> bool:
    try:
        output = subprocess.check_output(
            ["dpkg-buildflags", "--query-features", "abi"]
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

    for stanza in Deb822.iter_paragraphs(output):
        if stanza.get("Feature") == "time64" and stanza.get("Enabled") == "yes":
            return True
    return False


def _t64_migration_substvar(
    binary_package: BinaryPackage,
    control_output_dir: str,
    substvars: FlushableSubstvars,
) -> None:
    name = binary_package.name
    compat_name = binary_package.fields.get("X-Time64-Compat")
    if compat_name is None and not _T64_REGEX.match(name):
        return

    if not any(
        os.path.isfile(os.path.join(control_output_dir, n))
        for n in ["symbols", "shlibs"]
    ):
        return

    if compat_name is None:
        compat_name = name.replace("t64", "", 1)
        if compat_name == name:
            raise AssertionError(
                f"Failed to derive a t64 compat name for {name}. Please file a bug against debputy."
                " As a work around, you can explicitly provide a X-Time64-Compat header in debian/control"
                " where you specify the desired compat name."
            )

    arch_bits = binary_package.package_deb_architecture_variable("ARCH_BITS")

    if arch_bits != "32" or not _has_t64_enabled():
        substvars.add_dependency(
            _T64_PROVIDES,
            f"{compat_name} (= ${{binary:Version}})",
        )
    elif _T64_PROVIDES not in substvars:
        substvars[_T64_PROVIDES] = ""


@functools.lru_cache()
def dpkg_field_list_pkg_dep() -> Sequence[str]:
    try:
        output = subprocess.check_output(
            [
                "perl",
                "-MDpkg::Control::Fields",
                "-e",
                r'print "$_\n" for field_list_pkg_dep',
            ]
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        _error("Could not run perl -MDpkg::Control::Fields to get a list of fields")
    return output.decode("utf-8").splitlines(keepends=False)


def _handle_relationship_substvars(
    source: SourcePackage,
    dctrl_file: BinaryPackage,
    substvars: FlushableSubstvars,
    has_dbgsym: bool,
) -> Optional[str]:
    relationship_fields = dpkg_field_list_pkg_dep()
    relationship_fields_lc = frozenset(x.lower() for x in relationship_fields)
    substvar_fields = collections.defaultdict(list)
    needs_dbgsym_stanza = False
    for substvar_name, substvar in substvars.as_substvar.items():
        if ":" not in substvar_name:
            continue
        if substvar.assignment_operator in ("$=", "!="):
            # Will create incorrect results if there is a dbgsym and we do nothing
            needs_dbgsym_stanza = True

        if substvar.assignment_operator == "$=":
            # Automatically handled; no need for manual merging.
            continue
        _, field = substvar_name.rsplit(":", 1)
        field_lc = field.lower()
        if field_lc not in relationship_fields_lc:
            continue
        substvar_fields[field_lc].append("${" + substvar_name + "}")

    if not has_dbgsym:
        needs_dbgsym_stanza = False

    if not substvar_fields and not needs_dbgsym_stanza:
        return None

    replacement_stanza = debian.deb822.Deb822(dctrl_file.fields)

    for field_name in relationship_fields:
        field_name_lc = field_name.lower()
        addendum = substvar_fields.get(field_name_lc)
        if addendum is None:
            # No merging required
            continue
        substvars_part = ", ".join(addendum)
        existing_value = replacement_stanza.get(field_name)

        if existing_value is None or existing_value.isspace():
            final_value = substvars_part
        else:
            existing_value = existing_value.rstrip().rstrip(",")
            final_value = f"{existing_value}, {substvars_part}"
        replacement_stanza[field_name] = final_value

    tmpdir = generated_content_dir(package=dctrl_file)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=tmpdir,
        suffix="__DEBIAN_control",
        delete=False,
    ) as fd:
        try:
            cast("Any", source.fields).dump(fd)
        except AttributeError:
            debian.deb822.Deb822(source.fields).dump(fd)
        fd.write(b"\n")
        replacement_stanza.dump(fd)

        if has_dbgsym:
            # Minimal stanza to avoid substvars warnings. Most fields are still set
            # via -D.
            dbgsym_stanza = Deb822()
            dbgsym_stanza["Package"] = f"{dctrl_file.name}-dbgsym"
            dbgsym_stanza["Architecture"] = dctrl_file.fields["Architecture"]
            dbgsym_stanza["Description"] = f"debug symbols for {dctrl_file.name}"
            fd.write(b"\n")
            dbgsym_stanza.dump(fd)

    return fd.name


def _generate_control_files(
    source_package: SourcePackage,
    binary_package: BinaryPackage,
    package_state: PackageTransformationDefinition,
    control_output_dir: str,
    fs_root: FSPath,
    substvars: FlushableSubstvars,
    dbgsym_root_fs: Optional[VirtualPath],
    dbgsym_build_ids: Optional[List[str]],
) -> None:
    package = binary_package.name
    extra_common_params = []
    extra_params_specific = []
    _ensure_base_substvars_defined(substvars)
    if "Installed-Size" not in substvars:
        # Pass it via cmd-line to make it more visible that we are providing the
        # value.  It also prevents the dbgsym package from picking up this value.
        ctrl_fs_root = FSROOverlay.create_root_dir("DEBIAN", control_output_dir)
        total_size = _compute_installed_size(fs_root) + _compute_installed_size(
            ctrl_fs_root
        )
        extra_params_specific.append(f"-VInstalled-Size={total_size}")

    ma_value = binary_package.fields.get("Multi-Arch")
    if not binary_package.is_udeb and ma_value is None:
        ma_value = _auto_compute_multi_arch(binary_package, control_output_dir, fs_root)
        if ma_value is not None:
            _info(
                f'The package "{binary_package.name}" looks like it should be "Multi-Arch: {ma_value}" based'
                ' on the contents and there is no explicit "Multi-Arch" field. Setting the Multi-Arch field'
                ' accordingly in the binary.  If this auto-correction is wrong, please  add "Multi-Arch: no" to the'
                ' relevant part of "debian/control" to disable this feature.'
            )
            # We want this to apply to the `-dbgsym` package as well to avoid
            # lintian `debug-package-for-multi-arch-same-pkg-not-coinstallable`
            extra_common_params.append(f"-DMulti-Arch={ma_value}")
    elif ma_value == "no":
        extra_common_params.append("-UMulti-Arch")

    dbgsym_root_dir = dhe_dbgsym_root_dir(binary_package)
    dbgsym_ids = " ".join(dbgsym_build_ids) if dbgsym_build_ids else ""
    if package_state.binary_version is not None:
        extra_common_params.append(f"-v{package_state.binary_version}")

    _t64_migration_substvar(binary_package, control_output_dir, substvars)

    with substvars.flush() as flushed_substvars:
        has_dbgsym = dbgsym_root_fs is not None and any(
            f for f in dbgsym_root_fs.all_paths() if f.is_file
        )
        dctrl_file = _handle_relationship_substvars(
            source_package,
            binary_package,
            substvars,
            has_dbgsym,
        )
        if dctrl_file is None:
            dctrl_file = "debian/control"

        if has_dbgsym:
            _generate_dbgsym_control_file_if_relevant(
                binary_package,
                dbgsym_root_fs,
                dbgsym_root_dir,
                dbgsym_ids,
                ma_value,
                dctrl_file,
                extra_common_params,
            )
            generate_md5sums_file(
                os.path.join(dbgsym_root_dir, "DEBIAN"),
                dbgsym_root_fs,
            )
        elif dbgsym_ids:
            extra_common_params.append(f"-DBuild-Ids={dbgsym_ids}")

        ctrl_file = os.path.join(control_output_dir, "control")
        dpkg_cmd = [
            "dpkg-gencontrol",
            f"-p{package}",
            # FIXME: Support d/<pkg>.changelog at some point.
            "-ldebian/changelog",
            f"-c{dctrl_file}",
            f"-T{flushed_substvars}",
            f"-O{ctrl_file}",
            f"-P{control_output_dir}",
            *extra_common_params,
            *extra_params_specific,
        ]
        print_command(*dpkg_cmd)
        try:
            subprocess.check_call(dpkg_cmd)
        except subprocess.CalledProcessError:
            _error(
                f"Attempting to generate DEBIAN/control file for {package} failed. Please review the output from "
                " dpkg-gencontrol above to understand what went wrong."
            )
        os.chmod(ctrl_file, 0o644)

    if not binary_package.is_udeb:
        generate_md5sums_file(control_output_dir, fs_root)
