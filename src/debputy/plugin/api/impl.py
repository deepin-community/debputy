import contextlib
import dataclasses
import functools
import importlib
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
from abc import ABC
from json import JSONDecodeError
from typing import (
    Optional,
    Callable,
    Dict,
    Tuple,
    Iterable,
    Sequence,
    Type,
    List,
    Union,
    Set,
    Iterator,
    IO,
    Mapping,
    AbstractSet,
    cast,
    FrozenSet,
    Any,
    Literal,
)

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.exceptions import (
    DebputySubstitutionError,
    PluginConflictError,
    PluginMetadataError,
    PluginBaseError,
    PluginInitializationError,
    PluginAPIViolationError,
    PluginNotFoundError,
)
from debputy.maintscript_snippet import (
    STD_CONTROL_SCRIPTS,
    MaintscriptSnippetContainer,
    MaintscriptSnippet,
)
from debputy.manifest_parser.base_types import TypeMapping
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
    PackagerProvidedFileClassSpec,
    MetadataOrMaintscriptDetector,
    PluginProvidedTrigger,
    TTP,
    DIPHandler,
    PF,
    SF,
    DIPKWHandler,
    PluginProvidedManifestVariable,
    PluginProvidedPackageProcessor,
    PluginProvidedDiscardRule,
    AutomaticDiscardRuleExample,
    PPFFormatParam,
    ServiceManagerDetails,
    resolve_package_type_selectors,
    KnownPackagingFileInfo,
    PluginProvidedKnownPackagingFile,
    InstallPatternDHCompatRule,
    PluginProvidedTypeMapping,
)
from debputy.plugin.api.plugin_parser import (
    PLUGIN_METADATA_PARSER,
    PluginJsonMetadata,
    PLUGIN_PPF_PARSER,
    PackagerProvidedFileJsonDescription,
    PLUGIN_MANIFEST_VARS_PARSER,
    PLUGIN_KNOWN_PACKAGING_FILES_PARSER,
)
from debputy.plugin.api.spec import (
    MaintscriptAccessor,
    Maintscript,
    DpkgTriggerType,
    BinaryCtrlAccessor,
    PackageProcessingContext,
    MetadataAutoDetector,
    PluginInitializationEntryPoint,
    DebputyPluginInitializer,
    PackageTypeSelector,
    FlushableSubstvars,
    ParserDocumentation,
    PackageProcessor,
    VirtualPath,
    ServiceIntegrator,
    ServiceDetector,
    ServiceRegistry,
    ServiceDefinition,
    DSD,
    ServiceUpgradeRule,
    PackagerProvidedFileReferenceDocumentation,
    packager_provided_file_reference_documentation,
    TypeMappingDocumentation,
)
from debputy.substitution import (
    Substitution,
    VariableNameState,
    SUBST_VAR_RE,
    VariableContext,
)
from debputy.util import (
    _normalize_path,
    POSTINST_DEFAULT_CONDITION,
    _error,
    print_command,
    _warn,
)

PLUGIN_TEST_SUFFIX = re.compile(r"_(?:t|test|check)(?:_([a-z0-9_]+))?[.]py$")


def _validate_known_packaging_file_dh_compat_rules(
    dh_compat_rules: Optional[List[InstallPatternDHCompatRule]],
) -> None:
    max_compat = None
    if not dh_compat_rules:
        return
    dh_compat_rule: InstallPatternDHCompatRule
    for idx, dh_compat_rule in enumerate(dh_compat_rules):
        dh_version = dh_compat_rule.get("starting_with_debhelper_version")
        compat = dh_compat_rule.get("starting_with_compat_level")

        remaining = dh_compat_rule.keys() - {
            "after_debhelper_version",
            "starting_with_compat_level",
        }
        if not remaining:
            raise ValueError(
                f"The dh compat-rule at index {idx} does not affect anything not have any rules!? So why have it?"
            )
        if dh_version is None and compat is None and idx < len(dh_compat_rules) - 1:
            raise ValueError(
                f"The dh compat-rule at index {idx} is not the last and is missing either"
                " before-debhelper-version or before-compat-level"
            )
        if compat is not None and compat < 0:
            raise ValueError(
                f"There is no compat below 1 but dh compat-rule at {idx} wants to declare some rule"
                f" for something that appeared when migrating from {compat} to {compat + 1}."
            )

        if max_compat is None:
            max_compat = compat
        elif compat is not None:
            if compat >= max_compat:
                raise ValueError(
                    f"The dh compat-rule at {idx} should be moved earlier than the entry for compat {max_compat}."
                )
            max_compat = compat

        install_pattern = dh_compat_rule.get("install_pattern")
        if (
            install_pattern is not None
            and _normalize_path(install_pattern, with_prefix=False) != install_pattern
        ):
            raise ValueError(
                f"The install-pattern in dh compat-rule at {idx} must be normalized as"
                f' "{_normalize_path(install_pattern, with_prefix=False)}".'
            )


class DebputyPluginInitializerProvider(DebputyPluginInitializer):
    __slots__ = (
        "_plugin_metadata",
        "_feature_set",
        "_plugin_detector_ids",
        "_substitution",
        "_unloaders",
        "_load_started",
    )

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        feature_set: PluginProvidedFeatureSet,
        substitution: Substitution,
    ) -> None:
        self._plugin_metadata: DebputyPluginMetadata = plugin_metadata
        self._feature_set = feature_set
        self._plugin_detector_ids: Set[str] = set()
        self._substitution = substitution
        self._unloaders: List[Callable[[], None]] = []
        self._load_started = False

    def unload_plugin(self) -> None:
        if self._load_started:
            for unloader in self._unloaders:
                unloader()
            del self._feature_set.plugin_data[self._plugin_name]

    def load_plugin(self) -> None:
        metadata = self._plugin_metadata
        if metadata.plugin_name in self._feature_set.plugin_data:
            raise PluginConflictError(
                f'The plugin "{metadata.plugin_name}" has already been loaded!?'
            )
        assert (
            metadata.api_compat_version == 1
        ), f"Unsupported plugin API compat version {metadata.api_compat_version}"
        self._feature_set.plugin_data[metadata.plugin_name] = metadata
        self._load_started = True
        assert not metadata.is_initialized
        try:
            metadata.initialize_plugin(self)
        except Exception as e:
            initializer = metadata.plugin_initializer
            if (
                isinstance(e, TypeError)
                and initializer is not None
                and not callable(initializer)
            ):
                raise PluginMetadataError(
                    f"The specified entry point for plugin {metadata.plugin_name} does not appear to be a"
                    f" callable (callable returns False). The specified entry point identifies"
                    f' itself as "{initializer.__qualname__}".'
                ) from e
            elif isinstance(e, PluginBaseError):
                raise
            raise PluginInitializationError(
                f"Exception while attempting to load plugin {metadata.plugin_name}"
            ) from e

    def packager_provided_file(
        self,
        stem: str,
        installed_path: str,
        *,
        default_mode: int = 0o0644,
        default_priority: Optional[int] = None,
        allow_name_segment: bool = True,
        allow_architecture_segment: bool = False,
        post_formatting_rewrite: Optional[Callable[[str], str]] = None,
        packageless_is_fallback_for_all_packages: bool = False,
        reservation_only: bool = False,
        format_callback: Optional[
            Callable[[str, PPFFormatParam, VirtualPath], str]
        ] = None,
        reference_documentation: Optional[
            PackagerProvidedFileReferenceDocumentation
        ] = None,
    ) -> None:
        packager_provided_files = self._feature_set.packager_provided_files
        existing = packager_provided_files.get(stem)

        if format_callback is not None and self._plugin_name != "debputy":
            raise ValueError(
                "Sorry; Using format_callback is a debputy-internal"
                f" API. Triggered by plugin {self._plugin_name}"
            )

        if installed_path.endswith("/"):
            raise ValueError(
                f'The installed_path ends with "/" indicating it is a directory, but it must be a file.'
                f" Triggered by plugin {self._plugin_name}."
            )

        installed_path = _normalize_path(installed_path)

        has_name_var = "{name}" in installed_path

        if installed_path.startswith("./DEBIAN") or reservation_only:
            # Special-case, used for control files.
            if self._plugin_name != "debputy":
                raise ValueError(
                    "Sorry; Using DEBIAN as install path or/and reservation_only is a debputy-internal"
                    f" API. Triggered by plugin {self._plugin_name}"
                )
        elif not has_name_var and "{owning_package}" not in installed_path:
            raise ValueError(
                'The installed_path must contain a "{name}" (preferred) or a "{owning_package}"'
                " substitution (or have installed_path end with a slash).  Otherwise, the installed"
                f" path would caused file-conflicts. Triggered by plugin {self._plugin_name}"
            )

        if allow_name_segment and not has_name_var:
            raise ValueError(
                'When allow_name_segment is True, the installed_path must have a "{name}" substitution'
                " variable. Otherwise, the name segment will not work properly. Triggered by"
                f" plugin {self._plugin_name}"
            )

        if (
            default_priority is not None
            and "{priority}" not in installed_path
            and "{priority:02}" not in installed_path
        ):
            raise ValueError(
                'When default_priority is not None, the installed_path should have a "{priority}"'
                ' or a "{priority:02}" substitution variable. Otherwise, the priority would be lost.'
                f" Triggered by plugin {self._plugin_name}"
            )

        if existing is not None:
            if existing.debputy_plugin_metadata.plugin_name != self._plugin_name:
                message = (
                    f'The stem "{stem}" is registered twice for packager provided files.'
                    f" Once by {existing.debputy_plugin_metadata.plugin_name} and once"
                    f" by {self._plugin_name}"
                )
            else:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' stem "{stem}" twice for packager provided files.'
                )
            raise PluginConflictError(
                message, existing.debputy_plugin_metadata, self._plugin_metadata
            )
        packager_provided_files[stem] = PackagerProvidedFileClassSpec(
            self._plugin_metadata,
            stem,
            installed_path,
            default_mode=default_mode,
            default_priority=default_priority,
            allow_name_segment=allow_name_segment,
            allow_architecture_segment=allow_architecture_segment,
            post_formatting_rewrite=post_formatting_rewrite,
            packageless_is_fallback_for_all_packages=packageless_is_fallback_for_all_packages,
            reservation_only=reservation_only,
            formatting_callback=format_callback,
            reference_documentation=reference_documentation,
        )

        def _unload() -> None:
            del packager_provided_files[stem]

        self._unloaders.append(_unload)

    def metadata_or_maintscript_detector(
        self,
        auto_detector_id: str,
        auto_detector: MetadataAutoDetector,
        *,
        package_type: PackageTypeSelector = "deb",
    ) -> None:
        if auto_detector_id in self._plugin_detector_ids:
            raise ValueError(
                f"The plugin {self._plugin_name} tried to register"
                f' "{auto_detector_id}" twice'
            )
        self._plugin_detector_ids.add(auto_detector_id)
        all_detectors = self._feature_set.metadata_maintscript_detectors
        if self._plugin_name not in all_detectors:
            all_detectors[self._plugin_name] = []
        package_types = resolve_package_type_selectors(package_type)
        all_detectors[self._plugin_name].append(
            MetadataOrMaintscriptDetector(
                detector_id=auto_detector_id,
                detector=auto_detector,
                plugin_metadata=self._plugin_metadata,
                applies_to_package_types=package_types,
                enabled=True,
            )
        )

        def _unload() -> None:
            if self._plugin_name in all_detectors:
                del all_detectors[self._plugin_name]

        self._unloaders.append(_unload)

    def document_builtin_variable(
        self,
        variable_name: str,
        variable_reference_documentation: str,
        *,
        is_context_specific: bool = False,
        is_for_special_case: bool = False,
    ) -> None:
        manifest_variables = self._feature_set.manifest_variables
        self._restricted_api()
        state = self._substitution.variable_state(variable_name)
        if state == VariableNameState.UNDEFINED:
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to document built-in {variable_name},"
                f" but it is not known to be a variable"
            )

        assert variable_name not in manifest_variables

        manifest_variables[variable_name] = PluginProvidedManifestVariable(
            self._plugin_metadata,
            variable_name,
            None,
            is_context_specific_variable=is_context_specific,
            variable_reference_documentation=variable_reference_documentation,
            is_documentation_placeholder=True,
            is_for_special_case=is_for_special_case,
        )

        def _unload() -> None:
            del manifest_variables[variable_name]

        self._unloaders.append(_unload)

    def manifest_variable_provider(
        self,
        provider: Callable[[VariableContext], Mapping[str, str]],
        variables: Union[Sequence[str], Mapping[str, Optional[str]]],
    ) -> None:
        self._restricted_api()
        cached_provider = functools.lru_cache(None)(provider)
        permitted_variables = frozenset(variables)
        variables_iter: Iterable[Tuple[str, Optional[str]]]
        if not isinstance(variables, Mapping):
            variables_iter = zip(variables, itertools.repeat(None))
        else:
            variables_iter = variables.items()

        checked_vars = False
        manifest_variables = self._feature_set.manifest_variables
        plugin_name = self._plugin_name

        def _value_resolver_generator(
            variable_name: str,
        ) -> Callable[[VariableContext], str]:
            def _value_resolver(variable_context: VariableContext) -> str:
                res = cached_provider(variable_context)
                nonlocal checked_vars
                if not checked_vars:
                    if permitted_variables != res.keys():
                        expected = ", ".join(sorted(permitted_variables))
                        actual = ", ".join(sorted(res))
                        raise PluginAPIViolationError(
                            f"The plugin {plugin_name} claimed to provide"
                            f" the following variables {expected},"
                            f" but when resolving the variables, the plugin provided"
                            f" {actual}.  These two lists should have been the same."
                        )
                    checked_vars = False
                return res[variable_name]

            return _value_resolver

        for varname, vardoc in variables_iter:
            self._check_variable_name(varname)
            manifest_variables[varname] = PluginProvidedManifestVariable(
                self._plugin_metadata,
                varname,
                _value_resolver_generator(varname),
                is_context_specific_variable=False,
                variable_reference_documentation=vardoc,
            )

        def _unload() -> None:
            raise PluginInitializationError(
                "Cannot unload manifest_variable_provider (not implemented)"
            )

        self._unloaders.append(_unload)

    def _check_variable_name(self, variable_name: str) -> None:
        manifest_variables = self._feature_set.manifest_variables
        existing = manifest_variables.get(variable_name)

        if existing is not None:
            if existing.plugin_metadata.plugin_name == self._plugin_name:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' manifest variable "{variable_name}" twice.'
                )
            else:
                message = (
                    f"The plugins {existing.plugin_metadata.plugin_name} and {self._plugin_name}"
                    f" both tried to provide the manifest variable {variable_name}"
                )
            raise PluginConflictError(
                message, existing.plugin_metadata, self._plugin_metadata
            )
        if not SUBST_VAR_RE.match("{{" + variable_name + "}}"):
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to declare {variable_name},"
                f" which is not a valid variable name"
            )

        namespace = ""
        variable_basename = variable_name
        if ":" in variable_name:
            namespace, variable_basename = variable_name.rsplit(":", 1)
            assert namespace != ""
            assert variable_name != ""

        if namespace != "" and namespace not in ("token", "path"):
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to declare {variable_name},"
                f" which is in the reserved namespace {namespace}"
            )

        variable_name_upper = variable_name.upper()
        if (
            variable_name_upper.startswith(("DEB_", "DPKG_", "DEBPUTY"))
            or variable_basename.startswith("_")
            or variable_basename.upper().startswith("DEBPUTY")
        ) and self._plugin_name != "debputy":
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to declare {variable_name},"
                f" which is a variable name reserved by debputy"
            )

        state = self._substitution.variable_state(variable_name)
        if state != VariableNameState.UNDEFINED and self._plugin_name != "debputy":
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to declare {variable_name},"
                f" which would shadow a built-in variable"
            )

    def package_processor(
        self,
        processor_id: str,
        processor: PackageProcessor,
        *,
        depends_on_processor: Iterable[str] = tuple(),
        package_type: PackageTypeSelector = "deb",
    ) -> None:
        self._restricted_api(allowed_plugins={"lua"})
        package_processors = self._feature_set.all_package_processors
        dependencies = set()
        processor_key = (self._plugin_name, processor_id)

        if processor_key in package_processors:
            raise PluginConflictError(
                f"The plugin {self._plugin_name} already registered a processor with id {processor_id}",
                self._plugin_metadata,
                self._plugin_metadata,
            )

        for depends_ref in depends_on_processor:
            if isinstance(depends_ref, str):
                if (self._plugin_name, depends_ref) in package_processors:
                    depends_key = (self._plugin_name, depends_ref)
                elif ("debputy", depends_ref) in package_processors:
                    depends_key = ("debputy", depends_ref)
                else:
                    raise ValueError(
                        f'Could not resolve dependency "{depends_ref}" for'
                        f' "{processor_id}". It was not provided by the plugin itself'
                        f" ({self._plugin_name}) nor debputy."
                    )
            else:
                # TODO: Add proper dependencies first, at which point we should probably resolve "name"
                #  via the direct dependencies.
                assert False

            existing_processor = package_processors.get(depends_key)
            if existing_processor is None:
                # We currently require the processor to be declared already.  If this ever changes,
                # PluginProvidedFeatureSet.package_processors_in_order will need an update
                dplugin_name, dprocessor_name = depends_key
                available_processors = ", ".join(
                    n for p, n in package_processors.keys() if p == dplugin_name
                )
                raise ValueError(
                    f"The plugin {dplugin_name} does not provide a processor called"
                    f" {dprocessor_name}. Available processors for that plugin are:"
                    f" {available_processors}"
                )
            dependencies.add(depends_key)

        package_processors[processor_key] = PluginProvidedPackageProcessor(
            processor_id,
            resolve_package_type_selectors(package_type),
            processor,
            frozenset(dependencies),
            self._plugin_metadata,
        )

        def _unload() -> None:
            del package_processors[processor_key]

        self._unloaders.append(_unload)

    def automatic_discard_rule(
        self,
        name: str,
        should_discard: Callable[[VirtualPath], bool],
        *,
        rule_reference_documentation: Optional[str] = None,
        examples: Union[
            AutomaticDiscardRuleExample, Sequence[AutomaticDiscardRuleExample]
        ] = tuple(),
    ) -> None:
        """Register an automatic discard rule

        An automatic discard rule is basically applied to *every* path about to be installed in to any package.
        If any discard rule concludes that a path should not be installed, then the path is not installed.
        In the case where the discard path is a:

         * directory: Then the entire directory is excluded along with anything beneath it.
         * symlink: Then the symlink itself (but not its target) is excluded.
         * hardlink: Then the current hardlink will not be installed, but other instances of it will be.

        Note: Discarded files are *never* deleted by `debputy`. They just make `debputy` skip the file.

        Automatic discard rules should be written with the assumption that directories will be tested
        before their content *when it is relevant* for the discard rule to examine whether the directory
        can be excluded.

        The packager can via the manifest overrule automatic discard rules by explicitly listing the path
        without any globs. As example:

            installations:
              - install:
                  sources:
                  - usr/lib/libfoo.la  # <-- This path is always installed
                                       #     (Discard rules are never asked in this case)
                                       #
                  - usr/lib/*.so*      # <-- Discard rules applies to any path beneath usr/lib and can exclude matches
                                       #     Though, they will not examine `libfoo.la` as it has already been installed
                                       #
                                       # Note: usr/lib itself is never tested in this case (it is assumed to be
                                       # explicitly requested). But any subdir of usr/lib will be examined.

        When an automatic discard rule is evaluated, it can see the source path currently being considered
        for installation.  While it can look at "surrounding" context (like parent directory), it will not
        know whether those paths are to be installed or will be installed.

        :param name: A user visible name discard rule. It can be used on the command line, so avoid shell
           metacharacters and spaces.
        :param should_discard: A callable that is the implementation of the automatic discard rule. It will receive
          a VirtualPath representing the *source* path about to be installed.  If callable returns `True`, then the
          path is discarded. If it returns `False`, the path is not discarded (by this rule at least).
          A source path will either be from the root of the source tree or the root of a search directory such as
          `debian/tmp`.  Where the path will be installed is not available at the time the discard rule is
          evaluated.
        :param rule_reference_documentation: Optionally, the reference documentation to be shown when a user
          looks up this automatic discard rule.
        :param examples: Provide examples for the rule. Use the automatic_discard_rule_example function to
          generate the examples.

        """
        self._restricted_api()
        auto_discard_rules = self._feature_set.auto_discard_rules
        existing = auto_discard_rules.get(name)
        if existing is not None:
            if existing.plugin_metadata.plugin_name == self._plugin_name:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' automatic discard rule "{name}" twice.'
                )
            else:
                message = (
                    f"The plugins {existing.plugin_metadata.plugin_name} and {self._plugin_name}"
                    f" both tried to provide the automatic discard rule {name}"
                )
            raise PluginConflictError(
                message, existing.plugin_metadata, self._plugin_metadata
            )
        examples = (
            (examples,)
            if isinstance(examples, AutomaticDiscardRuleExample)
            else tuple(examples)
        )
        auto_discard_rules[name] = PluginProvidedDiscardRule(
            name,
            self._plugin_metadata,
            should_discard,
            rule_reference_documentation,
            examples,
        )

        def _unload() -> None:
            del auto_discard_rules[name]

        self._unloaders.append(_unload)

    def service_provider(
        self,
        service_manager: str,
        detector: ServiceDetector,
        integrator: ServiceIntegrator,
    ) -> None:
        self._restricted_api()
        service_managers = self._feature_set.service_managers
        existing = service_managers.get(service_manager)
        if existing is not None:
            if existing.plugin_metadata.plugin_name == self._plugin_name:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' service manager "{service_manager}" twice.'
                )
            else:
                message = (
                    f"The plugins {existing.plugin_metadata.plugin_name} and {self._plugin_name}"
                    f' both tried to provide the service manager "{service_manager}"'
                )
            raise PluginConflictError(
                message, existing.plugin_metadata, self._plugin_metadata
            )
        service_managers[service_manager] = ServiceManagerDetails(
            service_manager,
            detector,
            integrator,
            self._plugin_metadata,
        )

        def _unload() -> None:
            del service_managers[service_manager]

        self._unloaders.append(_unload)

    def manifest_variable(
        self,
        variable_name: str,
        value: str,
        variable_reference_documentation: Optional[str] = None,
    ) -> None:
        self._check_variable_name(variable_name)
        manifest_variables = self._feature_set.manifest_variables
        try:
            resolved_value = self._substitution.substitute(
                value, "Plugin initialization"
            )
            depends_on_variable = resolved_value != value
        except DebputySubstitutionError:
            depends_on_variable = True
        if depends_on_variable:
            raise ValueError(
                f"The plugin {self._plugin_name} attempted to declare {variable_name} with value {value!r}."
                f" This value depends on another variable, which is not supported. This restriction may be"
                f" lifted in the future."
            )

        manifest_variables[variable_name] = PluginProvidedManifestVariable(
            self._plugin_metadata,
            variable_name,
            value,
            is_context_specific_variable=False,
            variable_reference_documentation=variable_reference_documentation,
        )

        def _unload() -> None:
            # We need to check it was never resolved
            raise PluginInitializationError(
                "Cannot unload manifest_variable (not implemented)"
            )

        self._unloaders.append(_unload)

    @property
    def _plugin_name(self) -> str:
        return self._plugin_metadata.plugin_name

    def provide_manifest_keyword(
        self,
        rule_type: TTP,
        rule_name: Union[str, List[str]],
        handler: DIPKWHandler,
        *,
        inline_reference_documentation: Optional[ParserDocumentation] = None,
    ) -> None:
        self._restricted_api()
        parser_generator = self._feature_set.manifest_parser_generator
        if rule_type not in parser_generator.dispatchable_table_parsers:
            types = ", ".join(
                sorted(x.__name__ for x in parser_generator.dispatchable_table_parsers)
            )
            raise ValueError(
                f"The rule_type was not a supported type. It must be one of {types}"
            )
        dispatching_parser = parser_generator.dispatchable_table_parsers[rule_type]
        dispatching_parser.register_keyword(
            rule_name,
            handler,
            self._plugin_metadata,
            inline_reference_documentation=inline_reference_documentation,
        )

        def _unload() -> None:
            raise PluginInitializationError(
                "Cannot unload provide_manifest_keyword (not implemented)"
            )

        self._unloaders.append(_unload)

    def pluggable_object_parser(
        self,
        rule_type: str,
        rule_name: str,
        *,
        object_parser_key: Optional[str] = None,
        on_end_parse_step: Optional[
            Callable[
                [str, Optional[Mapping[str, Any]], AttributePath, ParserContextData],
                None,
            ]
        ] = None,
        nested_in_package_context: bool = False,
    ) -> None:
        self._restricted_api()
        if object_parser_key is None:
            object_parser_key = rule_name

        parser_generator = self._feature_set.manifest_parser_generator
        dispatchable_object_parsers = parser_generator.dispatchable_object_parsers
        if rule_type not in dispatchable_object_parsers:
            types = ", ".join(sorted(dispatchable_object_parsers))
            raise ValueError(
                f"The rule_type was not a supported type. It must be one of {types}"
            )
        if object_parser_key not in dispatchable_object_parsers:
            types = ", ".join(sorted(dispatchable_object_parsers))
            raise ValueError(
                f"The object_parser_key was not a supported type. It must be one of {types}"
            )
        parent_dispatcher = dispatchable_object_parsers[rule_type]
        child_dispatcher = dispatchable_object_parsers[object_parser_key]
        parent_dispatcher.register_child_parser(
            rule_name,
            child_dispatcher,
            self._plugin_metadata,
            on_end_parse_step=on_end_parse_step,
            nested_in_package_context=nested_in_package_context,
        )

        def _unload() -> None:
            raise PluginInitializationError(
                "Cannot unload pluggable_object_parser (not implemented)"
            )

        self._unloaders.append(_unload)

    def pluggable_manifest_rule(
        self,
        rule_type: Union[TTP, str],
        rule_name: Union[str, List[str]],
        parsed_format: Type[PF],
        handler: DIPHandler,
        *,
        source_format: Optional[SF] = None,
        inline_reference_documentation: Optional[ParserDocumentation] = None,
    ) -> None:
        self._restricted_api()
        feature_set = self._feature_set
        parser_generator = feature_set.manifest_parser_generator
        if isinstance(rule_type, str):
            if rule_type not in parser_generator.dispatchable_object_parsers:
                types = ", ".join(sorted(parser_generator.dispatchable_object_parsers))
                raise ValueError(
                    f"The rule_type was not a supported type. It must be one of {types}"
                )
            dispatching_parser = parser_generator.dispatchable_object_parsers[rule_type]
        else:
            if rule_type not in parser_generator.dispatchable_table_parsers:
                types = ", ".join(
                    sorted(
                        x.__name__ for x in parser_generator.dispatchable_table_parsers
                    )
                )
                raise ValueError(
                    f"The rule_type was not a supported type. It must be one of {types}"
                )
            dispatching_parser = parser_generator.dispatchable_table_parsers[rule_type]

        parser = feature_set.manifest_parser_generator.generate_parser(
            parsed_format,
            source_content=source_format,
            inline_reference_documentation=inline_reference_documentation,
        )
        dispatching_parser.register_parser(
            rule_name,
            parser,
            handler,
            self._plugin_metadata,
        )

        def _unload() -> None:
            raise PluginInitializationError(
                "Cannot unload pluggable_manifest_rule (not implemented)"
            )

        self._unloaders.append(_unload)

    def known_packaging_files(
        self,
        packaging_file_details: KnownPackagingFileInfo,
    ) -> None:
        known_packaging_files = self._feature_set.known_packaging_files
        detection_method = packaging_file_details.get(
            "detection_method", cast("Literal['path']", "path")
        )
        path = packaging_file_details.get("path")
        dhpkgfile = packaging_file_details.get("pkgfile")

        packaging_file_details: KnownPackagingFileInfo = packaging_file_details.copy()

        if detection_method == "path":
            if dhpkgfile is not None:
                raise ValueError(
                    'The "pkgfile" attribute cannot be used when detection-method is "path" (or omitted)'
                )
            if path != _normalize_path(path, with_prefix=False):
                raise ValueError(
                    f"The path for known packaging files must be normalized. Please replace"
                    f' "{path}" with "{_normalize_path(path, with_prefix=False)}"'
                )
            detection_value = path
        else:
            assert detection_method == "dh.pkgfile"
            if path is not None:
                raise ValueError(
                    'The "path" attribute cannot be used when detection-method is "dh.pkgfile"'
                )
            if "/" in dhpkgfile:
                raise ValueError(
                    'The "pkgfile" attribute ḿust be a name stem such as "install" (no "/" are allowed)'
                )
            detection_value = dhpkgfile
        key = f"{detection_method}::{detection_value}"
        existing = known_packaging_files.get(key)
        if existing is not None:
            if existing.plugin_metadata.plugin_name != self._plugin_name:
                message = (
                    f'The key "{key}" is registered twice for known packaging files.'
                    f" Once by {existing.plugin_metadata.plugin_name} and once by {self._plugin_name}"
                )
            else:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' key "{key}" twice for known packaging files.'
                )
            raise PluginConflictError(
                message, existing.plugin_metadata, self._plugin_metadata
            )
        _validate_known_packaging_file_dh_compat_rules(
            packaging_file_details.get("dh_compat_rules")
        )
        known_packaging_files[key] = PluginProvidedKnownPackagingFile(
            packaging_file_details,
            detection_method,
            detection_value,
            self._plugin_metadata,
        )

        def _unload() -> None:
            del known_packaging_files[key]

        self._unloaders.append(_unload)

    def register_mapped_type(
        self,
        type_mapping: TypeMapping,
        *,
        reference_documentation: Optional[TypeMappingDocumentation] = None,
    ) -> None:
        self._restricted_api()
        target_type = type_mapping.target_type
        mapped_types = self._feature_set.mapped_types
        existing = mapped_types.get(target_type)
        if existing is not None:
            if existing.plugin_metadata.plugin_name != self._plugin_name:
                message = (
                    f'The key "{target_type.__name__}" is registered twice for known packaging files.'
                    f" Once by {existing.plugin_metadata.plugin_name} and once by {self._plugin_name}"
                )
            else:
                message = (
                    f"Bug in the plugin {self._plugin_name}: It tried to register the"
                    f' key "{target_type.__name__}" twice for known packaging files.'
                )
            raise PluginConflictError(
                message, existing.plugin_metadata, self._plugin_metadata
            )
        parser_generator = self._feature_set.manifest_parser_generator
        mapped_types[target_type] = PluginProvidedTypeMapping(
            type_mapping, reference_documentation, self._plugin_metadata
        )
        parser_generator.register_mapped_type(type_mapping)

    def _restricted_api(
        self,
        *,
        allowed_plugins: Union[Set[str], FrozenSet[str]] = frozenset(),
    ) -> None:
        if self._plugin_name != "debputy" and self._plugin_name not in allowed_plugins:
            raise PluginAPIViolationError(
                f"Plugin {self._plugin_name} attempted to access a debputy-only API."
                " If you are the maintainer of this plugin and want access to this"
                " API, please file a feature request to make this public."
                " (The API is currently private as it is unstable.)"
            )


class MaintscriptAccessorProviderBase(MaintscriptAccessor, ABC):
    __slots__ = ()

    def _append_script(
        self,
        caller_name: str,
        maintscript: Maintscript,
        full_script: str,
        /,
        perform_substitution: bool = True,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def _apply_condition_to_script(
        cls,
        condition: str,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
    ) -> str:
        if indent is None:
            # We auto-determine this based on heredocs currently
            indent = "<<" not in run_snippet

        if indent:
            run_snippet = "".join("  " + x for x in run_snippet.splitlines(True))
        if not run_snippet.endswith("\n"):
            run_snippet += "\n"
        condition_line = f"if {condition}; then\n"
        end_line = "fi\n"
        return "".join((condition_line, run_snippet, end_line))

    def on_configure(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
        skip_on_rollback: bool = False,
    ) -> None:
        condition = POSTINST_DEFAULT_CONDITION
        if skip_on_rollback:
            condition = '[ "$1" = "configure" ]'
        return self._append_script(
            "on_configure",
            "postinst",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_initial_install(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "configure" -a -z "$2" ]'
        return self._append_script(
            "on_initial_install",
            "postinst",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_upgrade(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "configure" -a -n "$2" ]'
        return self._append_script(
            "on_upgrade",
            "postinst",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_upgrade_from(
        self,
        version: str,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "configure" ] && dpkg --compare-versions le-nl "$2"'
        return self._append_script(
            "on_upgrade_from",
            "postinst",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_before_removal(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "remove" ]'
        return self._append_script(
            "on_before_removal",
            "prerm",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_removed(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "remove" ]'
        return self._append_script(
            "on_removed",
            "postrm",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def on_purge(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        condition = '[ "$1" = "purge" ]'
        return self._append_script(
            "on_purge",
            "postrm",
            self._apply_condition_to_script(condition, run_snippet, indent=indent),
            perform_substitution=perform_substitution,
        )

    def unconditionally_in_script(
        self,
        maintscript: Maintscript,
        run_snippet: str,
        /,
        perform_substitution: bool = True,
    ) -> None:
        if maintscript not in STD_CONTROL_SCRIPTS:
            raise ValueError(
                f'Unknown script "{maintscript}". Should have been one of:'
                f' {", ".join(sorted(STD_CONTROL_SCRIPTS))}'
            )
        return self._append_script(
            "unconditionally_in_script",
            maintscript,
            run_snippet,
            perform_substitution=perform_substitution,
        )


class MaintscriptAccessorProvider(MaintscriptAccessorProviderBase):
    __slots__ = (
        "_plugin_metadata",
        "_maintscript_snippets",
        "_plugin_source_id",
        "_package_substitution",
        "_default_snippet_order",
    )

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
        package_substitution: Substitution,
        *,
        default_snippet_order: Optional[Literal["service"]] = None,
    ):
        self._plugin_metadata = plugin_metadata
        self._plugin_source_id = plugin_source_id
        self._maintscript_snippets = maintscript_snippets
        self._package_substitution = package_substitution
        self._default_snippet_order = default_snippet_order

    def _append_script(
        self,
        caller_name: str,
        maintscript: Maintscript,
        full_script: str,
        /,
        perform_substitution: bool = True,
    ) -> None:
        def_source = f"{self._plugin_metadata.plugin_name} ({self._plugin_source_id})"
        if perform_substitution:
            full_script = self._package_substitution.substitute(full_script, def_source)

        snippet = MaintscriptSnippet(
            snippet=full_script,
            definition_source=def_source,
            snippet_order=self._default_snippet_order,
        )
        self._maintscript_snippets[maintscript].append(snippet)


class BinaryCtrlAccessorProviderBase(BinaryCtrlAccessor):
    __slots__ = (
        "_plugin_metadata",
        "_plugin_source_id",
        "_package_metadata_context",
        "_triggers",
        "_substvars",
        "_maintscript",
        "_shlibs_details",
    )

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        package_metadata_context: PackageProcessingContext,
        triggers: Dict[Tuple[DpkgTriggerType, str], PluginProvidedTrigger],
        substvars: FlushableSubstvars,
        shlibs_details: Tuple[Optional[str], Optional[List[str]]],
    ) -> None:
        self._plugin_metadata = plugin_metadata
        self._plugin_source_id = plugin_source_id
        self._package_metadata_context = package_metadata_context
        self._triggers = triggers
        self._substvars = substvars
        self._maintscript: Optional[MaintscriptAccessor] = None
        self._shlibs_details = shlibs_details

    def _create_maintscript_accessor(self) -> MaintscriptAccessor:
        raise NotImplementedError

    def dpkg_trigger(self, trigger_type: DpkgTriggerType, trigger_target: str) -> None:
        """Register a declarative dpkg level trigger

        The provided trigger will be added to the package's metadata (the triggers file of the control.tar).

        If the trigger has already been added previously, a second call with the same trigger data will be ignored.
        """
        key = (trigger_type, trigger_target)
        if key in self._triggers:
            return
        self._triggers[key] = PluginProvidedTrigger(
            dpkg_trigger_type=trigger_type,
            dpkg_trigger_target=trigger_target,
            provider=self._plugin_metadata,
            provider_source_id=self._plugin_source_id,
        )

    @property
    def maintscript(self) -> MaintscriptAccessor:
        maintscript = self._maintscript
        if maintscript is None:
            maintscript = self._create_maintscript_accessor()
            self._maintscript = maintscript
        return maintscript

    @property
    def substvars(self) -> FlushableSubstvars:
        return self._substvars

    def dpkg_shlibdeps(self, paths: Sequence[VirtualPath]) -> None:
        binary_package = self._package_metadata_context.binary_package
        with self.substvars.flush() as substvars_file:
            dpkg_cmd = ["dpkg-shlibdeps", f"-T{substvars_file}"]
            if binary_package.is_udeb:
                dpkg_cmd.append("-tudeb")
            if binary_package.is_essential:
                dpkg_cmd.append("-dPre-Depends")
            shlibs_local, shlib_dirs = self._shlibs_details
            if shlibs_local is not None:
                dpkg_cmd.append(f"-L{shlibs_local}")
            if shlib_dirs:
                dpkg_cmd.extend(f"-l{sd}" for sd in shlib_dirs)
            dpkg_cmd.extend(p.fs_path for p in paths)
            print_command(*dpkg_cmd)
            try:
                subprocess.check_call(dpkg_cmd)
            except subprocess.CalledProcessError:
                _error(
                    f"Attempting to auto-detect dependencies via dpkg-shlibdeps for {binary_package.name} failed. Please"
                    " review the output from dpkg-shlibdeps above to understand what went wrong."
                )


class BinaryCtrlAccessorProvider(BinaryCtrlAccessorProviderBase):
    __slots__ = (
        "_maintscript",
        "_maintscript_snippets",
        "_package_substitution",
    )

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        package_metadata_context: PackageProcessingContext,
        triggers: Dict[Tuple[DpkgTriggerType, str], PluginProvidedTrigger],
        substvars: FlushableSubstvars,
        maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
        package_substitution: Substitution,
        shlibs_details: Tuple[Optional[str], Optional[List[str]]],
        *,
        default_snippet_order: Optional[Literal["service"]] = None,
    ) -> None:
        super().__init__(
            plugin_metadata,
            plugin_source_id,
            package_metadata_context,
            triggers,
            substvars,
            shlibs_details,
        )
        self._maintscript_snippets = maintscript_snippets
        self._package_substitution = package_substitution
        self._maintscript = MaintscriptAccessorProvider(
            plugin_metadata,
            plugin_source_id,
            maintscript_snippets,
            package_substitution,
            default_snippet_order=default_snippet_order,
        )

    def _create_maintscript_accessor(self) -> MaintscriptAccessor:
        return MaintscriptAccessorProvider(
            self._plugin_metadata,
            self._plugin_source_id,
            self._maintscript_snippets,
            self._package_substitution,
        )


class BinaryCtrlAccessorProviderCreator:
    def __init__(
        self,
        package_metadata_context: PackageProcessingContext,
        substvars: FlushableSubstvars,
        maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
        substitution: Substitution,
    ) -> None:
        self._package_metadata_context = package_metadata_context
        self._substvars = substvars
        self._maintscript_snippets = maintscript_snippets
        self._substitution = substitution
        self._triggers: Dict[Tuple[DpkgTriggerType, str], PluginProvidedTrigger] = {}
        self.shlibs_details: Tuple[Optional[str], Optional[List[str]]] = None, None

    def for_plugin(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        *,
        default_snippet_order: Optional[Literal["service"]] = None,
    ) -> BinaryCtrlAccessor:
        return BinaryCtrlAccessorProvider(
            plugin_metadata,
            plugin_source_id,
            self._package_metadata_context,
            self._triggers,
            self._substvars,
            self._maintscript_snippets,
            self._substitution,
            self.shlibs_details,
            default_snippet_order=default_snippet_order,
        )

    def generated_triggers(self) -> Iterable[PluginProvidedTrigger]:
        return self._triggers.values()


def plugin_metadata_for_debputys_own_plugin(
    loader: Optional[PluginInitializationEntryPoint] = None,
) -> DebputyPluginMetadata:
    if loader is None:
        from debputy.plugin.debputy.debputy_plugin import initialize_debputy_features

        loader = initialize_debputy_features
    return DebputyPluginMetadata(
        plugin_name="debputy",
        api_compat_version=1,
        plugin_initializer=loader,
        plugin_loader=None,
        plugin_path="<bundled>",
    )


def load_plugin_features(
    plugin_search_dirs: Sequence[str],
    substitution: Substitution,
    requested_plugins_only: Optional[Sequence[str]] = None,
    required_plugins: Optional[Set[str]] = None,
    plugin_feature_set: Optional[PluginProvidedFeatureSet] = None,
    debug_mode: bool = False,
) -> PluginProvidedFeatureSet:
    if plugin_feature_set is None:
        plugin_feature_set = PluginProvidedFeatureSet()
    plugins = [plugin_metadata_for_debputys_own_plugin()]
    unloadable_plugins = set()
    if required_plugins:
        plugins.extend(
            find_json_plugins(
                plugin_search_dirs,
                required_plugins,
            )
        )
    if requested_plugins_only is not None:
        plugins.extend(
            find_json_plugins(
                plugin_search_dirs,
                requested_plugins_only,
            )
        )
    else:
        auto_loaded = _find_all_json_plugins(
            plugin_search_dirs,
            required_plugins if required_plugins is not None else frozenset(),
            debug_mode=debug_mode,
        )
        for plugin_metadata in auto_loaded:
            plugins.append(plugin_metadata)
            unloadable_plugins.add(plugin_metadata.plugin_name)

    for plugin_metadata in plugins:
        api = DebputyPluginInitializerProvider(
            plugin_metadata, plugin_feature_set, substitution
        )
        try:
            api.load_plugin()
        except PluginBaseError as e:
            if plugin_metadata.plugin_name not in unloadable_plugins:
                raise
            if debug_mode:
                raise
            try:
                api.unload_plugin()
            except Exception:
                _warn(
                    f"Failed to load optional {plugin_metadata.plugin_name} and an error was raised when trying to"
                    " clean up after the half-initialized plugin. Re-raising load error as the partially loaded"
                    " module might have tainted the feature set."
                )
                raise e from None
            else:
                if debug_mode:
                    _warn(
                        f"The optional plugin {plugin_metadata.plugin_name} failed during load. Re-raising due"
                        f" to --debug/-d."
                    )
                _warn(
                    f"The optional plugin {plugin_metadata.plugin_name} failed during load. The plugin was"
                    f" deactivated. Use debug mode (--debug) to show the stacktrace (the warning will become an error)"
                )

    return plugin_feature_set


def find_json_plugin(
    search_dirs: Sequence[str],
    requested_plugin: str,
) -> DebputyPluginMetadata:
    r = list(find_json_plugins(search_dirs, [requested_plugin]))
    assert len(r) == 1
    return r[0]


def find_related_implementation_files_for_plugin(
    plugin_metadata: DebputyPluginMetadata,
) -> List[str]:
    plugin_path = plugin_metadata.plugin_path
    if not os.path.isfile(plugin_path):
        plugin_name = plugin_metadata.plugin_name
        _error(
            f"Cannot run find related files for {plugin_name}: The plugin seems to be bundled"
            " or loaded via a mechanism that does not support detecting its tests."
        )
    files = []
    module_name, module_file = _find_plugin_implementation_file(
        plugin_metadata.plugin_name,
        plugin_metadata.plugin_path,
    )
    if os.path.isfile(module_file):
        files.append(module_file)
    else:
        if not plugin_metadata.is_loaded:
            plugin_metadata.load_plugin()
        if module_name in sys.modules:
            _error(
                f'The plugin {plugin_metadata.plugin_name} uses the "module"" key in its'
                f" JSON metadata file ({plugin_metadata.plugin_path}) and cannot be "
                f" installed via this method. The related Python would not be installed"
                f" (which would result in a plugin that would fail to load)"
            )

    return files


def find_tests_for_plugin(
    plugin_metadata: DebputyPluginMetadata,
) -> List[str]:
    plugin_name = plugin_metadata.plugin_name
    plugin_path = plugin_metadata.plugin_path

    if not os.path.isfile(plugin_path):
        _error(
            f"Cannot run tests for {plugin_name}: The plugin seems to be bundled or loaded via a"
            " mechanism that does not support detecting its tests."
        )

    plugin_dir = os.path.dirname(plugin_path)
    test_basename_prefix = plugin_metadata.plugin_name.replace("-", "_")
    tests = []
    with os.scandir(plugin_dir) as dir_iter:
        for p in dir_iter:
            if (
                p.is_file()
                and p.name.startswith(test_basename_prefix)
                and PLUGIN_TEST_SUFFIX.search(p.name)
            ):
                tests.append(p.path)
    return tests


def find_json_plugins(
    search_dirs: Sequence[str],
    requested_plugins: Iterable[str],
) -> Iterable[DebputyPluginMetadata]:
    for plugin_name_or_path in requested_plugins:
        found = False
        if "/" in plugin_name_or_path:
            if not os.path.isfile(plugin_name_or_path):
                raise PluginNotFoundError(
                    f"Unable to load the plugin {plugin_name_or_path}: The path is not a file."
                    ' (Because the plugin name contains "/", it is assumed to be a path and search path'
                    " is not used."
                )
            yield parse_json_plugin_desc(plugin_name_or_path)
            return
        for search_dir in search_dirs:
            path = os.path.join(
                search_dir, "debputy", "plugins", f"{plugin_name_or_path}.json"
            )
            if not os.path.isfile(path):
                continue
            found = True
            yield parse_json_plugin_desc(path)
        if not found:
            search_dir_str = ":".join(search_dirs)
            raise PluginNotFoundError(
                f"Unable to load the plugin {plugin_name_or_path}: Could not find {plugin_name_or_path}.json in the"
                f" debputy/plugins subdir of any of the search dirs ({search_dir_str})"
            )


def _find_all_json_plugins(
    search_dirs: Sequence[str],
    required_plugins: AbstractSet[str],
    debug_mode: bool = False,
) -> Iterable[DebputyPluginMetadata]:
    seen = set(required_plugins)
    error_seen = False
    for search_dir in search_dirs:
        try:
            dir_fd = os.scandir(os.path.join(search_dir, "debputy", "plugins"))
        except FileNotFoundError:
            continue
        with dir_fd:
            for entry in dir_fd:
                if (
                    not entry.is_file(follow_symlinks=True)
                    or not entry.name.endswith(".json")
                    or entry.name in seen
                ):
                    continue
                try:
                    plugin_metadata = parse_json_plugin_desc(entry.path)
                except PluginBaseError as e:
                    if debug_mode:
                        raise
                    if not error_seen:
                        error_seen = True
                        _warn(
                            f"Failed to load the plugin in {entry.path} due to the following error: {e.message}"
                        )
                    else:
                        _warn(
                            f"Failed to load plugin in {entry.path} due to errors (not shown)."
                        )
                else:
                    yield plugin_metadata


def _find_plugin_implementation_file(
    plugin_name: str,
    json_file_path: str,
) -> Tuple[str, str]:
    guessed_module_basename = plugin_name.replace("-", "_")
    module_name = f"debputy.plugin.{guessed_module_basename}"
    module_fs_path = os.path.join(
        os.path.dirname(json_file_path), f"{guessed_module_basename}.py"
    )
    return module_name, module_fs_path


def _resolve_module_initializer(
    plugin_name: str,
    plugin_initializer_name: str,
    module_name: Optional[str],
    json_file_path: str,
) -> PluginInitializationEntryPoint:
    module = None
    module_fs_path = None
    if module_name is None:
        module_name, module_fs_path = _find_plugin_implementation_file(
            plugin_name, json_file_path
        )
        if os.path.isfile(module_fs_path):
            spec = importlib.util.spec_from_file_location(module_name, module_fs_path)
            if spec is None:
                raise PluginInitializationError(
                    f"Failed to load {plugin_name} (path: {module_fs_path})."
                    " The spec_from_file_location function returned None."
                )
            mod = importlib.util.module_from_spec(spec)
            loader = spec.loader
            if loader is None:
                raise PluginInitializationError(
                    f"Failed to load {plugin_name} (path: {module_fs_path})."
                    " Python could not find a suitable loader (spec.loader was None)"
                )
            sys.modules[module_name] = mod
            try:
                loader.exec_module(mod)
            except (Exception, GeneratorExit) as e:
                raise PluginInitializationError(
                    f"Failed to load {plugin_name} (path: {module_fs_path})."
                    " The module threw an exception while being loaded."
                ) from e
            module = mod

    if module is None:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            if module_fs_path is None:
                raise PluginMetadataError(
                    f'The plugin defined in "{json_file_path}" wanted to load the module "{module_name}", but'
                    " this module is not available in the python search path"
                ) from e
            raise PluginInitializationError(
                f"Failed to load {plugin_name}. Tried loading it from"
                f' "{module_fs_path}" (which did not exist) and PYTHONPATH as'
                f" {module_name} (where it was not found either). Please ensure"
                " the module code is installed in the correct spot or provide an"
                f' explicit "module" definition in {json_file_path}.'
            ) from e

    plugin_initializer = getattr(module, plugin_initializer_name)

    if plugin_initializer is None:
        raise PluginMetadataError(
            f'The plugin defined in {json_file_path} claimed that module "{module_name}" would have an'
            f" attribute called {plugin_initializer}. However, it does not. Please correct the plugin"
            f" metadata or initializer name in the Python module."
        )
    return cast("PluginInitializationEntryPoint", plugin_initializer)


def _json_plugin_loader(
    plugin_name: str,
    plugin_json_metadata: PluginJsonMetadata,
    json_file_path: str,
    attribute_path: AttributePath,
) -> Callable[["DebputyPluginInitializer"], None]:
    api_compat = plugin_json_metadata["api_compat_version"]
    module_name = plugin_json_metadata.get("module")
    plugin_initializer_name = plugin_json_metadata.get("plugin_initializer")
    packager_provided_files_raw = plugin_json_metadata.get(
        "packager_provided_files", []
    )
    manifest_variables_raw = plugin_json_metadata.get("manifest_variables")
    known_packaging_files_raw = plugin_json_metadata.get("known_packaging_files")
    if api_compat != 1:
        raise PluginMetadataError(
            f'The plugin defined in "{json_file_path}" requires API compat level {api_compat}, but this'
            f" version of debputy only supports API compat version of 1"
        )
    if plugin_initializer_name is not None and "." in plugin_initializer_name:
        p = attribute_path["plugin_initializer"]
        raise PluginMetadataError(
            f'The "{p}" must not contain ".". Problematic file is "{json_file_path}".'
        )

    plugin_initializers = []

    if plugin_initializer_name is not None:
        plugin_initializer = _resolve_module_initializer(
            plugin_name,
            plugin_initializer_name,
            module_name,
            json_file_path,
        )
        plugin_initializers.append(plugin_initializer)

    if known_packaging_files_raw:
        kpf_root_path = attribute_path["known_packaging_files"]
        known_packaging_files = []
        for k, v in enumerate(known_packaging_files_raw):
            kpf_path = kpf_root_path[k]
            p = v.get("path")
            if isinstance(p, str):
                kpf_path.path_hint = p
            if plugin_name.startswith("debputy-") and isinstance(v, dict):
                docs = v.get("documentation-uris")
                if docs is not None and isinstance(docs, list):
                    docs = [
                        (
                            d.replace("@DEBPUTY_DOC_ROOT_DIR@", DEBPUTY_DOC_ROOT_DIR)
                            if isinstance(d, str)
                            else d
                        )
                        for d in docs
                    ]
                    v["documentation-uris"] = docs
            known_packaging_file: KnownPackagingFileInfo = (
                PLUGIN_KNOWN_PACKAGING_FILES_PARSER.parse_input(
                    v,
                    kpf_path,
                )
            )
            known_packaging_files.append((kpf_path, known_packaging_file))

        def _initialize_json_provided_known_packaging_files(
            api: DebputyPluginInitializerProvider,
        ) -> None:
            for p, details in known_packaging_files:
                try:
                    api.known_packaging_files(details)
                except ValueError as ex:
                    raise PluginMetadataError(
                        f"Error while processing {p.path} defined in {json_file_path}: {ex.args[0]}"
                    )

        plugin_initializers.append(_initialize_json_provided_known_packaging_files)

    if manifest_variables_raw:
        manifest_var_path = attribute_path["manifest_variables"]
        manifest_variables = [
            PLUGIN_MANIFEST_VARS_PARSER.parse_input(p, manifest_var_path[i])
            for i, p in enumerate(manifest_variables_raw)
        ]

        def _initialize_json_provided_manifest_vars(
            api: DebputyPluginInitializer,
        ) -> None:
            for idx, manifest_variable in enumerate(manifest_variables):
                name = manifest_variable["name"]
                value = manifest_variable["value"]
                doc = manifest_variable.get("reference_documentation")
                try:
                    api.manifest_variable(
                        name, value, variable_reference_documentation=doc
                    )
                except ValueError as ex:
                    var_path = manifest_var_path[idx]
                    raise PluginMetadataError(
                        f"Error while processing {var_path.path} defined in {json_file_path}: {ex.args[0]}"
                    )

        plugin_initializers.append(_initialize_json_provided_manifest_vars)

    if packager_provided_files_raw:
        ppf_path = attribute_path["packager_provided_files"]
        ppfs = [
            PLUGIN_PPF_PARSER.parse_input(p, ppf_path[i])
            for i, p in enumerate(packager_provided_files_raw)
        ]

        def _initialize_json_provided_ppfs(api: DebputyPluginInitializer) -> None:
            ppf: PackagerProvidedFileJsonDescription
            for idx, ppf in enumerate(ppfs):
                c = dict(ppf)
                stem = ppf["stem"]
                installed_path = ppf["installed_path"]
                default_mode = ppf.get("default_mode")
                ref_doc_dict = ppf.get("reference_documentation")
                if default_mode is not None:
                    c["default_mode"] = default_mode.octal_mode

                if ref_doc_dict is not None:
                    ref_doc = packager_provided_file_reference_documentation(
                        **ref_doc_dict
                    )
                else:
                    ref_doc = None

                for k in [
                    "stem",
                    "installed_path",
                    "reference_documentation",
                ]:
                    try:
                        del c[k]
                    except KeyError:
                        pass

                try:
                    api.packager_provided_file(stem, installed_path, reference_documentation=ref_doc, **c)  # type: ignore
                except ValueError as ex:
                    p_path = ppf_path[idx]
                    raise PluginMetadataError(
                        f"Error while processing {p_path.path} defined in {json_file_path}: {ex.args[0]}"
                    )

        plugin_initializers.append(_initialize_json_provided_ppfs)

    if not plugin_initializers:
        raise PluginMetadataError(
            f"The plugin defined in {json_file_path} does not seem to provide features, "
            f" such as module + plugin-initializer or packager-provided-files."
        )

    if len(plugin_initializers) == 1:
        return plugin_initializers[0]

    def _chain_loader(api: DebputyPluginInitializer) -> None:
        for initializer in plugin_initializers:
            initializer(api)

    return _chain_loader


@contextlib.contextmanager
def _open(path: str, fd: Optional[IO[bytes]] = None) -> Iterator[IO[bytes]]:
    if fd is not None:
        yield fd
    else:
        with open(path, "rb") as fd:
            yield fd


def parse_json_plugin_desc(
    path: str, *, fd: Optional[IO[bytes]] = None
) -> DebputyPluginMetadata:
    with _open(path, fd=fd) as rfd:
        try:
            raw = json.load(rfd)
        except JSONDecodeError as e:
            raise PluginMetadataError(
                f'The plugin defined in "{path}" could not be parsed as valid JSON: {e.args[0]}'
            ) from e
    plugin_name = os.path.basename(path)
    if plugin_name.endswith(".json"):
        plugin_name = plugin_name[:-5]
    elif plugin_name.endswith(".json.in"):
        plugin_name = plugin_name[:-8]

    if plugin_name == "debputy":
        # Provide a better error message than "The plugin has already loaded!?"
        raise PluginMetadataError(
            f'The plugin named {plugin_name} must be bundled with `debputy`. Please rename "{path}" so it does not'
            f" clash with the bundled plugin of same name."
        )

    attribute_path = AttributePath.root_path()

    try:
        plugin_json_metadata = PLUGIN_METADATA_PARSER.parse_input(
            raw,
            attribute_path,
        )
    except ManifestParseException as e:
        raise PluginMetadataError(
            f'The plugin defined in "{path}" was valid JSON but could not be parsed: {e.message}'
        ) from e
    api_compat = plugin_json_metadata["api_compat_version"]

    return DebputyPluginMetadata(
        plugin_name=plugin_name,
        plugin_loader=lambda: _json_plugin_loader(
            plugin_name,
            plugin_json_metadata,
            path,
            attribute_path,
        ),
        api_compat_version=api_compat,
        plugin_initializer=None,
        plugin_path=path,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class ServiceDefinitionImpl(ServiceDefinition[DSD]):
    name: str
    names: Sequence[str]
    path: VirtualPath
    type_of_service: str
    service_scope: str
    auto_enable_on_install: bool
    auto_start_on_install: bool
    on_upgrade: ServiceUpgradeRule
    definition_source: str
    is_plugin_provided_definition: bool
    service_context: Optional[DSD]

    def replace(self, **changes: Any) -> "ServiceDefinitionImpl[DSD]":
        return dataclasses.replace(self, **changes)


class ServiceRegistryImpl(ServiceRegistry[DSD]):
    __slots__ = ("_service_manager_details", "_service_definitions", "_seen_services")

    def __init__(self, service_manager_details: ServiceManagerDetails) -> None:
        self._service_manager_details = service_manager_details
        self._service_definitions: List[ServiceDefinition[DSD]] = []
        self._seen_services = set()

    @property
    def detected_services(self) -> Sequence[ServiceDefinition[DSD]]:
        return self._service_definitions

    def register_service(
        self,
        path: VirtualPath,
        name: Union[str, List[str]],
        *,
        type_of_service: str = "service",  # "timer", etc.
        service_scope: str = "system",
        enable_by_default: bool = True,
        start_by_default: bool = True,
        default_upgrade_rule: ServiceUpgradeRule = "restart",
        service_context: Optional[DSD] = None,
    ) -> None:
        names = name if isinstance(name, list) else [name]
        if len(names) < 1:
            raise ValueError(
                f"The service must have at least one name - {path.absolute} did not have any"
            )
        for n in names:
            key = (n, type_of_service, service_scope)
            if key in self._seen_services:
                raise PluginAPIViolationError(
                    f"The service manager (from {self._service_manager_details.plugin_metadata.plugin_name}) used"
                    f" the service name {n} (type: {type_of_service}, scope: {service_scope}) twice. This is not"
                    " allowed by the debputy plugin API."
                )
        # TODO: We cannot create a service definition immediate once the manifest is involved
        self._service_definitions.append(
            ServiceDefinitionImpl(
                names[0],
                names,
                path,
                type_of_service,
                service_scope,
                enable_by_default,
                start_by_default,
                default_upgrade_rule,
                f"Auto-detected by plugin {self._service_manager_details.plugin_metadata.plugin_name}",
                True,
                service_context,
            )
        )
