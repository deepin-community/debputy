import collections
import contextlib
from typing import (
    Optional,
    Dict,
    Callable,
    List,
    Any,
    Union,
    Mapping,
    IO,
    Iterator,
    cast,
    Tuple,
)

from debian.debian_support import DpkgArchTable

from debputy.highlevel_manifest import (
    HighLevelManifest,
    PackageTransformationDefinition,
    MutableYAMLManifest,
)
from debputy.maintscript_snippet import (
    MaintscriptSnippet,
    STD_CONTROL_SCRIPTS,
    MaintscriptSnippetContainer,
)
from debputy.packages import BinaryPackage, SourcePackage
from debputy.path_matcher import (
    MatchRuleType,
    ExactFileSystemPath,
    MatchRule,
)
from debputy.substitution import Substitution
from debputy.util import (
    _normalize_path,
    escape_shell,
    assume_not_none,
)
from debputy.util import _warn, _info
from ._deb_options_profiles import DebBuildOptionsAndProfiles
from .architecture_support import DpkgArchitectureBuildProcessValuesTable
from .filesystem_scan import FSROOverlay
from .installations import InstallRule, PPFInstallRule
from .manifest_parser.exceptions import ManifestParseException
from .manifest_parser.parser_data import ParserContextData
from .manifest_parser.util import AttributePath
from .packager_provided_files import detect_all_packager_provided_files
from .plugin.api import VirtualPath
from .plugin.api.impl_types import (
    TP,
    TTP,
    DispatchingTableParser,
    OPARSER_MANIFEST_ROOT,
    PackageContextData,
)
from .plugin.api.feature_set import PluginProvidedFeatureSet
from .yaml import YAMLError, MANIFEST_YAML

try:
    from Levenshtein import distance
except ImportError:

    def _detect_possible_typo(
        _d,
        _key,
        _attribute_parent_path: AttributePath,
        required: bool,
    ) -> None:
        if required:
            _info(
                "Install python3-levenshtein to have debputy try to detect typos in the manifest."
            )

else:

    def _detect_possible_typo(
        d,
        key,
        _attribute_parent_path: AttributePath,
        _required: bool,
    ) -> None:
        k_len = len(key)
        for actual_key in d:
            if abs(k_len - len(actual_key)) > 2:
                continue
            d = distance(key, actual_key)
            if d > 2:
                continue
            path = _attribute_parent_path.path
            ref = f'at "{path}"' if path else "at the manifest root level"
            _warn(
                f'Possible typo: The key "{actual_key}" should probably have been "{key}" {ref}'
            )


def _per_package_subst_variables(
    p: BinaryPackage,
    *,
    name: Optional[str] = None,
) -> Dict[str, str]:
    return {
        "PACKAGE": name if name is not None else p.name,
    }


class HighLevelManifestParser(ParserContextData):
    def __init__(
        self,
        manifest_path: str,
        source_package: SourcePackage,
        binary_packages: Mapping[str, BinaryPackage],
        substitution: Substitution,
        dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
        dpkg_arch_query_table: DpkgArchTable,
        build_env: DebBuildOptionsAndProfiles,
        plugin_provided_feature_set: PluginProvidedFeatureSet,
        *,
        # Available for testing purposes only
        debian_dir: Union[str, VirtualPath] = "./debian",
    ):
        self.manifest_path = manifest_path
        self._source_package = source_package
        self._binary_packages = binary_packages
        self._mutable_yaml_manifest: Optional[MutableYAMLManifest] = None
        # In source context, some variables are known to be unresolvable. Record this, so
        # we can give better error messages.
        self._substitution = substitution
        self._dpkg_architecture_variables = dpkg_architecture_variables
        self._dpkg_arch_query_table = dpkg_arch_query_table
        self._build_env = build_env
        self._package_state_stack: List[PackageTransformationDefinition] = []
        self._plugin_provided_feature_set = plugin_provided_feature_set
        self._declared_variables = {}

        if isinstance(debian_dir, str):
            debian_dir = FSROOverlay.create_root_dir("debian", debian_dir)

        self._debian_dir = debian_dir

        # Delayed initialized; we rely on this delay to parse the variables.
        self._all_package_states = None

        self._install_rules: Optional[List[InstallRule]] = None
        self._ownership_caches_loaded = False
        self._used = False

    def _ensure_package_states_is_initialized(self) -> None:
        if self._all_package_states is not None:
            return
        substitution = self._substitution
        binary_packages = self._binary_packages
        assert self._all_package_states is None

        self._all_package_states = {
            n: PackageTransformationDefinition(
                binary_package=p,
                substitution=substitution.with_extra_substitutions(
                    **_per_package_subst_variables(p)
                ),
                is_auto_generated_package=False,
                maintscript_snippets=collections.defaultdict(
                    MaintscriptSnippetContainer
                ),
            )
            for n, p in binary_packages.items()
        }
        for n, p in binary_packages.items():
            dbgsym_name = f"{n}-dbgsym"
            if dbgsym_name in self._all_package_states:
                continue
            self._all_package_states[dbgsym_name] = PackageTransformationDefinition(
                binary_package=p,
                substitution=substitution.with_extra_substitutions(
                    **_per_package_subst_variables(p, name=dbgsym_name)
                ),
                is_auto_generated_package=True,
                maintscript_snippets=collections.defaultdict(
                    MaintscriptSnippetContainer
                ),
            )

    @property
    def binary_packages(self) -> Mapping[str, BinaryPackage]:
        return self._binary_packages

    @property
    def _package_states(self) -> Mapping[str, PackageTransformationDefinition]:
        assert self._all_package_states is not None
        return self._all_package_states

    @property
    def dpkg_architecture_variables(self) -> DpkgArchitectureBuildProcessValuesTable:
        return self._dpkg_architecture_variables

    @property
    def dpkg_arch_query_table(self) -> DpkgArchTable:
        return self._dpkg_arch_query_table

    @property
    def build_env(self) -> DebBuildOptionsAndProfiles:
        return self._build_env

    def build_manifest(self) -> HighLevelManifest:
        if self._used:
            raise TypeError("build_manifest can only be called once!")
        self._used = True
        self._ensure_package_states_is_initialized()
        for var, attribute_path in self._declared_variables.items():
            if not self.substitution.is_used(var):
                raise ManifestParseException(
                    f'The variable "{var}" is unused. Either use it or remove it.'
                    f" The variable was declared at {attribute_path.path}."
                )
        if isinstance(self, YAMLManifestParser) and self._mutable_yaml_manifest is None:
            self._mutable_yaml_manifest = MutableYAMLManifest.empty_manifest()
        all_packager_provided_files = detect_all_packager_provided_files(
            self._plugin_provided_feature_set.packager_provided_files,
            self._debian_dir,
            self.binary_packages,
        )

        for package in self._package_states:
            with self.binary_package_context(package) as context:
                if not context.is_auto_generated_package:
                    ppf_result = all_packager_provided_files[package]
                    if ppf_result.auto_installable:
                        context.install_rules.append(
                            PPFInstallRule(
                                context.binary_package,
                                context.substitution,
                                ppf_result.auto_installable,
                            )
                        )
                    context.reserved_packager_provided_files.update(
                        ppf_result.reserved_only
                    )
                self._transform_dpkg_maintscript_helpers_to_snippets()

        return HighLevelManifest(
            self.manifest_path,
            self._mutable_yaml_manifest,
            self._install_rules,
            self._source_package,
            self.binary_packages,
            self.substitution,
            self._package_states,
            self._dpkg_architecture_variables,
            self._dpkg_arch_query_table,
            self._build_env,
            self._plugin_provided_feature_set,
            self._debian_dir,
        )

    @contextlib.contextmanager
    def binary_package_context(
        self, package_name: str
    ) -> Iterator[PackageTransformationDefinition]:
        if package_name not in self._package_states:
            self._error(
                f'The package "{package_name}" is not present in the debian/control file (could not find'
                f' "Package: {package_name}" in a binary stanza) nor is it a -dbgsym package for one'
                " for a package in debian/control."
            )
        package_state = self._package_states[package_name]
        self._package_state_stack.append(package_state)
        ps_len = len(self._package_state_stack)
        yield package_state
        if ps_len != len(self._package_state_stack):
            raise RuntimeError("Internal error: Unbalanced stack manipulation detected")
        self._package_state_stack.pop()

    def dispatch_parser_table_for(self, rule_type: TTP) -> DispatchingTableParser[TP]:
        t = self._plugin_provided_feature_set.manifest_parser_generator.dispatch_parser_table_for(
            rule_type
        )
        if t is None:
            raise AssertionError(
                f"Internal error: No dispatching parser for {rule_type.__name__}"
            )
        return t

    @property
    def substitution(self) -> Substitution:
        if self._package_state_stack:
            return self._package_state_stack[-1].substitution
        return self._substitution

    def add_extra_substitution_variables(
        self,
        **extra_substitutions: Tuple[str, AttributePath],
    ) -> Substitution:
        if self._package_state_stack or self._all_package_states is not None:
            # For one, it would not "bubble up" correctly when added to the lowest stack.
            # And if it is not added to the lowest stack, then you get errors about it being
            # unknown as soon as you leave the stack (which is weird for the user when
            # the variable is something known, sometimes not)
            raise RuntimeError("Cannot use add_extra_substitution from this state")
        for key, (_, path) in extra_substitutions.items():
            self._declared_variables[key] = path
        self._substitution = self._substitution.with_extra_substitutions(
            **{k: v[0] for k, v in extra_substitutions.items()}
        )
        return self._substitution

    @property
    def current_binary_package_state(self) -> PackageTransformationDefinition:
        if not self._package_state_stack:
            raise RuntimeError("Invalid state: Not in a binary package context")
        return self._package_state_stack[-1]

    @property
    def is_in_binary_package_state(self) -> bool:
        return bool(self._package_state_stack)

    def _transform_dpkg_maintscript_helpers_to_snippets(self) -> None:
        package_state = self.current_binary_package_state
        for dmh in package_state.dpkg_maintscript_helper_snippets:
            snippet = MaintscriptSnippet(
                definition_source=dmh.definition_source,
                snippet=f'dpkg-maintscript-helper {escape_shell(*dmh.cmdline)} -- "$@"\n',
            )
            for script in STD_CONTROL_SCRIPTS:
                package_state.maintscript_snippets[script].append(snippet)

    def normalize_path(
        self,
        path: str,
        definition_source: AttributePath,
        *,
        allow_root_dir_match: bool = False,
    ) -> ExactFileSystemPath:
        try:
            normalized = _normalize_path(path)
        except ValueError:
            self._error(
                f'The path "{path}" provided in {definition_source.path} should be relative to the root of the'
                ' package and not use any ".." or "." segments.'
            )
        if normalized == "." and not allow_root_dir_match:
            self._error(
                "Manifests must not change the root directory of the deb file.  Please correct"
                f' "{definition_source.path}" (path: "{path}) in {self.manifest_path}'
            )
        return ExactFileSystemPath(
            self.substitution.substitute(normalized, definition_source.path)
        )

    def parse_path_or_glob(
        self,
        path_or_glob: str,
        definition_source: AttributePath,
    ) -> MatchRule:
        match_rule = MatchRule.from_path_or_glob(
            path_or_glob, definition_source.path, substitution=self.substitution
        )
        # NB: "." and "/" will be translated to MATCH_ANYTHING by MatchRule.from_path_or_glob,
        # so there is no need to check for an exact match on "." like in normalize_path.
        if match_rule.rule_type == MatchRuleType.MATCH_ANYTHING:
            self._error(
                f'The chosen match rule "{path_or_glob}" matches everything (including the deb root directory).'
                f' Please correct "{definition_source.path}" (path: "{path_or_glob}) in {self.manifest_path} to'
                f' something that matches "less" than everything.'
            )
        return match_rule

    def parse_manifest(self) -> HighLevelManifest:
        raise NotImplementedError


class YAMLManifestParser(HighLevelManifestParser):
    def _optional_key(
        self,
        d: Mapping[str, Any],
        key: str,
        attribute_parent_path: AttributePath,
        expected_type=None,
        default_value=None,
    ):
        v = d.get(key)
        if v is None:
            _detect_possible_typo(d, key, attribute_parent_path, False)
            return default_value
        if expected_type is not None:
            return self._ensure_value_is_type(
                v, expected_type, key, attribute_parent_path
            )
        return v

    def _required_key(
        self,
        d: Mapping[str, Any],
        key: str,
        attribute_parent_path: AttributePath,
        expected_type=None,
        extra: Optional[Union[str, Callable[[], str]]] = None,
    ):
        v = d.get(key)
        if v is None:
            _detect_possible_typo(d, key, attribute_parent_path, True)
            if extra is not None:
                msg = extra if isinstance(extra, str) else extra()
                extra_info = " " + msg
            else:
                extra_info = ""
            self._error(
                f'Missing required key {key} at {attribute_parent_path.path} in manifest "{self.manifest_path}.'
                f"{extra_info}"
            )

        if expected_type is not None:
            return self._ensure_value_is_type(
                v, expected_type, key, attribute_parent_path
            )
        return v

    def _ensure_value_is_type(
        self,
        v,
        t,
        key: Union[str, int, AttributePath],
        attribute_parent_path: Optional[AttributePath],
    ):
        if v is None:
            return None
        if not isinstance(v, t):
            if isinstance(t, tuple):
                t_msg = "one of: " + ", ".join(x.__name__ for x in t)
            else:
                t_msg = f"a {t.__name__}"
            key_path = (
                key.path
                if isinstance(key, AttributePath)
                else assume_not_none(attribute_parent_path)[key].path
            )
            self._error(
                f'The key {key_path} must be {t_msg} in manifest "{self.manifest_path}"'
            )
        return v

    def from_yaml_dict(self, yaml_data: object) -> "HighLevelManifest":
        attribute_path = AttributePath.root_path()
        parser_generator = self._plugin_provided_feature_set.manifest_parser_generator
        dispatchable_object_parsers = parser_generator.dispatchable_object_parsers
        manifest_root_parser = dispatchable_object_parsers[OPARSER_MANIFEST_ROOT]
        parsed_data = cast(
            "ManifestRootRule",
            manifest_root_parser.parse_input(
                yaml_data,
                attribute_path,
                parser_context=self,
            ),
        )

        packages_dict: Mapping[str, PackageContextData[Mapping[str, Any]]] = cast(
            "Mapping[str, PackageContextData[Mapping[str, Any]]]",
            parsed_data.get("packages", {}),
        )
        install_rules = parsed_data.get("installations")
        if install_rules:
            self._install_rules = install_rules
        packages_parent_path = attribute_path["packages"]
        for package_name_raw, pcd in packages_dict.items():
            definition_source = packages_parent_path[package_name_raw]
            package_name = pcd.resolved_package_name
            parsed = pcd.value

            package_state: PackageTransformationDefinition
            with self.binary_package_context(package_name) as package_state:
                if package_state.is_auto_generated_package:
                    # Maybe lift (part) of this restriction.
                    self._error(
                        f'Cannot define rules for package "{package_name}" (at {definition_source.path}). It is an'
                        " auto-generated package."
                    )
                binary_version = parsed.get("binary-version")
                if binary_version is not None:
                    package_state.binary_version = (
                        package_state.substitution.substitute(
                            binary_version,
                            definition_source["binary-version"].path,
                        )
                    )
                search_dirs = parsed.get("installation_search_dirs")
                if search_dirs is not None:
                    package_state.search_dirs = search_dirs
                transformations = parsed.get("transformations")
                conffile_management = parsed.get("conffile_management")
                service_rules = parsed.get("services")
                if transformations:
                    package_state.transformations.extend(transformations)
                if conffile_management:
                    package_state.dpkg_maintscript_helper_snippets.extend(
                        conffile_management
                    )
                if service_rules:
                    package_state.requested_service_rules.extend(service_rules)

        return self.build_manifest()

    def _parse_manifest(self, fd: Union[IO[bytes], str]) -> HighLevelManifest:
        try:
            data = MANIFEST_YAML.load(fd)
        except YAMLError as e:
            msg = str(e)
            lines = msg.splitlines(keepends=True)
            i = -1
            for i, line in enumerate(lines):
                # Avoid an irrelevant "how do configure the YAML parser" message, which the
                # user cannot use.
                if line.startswith("To suppress this check"):
                    break
            if i > -1 and len(lines) > i + 1:
                lines = lines[:i]
                msg = "".join(lines)
            msg = msg.rstrip()
            msg += (
                f"\n\nYou can use `yamllint -d relaxed {escape_shell(self.manifest_path)}` to validate"
                " the YAML syntax. The yamllint tool also supports style rules for YAML documents"
                " (such as indentation rules) in case that is of interest."
            )
            raise ManifestParseException(
                f"Could not parse {self.manifest_path} as a YAML document: {msg}"
            ) from e
        self._mutable_yaml_manifest = MutableYAMLManifest(data)
        return self.from_yaml_dict(data)

    def parse_manifest(
        self,
        *,
        fd: Optional[Union[IO[bytes], str]] = None,
    ) -> HighLevelManifest:
        if fd is None:
            with open(self.manifest_path, "rb") as fd:
                return self._parse_manifest(fd)
        else:
            return self._parse_manifest(fd)
