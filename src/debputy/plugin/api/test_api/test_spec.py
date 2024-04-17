import dataclasses
import os
from abc import ABCMeta
from typing import (
    Iterable,
    Mapping,
    Callable,
    Optional,
    Union,
    List,
    Tuple,
    Set,
    Sequence,
    Generic,
    Type,
    Self,
    FrozenSet,
)

from debian.substvars import Substvars

from debputy import filesystem_scan
from debputy.plugin.api import (
    VirtualPath,
    PackageProcessingContext,
    DpkgTriggerType,
    Maintscript,
)
from debputy.plugin.api.impl_types import PluginProvidedTrigger
from debputy.plugin.api.spec import DSD, ServiceUpgradeRule, PathDef
from debputy.substitution import VariableContext

DEBPUTY_TEST_AGAINST_INSTALLED_PLUGINS = (
    os.environ.get("DEBPUTY_TEST_PLUGIN_LOCATION", "uninstalled") == "installed"
)


@dataclasses.dataclass(slots=True, frozen=True)
class ADRExampleIssue:
    name: str
    example_index: int
    inconsistent_paths: Sequence[str]


def build_virtual_file_system(
    paths: Iterable[Union[str, PathDef]],
    read_write_fs: bool = True,
) -> VirtualPath:
    """Create a pure-virtual file system for use with metadata detectors

    This method will generate a virtual file system a list of path names or virtual path definitions.  It will
    also insert any implicit path required to make the file system connected.  As an example:

        >>> fs_root = build_virtual_file_system(['./usr/share/doc/package/copyright'])
        >>> # The file we explicitly requested is obviously there
        >>> fs_root.lookup('./usr/share/doc/package/copyright') is not None
        True
        >>> # but so is every directory up to that point
        >>> all(fs_root.lookup(d).is_dir
        ...     for d in ['./usr', './usr/share', './usr/share/doc', './usr/share/doc/package']
        ... )
        True

    Any string provided will be passed to `virtual_path` using all defaults for other parameters, making `str`
    arguments a nice easy shorthand if you just want a path to exist, but do not really care about it otherwise
    (or `virtual_path_def` defaults happens to work for you).

    Here is a very small example of how to create some basic file system objects to get you started:

        >>> from debputy.plugin.api import virtual_path_def
        >>> path_defs = [
        ...    './usr/share/doc/',                                       # Create a directory
        ...    virtual_path_def("./bin/zcat", link_target="/bin/gzip"),  # Create a symlink
        ...    virtual_path_def("./bin/gzip", mode=0o755),               # Create a file (with a custom mode)
        ... ]
        >>> fs_root = build_virtual_file_system(path_defs)
        >>> fs_root.lookup('./usr/share/doc').is_dir
        True
        >>> fs_root.lookup('./bin/zcat').is_symlink
        True
        >>> fs_root.lookup('./bin/zcat').readlink() == '/bin/gzip'
        True
        >>> fs_root.lookup('./bin/gzip').is_file
        True
        >>> fs_root.lookup('./bin/gzip').mode == 0o755
        True

    :param paths: An iterable any mix of path names (str) and virtual_path_def definitions
        (results from `virtual_path_def`).
    :param read_write_fs: Whether the file system is read-write (True) or read-only (False).
        Note that this is the default permission; the plugin test API may temporarily turn a
        read-write to read-only temporarily (when running a metadata detector, etc.).
    :return: The root of the generated file system
    """
    return filesystem_scan.build_virtual_fs(paths, read_write_fs=read_write_fs)


@dataclasses.dataclass(slots=True, frozen=True)
class RegisteredTrigger:
    dpkg_trigger_type: DpkgTriggerType
    dpkg_trigger_target: str

    def serialized_format(self) -> str:
        """The semantic contents of the DEBIAN/triggers file"""
        return f"{self.dpkg_trigger_type} {self.dpkg_trigger_target}"

    @classmethod
    def from_plugin_provided_trigger(
        cls,
        plugin_provided_trigger: PluginProvidedTrigger,
    ) -> "Self":
        return cls(
            plugin_provided_trigger.dpkg_trigger_type,
            plugin_provided_trigger.dpkg_trigger_target,
        )


@dataclasses.dataclass(slots=True, frozen=True)
class RegisteredMaintscript:
    """Details about a maintscript registered by a plugin"""

    """Which maintscript is applies to (e.g., "postinst")"""
    maintscript: Maintscript
    """Which method was used to trigger the script (e.g., "on_configure")"""
    registration_method: str
    """The snippet provided by the plugin as it was provided

    That is, no indentation/conditions/substitutions have been applied to this text
    """
    plugin_provided_script: str
    """Whether substitutions would have been applied in a production run"""
    requested_substitution: bool


@dataclasses.dataclass(slots=True, frozen=True)
class DetectedService(Generic[DSD]):
    path: VirtualPath
    names: Sequence[str]
    type_of_service: str
    service_scope: str
    enable_by_default: bool
    start_by_default: bool
    default_upgrade_rule: ServiceUpgradeRule
    service_context: Optional[DSD]


class RegisteredPackagerProvidedFile(metaclass=ABCMeta):
    """Record of a registered packager provided file - No instantiation

    New "mandatory" attributes may be added in minor versions, which means instantiation will break tests.
    Plugin providers should therefore not create instances of this dataclass.  It is visible only to aid
    test writing by providing type-safety / auto-completion.
    """

    """The name stem used for generating the file"""
    stem: str
    """The recorded directory these file should be installed into"""
    installed_path: str
    """The mode that debputy will give these files when installed (unless overridden)"""
    default_mode: int
    """The default priority assigned to files unless overridden (if priories are assigned at all)"""
    default_priority: Optional[int]
    """The filename format to be used"""
    filename_format: Optional[str]
    """The formatting correcting callback"""
    post_formatting_rewrite: Optional[Callable[[str], str]]

    def compute_dest(
        self,
        assigned_name: str,
        *,
        assigned_priority: Optional[int] = None,
        owning_package: Optional[str] = None,
        path: Optional[VirtualPath] = None,
    ) -> Tuple[str, str]:
        """Determine the basename of this packager provided file

        This method is useful for verifying that the `installed_path` and `post_formatting_rewrite` works
        as intended. As example, some programs do not support "." in their configuration files, so you might
        have a post_formatting_rewrite à la `lambda x: x.replace(".", "_")`.  Then you can test it by
        calling `assert rppf.compute_dest("python3.11")[1] == "python3_11"` to verify that if a package like
        `python3.11` were to use this packager provided file, it would still generate a supported file name.

        For the `assigned_name` parameter, then this is normally derived from the filename. Examples for
        how to derive it:

          * `debian/my-pkg.stem` => `my-pkg`
          * `debian/my-pkg.my-custom-name.stem` => `my-custom-name`

        Note that all parts (`my-pkg`, `my-custom-name` and `stem`) can contain periods (".") despite
        also being a delimiter. Additionally, `my-custom-name` is not restricted to being a valid package
        name, so it can have any file-system valid character in it.

        For the 0.01% case, where the plugin is using *both* `{name}` *and* `{owning_package}` in the
        installed_path, then you can separately *also* set the `owning_package` attribute.  However, by
        default the `assigned_named` is used for both when `owning_package` is not provided.

        :param assigned_name: The name assigned.  Usually this is the name of the package containing the file.
        :param assigned_priority: Optionally a priority override for the file (if priority is supported). Must be
          omitted/None if priorities are not supported.
        :param owning_package: Optionally the name of the owning package.  It is only needed for those exceedingly
          rare cases where the `installed_path` contains both `{owning_package}` (usually in addition to `{name}`).
        :param path: Special-case param, only needed for when testing a special `debputy` PPF..
        :return: A tuple of the directory name and the basename (in that order) that combined makes up that path
          that debputy would use.
        """
        raise NotImplementedError


class RegisteredMetadata:
    __slots__ = ()

    @property
    def substvars(self) -> Substvars:
        """Returns the Substvars

        :return: The substvars in their current state.
        """
        raise NotImplementedError

    @property
    def triggers(self) -> List[RegisteredTrigger]:
        raise NotImplementedError

    def maintscripts(
        self,
        *,
        maintscript: Optional[Maintscript] = None,
    ) -> List[RegisteredMaintscript]:
        """Extract the maintscript provided by the given metadata detector

        :param maintscript: If provided, only snippet registered for the given maintscript is returned. Can be
          used to say "Give me all the 'postinst' snippets by this metadata detector", which can simplify
          verification in some cases.
        :return: A list of all matching maintscript registered by the metadata detector. If the detector has
          not been run, then the list will be empty.  If the metadata detector has been run multiple times,
          then this is the aggregation of all the runs.
        """
        raise NotImplementedError


class InitializedPluginUnderTest:
    def packager_provided_files(self) -> Iterable[RegisteredPackagerProvidedFile]:
        """An iterable of all packager provided files registered by the plugin under test

        If you want a particular order, please sort the result.
        """
        return self.packager_provided_files_by_stem().values()

    def packager_provided_files_by_stem(
        self,
    ) -> Mapping[str, RegisteredPackagerProvidedFile]:
        """All packager provided files registered by the plugin under test grouped by name stem"""
        raise NotImplementedError

    def run_metadata_detector(
        self,
        metadata_detector_id: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
    ) -> RegisteredMetadata:
        """Run a metadata detector (by its ID) against a given file system

        :param metadata_detector_id: The ID of the metadata detector to run
        :param fs_root: The file system the metadata detector should see (must be the root of the file system)
        :param context: The context the metadata detector should see. If not provided, one will be mock will be
          provided to the extent possible.
        :return: The metadata registered by the metadata detector
        """
        raise NotImplementedError

    def run_package_processor(
        self,
        package_processor_id: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
    ) -> None:
        """Run a package processor (by its ID) against a given file system

        Note: Dependency processors are *not* run first.

        :param package_processor_id: The ID of the package processor to run
        :param fs_root: The file system the package processor should see (must be the root of the file system)
        :param context: The context the package processor should see. If not provided, one will be mock will be
          provided to the extent possible.
        """
        raise NotImplementedError

    @property
    def declared_manifest_variables(self) -> Union[Set[str], FrozenSet[str]]:
        """Extract the manifest variables declared by the plugin

        :return: All manifest variables declared by the plugin
        """
        raise NotImplementedError

    def automatic_discard_rules_examples_with_issues(self) -> Sequence[ADRExampleIssue]:
        """Validate examples of the automatic discard rules

        For any failed example, use `debputy plugin show automatic-discard-rules <name>` to see
        the failed example in full.

        :return: If any examples have issues, this will return a non-empty sequence with an
          entry with each issue.
        """
        raise NotImplementedError

    def run_service_detection_and_integrations(
        self,
        service_manager: str,
        fs_root: VirtualPath,
        context: Optional[PackageProcessingContext] = None,
        *,
        service_context_type_hint: Optional[Type[DSD]] = None,
    ) -> Tuple[List[DetectedService[DSD]], RegisteredMetadata]:
        """Run the service manager's detection logic and return the results

        This method can be used to validate the service detection and integration logic of a plugin
        for a given service manager.

        First the service detector is run and if it finds any services, the integrator code is then
        run on those services with their default values.

        :param service_manager: The name of the service manager as provided during the initialization
        :param fs_root: The file system the system detector should see (must be the root of
           the file system)
        :param context: The context the service detector should see. If not provided, one will be mock
          will be provided to the extent possible.
        :param service_context_type_hint: Unused; but can be used as a type hint for `mypy` (etc.)
          to align the return type.
        :return: A tuple of the list of all detected services in the provided file system and the
          metadata generated by the integrator (if any services were detected).
        """
        raise NotImplementedError

    def manifest_variables(
        self,
        *,
        resolution_context: Optional[VariableContext] = None,
        mocked_variables: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, str]:
        """Provide a table of the manifest variables registered by the plugin

        Each key is a manifest variable and the value of said key is the value of the manifest
        variable.  Lazy loaded variables are resolved when accessed for the first time and may
        raise exceptions if the preconditions are not correct.

        Note this method can be called multiple times with different parameters to provide
        different contexts. Lazy loaded variables are resolved at most once per context.

        :param resolution_context: An optional context for lazy loaded manifest variables.
          Create an instance of it via `manifest_variable_resolution_context`.
        :param mocked_variables: An optional mapping that provides values for certain manifest
          variables. This can be used if you want a certain variable to have a certain value
          for the test to be stable (or because the manifest variable you are mocking is from
          another plugin, and you do not want to deal with the implementation details of how
          it is set). Any variable that depends on the mocked variable will use the mocked
          variable in the given context.
        :return: A table of the manifest variables provided by the plugin.  Note this table
          only contains manifest variables registered by the plugin. Attempting to resolve
          other variables (directly), such as mocked variables or from other plugins, will
          trigger a `KeyError`.
        """
        raise NotImplementedError
