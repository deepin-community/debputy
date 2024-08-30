from typing import (
    Dict,
    Union,
    Tuple,
    Optional,
    Set,
    cast,
    Mapping,
    FrozenSet,
    Iterable,
    overload,
)

from debian.deb822 import Deb822
from debian.debian_support import DpkgArchTable

from ._deb_options_profiles import DebBuildOptionsAndProfiles
from .architecture_support import (
    DpkgArchitectureBuildProcessValuesTable,
    dpkg_architecture_table,
)
from .lsp.vendoring._deb822_repro import (
    parse_deb822_file,
    Deb822ParagraphElement,
    Deb822FileElement,
)
from .util import DEFAULT_PACKAGE_TYPE, UDEB_PACKAGE_TYPE, _error, active_profiles_match

_MANDATORY_BINARY_PACKAGE_FIELD = [
    "Package",
    "Architecture",
]


class DctrlParser:

    def __init__(
        self,
        selected_packages: Union[Set[str], FrozenSet[str]],
        excluded_packages: Union[Set[str], FrozenSet[str]],
        select_arch_all: bool,
        select_arch_any: bool,
        dpkg_architecture_variables: Optional[
            DpkgArchitectureBuildProcessValuesTable
        ] = None,
        dpkg_arch_query_table: Optional[DpkgArchTable] = None,
        deb_options_and_profiles: Optional[DebBuildOptionsAndProfiles] = None,
        ignore_errors: bool = False,
    ) -> None:
        if dpkg_architecture_variables is None:
            dpkg_architecture_variables = dpkg_architecture_table()
        if dpkg_arch_query_table is None:
            dpkg_arch_query_table = DpkgArchTable.load_arch_table()
        if deb_options_and_profiles is None:
            deb_options_and_profiles = DebBuildOptionsAndProfiles.instance()

        # If no selection option is set, then all packages are acted on (except the
        # excluded ones)
        if not selected_packages and not select_arch_all and not select_arch_any:
            select_arch_all = True
            select_arch_any = True

        self.selected_packages = selected_packages
        self.excluded_packages = excluded_packages
        self.select_arch_all = select_arch_all
        self.select_arch_any = select_arch_any
        self.dpkg_architecture_variables = dpkg_architecture_variables
        self.dpkg_arch_query_table = dpkg_arch_query_table
        self.deb_options_and_profiles = deb_options_and_profiles
        self.ignore_errors = ignore_errors

    @overload
    def parse_source_debian_control(
        self,
        debian_control_lines: Iterable[str],
    ) -> Tuple[Deb822FileElement, "SourcePackage", Dict[str, "BinaryPackage"]]: ...

    @overload
    def parse_source_debian_control(
        self,
        debian_control_lines: Iterable[str],
        *,
        ignore_errors: bool = False,
    ) -> Tuple[
        Deb822FileElement,
        Optional["SourcePackage"],
        Optional[Dict[str, "BinaryPackage"]],
    ]: ...

    def parse_source_debian_control(
        self,
        debian_control_lines: Iterable[str],
        *,
        ignore_errors: bool = False,
    ) -> Tuple[
        Optional[Deb822FileElement],
        Optional["SourcePackage"],
        Optional[Dict[str, "BinaryPackage"]],
    ]:
        deb822_file = parse_deb822_file(
            debian_control_lines,
            accept_files_with_error_tokens=ignore_errors,
            accept_files_with_duplicated_fields=ignore_errors,
        )
        dctrl_paragraphs = list(deb822_file)
        if len(dctrl_paragraphs) < 2:
            if not ignore_errors:
                _error(
                    "debian/control must contain at least two stanza (1 Source + 1-N Package stanza)"
                )
            source_package = (
                SourcePackage(dctrl_paragraphs[0]) if dctrl_paragraphs else None
            )
            return deb822_file, source_package, None

        source_package = SourcePackage(dctrl_paragraphs[0])
        bin_pkgs = []
        for i, p in enumerate(dctrl_paragraphs[1:], 1):
            if ignore_errors:
                if "Package" not in p:
                    continue
                missing_field = any(f not in p for f in _MANDATORY_BINARY_PACKAGE_FIELD)
                if missing_field:
                    # In the LSP context, it is problematic if we "add" fields as it ranges and provides invalid
                    # results. However, `debputy` also needs the mandatory fields to be there, so we clone the
                    # stanzas that `debputy` (build) will see to add missing fields.
                    copy = Deb822(p)
                    for f in _MANDATORY_BINARY_PACKAGE_FIELD:
                        if f not in p:
                            copy[f] = "unknown"
                    p = copy
            bin_pkgs.append(
                _create_binary_package(
                    p,
                    self.selected_packages,
                    self.excluded_packages,
                    self.select_arch_all,
                    self.select_arch_any,
                    self.dpkg_architecture_variables,
                    self.dpkg_arch_query_table,
                    self.deb_options_and_profiles,
                    i,
                )
            )
        bin_pkgs_table = {p.name: p for p in bin_pkgs}

        if not ignore_errors:
            if not self.selected_packages.issubset(bin_pkgs_table.keys()):
                unknown = self.selected_packages - bin_pkgs_table.keys()
                _error(
                    f"The following *selected* packages (-p) are not listed in debian/control: {sorted(unknown)}"
                )
            if not self.excluded_packages.issubset(bin_pkgs_table.keys()):
                unknown = self.selected_packages - bin_pkgs_table.keys()
                _error(
                    f"The following *excluded* packages (-N) are not listed in debian/control: {sorted(unknown)}"
                )

        return deb822_file, source_package, bin_pkgs_table


def _check_package_sets(
    provided_packages: Set[str],
    valid_package_names: Set[str],
    option_name: str,
) -> None:
    # SonarLint proposes to use `provided_packages > valid_package_names`, which is valid for boolean
    # logic, but not for set logic.  We want to assert that provided_packages is a proper subset
    # of valid_package_names.  The rewrite would cause no errors for {'foo'} > {'bar'} - in set logic,
    # neither is a superset / subset of the other, but we want an error for this case.
    #
    # Bug filed:
    # https://community.sonarsource.com/t/sonarlint-python-s1940-rule-does-not-seem-to-take-set-logic-into-account/79718
    if not (provided_packages <= valid_package_names):
        non_existing_packages = sorted(provided_packages - valid_package_names)
        invalid_package_list = ", ".join(non_existing_packages)
        msg = (
            f"Invalid package names passed to {option_name}: {invalid_package_list}: "
            f'Valid package names are: {", ".join(valid_package_names)}'
        )
        _error(msg)


def _create_binary_package(
    paragraph: Union[Deb822ParagraphElement, Dict[str, str]],
    selected_packages: Union[Set[str], FrozenSet[str]],
    excluded_packages: Union[Set[str], FrozenSet[str]],
    select_arch_all: bool,
    select_arch_any: bool,
    dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
    dpkg_arch_query_table: DpkgArchTable,
    build_env: DebBuildOptionsAndProfiles,
    paragraph_index: int,
) -> "BinaryPackage":
    try:
        package_name = paragraph["Package"]
    except KeyError:
        _error(f'Missing mandatory field "Package" in stanza number {paragraph_index}')
        # The raise is there to help PyCharm type-checking (which fails at "NoReturn")
        raise

    for mandatory_field in _MANDATORY_BINARY_PACKAGE_FIELD:
        if mandatory_field not in paragraph:
            _error(
                f'Missing mandatory field "{mandatory_field}" for binary package {package_name}'
                f" (stanza number {paragraph_index})"
            )

    architecture = paragraph["Architecture"]

    if paragraph_index < 1:
        raise ValueError("stanza index must be 1-indexed (1, 2, ...)")
    is_main_package = paragraph_index == 1

    if package_name in excluded_packages:
        should_act_on = False
    elif package_name in selected_packages:
        should_act_on = True
    elif architecture == "all":
        should_act_on = select_arch_all
    else:
        should_act_on = select_arch_any

    profiles_raw = paragraph.get("Build-Profiles", "").strip()
    if should_act_on and profiles_raw:
        try:
            should_act_on = active_profiles_match(
                profiles_raw, build_env.deb_build_profiles
            )
        except ValueError as e:
            _error(f"Invalid Build-Profiles field for {package_name}: {e.args[0]}")

    return BinaryPackage(
        paragraph,
        dpkg_architecture_variables,
        dpkg_arch_query_table,
        should_be_acted_on=should_act_on,
        is_main_package=is_main_package,
    )


def _check_binary_arch(
    arch_table: DpkgArchTable,
    binary_arch: str,
    declared_arch: str,
) -> bool:
    if binary_arch == "all":
        return True
    arch_wildcards = declared_arch.split()
    for arch_wildcard in arch_wildcards:
        if arch_table.matches_architecture(binary_arch, arch_wildcard):
            return True
    return False


class BinaryPackage:
    __slots__ = [
        "_package_fields",
        "_dbgsym_binary_package",
        "_should_be_acted_on",
        "_dpkg_architecture_variables",
        "_declared_arch_matches_output_arch",
        "_is_main_package",
        "_substvars",
        "_maintscript_snippets",
    ]

    def __init__(
        self,
        fields: Union[Mapping[str, str], Deb822ParagraphElement],
        dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
        dpkg_arch_query: DpkgArchTable,
        *,
        is_main_package: bool = False,
        should_be_acted_on: bool = True,
    ) -> None:
        super(BinaryPackage, self).__init__()
        # Typing-wise, Deb822ParagraphElement is *not* a Mapping[str, str] but it behaves enough
        # like one that we rely on it and just cast it.
        self._package_fields = cast("Mapping[str, str]", fields)
        self._dbgsym_binary_package = None
        self._should_be_acted_on = should_be_acted_on
        self._dpkg_architecture_variables = dpkg_architecture_variables
        self._is_main_package = is_main_package
        self._declared_arch_matches_output_arch = _check_binary_arch(
            dpkg_arch_query, self.resolved_architecture, self.declared_architecture
        )

    @property
    def name(self) -> str:
        return self.fields["Package"]

    @property
    def archive_section(self) -> str:
        value = self.fields.get("Section")
        if value is None:
            return "Unknown"
        return value

    @property
    def archive_component(self) -> str:
        component = ""
        section = self.archive_section
        if "/" in section:
            component = section.rsplit("/", 1)[0]
            # The "main" component is always shortened to ""
            if component == "main":
                component = ""
        return component

    @property
    def is_essential(self) -> bool:
        return self._package_fields.get("Essential") == "yes"

    @property
    def is_udeb(self) -> bool:
        return self.package_type == UDEB_PACKAGE_TYPE

    @property
    def should_be_acted_on(self) -> bool:
        return self._should_be_acted_on and self._declared_arch_matches_output_arch

    @property
    def fields(self) -> Mapping[str, str]:
        return self._package_fields

    @property
    def resolved_architecture(self) -> str:
        arch = self.declared_architecture
        if arch == "all":
            return arch
        if self._x_dh_build_for_type == "target":
            return self._dpkg_architecture_variables["DEB_TARGET_ARCH"]
        return self._dpkg_architecture_variables.current_host_arch

    def package_deb_architecture_variable(self, variable_suffix: str) -> str:
        if self._x_dh_build_for_type == "target":
            return self._dpkg_architecture_variables[f"DEB_TARGET_{variable_suffix}"]
        return self._dpkg_architecture_variables[f"DEB_HOST_{variable_suffix}"]

    @property
    def deb_multiarch(self) -> str:
        return self.package_deb_architecture_variable("MULTIARCH")

    @property
    def _x_dh_build_for_type(self) -> str:
        v = self._package_fields.get("X-DH-Build-For-Type")
        if v is None:
            return "host"
        return v.lower()

    @property
    def package_type(self) -> str:
        """Short for Package-Type (with proper default if absent)"""
        v = self.fields.get("Package-Type")
        if v is None:
            return DEFAULT_PACKAGE_TYPE
        return v

    @property
    def is_main_package(self) -> bool:
        return self._is_main_package

    def cross_command(self, command: str) -> str:
        arch_table = self._dpkg_architecture_variables
        if self._x_dh_build_for_type == "target":
            target_gnu_type = arch_table["DEB_TARGET_GNU_TYPE"]
            if arch_table["DEB_HOST_GNU_TYPE"] != target_gnu_type:
                return f"{target_gnu_type}-{command}"
        if arch_table.is_cross_compiling:
            return f"{arch_table['DEB_HOST_GNU_TYPE']}-{command}"
        return command

    @property
    def declared_architecture(self) -> str:
        return self.fields["Architecture"]

    @property
    def is_arch_all(self) -> bool:
        return self.declared_architecture == "all"


class SourcePackage:
    __slots__ = ("_package_fields",)

    def __init__(self, fields: Union[Mapping[str, str], Deb822ParagraphElement]):
        # Typing-wise, Deb822ParagraphElement is *not* a Mapping[str, str] but it behaves enough
        # like one that we rely on it and just cast it.
        self._package_fields = cast("Mapping[str, str]", fields)

    @property
    def fields(self) -> Mapping[str, str]:
        return self._package_fields

    @property
    def name(self) -> str:
        return self._package_fields["Source"]
