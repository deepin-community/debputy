import collections
import dataclasses
from typing import Mapping, Iterable, Dict, List, Optional, Tuple, Sequence, Container

from debputy.packages import BinaryPackage
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.impl_types import PackagerProvidedFileClassSpec
from debputy.util import _error, CAN_DETECT_TYPOS, detect_possible_typo


_KNOWN_NON_PPFS = frozenset(
    {
        # Some of these overlap with the _KNOWN_NON_TYPO_EXTENSIONS below
        # This one is a quicker check. The _KNOWN_NON_TYPO_EXTENSIONS is a general (but more
        # expensive check).
        "gbp.conf",  # Typo matches with `gbp.config` (dh_installdebconf) in two edits steps
        "salsa-ci.yml",  # Typo matches with `salsa-ci.wm` (dh_installwm) in two edits steps
        # No reason to check any of these as they are never PPFs
        "clean",
        "control",
        "compat",
        "debputy.manifest",
        "rules",
        # NB: changelog and copyright are (de facto) ppfs, so they are deliberately omitted
    }
)

_KNOWN_NON_TYPO_EXTENSIONS = frozenset(
    {
        "conf",
        "sh",
        "yml",
        "yaml",
        "json",
        "bash",
        "pl",
        "py",
        # Fairly common image format in older packages
        "xpm",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class PackagerProvidedFile:
    path: VirtualPath
    package_name: str
    installed_as_basename: str
    provided_key: str
    definition: PackagerProvidedFileClassSpec
    match_priority: int = 0
    fuzzy_match: bool = False
    uses_explicit_package_name: bool = False
    name_segment: Optional[str] = None
    architecture_restriction: Optional[str] = None
    expected_path: Optional[str] = None

    def compute_dest(self) -> Tuple[str, str]:
        return self.definition.compute_dest(
            self.installed_as_basename,
            owning_package=self.package_name,
            path=self.path,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class PerPackagePackagerProvidedResult:
    auto_installable: List[PackagerProvidedFile]
    reserved_only: Dict[str, List[PackagerProvidedFile]]


def _find_package_name_prefix(
    binary_packages: Mapping[str, BinaryPackage],
    main_binary_package: str,
    max_periods_in_package_name: int,
    path: VirtualPath,
    *,
    allow_fuzzy_matches: bool = False,
) -> Iterable[Tuple[str, str, bool, bool]]:
    if max_periods_in_package_name < 1:
        prefix, remaining = path.name.split(".", 1)
        package_name = prefix
        bug_950723 = False
        if allow_fuzzy_matches and package_name.endswith("@"):
            package_name = package_name[:-1]
            bug_950723 = True
        if package_name in binary_packages:
            yield package_name, remaining, True, bug_950723
        else:
            yield main_binary_package, path.name, False, False
        return

    parts = path.name.split(".", max_periods_in_package_name + 1)
    for p in range(len(parts) - 1, 0, -1):
        name = ".".join(parts[0:p])
        bug_950723 = False
        if allow_fuzzy_matches and name.endswith("@"):
            name = name[:-1]
            bug_950723 = True

        if name in binary_packages:
            remaining = ".".join(parts[p:])
            yield name, remaining, True, bug_950723
    # main package case
    yield main_binary_package, path.name, False, False


def _iterate_stem_splits(basename: str) -> Tuple[str, str, int]:
    stem = basename
    period_count = stem.count(".")
    yield stem, None, period_count
    install_as_name = ""
    while period_count > 0:
        period_count -= 1
        install_as_name_part, stem = stem.split(".", 1)
        install_as_name = (
            install_as_name + "." + install_as_name_part
            if install_as_name != ""
            else install_as_name_part
        )
        yield stem, install_as_name, period_count


def _find_definition(
    packager_provided_files: Mapping[str, PackagerProvidedFileClassSpec],
    basename: str,
    *,
    period2stems: Optional[Mapping[int, Sequence[str]]] = None,
    had_arch: bool = False,
) -> Tuple[Optional[str], Optional[PackagerProvidedFileClassSpec], Optional[str]]:
    for stem, install_as_name, period_count in _iterate_stem_splits(basename):
        definition = packager_provided_files.get(stem)
        if definition is not None:
            return install_as_name, definition, None
        if not period2stems:
            continue
        stems = period2stems.get(period_count)

        if not stems:
            continue
        # If the stem is also the extension and a known one at that, then
        # we do not consider it a typo match (to avoid false positives).
        #
        # We also ignore "foo.1" since manpages are kind of common.
        if not had_arch and (stem in _KNOWN_NON_TYPO_EXTENSIONS or stem.isdigit()):
            continue
        matches = detect_possible_typo(stem, stems)
        if matches is not None and len(matches) == 1:
            definition = packager_provided_files[matches[0]]
            return install_as_name, definition, stem
    return None, None, None


def _check_mismatches(
    path: VirtualPath,
    definition: PackagerProvidedFileClassSpec,
    owning_package: BinaryPackage,
    install_as_name: Optional[str],
    had_arch: bool,
) -> None:
    if install_as_name is not None and not definition.allow_name_segment:
        _error(
            f'The file "{path.fs_path}" looks like a packager provided file for'
            f' {owning_package.name} of type {definition.stem} with the custom name "{install_as_name}".'
            " However, this file type does not allow custom naming. The file type was registered"
            f" by {definition.debputy_plugin_metadata.plugin_name} in case you disagree and want"
            " to file a bug/feature request."
        )
    if had_arch:
        if owning_package.is_arch_all:
            _error(
                f'The file "{path.fs_path}" looks like an architecture specific packager provided file for'
                f" {owning_package.name} of type {definition.stem}."
                " However, the package in question is arch:all. The use of architecture specific files"
                " for arch:all packages does not make sense."
            )
        if not definition.allow_architecture_segment:
            _error(
                f'The file "{path.fs_path}" looks like an architecture specific packager provided file for'
                f" {owning_package.name} of type {definition.stem}."
                " However, this file type does not allow architecture specific variants. The file type was registered"
                f" by {definition.debputy_plugin_metadata.plugin_name} in case you disagree and want"
                " to file a bug/feature request."
            )


def _split_path(
    packager_provided_files: Mapping[str, PackagerProvidedFileClassSpec],
    binary_packages: Mapping[str, BinaryPackage],
    main_binary_package: str,
    max_periods_in_package_name: int,
    path: VirtualPath,
    *,
    allow_fuzzy_matches: bool = False,
    period2stems: Optional[Mapping[int, Sequence[str]]] = None,
) -> Iterable[PackagerProvidedFile]:
    owning_package_name = main_binary_package
    basename = path.name
    match_priority = 0
    had_arch = False
    if "." not in basename:
        definition = packager_provided_files.get(basename)
        if definition is None:
            return
        if definition.packageless_is_fallback_for_all_packages:
            yield from (
                PackagerProvidedFile(
                    path=path,
                    package_name=n,
                    installed_as_basename=n,
                    provided_key=".UNNAMED.",
                    definition=definition,
                    match_priority=match_priority,
                    fuzzy_match=False,
                    uses_explicit_package_name=False,
                    name_segment=None,
                    architecture_restriction=None,
                )
                for n in binary_packages
            )
        else:
            yield PackagerProvidedFile(
                path=path,
                package_name=owning_package_name,
                installed_as_basename=owning_package_name,
                provided_key=".UNNAMED.",
                definition=definition,
                match_priority=match_priority,
                fuzzy_match=False,
                uses_explicit_package_name=False,
                name_segment=None,
                architecture_restriction=None,
            )
        return

    for (
        owning_package_name,
        basename,
        explicit_package,
        bug_950723,
    ) in _find_package_name_prefix(
        binary_packages,
        main_binary_package,
        max_periods_in_package_name,
        path,
        allow_fuzzy_matches=allow_fuzzy_matches,
    ):
        owning_package = binary_packages[owning_package_name]
        match_priority = 1 if explicit_package else 0
        fuzzy_match = False
        arch_restriction: Optional[str] = None

        if allow_fuzzy_matches and basename.endswith(".in") and len(basename) > 3:
            basename = basename[:-3]
            fuzzy_match = True

        if "." in basename:
            remaining, last_word = basename.rsplit(".", 1)
            # We cannot use "resolved_architecture" as it would return "all".
            if last_word == owning_package.package_deb_architecture_variable("ARCH"):
                match_priority = 3
                basename = remaining
                arch_restriction = last_word
            elif last_word == owning_package.package_deb_architecture_variable(
                "ARCH_OS"
            ):
                match_priority = 2
                basename = remaining
                arch_restriction = last_word
            elif last_word == "all" and owning_package.is_arch_all:
                # This case does not make sense, but we detect it, so we can report an error
                # via _check_mismatches.
                match_priority = -1
                basename = remaining
                arch_restriction = last_word

        install_as_name, definition, typoed_stem = _find_definition(
            packager_provided_files,
            basename,
            period2stems=period2stems,
            had_arch=bool(arch_restriction),
        )
        if definition is None:
            continue

        # Note: bug_950723 implies allow_fuzzy_matches
        if bug_950723 and not definition.bug_950723:
            continue

        if not allow_fuzzy_matches:
            # LSP/Lint checks here but should not use `_check_mismatches` as
            # the hard error disrupts them.
            _check_mismatches(
                path,
                definition,
                owning_package,
                install_as_name,
                arch_restriction is not None,
            )

        expected_path: Optional[str] = None
        if (
            definition.packageless_is_fallback_for_all_packages
            and install_as_name is None
            and not had_arch
            and not explicit_package
            and arch_restriction is None
        ):
            if typoed_stem is not None:
                parent_path = (
                    path.parent_dir.path + "/" if path.parent_dir is not None else ""
                )
                expected_path = f"{parent_path}{definition.stem}"
                if fuzzy_match and path.name.endswith(".in"):
                    expected_path += ".in"
            yield from (
                PackagerProvidedFile(
                    path=path,
                    package_name=n,
                    installed_as_basename=f"{n}@" if bug_950723 else n,
                    provided_key=".UNNAMED." if bug_950723 else ".UNNAMED@.",
                    definition=definition,
                    match_priority=match_priority,
                    fuzzy_match=fuzzy_match,
                    uses_explicit_package_name=False,
                    name_segment=None,
                    architecture_restriction=None,
                    expected_path=expected_path,
                )
                for n in binary_packages
            )
        else:
            provided_key = (
                install_as_name if install_as_name is not None else ".UNNAMED."
            )
            basename = (
                install_as_name if install_as_name is not None else owning_package_name
            )
            if bug_950723:
                provided_key = f"{provided_key}@"
                basename = f"{basename}@"
                package_prefix = f"{owning_package_name}@"
            else:
                package_prefix = owning_package_name
            if typoed_stem:
                parent_path = (
                    path.parent_dir.path + "/" if path.parent_dir is not None else ""
                )
                basename = definition.stem
                if install_as_name is not None:
                    basename = f"{install_as_name}.{basename}"
                if explicit_package:
                    basename = f"{package_prefix}.{basename}"
                if arch_restriction is not None and arch_restriction != "all":
                    basename = f"{basename}.{arch_restriction}"
                expected_path = f"{parent_path}{basename}"
                if fuzzy_match and path.name.endswith(".in"):
                    expected_path += ".in"
            yield PackagerProvidedFile(
                path=path,
                package_name=owning_package_name,
                installed_as_basename=basename,
                provided_key=provided_key,
                definition=definition,
                match_priority=match_priority,
                fuzzy_match=fuzzy_match,
                uses_explicit_package_name=bool(explicit_package),
                name_segment=install_as_name,
                architecture_restriction=arch_restriction,
                expected_path=expected_path,
            )
        return


def _period_stem(stems: Iterable[str]) -> Mapping[int, Sequence[str]]:
    result: Dict[int, List[str]] = {}
    for stem in stems:
        period_count = stem.count(".")
        matched_stems = result.get(period_count)
        if not matched_stems:
            matched_stems = [stem]
            result[period_count] = matched_stems
        else:
            matched_stems.append(stem)
    return result


def detect_all_packager_provided_files(
    packager_provided_files: Mapping[str, PackagerProvidedFileClassSpec],
    debian_dir: VirtualPath,
    binary_packages: Mapping[str, BinaryPackage],
    *,
    allow_fuzzy_matches: bool = False,
    detect_typos: bool = False,
    ignore_paths: Container[str] = frozenset(),
) -> Dict[str, PerPackagePackagerProvidedResult]:
    main_packages = [p.name for p in binary_packages.values() if p.is_main_package]
    if not main_packages:
        assert allow_fuzzy_matches
        main_binary_package = next(
            iter(p.name for p in binary_packages.values() if "Package" in p.fields),
            None,
        )
        if main_binary_package is None:
            return {}
    else:
        main_binary_package = main_packages[0]
    provided_files: Dict[str, Dict[Tuple[str, str], PackagerProvidedFile]] = {
        n: {} for n in binary_packages
    }
    max_periods_in_package_name = max(name.count(".") for name in binary_packages)
    if detect_typos and CAN_DETECT_TYPOS:
        period2stems = _period_stem(packager_provided_files.keys())
    else:
        period2stems = {}

    for entry in debian_dir.iterdir:
        if entry.is_dir:
            continue
        if entry.path in ignore_paths or entry.name in _KNOWN_NON_PPFS:
            continue
        matching_ppfs = _split_path(
            packager_provided_files,
            binary_packages,
            main_binary_package,
            max_periods_in_package_name,
            entry,
            allow_fuzzy_matches=allow_fuzzy_matches,
            period2stems=period2stems,
        )
        for packager_provided_file in matching_ppfs:
            provided_files_for_package = provided_files[
                packager_provided_file.package_name
            ]
            match_key = (
                packager_provided_file.definition.stem,
                packager_provided_file.provided_key,
            )
            existing = provided_files_for_package.get(match_key)
            if (
                existing is not None
                and existing.match_priority > packager_provided_file.match_priority
            ):
                continue
            provided_files_for_package[match_key] = packager_provided_file

    result = {}
    for package_name, provided_file_data in provided_files.items():
        auto_install_list = [
            x for x in provided_file_data.values() if not x.definition.reservation_only
        ]
        reservation_only = collections.defaultdict(list)
        for packager_provided_file in provided_file_data.values():
            if not packager_provided_file.definition.reservation_only:
                continue
            reservation_only[packager_provided_file.definition.stem].append(
                packager_provided_file
            )

        result[package_name] = PerPackagePackagerProvidedResult(
            auto_install_list,
            reservation_only,
        )

    return result
