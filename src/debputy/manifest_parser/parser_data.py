import contextlib
from typing import (
    Iterator,
    Optional,
    Mapping,
    NoReturn,
    Union,
    Any,
    TYPE_CHECKING,
    Tuple,
)

from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.manifest_conditions import ManifestCondition
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.plugin.api.impl_types import (
    _ALL_PACKAGE_TYPES,
    resolve_package_type_selectors,
    TP,
    DispatchingTableParser,
    TTP,
    DispatchingObjectParser,
)
from debputy.plugin.api.spec import PackageTypeSelector
from debputy.substitution import Substitution


if TYPE_CHECKING:
    from debputy.highlevel_manifest import PackageTransformationDefinition


class ParserContextData:
    @property
    def binary_packages(self) -> Mapping[str, BinaryPackage]:
        raise NotImplementedError

    @property
    def _package_states(self) -> Mapping[str, "PackageTransformationDefinition"]:
        raise NotImplementedError

    @property
    def is_single_binary_package(self) -> bool:
        return len(self.binary_packages) == 1

    def single_binary_package(
        self,
        attribute_path: AttributePath,
        *,
        package_type: PackageTypeSelector = _ALL_PACKAGE_TYPES,
        package_attribute: Optional[str] = None,
    ) -> Optional[BinaryPackage]:
        resolved_package_types = resolve_package_type_selectors(package_type)
        possible_matches = [
            p
            for p in self.binary_packages.values()
            if p.package_type in resolved_package_types
        ]
        if len(possible_matches) == 1:
            return possible_matches[0]

        if package_attribute is not None:
            raise ManifestParseException(
                f"The {attribute_path.path} rule needs the attribute `{package_attribute}`"
                " for this source package."
            )

        if not possible_matches:
            _package_types = ", ".join(sorted(resolved_package_types))
            raise ManifestParseException(
                f"The {attribute_path.path} rule is not applicable to this source package"
                f" (it only applies to source packages that builds exactly one of"
                f" the following package types: {_package_types})."
            )
        raise ManifestParseException(
            f"The {attribute_path.path} rule is not applicable to multi-binary packages."
        )

    def _error(self, msg: str) -> "NoReturn":
        raise ManifestParseException(msg)

    def is_known_package(self, package_name: str) -> bool:
        return package_name in self._package_states

    def binary_package_data(
        self,
        package_name: str,
    ) -> "PackageTransformationDefinition":
        if package_name not in self._package_states:
            self._error(
                f'The package "{package_name}" is not present in the debian/control file (could not find'
                f' "Package: {package_name}" in a binary stanza) nor is it a -dbgsym package for one'
                " for a package in debian/control."
            )
        return self._package_states[package_name]

    @property
    def dpkg_architecture_variables(self) -> DpkgArchitectureBuildProcessValuesTable:
        raise NotImplementedError

    @property
    def dpkg_arch_query_table(self) -> DpkgArchTable:
        raise NotImplementedError

    @property
    def build_env(self) -> DebBuildOptionsAndProfiles:
        raise NotImplementedError

    @contextlib.contextmanager
    def binary_package_context(
        self,
        package_name: str,
    ) -> Iterator["PackageTransformationDefinition"]:
        raise NotImplementedError

    @property
    def substitution(self) -> Substitution:
        raise NotImplementedError

    @property
    def current_binary_package_state(self) -> "PackageTransformationDefinition":
        raise NotImplementedError

    @property
    def is_in_binary_package_state(self) -> bool:
        raise NotImplementedError

    def dispatch_parser_table_for(self, rule_type: TTP) -> DispatchingTableParser[TP]:
        raise NotImplementedError
