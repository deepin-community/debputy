import contextlib
import dataclasses
import inspect
import os.path
from io import BytesIO
from typing import (
    Mapping,
    Dict,
    Optional,
    Tuple,
    List,
    cast,
    FrozenSet,
    Sequence,
    Union,
    Type,
    Iterator,
    Set,
    KeysView,
    Callable,
)

from debian.deb822 import Deb822
from debian.substvars import Substvars

from debputy import DEBPUTY_PLUGIN_ROOT_DIR
from debputy.architecture_support import faked_arch_table
from debputy.filesystem_scan import FSROOverlay, FSRootDir
from debputy.packages import BinaryPackage
from debputy.plugin.api import (
    PluginInitializationEntryPoint,
    VirtualPath,
    PackageProcessingContext,
    DpkgTriggerType,
    Maintscript,
)
from debputy.plugin.api.example_processing import process_discard_rule_example
from debputy.plugin.api.impl import (
    plugin_metadata_for_debputys_own_plugin,
    DebputyPluginInitializerProvider,
    parse_json_plugin_desc,
    MaintscriptAccessorProviderBase,
    BinaryCtrlAccessorProviderBase,
    PLUGIN_TEST_SUFFIX,
    find_json_plugin,
    ServiceDefinitionImpl,
)
from debputy.plugin.api.impl_types import (
    PackagerProvidedFileClassSpec,
    DebputyPluginMetadata,
    PluginProvidedTrigger,
    ServiceManagerDetails,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.spec import (
    MaintscriptAccessor,
    FlushableSubstvars,
    ServiceRegistry,
    DSD,
    ServiceUpgradeRule,
)
from debputy.plugin.api.test_api.test_spec import (
    InitializedPluginUnderTest,
    RegisteredPackagerProvidedFile,
    RegisteredTrigger,
    RegisteredMaintscript,
    DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS,
    ADRExampleIssue,
    DetectedService,
    RegisteredMetadata,
)
from debputy.plugin.debputy.debputy_plugin import initialize_debputy_features
from debputy.substitution import SubstitutionImpl, VariableContext, Substitution
from debputy.util import package_cross_check_precheck

RegisteredPackagerProvidedFile.register(PackagerProvidedFileClassSpec)


@dataclasses.dataclass(frozen=True, slots=True)
class PackageProcessingContextTestProvider(PackageProcessingContext):
    binary_package: BinaryPackage
    binary_package_version: str
    related_udeb_package: Optional[BinaryPackage]
    related_udeb_package_version: Optional[str]
    accessible_package_roots: Callable[[], Sequence[Tuple[BinaryPackage, VirtualPath]]]


def _initialize_plugin_under_test(
    plugin_metadata: DebputyPluginMetadata,
    load_debputy_plugin: bool = True,
) -> "InitializedPluginUnderTest":
    feature_set = PluginProvidedFeatureSet()
    substitution = SubstitutionImpl(
        unresolvable_substitutions=frozenset(["SOURCE_DATE_EPOCH", "PACKAGE"]),
        variable_context=VariableContext(
            FSROOverlay.create_root_dir("debian", "debian"),
        ),
        plugin_feature_set=feature_set,
    )

    if load_debputy_plugin:
        debputy_plugin_metadata = plugin_metadata_for_debputys_own_plugin(
            initialize_debputy_features
        )
        # Load debputy's own plugin first, so conflicts with debputy's plugin are detected early
        debputy_provider = DebputyPluginInitializerProvider(
            debputy_plugin_metadata,
            feature_set,
            substitution,
        )
        debputy_provider.load_plugin()

    plugin_under_test_provider = DebputyPluginInitializerProvider(
        plugin_metadata,
        feature_set,
        substitution,
    )
    plugin_under_test_provider.load_plugin()

    return InitializedPluginUnderTestImpl(
        plugin_metadata.plugin_name,
        feature_set,
        substitution,
    )


def _auto_load_plugin_from_filename(
    py_test_filename: str,
) -> "InitializedPluginUnderTest":
    dirname, basename = os.path.split(py_test_filename)
    plugin_name = PLUGIN_TEST_SUFFIX.sub("", basename).replace("_", "-")

    test_location = os.environ.get("DEBPUTY_TEST_PLUGIN_LOCATION", "uninstalled")
    if test_location == "uninstalled":
        json_basename = f"{plugin_name}.json"
        json_desc_file = os.path.join(dirname, json_basename)
        if "/" not in json_desc_file:
            json_desc_file = f"./{json_desc_file}"

        if os.path.isfile(json_desc_file):
            return _initialize_plugin_from_desc(json_desc_file)

        json_desc_file_in = f"{json_desc_file}.in"
        if os.path.isfile(json_desc_file_in):
            return _initialize_plugin_from_desc(json_desc_file)
        raise FileNotFoundError(
            f"Cannot determine the plugin JSON metadata descriptor: Expected it to be"
            f" {json_desc_file} or {json_desc_file_in}"
        )

    if test_location == "installed":
        plugin_metadata = find_json_plugin([str(DEBPUTY_PLUGIN_ROOT_DIR)], plugin_name)
        return _initialize_plugin_under_test(plugin_metadata, load_debputy_plugin=True)

    raise ValueError(
        'Invalid or unsupported "DEBPUTY_TEST_PLUGIN_LOCATION" environment variable. It must be either'
        ' unset OR one of "installed", "uninstalled".'
    )


def initialize_plugin_under_test(
    *,
    plugin_desc_file: Optional[str] = None,
) -> "InitializedPluginUnderTest":
    """Load and initialize a plugin for testing it

    This method will load the plugin via plugin description, which is the method that `debputy` does at
     run-time (in contrast to `initialize_plugin_under_test_preloaded`, which bypasses this concrete part
     of the flow).

    :param plugin_desc_file: The plugin description file (`.json`) that describes how to load the plugin.
      If omitted, `debputy` will attempt to attempt the plugin description file based on the test itself.
      This works for "single-file" plugins, where the description file and the test are right next to
      each other.

      Note that the description file is *not* required to a valid version at this stage (e.g., "N/A" or
      "@PLACEHOLDER@") is fine. So you still use this method if you substitute in the version during
      build after running the tests. To support this flow, the file name can also end with `.json.in`
      (instead of `.json`).
    :return: The loaded plugin for testing
    """
    if plugin_desc_file is None:
        caller_file = inspect.stack()[1].filename
        return _auto_load_plugin_from_filename(caller_file)
    if DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS:
        raise RuntimeError(
            "Running the test against an installed plugin does not work when"
            " plugin_desc_file is provided. Please skip this test. You can "
            " import DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS and use that as"
            " conditional for this purpose."
        )
    return _initialize_plugin_from_desc(plugin_desc_file)


def _initialize_plugin_from_desc(
    desc_file: str,
) -> "InitializedPluginUnderTest":
    if not desc_file.endswith((".json", ".json.in")):
        raise ValueError("The plugin file must end with .json or .json.in")

    plugin_metadata = parse_json_plugin_desc(desc_file)

    return _initialize_plugin_under_test(plugin_metadata, load_debputy_plugin=True)


def initialize_plugin_under_test_from_inline_json(
    plugin_name: str,
    json_content: str,
) -> "InitializedPluginUnderTest":
    with BytesIO(json_content.encode("utf-8")) as fd:
        plugin_metadata = parse_json_plugin_desc(plugin_name, fd=fd)

    return _initialize_plugin_under_test(plugin_metadata, load_debputy_plugin=True)


def initialize_plugin_under_test_preloaded(
    api_compat_version: int,
    plugin_initializer: PluginInitializationEntryPoint,
    /,
    plugin_name: str = "plugin-under-test",
    load_debputy_plugin: bool = True,
) -> "InitializedPluginUnderTest":
    """Internal API: Initialize a plugin for testing without loading it from a file

    This method by-passes the standard loading mechanism, meaning you will not test that your plugin
    description file is correct. Notably, any feature provided via the JSON description file will
    **NOT** be visible for the test.

    This API is mostly useful for testing parts of debputy itself.

    :param api_compat_version: The API version the plugin was written for. Use the same version as the
      version from the entry point (The `v1` part of `debputy.plugins.v1.initialize` translate into `1`).
    :param plugin_initializer: The entry point of the plugin
    :param plugin_name: Normally, debputy would derive this from the entry point. In the test, it will
      use a test name and version. However, you can explicitly set if you want the real name/version.
    :param load_debputy_plugin: Whether to load debputy's own plugin first. Doing so provides a more
      realistic test and enables the test to detect conflicts with debputy's own plugins (de facto making
      the plugin unloadable in practice if such a conflict is present).  This option is mostly provided
      to enable debputy to use this method for self testing.
    :return: The loaded plugin for testing
    """

    if DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS:
        raise RuntimeError(
            "Running the test against an installed plugin does not work when"
            " the plugin is preload. Please skip this test. You can "
            " import DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS and use that as"
            " conditional for this purpose."
        )

    plugin_metadata = DebputyPluginMetadata(
        plugin_name=plugin_name,
        api_compat_version=api_compat_version,
        plugin_initializer=plugin_initializer,
        plugin_loader=None,
        plugin_path="<loaded-via-test>",
    )

    return _initialize_plugin_under_test(
        plugin_metadata,
        load_debputy_plugin=load_debputy_plugin,
    )


class _MockArchTable:
    @staticmethod
    def matches_architecture(_a: str, _b: str) -> bool:
        return True


FAKE_DPKG_QUERY_TABLE = cast("DpkgArchTable", _MockArchTable())
del _MockArchTable


def package_metadata_context(
    *,
    host_arch: str = "amd64",
    package_fields: Optional[Dict[str, str]] = None,
    related_udeb_package_fields: Optional[Dict[str, str]] = None,
    binary_package_version: str = "1.0-1",
    related_udeb_package_version: Optional[str] = None,
    should_be_acted_on: bool = True,
    related_udeb_fs_root: Optional[VirtualPath] = None,
    accessible_package_roots: Sequence[Tuple[Mapping[str, str], VirtualPath]] = tuple(),
) -> PackageProcessingContext:
    process_table = faked_arch_table(host_arch)
    f = {
        "Package": "foo",
        "Architecture": "any",
    }
    if package_fields is not None:
        f.update(package_fields)

    bin_package = BinaryPackage(
        Deb822(f),
        process_table,
        FAKE_DPKG_QUERY_TABLE,
        is_main_package=True,
        should_be_acted_on=should_be_acted_on,
    )
    udeb_package = None
    if related_udeb_package_fields is not None:
        uf = dict(related_udeb_package_fields)
        uf.setdefault("Package", f'{f["Package"]}-udeb')
        uf.setdefault("Architecture", f["Architecture"])
        uf.setdefault("Package-Type", "udeb")
        udeb_package = BinaryPackage(
            Deb822(uf),
            process_table,
            FAKE_DPKG_QUERY_TABLE,
            is_main_package=False,
            should_be_acted_on=True,
        )
        if related_udeb_package_version is None:
            related_udeb_package_version = binary_package_version
    if accessible_package_roots:
        apr = []
        for fields, apr_fs_root in accessible_package_roots:
            apr_fields = Deb822(dict(fields))
            if "Package" not in apr_fields:
                raise ValueError(
                    "Missing mandatory Package field in member of accessible_package_roots"
                )
            if "Architecture" not in apr_fields:
                raise ValueError(
                    "Missing mandatory Architecture field in member of accessible_package_roots"
                )
            apr_package = BinaryPackage(
                apr_fields,
                process_table,
                FAKE_DPKG_QUERY_TABLE,
                is_main_package=False,
                should_be_acted_on=True,
            )
            r = package_cross_check_precheck(bin_package, apr_package)
            if not r[0]:
                raise ValueError(
                    f"{apr_package.name} would not be accessible for {bin_package.name}"
                )
            apr.append((apr_package, apr_fs_root))

        if related_udeb_fs_root is not None:
            if udeb_package is None:
                raise ValueError(
                    "related_udeb_package_fields must be given when related_udeb_fs_root is given"
                )
            r = package_cross_check_precheck(bin_package, udeb_package)
            if not r[0]:
                raise ValueError(
                    f"{udeb_package.name} would not be accessible for {bin_package.name}, so providing"
                    " related_udeb_fs_root is irrelevant"
                )
            apr.append(udeb_package)
        apr = tuple(apr)
    else:
        apr = tuple()

    return PackageProcessingContextTestProvider(
        binary_package=bin_package,
        related_udeb_package=udeb_package,
        binary_package_version=binary_package_version,
        related_udeb_package_version=related_udeb_package_version,
        accessible_package_roots=lambda: apr,
    )


def manifest_variable_resolution_context(
    *,
    debian_dir: Optional[VirtualPath] = None,
) -> VariableContext:
    if debian_dir is None:
        debian_dir = FSRootDir()

    return VariableContext(debian_dir)


class MaintscriptAccessorTestProvider(MaintscriptAccessorProviderBase):
    __slots__ = ("_plugin_metadata", "_plugin_source_id", "_maintscript_container")

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        maintscript_container: Dict[str, List[RegisteredMaintscript]],
    ):
        self._plugin_metadata = plugin_metadata
        self._plugin_source_id = plugin_source_id
        self._maintscript_container = maintscript_container

    @classmethod
    def _apply_condition_to_script(
        cls, condition: str, run_snippet: str, /, indent: Optional[bool] = None
    ) -> str:
        return run_snippet

    def _append_script(
        self,
        caller_name: str,
        maintscript: Maintscript,
        full_script: str,
        /,
        perform_substitution: bool = True,
    ) -> None:
        if self._plugin_source_id not in self._maintscript_container:
            self._maintscript_container[self._plugin_source_id] = []
        self._maintscript_container[self._plugin_source_id].append(
            RegisteredMaintscript(
                maintscript,
                caller_name,
                full_script,
                perform_substitution,
            )
        )


class RegisteredMetadataImpl(RegisteredMetadata):
    __slots__ = (
        "_substvars",
        "_triggers",
        "_maintscripts",
    )

    def __init__(
        self,
        substvars: Substvars,
        triggers: List[RegisteredTrigger],
        maintscripts: List[RegisteredMaintscript],
    ) -> None:
        self._substvars = substvars
        self._triggers = triggers
        self._maintscripts = maintscripts

    @property
    def substvars(self) -> Substvars:
        return self._substvars

    @property
    def triggers(self) -> List[RegisteredTrigger]:
        return self._triggers

    def maintscripts(
        self,
        *,
        maintscript: Optional[Maintscript] = None,
    ) -> List[RegisteredMaintscript]:
        if maintscript is None:
            return self._maintscripts
        return [m for m in self._maintscripts if m.maintscript == maintscript]


class BinaryCtrlAccessorTestProvider(BinaryCtrlAccessorProviderBase):
    __slots__ = ("_maintscript_container",)

    def __init__(
        self,
        plugin_metadata: DebputyPluginMetadata,
        plugin_source_id: str,
        context: PackageProcessingContext,
    ) -> None:
        super().__init__(
            plugin_metadata,
            plugin_source_id,
            context,
            {},
            FlushableSubstvars(),
            (None, None),
        )
        self._maintscript_container: Dict[str, List[RegisteredMaintscript]] = {}

    def _create_maintscript_accessor(self) -> MaintscriptAccessor:
        return MaintscriptAccessorTestProvider(
            self._plugin_metadata,
            self._plugin_source_id,
            self._maintscript_container,
        )

    def registered_metadata(self) -> RegisteredMetadata:
        return RegisteredMetadataImpl(
            self._substvars,
            [
                RegisteredTrigger.from_plugin_provided_trigger(t)
                for t in self._triggers.values()
                if t.provider_source_id == self._plugin_source_id
            ],
            self._maintscript_container.get(self._plugin_source_id, []),
        )


class ServiceRegistryTestImpl(ServiceRegistry[DSD]):
    __slots__ = ("_service_manager_details", "_service_definitions")

    def __init__(
        self,
        service_manager_details: ServiceManagerDetails,
        detected_services: List[DetectedService[DSD]],
    ) -> None:
        self._service_manager_details = service_manager_details
        self._service_definitions = detected_services

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
        self._service_definitions.append(
            DetectedService(
                path,
                names,
                type_of_service,
                service_scope,
                enable_by_default,
                start_by_default,
                default_upgrade_rule,
                service_context,
            )
        )


@contextlib.contextmanager
def _read_only_fs_root(fs_root: VirtualPath) -> Iterator[VirtualPath]:
    if fs_root.is_read_write:
        assert isinstance(fs_root, FSRootDir)
        fs_root.is_read_write = False
        yield fs_root
        fs_root.is_read_write = True
    else:
        yield fs_root


class InitializedPluginUnderTestImpl(InitializedPluginUnderTest):
    def __init__(
        self,
        plugin_name: str,
        feature_set: PluginProvidedFeatureSet,
        substitution: SubstitutionImpl,
    ) -> None:
        self._feature_set = feature_set
        self._plugin_name = plugin_name
        self._packager_provided_files: Optional[
            Dict[str, RegisteredPackagerProvidedFile]
        ] = None
        self._triggers: Dict[Tuple[DpkgTriggerType, str], PluginProvidedTrigger] = {}
        self._maintscript_container: Dict[str, List[RegisteredMaintscript]] = {}
        self._substitution = substitution
        assert plugin_name in self._feature_set.plugin_data

    @property
    def _plugin_metadata(self) -> DebputyPluginMetadata:
        return self._feature_set.plugin_data[self._plugin_name]

    def packager_provided_files_by_stem(
        self,
    ) -> Mapping[str, RegisteredPackagerProvidedFile]:
        ppf = self._packager_provided_files
        if ppf is None:
            result: Dict[str, RegisteredPackagerProvidedFile] = {}
            for spec in self._feature_set.packager_provided_files.values():
                if spec.debputy_plugin_metadata.plugin_name != self._plugin_name:
                    continue
                # Registered as a virtual subclass, so this should always be True
                assert isinstance(spec, RegisteredPackagerProvidedFile)
                result[spec.stem] = spec
            self._packager_provided_files = result
            ppf = result
        return ppf

    def run_metadata_detector(
        self,
        metadata_detector_id: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
    ) -> RegisteredMetadata:
        if fs_root.parent_dir is not None:
            raise ValueError("Provided path must be the file system root.")
        detectors = self._feature_set.metadata_maintscript_detectors[self._plugin_name]
        matching_detectors = [
            d for d in detectors if d.detector_id == metadata_detector_id
        ]
        if len(matching_detectors) != 1:
            assert not matching_detectors
            raise ValueError(
                f"The plugin {self._plugin_name} did not provide a metadata detector with ID"
                f' "{metadata_detector_id}"'
            )
        if context is None:
            context = package_metadata_context()
        detector = matching_detectors[0]
        if not detector.applies_to(context.binary_package):
            raise ValueError(
                f'The detector "{metadata_detector_id}" from {self._plugin_name} does not apply to the'
                " given package. Consider using `package_metadata_context()` to emulate a binary package"
                " with the correct specification. As an example: "
                '`package_metadata_context(package_fields={"Package-Type": "udeb"})` would emulate a udeb'
                " package."
            )

        ctrl = BinaryCtrlAccessorTestProvider(
            self._plugin_metadata,
            metadata_detector_id,
            context,
        )
        with _read_only_fs_root(fs_root) as ro_root:
            detector.run_detector(
                ro_root,
                ctrl,
                context,
            )
        return ctrl.registered_metadata()

    def run_package_processor(
        self,
        package_processor_id: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
    ) -> None:
        if fs_root.parent_dir is not None:
            raise ValueError("Provided path must be the file system root.")
        pp_key = (self._plugin_name, package_processor_id)
        package_processor = self._feature_set.all_package_processors.get(pp_key)
        if package_processor is None:
            raise ValueError(
                f"The plugin {self._plugin_name} did not provide a package processor with ID"
                f' "{package_processor_id}"'
            )
        if context is None:
            context = package_metadata_context()
        if not fs_root.is_read_write:
            raise ValueError(
                "The provided fs_root is read-only and it must be read-write for package processor"
            )
        if not package_processor.applies_to(context.binary_package):
            raise ValueError(
                f'The package processor "{package_processor_id}" from {self._plugin_name} does not apply'
                " to the given package. Consider using `package_metadata_context()` to emulate a binary"
                " package with the correct specification. As an example: "
                '`package_metadata_context(package_fields={"Package-Type": "udeb"})` would emulate a udeb'
                " package."
            )
        package_processor.run_package_processor(
            fs_root,
            None,
            context,
        )

    @property
    def declared_manifest_variables(self) -> FrozenSet[str]:
        return frozenset(
            {
                k
                for k, v in self._feature_set.manifest_variables.items()
                if v.plugin_metadata.plugin_name == self._plugin_name
            }
        )

    def automatic_discard_rules_examples_with_issues(self) -> Sequence[ADRExampleIssue]:
        issues = []
        for adr in self._feature_set.auto_discard_rules.values():
            if adr.plugin_metadata.plugin_name != self._plugin_name:
                continue
            for idx, example in enumerate(adr.examples):
                result = process_discard_rule_example(
                    adr,
                    example,
                )
                if result.inconsistent_paths:
                    issues.append(
                        ADRExampleIssue(
                            adr.name,
                            idx,
                            [
                                x.absolute + ("/" if x.is_dir else "")
                                for x in result.inconsistent_paths
                            ],
                        )
                    )
        return issues

    def run_service_detection_and_integrations(
        self,
        service_manager: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
        *,
        service_context_type_hint: Optional[Type[DSD]] = None,
    ) -> Tuple[List[DetectedService[DSD]], RegisteredMetadata]:
        if fs_root.parent_dir is not None:
            raise ValueError("Provided path must be the file system root.")
        try:
            service_manager_details = self._feature_set.service_managers[
                service_manager
            ]
            if service_manager_details.plugin_metadata.plugin_name != self._plugin_name:
                raise KeyError(service_manager)
        except KeyError:
            raise ValueError(
                f"The plugin {self._plugin_name} does not provide a"
                f" service manager called {service_manager}"
            ) from None

        if context is None:
            context = package_metadata_context()
        detected_services: List[DetectedService[DSD]] = []
        registry = ServiceRegistryTestImpl(service_manager_details, detected_services)
        service_manager_details.service_detector(
            fs_root,
            registry,
            context,
        )
        ctrl = BinaryCtrlAccessorTestProvider(
            self._plugin_metadata,
            service_manager_details.service_manager,
            context,
        )
        if detected_services:
            service_definitions = [
                ServiceDefinitionImpl(
                    ds.names[0],
                    ds.names,
                    ds.path,
                    ds.type_of_service,
                    ds.service_scope,
                    ds.enable_by_default,
                    ds.start_by_default,
                    ds.default_upgrade_rule,
                    self._plugin_name,
                    True,
                    ds.service_context,
                )
                for ds in detected_services
            ]
            service_manager_details.service_integrator(
                service_definitions,
                ctrl,
                context,
            )
        return detected_services, ctrl.registered_metadata()

    def manifest_variables(
        self,
        *,
        resolution_context: Optional[VariableContext] = None,
        mocked_variables: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, str]:
        valid_manifest_variables = frozenset(
            {
                n
                for n, v in self._feature_set.manifest_variables.items()
                if v.plugin_metadata.plugin_name == self._plugin_name
            }
        )
        if resolution_context is None:
            resolution_context = manifest_variable_resolution_context()
        substitution = self._substitution.copy_for_subst_test(
            self._feature_set,
            resolution_context,
            extra_substitutions=mocked_variables,
        )
        return SubstitutionTable(
            valid_manifest_variables,
            substitution,
        )


class SubstitutionTable(Mapping[str, str]):
    def __init__(
        self, valid_manifest_variables: FrozenSet[str], substitution: Substitution
    ) -> None:
        self._valid_manifest_variables = valid_manifest_variables
        self._resolved: Set[str] = set()
        self._substitution = substitution

    def __contains__(self, item: object) -> bool:
        return item in self._valid_manifest_variables

    def __getitem__(self, key: str) -> str:
        if key not in self._valid_manifest_variables:
            raise KeyError(key)
        v = self._substitution.substitute(
            "{{" + key + "}}", f"test of manifest variable `{key}`"
        )
        self._resolved.add(key)
        return v

    def __len__(self) -> int:
        return len(self._valid_manifest_variables)

    def __iter__(self) -> Iterator[str]:
        return iter(self._valid_manifest_variables)

    def keys(self) -> KeysView[str]:
        return cast("KeysView[str]", self._valid_manifest_variables)
