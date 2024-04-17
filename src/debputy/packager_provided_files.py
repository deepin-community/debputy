import collections
import dataclasses
from typing import Mapping, Iterable, Dict, List, Optional, Tuple

from debputy.packages import BinaryPackage
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.impl_types import PackagerProvidedFileClassSpec
from debputy.util import _error


@dataclasses.dataclass(frozen=True, slots=True)
class PackagerProvidedFile:
    path: VirtualPath
    package_name: str
    installed_as_basename: str
    provided_key: str
    definition: PackagerProvidedFileClassSpec
    match_priority: int = 0
    fuzzy_match: bool = False

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


def _find_definition(
    packager_provided_files: Mapping[str, PackagerProvidedFileClassSpec],
    basename: str,
) -> Tuple[Optional[str], Optional[PackagerProvidedFileClassSpec]]:
    definition = packager_provided_files.get(basename)
    if definition is not None:
        return None, definition
    install_as_name = basename
    file_class = ""
    while "." in install_as_name:
        install_as_name, file_class_part = install_as_name.rsplit(".", 1)
        file_class = (
            file_class_part + "." + file_class if file_class != "" else file_class_part
        )
        definition = packager_provided_files.get(file_class)
        if definition is not None:
            return install_as_name, definition
    return None, None


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

        if allow_fuzzy_matches and basename.endswith(".in") and len(basename) > 3:
            basename = basename[:-3]
            fuzzy_match = True

        if "." in basename:
            remaining, last_word = basename.rsplit(".", 1)
            # We cannot use "resolved_architecture" as it would return "all".
            if last_word == owning_package.package_deb_architecture_variable("ARCH"):
                match_priority = 3
                basename = remaining
                had_arch = True
            elif last_word == owning_package.package_deb_architecture_variable(
                "ARCH_OS"
            ):
                match_priority = 2
                basename = remaining
                had_arch = True
            elif last_word == "all" and owning_package.is_arch_all:
                # This case does not make sense, but we detect it so we can report an error
                # via _check_mismatches.
                match_priority = -1
                basename = remaining
                had_arch = True

        install_as_name, definition = _find_definition(
            packager_provided_files, basename
        )
        if definition is None:
            continue

        # Note: bug_950723 implies allow_fuzzy_matches
        if bug_950723 and not definition.bug_950723:
            continue

        _check_mismatches(
            path,
            definition,
            owning_package,
            install_as_name,
            had_arch,
        )
        if (
            definition.packageless_is_fallback_for_all_packages
            and install_as_name is None
            and not had_arch
            and not explicit_package
        ):
            yield from (
                PackagerProvidedFile(
                    path=path,
                    package_name=n,
                    installed_as_basename=f"{n}@" if bug_950723 else n,
                    provided_key=".UNNAMED." if bug_950723 else ".UNNAMED@.",
                    definition=definition,
                    match_priority=match_priority,
                    fuzzy_match=fuzzy_match,
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
            yield PackagerProvidedFile(
                path=path,
                package_name=owning_package_name,
                installed_as_basename=basename,
                provided_key=provided_key,
                definition=definition,
                match_priority=match_priority,
                fuzzy_match=fuzzy_match,
            )
        return


def detect_all_packager_provided_files(
    packager_provided_files: Mapping[str, PackagerProvidedFileClassSpec],
    debian_dir: VirtualPath,
    binary_packages: Mapping[str, BinaryPackage],
    *,
    allow_fuzzy_matches: bool = False,
) -> Dict[str, PerPackagePackagerProvidedResult]:
    main_binary_package = [
        p.name for p in binary_packages.values() if p.is_main_package
    ][0]
    provided_files: Dict[str, Dict[Tuple[str, str], PackagerProvidedFile]] = {
        n: {} for n in binary_packages
    }
    max_periods_in_package_name = max(name.count(".") for name in binary_packages)

    for entry in debian_dir.iterdir:
        if entry.is_dir:
            continue
        matching_ppfs = _split_path(
            packager_provided_files,
            binary_packages,
            main_binary_package,
            max_periods_in_package_name,
            entry,
            allow_fuzzy_matches=allow_fuzzy_matches,
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
