import dataclasses
import os.path
import re
import shutil
from re import Match
from typing import (
    Optional,
    Callable,
    Union,
    Iterable,
    Tuple,
    Sequence,
    cast,
    Mapping,
    Any,
    List,
)

from debputy.packages import BinaryPackage
from debputy.plugin.api import VirtualPath
from debputy.substitution import Substitution
from debputy.util import ensure_dir, print_command, _error

SnippetReplacement = Union[str, Callable[[str], str]]
MAINTSCRIPT_TOKEN_NAME_PATTERN = r"[A-Za-z0-9_.+]+"
MAINTSCRIPT_TOKEN_NAME_REGEX = re.compile(MAINTSCRIPT_TOKEN_NAME_PATTERN)
MAINTSCRIPT_TOKEN_REGEX = re.compile(f"#({MAINTSCRIPT_TOKEN_NAME_PATTERN})#")
_ARCH_FILTER_START = re.compile(r"^\s*(\[([^]]*)])[ \t]+")
_ARCH_FILTER_END = re.compile(r"\s+(\[([^]]*)])\s*$")
_BUILD_PROFILE_FILTER = re.compile(r"(<([^>]*)>(?:\s+<([^>]*)>)*)")


class CannotEmulateExecutableDHConfigFile(Exception):
    def message(self) -> str:
        return cast("str", self.args[0])

    def config_file(self) -> VirtualPath:
        return cast("VirtualPath", self.args[1])


@dataclasses.dataclass(slots=True, frozen=True)
class DHConfigFileLine:
    config_file: VirtualPath
    line_no: int
    executable_config: bool
    original_line: str
    tokens: Sequence[str]
    arch_filter: Optional[str]
    build_profile_filter: Optional[str]

    def conditional_key(self) -> Tuple[str, ...]:
        k = []
        if self.arch_filter is not None:
            k.append("arch")
            k.append(self.arch_filter)
        if self.build_profile_filter is not None:
            k.append("build-profiles")
            k.append(self.build_profile_filter)
        return tuple(k)

    def conditional(self) -> Optional[Mapping[str, Any]]:
        filters = []
        if self.arch_filter is not None:
            filters.append({"arch-matches": self.arch_filter})
        if self.build_profile_filter is not None:
            filters.append({"build-profiles-matches": self.build_profile_filter})
        if not filters:
            return None
        if len(filters) == 1:
            return filters[0]
        return {"all-of": filters}


def dhe_dbgsym_root_dir(binary_package: BinaryPackage) -> str:
    return os.path.join("debian", ".debhelper", binary_package.name, "dbgsym-root")


def read_dbgsym_file(binary_package: BinaryPackage) -> List[str]:
    dbgsym_id_file = os.path.join(
        "debian", ".debhelper", binary_package.name, "dbgsym-build-ids"
    )
    try:
        with open(dbgsym_id_file, "rt", encoding="utf-8") as fd:
            return fd.read().split()
    except FileNotFoundError:
        return []


def assert_no_dbgsym_migration(binary_package: BinaryPackage) -> None:
    dbgsym_migration_file = os.path.join(
        "debian", ".debhelper", binary_package.name, "dbgsym-migration"
    )
    if os.path.lexists(dbgsym_migration_file):
        _error(
            "Sorry, debputy does not support dh_strip --dbgsym-migration feature. Please either finish the"
            " migration first or migrate to debputy later"
        )


def _prune_match(
    line: str,
    match: Optional[Match[str]],
    match_mapper: Optional[Callable[[Match[str]], str]] = None,
) -> Tuple[str, Optional[str]]:
    if match is None:
        return line, None
    s, e = match.span()
    if match_mapper:
        matched_part = match_mapper(match)
    else:
        matched_part = line[s:e]
    # We prune exactly the matched part and assume the regexes leaves behind spaces if they were important.
    line = line[:s] + line[e:]
    # One special-case, if the match is at the beginning or end, then we can safely discard left
    # over whitespace.
    return line.strip(), matched_part


def dhe_filedoublearray(
    config_file: VirtualPath,
    substitution: Substitution,
    *,
    allow_dh_exec_rename: bool = False,
) -> Iterable[DHConfigFileLine]:
    with config_file.open() as fd:
        is_executable = config_file.is_executable
        for line_no, orig_line in enumerate(fd, start=1):
            arch_filter = None
            build_profile_filter = None
            if (
                line_no == 1
                and is_executable
                and not orig_line.startswith(
                    ("#!/usr/bin/dh-exec", "#! /usr/bin/dh-exec")
                )
            ):
                raise CannotEmulateExecutableDHConfigFile(
                    "Only #!/usr/bin/dh-exec based executables can be emulated",
                    config_file,
                )
            orig_line = orig_line.rstrip("\n")
            line = orig_line.strip()
            if not line or line.startswith("#"):
                continue
            if is_executable:
                if "=>" in line and not allow_dh_exec_rename:
                    raise CannotEmulateExecutableDHConfigFile(
                        'Cannot emulate dh-exec\'s "=>" feature to rename files for the concrete file',
                        config_file,
                    )
                line, build_profile_filter = _prune_match(
                    line,
                    _BUILD_PROFILE_FILTER.search(line),
                )
                line, arch_filter = _prune_match(
                    line,
                    _ARCH_FILTER_START.search(line) or _ARCH_FILTER_END.search(line),
                    # Remove the enclosing []
                    lambda m: m.group(1)[1:-1].strip(),
                )

            parts = tuple(
                substitution.substitute(
                    w, f'{config_file.path} line {line_no} token "{w}"'
                )
                for w in line.split()
            )
            yield DHConfigFileLine(
                config_file,
                line_no,
                is_executable,
                orig_line,
                parts,
                arch_filter,
                build_profile_filter,
            )


def dhe_pkgfile(
    debian_dir: VirtualPath,
    binary_package: BinaryPackage,
    basename: str,
    always_fallback_to_packageless_variant: bool = False,
    bug_950723_prefix_matching: bool = False,
) -> Optional[VirtualPath]:
    # TODO: Architecture specific files
    maybe_at_suffix = "@" if bug_950723_prefix_matching else ""
    possible_names = [f"{binary_package.name}{maybe_at_suffix}.{basename}"]
    if binary_package.is_main_package or always_fallback_to_packageless_variant:
        possible_names.append(
            f"{basename}@" if bug_950723_prefix_matching else basename
        )

    for name in possible_names:
        match = debian_dir.get(name)
        if match is not None and not match.is_dir:
            return match
    return None


def dhe_pkgdir(
    debian_dir: VirtualPath,
    binary_package: BinaryPackage,
    basename: str,
) -> Optional[VirtualPath]:
    possible_names = [f"{binary_package.name}.{basename}"]
    if binary_package.is_main_package:
        possible_names.append(basename)

    for name in possible_names:
        match = debian_dir.get(name)
        if match is not None and match.is_dir:
            return match
    return None


def dhe_install_pkg_file_as_ctrl_file_if_present(
    debian_dir: VirtualPath,
    binary_package: BinaryPackage,
    basename: str,
    control_output_dir: str,
    mode: int,
) -> None:
    source = dhe_pkgfile(debian_dir, binary_package, basename)
    if source is None:
        return
    ensure_dir(control_output_dir)
    dhe_install_path(source.fs_path, os.path.join(control_output_dir, basename), mode)


def dhe_install_path(source: str, dest: str, mode: int) -> None:
    # TODO: "install -p -mXXXX foo bar" silently discards broken
    # symlinks to install the file in place.  (#868204)
    print_command("install", "-p", f"-m{oct(mode)[2:]}", source, dest)
    shutil.copyfile(source, dest)
    os.chmod(dest, mode)
