import contextlib
import dataclasses
import os
import tempfile
import textwrap
from typing import (
    Iterable,
    Optional,
    Callable,
    Literal,
    Union,
    Iterator,
    overload,
    FrozenSet,
    Sequence,
    TypeVar,
    Any,
    TYPE_CHECKING,
    TextIO,
    BinaryIO,
    Generic,
    ContextManager,
    List,
    Type,
    Tuple,
    get_args,
    Container,
    final,
)

from debian.substvars import Substvars

from debputy import util
from debputy.exceptions import TestPathWithNonExistentFSPathError, PureVirtualPathError
from debputy.interpreter import Interpreter, extract_shebang_interpreter_from_file
from debputy.manifest_parser.tagging_types import DebputyDispatchableType
from debputy.manifest_parser.util import parse_symbolic_mode
from debputy.packages import BinaryPackage
from debputy.types import S

if TYPE_CHECKING:
    from debputy.plugin.debputy.to_be_api_types import BuildRule, BSR, BuildSystemRule
    from debputy.plugin.api.impl_types import DIPHandler
    from debputy.manifest_parser.base_types import (
        StaticFileSystemOwner,
        StaticFileSystemGroup,
    )


DP = TypeVar("DP", bound=DebputyDispatchableType)


PluginInitializationEntryPoint = Callable[["DebputyPluginInitializer"], None]
MetadataAutoDetector = Callable[
    ["VirtualPath", "BinaryCtrlAccessor", "PackageProcessingContext"], None
]
PackageProcessor = Callable[["VirtualPath", None, "PackageProcessingContext"], None]
DpkgTriggerType = Literal[
    "activate",
    "activate-await",
    "activate-noawait",
    "interest",
    "interest-await",
    "interest-noawait",
]
Maintscript = Literal["postinst", "preinst", "prerm", "postrm"]
PackageTypeSelector = Union[Literal["deb", "udeb"], Iterable[Literal["deb", "udeb"]]]
ServiceUpgradeRule = Literal[
    "do-nothing",
    "reload",
    "restart",
    "stop-then-start",
]

DSD = TypeVar("DSD")
ServiceDetector = Callable[
    ["VirtualPath", "ServiceRegistry[DSD]", "PackageProcessingContext"],
    None,
]
ServiceIntegrator = Callable[
    [
        Sequence["ServiceDefinition[DSD]"],
        "BinaryCtrlAccessor",
        "PackageProcessingContext",
    ],
    None,
]

PMT = TypeVar("PMT")
DebputyIntegrationMode = Literal[
    "full",
    "dh-sequence-zz-debputy",
    "dh-sequence-zz-debputy-rrr",
]

INTEGRATION_MODE_FULL: DebputyIntegrationMode = "full"
INTEGRATION_MODE_DH_DEBPUTY_RRR: DebputyIntegrationMode = "dh-sequence-zz-debputy-rrr"
INTEGRATION_MODE_DH_DEBPUTY: DebputyIntegrationMode = "dh-sequence-zz-debputy"
ALL_DEBPUTY_INTEGRATION_MODES: FrozenSet[DebputyIntegrationMode] = frozenset(
    get_args(DebputyIntegrationMode)
)

_DEBPUTY_DISPATCH_METADATA_ATTR_NAME = "_debputy_dispatch_metadata"


def only_integrations(
    *integrations: DebputyIntegrationMode,
) -> Container[DebputyIntegrationMode]:
    return frozenset(integrations)


def not_integrations(
    *integrations: DebputyIntegrationMode,
) -> Container[DebputyIntegrationMode]:
    return ALL_DEBPUTY_INTEGRATION_MODES - frozenset(integrations)


@dataclasses.dataclass(slots=True, frozen=True)
class PackagerProvidedFileReferenceDocumentation:
    description: Optional[str] = None
    format_documentation_uris: Sequence[str] = tuple()

    def replace(self, **changes: Any) -> "PackagerProvidedFileReferenceDocumentation":
        return dataclasses.replace(self, **changes)


def packager_provided_file_reference_documentation(
    *,
    description: Optional[str] = None,
    format_documentation_uris: Optional[Sequence[str]] = tuple(),
) -> PackagerProvidedFileReferenceDocumentation:
    """Provide documentation for a given packager provided file.

    :param description: Textual description presented to the user.
    :param format_documentation_uris: A sequence of URIs to documentation that describes
      the format of the file. Most relevant first.
    :return:
    """
    uris = tuple(format_documentation_uris) if format_documentation_uris else tuple()
    return PackagerProvidedFileReferenceDocumentation(
        description=description,
        format_documentation_uris=uris,
    )


class PathMetadataReference(Generic[PMT]):
    """An accessor to plugin provided metadata

    This is a *short-lived* reference to a piece of metadata.  It should *not* be stored beyond
    the boundaries of the current plugin execution context as it can be become invalid (as an
    example, if the path associated with this path is removed, then this reference become invalid)
    """

    @property
    def is_present(self) -> bool:
        """Determine whether the value has been set

        If the current plugin cannot access the value, then this method unconditionally returns
        `False` regardless of whether the value is there.

        :return: `True` if the value has been set to a not None value (and not been deleted).
          Otherwise, this property is `False`.
        """
        raise NotImplementedError

    @property
    def can_read(self) -> bool:
        """Test whether it is possible to read the metadata

        Note: That the metadata being readable does *not* imply that the metadata is present.

        :return: True if it is possible to read the metadata. This is always True for the
          owning plugin.
        """
        raise NotImplementedError

    @property
    def can_write(self) -> bool:
        """Test whether it is possible to update the metadata

        :return: True if it is possible to update the metadata.
        """
        raise NotImplementedError

    @property
    def value(self) -> Optional[PMT]:
        """Fetch the currently stored value if present.

        :return: The value previously stored if any. Returns `None` if the value was never
          stored, explicitly set to `None` or was deleted.
        """
        raise NotImplementedError

    @value.setter
    def value(self, value: Optional[PMT]) -> None:
        """Replace any current value with the provided value

        This operation is only possible if the path is writable *and* the caller is from
        the owning plugin OR the owning plugin made the reference read-write.
        """
        raise NotImplementedError

    @value.deleter
    def value(self) -> None:
        """Delete any current value.

        This has the same effect as setting the value to `None`.  It has the same restrictions
        as the value setter.
        """
        self.value = None


@dataclasses.dataclass(slots=True)
class PathDef:
    path_name: str
    mode: Optional[int] = None
    mtime: Optional[int] = None
    has_fs_path: Optional[bool] = None
    fs_path: Optional[str] = None
    link_target: Optional[str] = None
    content: Optional[str] = None
    materialized_content: Optional[str] = None


@dataclasses.dataclass(slots=True, frozen=True)
class DispatchablePluggableManifestRuleMetadata(Generic[DP]):
    """NOT PUBLIC API (used internally by part of the public API)"""

    manifest_keywords: Sequence[str]
    dispatched_type: Type[DP]
    unwrapped_constructor: "DIPHandler"
    expected_debputy_integration_mode: Optional[Container[DebputyIntegrationMode]] = (
        None
    )
    online_reference_documentation: Optional["ParserDocumentation"] = None
    apply_standard_attribute_documentation: bool = False
    source_format: Optional[Any] = None


@dataclasses.dataclass(slots=True, frozen=True)
class BuildSystemManifestRuleMetadata(DispatchablePluggableManifestRuleMetadata):
    build_system_impl: Optional[Type["BuildSystemRule"]] = (None,)
    auto_detection_shadow_build_systems: FrozenSet[str] = frozenset()


def virtual_path_def(
    path_name: str,
    /,
    mode: Optional[int] = None,
    mtime: Optional[int] = None,
    fs_path: Optional[str] = None,
    link_target: Optional[str] = None,
    content: Optional[str] = None,
    materialized_content: Optional[str] = None,
) -> PathDef:
    """Define a virtual path for use with examples or, in tests, `build_virtual_file_system`

    :param path_name: The full path. Must start with "./".  If it ends with "/", the path will be interpreted
      as a directory (the `is_dir` attribute will be True).  Otherwise, it will be a symlink or file depending
      on whether a `link_target` is provided.
    :param mode: The mode to use for this path.  Defaults to 0644 for files and 0755 for directories. The mode
      should be None for symlinks.
    :param mtime: Define the last modified time for this path. If not provided, debputy will provide a default
      if the mtime attribute is accessed.
    :param fs_path: Define a file system path for this path.  This causes `has_fs_path` to return True and the
      `fs_path` attribute will return this value.  The test is required to make this path available to the extent
      required. Note that the virtual file system will *not* examine the provided path in any way nor attempt
      to resolve defaults from the path.
    :param link_target: A target for the symlink. Providing a not None value for this parameter will make the
      path a symlink.
    :param content: The content of the path (if opened).  The path must be a file.
    :param materialized_content: Same as `content` except `debputy` will put the contents into a physical file
      as needed. Cannot be used with `content` or `fs_path`.
    :return: An *opaque* object to be passed to `build_virtual_file_system`. While the exact type is provided
      to aid with typing, the type name and its behaviour is not part of the API.
    """

    is_dir = path_name.endswith("/")
    is_symlink = link_target is not None

    if is_symlink:
        if mode is not None:
            raise ValueError(
                f'Please do not provide mode for symlinks. Triggered by "{path_name}"'
            )
        if is_dir:
            raise ValueError(
                "Path name looks like a directory, but a symlink target was also provided."
                f' Please remove the trailing slash OR the symlink_target. Triggered by "{path_name}"'
            )

    if content and (is_dir or is_symlink):
        raise ValueError(
            "Content was defined however, the path appears to be a directory a or a symlink"
            f' Please remove the content, the trailing slash OR the symlink_target. Triggered by "{path_name}"'
        )

    if materialized_content is not None:
        if content is not None:
            raise ValueError(
                "The materialized_content keyword is mutually exclusive with the content keyword."
                f' Triggered by "{path_name}"'
            )
        if fs_path is not None:
            raise ValueError(
                "The materialized_content keyword is mutually exclusive with the fs_path keyword."
                f' Triggered by "{path_name}"'
            )
    return PathDef(
        path_name,
        mode=mode,
        mtime=mtime,
        has_fs_path=bool(fs_path) or materialized_content is not None,
        fs_path=fs_path,
        link_target=link_target,
        content=content,
        materialized_content=materialized_content,
    )


class PackageProcessingContext:
    """Context for auto-detectors of metadata and package processors (no instantiation)

    This object holds some context related data for the metadata detector or/and package
    processors.  It may receive new attributes in the future.
    """

    __slots__ = ()

    @property
    def binary_package(self) -> BinaryPackage:
        """The binary package stanza from `debian/control`"""
        raise NotImplementedError

    @property
    def binary_package_version(self) -> str:
        """The version of the binary package

        Note this never includes the binNMU version for arch:all packages, but it may for arch:any.
        """
        raise NotImplementedError

    @property
    def related_udeb_package(self) -> Optional[BinaryPackage]:
        """An udeb related to this binary package (if any)"""
        raise NotImplementedError

    @property
    def related_udeb_package_version(self) -> Optional[str]:
        """The version of the related udeb package (if present)

        Note this never includes the binNMU version for arch:all packages, but it may for arch:any.
        """
        raise NotImplementedError

    def accessible_package_roots(self) -> Iterable[Tuple[BinaryPackage, "VirtualPath"]]:
        raise NotImplementedError

    # """The source package stanza from `debian/control`"""
    # source_package: SourcePackage


class DebputyPluginInitializer:
    __slots__ = ()

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
        reference_documentation: Optional[
            PackagerProvidedFileReferenceDocumentation
        ] = None,
    ) -> None:
        """Register a packager provided file (debian/<pkg>.foo)

        Register a packager provided file that debputy should automatically detect and install for the
        packager (example `debian/foo.tmpfiles` -> `debian/foo/usr/lib/tmpfiles.d/foo.conf`).  A packager
        provided file typically identified by a package prefix and a "stem" and by convention placed
        in the `debian/` directory.

        Like debhelper, debputy also supports the `foo.bar.tmpfiles` variant where the file is to be
        installed into the `foo` package but be named after the `bar` segment rather than the package name.
        This feature can be controlled via the `allow_name_segment` parameter.

        :param stem: The "stem" of the file. This would be the `tmpfiles` part of `debian/foo.tmpfiles`.
          Note that this value must be unique across all registered packager provided files.
        :param installed_path: A format string describing where the file should be installed. Would be
          `/usr/lib/tmpfiles.d/{name}.conf` from the example above.

          The caller should provide a string with one or more of the placeholders listed below (usually `{name}`
          should be one of them). The format affect the entire path.

          The following placeholders are supported:
            * `{name}` - The name in the name segment (defaulting the package name if no name segment is given)
            * `{priority}` / `{priority:02}` - The priority of the file. Only provided priorities are used (that
               is, default_priority is not None).  The latter variant ensuring that the priority takes at least
               two characters and the `0` character is left-padded for priorities that takes less than two
               characters.
            * `{owning_package}` - The name of the package.  Should only be used when `{name}` alone is insufficient.
              If you do not want the "name" segment in the first place, use `allow_name_segment=False` instead.

          The path is always interpreted as relative to the binary package root.

        :param default_mode: The mode the installed file should have by default. Common options are 0o0644 (the default)
          or 0o0755 (for files that must be executable).
        :param allow_architecture_segment: If True, the file may have an optional "architecture" segment at the end
           (`foo.tmpfiles.amd64`), which marks it architecture specific. When False, debputy will detect the
           "architecture" segment and report the use as an error.  Note the architecture segment is only allowed for
           arch:any packages. If a file targeting an arch:all package uses an architecture specific file it will
           always result in an error.
        :param allow_name_segment: If True, the file may have an optional "name" segment after the package name prefix.
           (`foo.<name-here>.tmpfiles`). When False, debputy will detect the "name" segment and report the use as an
           error.
        :param default_priority: Special-case option for packager files that are installed into directories that have
          "parse ordering" or "priority".  These files will generally be installed as something like `20-foo.conf`
          where the `20-` denotes their "priority".  If the plugin is registering such a file type, then it should
          provide a default priority.

          The following placeholders are supported:
            * `{name}` - The name in the name segment (defaulting the package name if no name segment is given)
            * `{priority}` - The priority of the file. Only provided priorities are used (that is, default_priority
               is not None)
            * `{owning_package}` - The name of the package.  Should only be used when `{name}` alone is insufficient.
              If you do not want the "name" segment in the first place, use `allow_name_segment=False` instead.
        :param post_formatting_rewrite: An optional "name correcting" callback. It receives the formatted name and can
          do any transformation required. The primary use-case for this is to replace "forbidden" characters. The most
          common case for debputy itself is to replace "." with "_" for tools that refuse to work with files containing
          "." (`lambda x: x.replace(".", "_")`).  The callback operates on basename of formatted version of the
          `installed_path` and the callback should return the basename.
        :param packageless_is_fallback_for_all_packages: If True, the packageless variant (such as, `debian/changelog`)
          is a fallback for every package.
        :param reference_documentation: Reference documentation for the packager provided file. Use the
           packager_provided_file_reference_documentation function to provide the value for this parameter.
        :param reservation_only: When True, tell debputy that the plugin reserves this packager provided file, but that
          debputy should not actually install it automatically.  This is useful in the cases, where the plugin
          needs to process the file before installing it.  The file will be marked as provided by this plugin. This
          enables introspection and detects conflicts if other plugins attempts to claim the file.
        """
        raise NotImplementedError

    def metadata_or_maintscript_detector(
        self,
        auto_detector_id: str,
        auto_detector: MetadataAutoDetector,
        *,
        package_type: PackageTypeSelector = "deb",
    ) -> None:
        """Provide a pre-assembly hook that can affect the metadata/maintscript of binary ("deb") packages

        The provided hook will be run once per binary package to be assembled, and it can see all the content
        ("data.tar") planned to be included in the deb. The hook may do any *read-only* analysis of this content
        and provide metadata, alter substvars or inject maintscript snippets.  However, the hook must *not*
        change the content ("data.tar") part of the deb.

        The hook will be run unconditionally for all binary packages built. When the hook does not apply to all
        packages, it must provide its own (internal) logic for detecting whether it is relevant and reduced itself
        to a no-op if it should not apply to the current package.

        Hooks are run in "some implementation defined order" and should not rely on being run before or after
        any other hook.

        The hooks are only applied to packages defined in `debian/control`. Notably, the metadata detector will
        not apply to auto-generated `-dbgsym` packages (as those are not listed explicitly in `debian/control`).

        :param auto_detector_id: A plugin-wide unique ID for this detector. Packagers may use this ID for disabling
          the detector and accordingly the ID is part of the plugin's API toward the packager.
        :param auto_detector: The code to be called that will be run at the metadata generation state (once for each
          binary package).
        :param package_type: Which kind of packages this metadata detector applies to.  The package type is generally
          defined by `Package-Type` field in the binary package. The default is to only run for regular `deb` packages
          and ignore `udeb` packages.
        """
        raise NotImplementedError

    def manifest_variable(
        self,
        variable_name: str,
        value: str,
        variable_reference_documentation: Optional[str] = None,
    ) -> None:
        """Provide a variable that can be used in the package manifest

            >>> # Enable users to use "{{path:BASH_COMPLETION_DIR}}/foo" in their manifest.
            >>> api.manifest_variable(  # doctest: +SKIP
            ...     "path:BASH_COMPLETION_DIR",
            ...     "/usr/share/bash-completion/completions",
            ...     variable_reference_documentation="Directory to install bash completions into",
            ... )

        :param variable_name: The variable name.
        :param value: The value the variable should resolve to.
        :param variable_reference_documentation: A short snippet of reference documentation that explains
          the purpose of the variable.
        """
        raise NotImplementedError


class MaintscriptAccessor:
    __slots__ = ()

    def on_configure(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
        skip_on_rollback: bool = False,
    ) -> None:
        """Provide a snippet to be run when the package is about to be "configured"

        This condition is the most common "post install" condition and covers the two
        common cases:
          * On initial install, OR
          * On upgrade

        In dpkg maintscript terms, this method roughly corresponds to postinst containing
             `if [ "$1" = configure ]; then <snippet>; fi`

        Additionally, the condition will by default also include rollback/abort scenarios such as "above-remove",
        which is normally what you want but most people forget about.

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param skip_on_rollback: By default, this condition will also cover common rollback scenarios. This
          is normally what you want (or benign in most cases due to the idempotence requirement for maintscripts).
          However, you can disable the rollback cases, leaving only "On initial install OR On upgrade".
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_initial_install(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package is about to be "configured" for the first time

        The snippet will only be run on the first time the package is installed (ever or since last purge).
        Note that "first" does not mean "exactly once" as dpkg does *not* provide such semantics. There are two
        common cases where this can snippet can be run multiple times for the same system (and why the snippet
        must still be idempotent):

          1) The package is installed (1), then purged and then installed again (2).  This can partly be mitigated
             by having an `on_purge` script to do clean up.

          2) As the package is installed, the `postinst` script terminates prematurely (Disk full, power loss, etc.).
             The user resolves the problem and runs `dpkg --configure <pkg>`, which in turn restarts the script
             from the beginning.  This is why scripts must be idempotent in general.

        In dpkg maintscript terms, this method roughly corresponds to postinst containing
             `if [ "$1" = configure ] && [ -z "$2" ]; then <snippet>; fi`

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_upgrade(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package is about to be "configured" after an upgrade

        The snippet will only be run on any upgrade (that is, it will be skipped on the initial install).

        In dpkg maintscript terms, this method roughly corresponds to postinst containing
             `if [ "$1" = configure ] && [ -n "$2" ]; then <snippet>; fi`

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_upgrade_from(
        self,
        version: str,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package is about to be "configured" after an upgrade from a given version

        The snippet will only be run on any upgrade (that is, it will be skipped on the initial install).

        In dpkg maintscript terms, this method roughly corresponds to postinst containing
             `if [ "$1" = configure ] && dpkg --compare-versions le-nl "$2" ; then <snippet>; fi`

        :param version: The version to upgrade from
        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_before_removal(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package is about to be removed

        The snippet will be run before dpkg removes any files.

        In dpkg maintscript terms, this method roughly corresponds to prerm containing
             `if [ "$1" = remove ] ; then <snippet>; fi`

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_removed(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package has been removed

        The snippet will be run after dpkg removes the package content from the file system.

        **WARNING**: The snippet *cannot* rely on dependencies and must rely on `Essential: yes` packages.

        In dpkg maintscript terms, this method roughly corresponds to postrm containing
             `if [ "$1" = remove ] ; then <snippet>; fi`

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def on_purge(
        self,
        run_snippet: str,
        /,
        indent: Optional[bool] = None,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run when the package is being purged.

        The snippet will when the package is purged from the system.

        **WARNING**: The snippet *cannot* rely on dependencies and must rely on `Essential: yes` packages.

        In dpkg maintscript terms, this method roughly corresponds to postrm containing
             `if [ "$1" = purge ] ; then <snippet>; fi`

        :param run_snippet: The actual shell snippet to be run in the given condition.  The snippet must be idempotent.
          The snippet may contain newlines as necessary, which will make the result more readable.  Additionally, the
          snippet may contain '{{FOO}}' substitutions by default.
        :param indent: If True, the provided snippet will be indented to fit the condition provided by debputy.
          In most cases, this is safe to do and provides more readable scripts. However, it may cause issues
          with some special shell syntax (such as "Heredocs"). When False, the snippet will *not* be re-indented.
          You are recommended to do 4 spaces of indentation when indent is False for readability.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def unconditionally_in_script(
        self,
        maintscript: Maintscript,
        run_snippet: str,
        /,
        perform_substitution: bool = True,
    ) -> None:
        """Provide a snippet to be run in a given script

        Run a given snippet unconditionally from a given script.  The snippet must contain its own conditional
        for when it should be run.

        :param maintscript: The maintscript to insert the snippet into.
        :param run_snippet: The actual shell snippet to be run.  The snippet will be run unconditionally and should
          contain its own conditions as necessary. The snippet must be idempotent. The snippet may contain newlines
          as necessary, which will make the result more readable.  Additionally, the snippet may contain '{{FOO}}'
          substitutions by default.
        :param perform_substitution: When True, `{{FOO}}` will be substituted in the snippet. When False, no
          substitution is provided.
        """
        raise NotImplementedError

    def escape_shell_words(self, *args: str) -> str:
        """Provide sh-shell escape of strings

          `assert escape_shell("foo", "fu bar", "baz") == 'foo "fu bar" baz'`

        This is useful for ensuring file names and other "input" are considered one parameter even when they
        contain spaces or shell meta-characters.

        :param args: The string(s) to be escaped.
        :return: Each argument escaped such that each argument becomes a single "word" and then all these words are
          joined by a single space.
        """
        return util.escape_shell(*args)


class BinaryCtrlAccessor:
    __slots__ = ()

    def dpkg_trigger(self, trigger_type: DpkgTriggerType, trigger_target: str) -> None:
        """Register a declarative dpkg level trigger

        The provided trigger will be added to the package's metadata (the triggers file of the control.tar).

        If the trigger has already been added previously, a second call with the same trigger data will be ignored.
        """
        raise NotImplementedError

    @property
    def maintscript(self) -> MaintscriptAccessor:
        """Attribute for manipulating maintscripts"""
        raise NotImplementedError

    @property
    def substvars(self) -> "FlushableSubstvars":
        """Attribute for manipulating dpkg substvars (deb-substvars)"""
        raise NotImplementedError


class VirtualPath:
    __slots__ = ()

    @property
    def name(self) -> str:
        """Basename of the path a.k.a. last segment of the path

        In a path "usr/share/doc/pkg/changelog.gz" the basename is "changelog.gz".

        For a directory, the basename *never* ends with a `/`.
        """
        raise NotImplementedError

    @property
    def iterdir(self) -> Iterable["VirtualPath"]:
        """Returns an iterable that iterates over all children of this path

        For directories, this returns an iterable of all children. For non-directories,
        the iterable is always empty.
        """
        raise NotImplementedError

    def lookup(self, path: str) -> Optional["VirtualPath"]:
        """Perform a path lookup relative to this path

        As an example `doc_dir = fs_root.lookup('./usr/share/doc')`

        If the provided path starts with `/`, then the lookup is performed relative to the
        file system root.  That is, you can assume the following to always be True:

            `fs_root.lookup("usr") == any_path_beneath_fs_root.lookup('/usr')`

        Note: This method requires the path to be attached (see `is_detached`) regardless of
        whether the lookup is relative or absolute.

        If the path traverse a symlink, the symlink will be resolved.

        :param path: The path to look. Can contain "." and ".." segments.  If starting with `/`,
          look up is performed relative to the file system root, otherwise the lookup is relative
          to this path.
        :return: The path object for the desired path if it can be found. Otherwise, None.
        """
        raise NotImplementedError

    def all_paths(self) -> Iterable["VirtualPath"]:
        """Iterate over this path and all of its descendants (if any)

        If used on the root path, then every path in the package is returned.

        The iterable is ordered, so using the order in output will be produce
        bit-for-bit reproducible output. Additionally, a directory will always
        be seen before its descendants. Otherwise, the order is implementation
        defined.

        The iteration is lazy and as a side effect do account for some obvious
        mutation. Like if the current path is removed, then none of its children
        will be returned (provided mutation happens before the lazy iteration
        was required to resolve it). Likewise, mutation of the directory will
        also work (again, provided mutation happens before the lazy iteration order).

        :return: An ordered iterable of this path followed by its descendants.
        """
        raise NotImplementedError

    @property
    def is_detached(self) -> bool:
        """Returns True if this path is detached

        Paths that are detached from the file system will not be present in the package and
        most operations are unsafe on them. This usually only happens if the path or one of
        its parent directories are unlinked (rm'ed) from the file system tree.

        All paths are attached by default and will only become detached as a result of
        an action to mutate the virtual file system.  Note that the file system may not
        always be manipulated.

        :return: True if the entry is detached. Detached entries should be discarded, so they
        can be garbage collected.
        """
        raise NotImplementedError

    # The __getitem__ behaves like __getitem__ from Dict but __iter__ would ideally work like a Sequence.
    # However, that does not feel compatible, so lets force people to use .children instead for the Sequence
    # behaviour to avoid surprises for now.
    # (Maybe it is a non-issue, but it is easier to add the API later than to remove it once we have committed
    # to using it)
    __iter__ = None

    def __getitem__(self, key: object) -> "VirtualPath":
        """Lookup a (direct) child by name

        Ignoring the possible `KeyError`, then the following are the same:
            `fs_root["usr"] == fs_root.lookup('usr')`

        Note that unlike `.lookup` this can only locate direct children.
        """
        raise NotImplementedError

    def __delitem__(self, key) -> None:
        """Remove a child from this node if it exists

        If that child is a directory, then the entire tree is removed (like `rm -fr`).
        """
        raise NotImplementedError

    def get(self, key: str) -> "Optional[VirtualPath]":
        """Lookup a (direct) child by name

        The following are the same:
            `fs_root.get("usr") == fs_root.lookup('usr')`

        Note that unlike `.lookup` this can only locate direct children.
        """
        try:
            return self[key]
        except KeyError:
            return None

    def __contains__(self, item: object) -> bool:
        """Determine if this path includes a given child (either by object or string)

        Examples:

            if 'foo' in dir: ...
        """
        if isinstance(item, VirtualPath):
            return item.parent_dir is self
        if not isinstance(item, str):
            return False
        m = self.get(item)
        return m is not None

    @property
    def path(self) -> str:
        """Returns the "full" path for this file system entry

        This is the path that debputy uses to refer to this file system entry. It is always
        normalized. Use the `absolute` attribute for how the path looks
        when the package is installed. Alternatively, there is also `fs_path`, which is the
        path to the underlying file system object (assuming there is one). That is the one
        you need if you want to read the file.

        This is attribute is mostly useful for debugging or for looking up the path relative
        to the "root" of the virtual file system that debputy maintains.

        If the path is detached (see `is_detached`), then this method returns the path as it
        was known prior to being detached.
        """
        raise NotImplementedError

    @property
    def absolute(self) -> str:
        """Returns the absolute version of this path

        This is how to refer to this path when the package is installed.

        If the path is detached (see `is_detached`), then this method returns the last known location
        of installation (prior to being detached).

        :return: The absolute path of this file as it would be on the installed system.
        """
        p = self.path.lstrip(".")
        if not p.startswith("/"):
            return f"/{p}"
        return p

    @property
    def parent_dir(self) -> Optional["VirtualPath"]:
        """The parent directory of this path

        Note this operation requires the path is "attached" (see `is_detached`).  All paths are attached
        by default but unlinking paths will cause them to become detached.

        :return: The parent path or None for the root.
        """
        raise NotImplementedError

    def stat(self) -> os.stat_result:
        """Attempt to do stat of the underlying path (if it exists)

        *Avoid* using `stat()` whenever possible where a more specialized attribute exist.  The
        `stat()` call returns the data from the file system and often, `debputy` does *not* track
        its state in the file system.  As an example, if you want to know the file system mode of
        a path, please use the `mode` attribute instead.

        This never follow symlinks (it behaves like `os.lstat`). It will raise an error
        if the path is not backed by a file system object (that is, `has_fs_path` is False).

        :return: The stat result or an error.
        """
        raise NotImplementedError()

    @property
    def size(self) -> int:
        """Resolve the file size (`st_size`)

        This may be using `stat()` and therefore `fs_path`.

        :return: The size of the file in bytes
        """
        return self.stat().st_size

    @property
    def mode(self) -> int:
        """Determine the mode bits of this path object

        Note that:
         * like with `stat` above, this never follows symlinks.
         * the mode returned by this method is not always a 1:1 with the mode in the
           physical file system. As an optimization, `debputy` skips unnecessary writes
           to the underlying file system in many cases.


        :return: The mode bits for the path.
        """
        raise NotImplementedError

    @mode.setter
    def mode(self, new_mode: int) -> None:
        """Set the octal file mode of this path

        Note that:
         * this operation will fail if `path.is_read_write` returns False.
         * this operation is generally *not* synced to the physical file system (as
           an optimization).

        :param new_mode: The new octal mode for this path.  Note that `debputy` insists
          that all paths have the `user read bit` and, for directories also, the
          `user execute bit`.  The absence of these minimal mode bits causes hard to
          debug errors.
        """
        raise NotImplementedError

    @property
    def is_executable(self) -> bool:
        """Determine whether a path is considered executable

        Generally, this means that at least one executable bit is set. This will
        basically always be true for directories as directories need the execute
        parameter to be traversable.

        :return: True if the path is considered executable with its current mode
        """
        return bool(self.mode & 0o0111)

    def chmod(self, new_mode: Union[int, str]) -> None:
        """Set the file mode of this path

        This is similar to setting the `mode` attribute. However, this method accepts
        a string argument, which will be parsed as a symbolic mode (example: `u+rX,go=rX`).

        Note that:
         * this operation will fail if `path.is_read_write` returns False.
         * this operation is generally *not* synced to the physical file system (as
           an optimization).

        :param new_mode: The new mode for this path.
          Note that `debputy` insists that all paths have the `user read bit` and, for
          directories also, the `user execute bit`.  The absence of these minimal mode
          bits causes hard to debug errors.
        """
        if isinstance(new_mode, str):
            segments = parse_symbolic_mode(new_mode, None)
            final_mode = self.mode
            is_dir = self.is_dir
            for segment in segments:
                final_mode = segment.apply(final_mode, is_dir)
            self.mode = final_mode
        else:
            self.mode = new_mode

    def chown(
        self,
        owner: Optional["StaticFileSystemOwner"],
        group: Optional["StaticFileSystemGroup"],
    ) -> None:
        """Change the owner/group of this path

        :param owner: The desired owner definition for this path. If None, then no change of owner is performed.
        :param group: The desired  group definition for this path. If None, then no change of group is performed.
        """
        raise NotImplementedError

    @property
    def mtime(self) -> float:
        """Determine the mtime of this path object

        Note that:
         * like with `stat` above, this never follows symlinks.
         * the mtime returned has *not* been clamped against ´SOURCE_DATE_EPOCH`. Timestamp
           normalization is handled later by `debputy`.
         * the mtime returned by this method is not always a 1:1 with the mtime in the
           physical file system. As an optimization, `debputy` skips unnecessary writes
           to the underlying file system in many cases.

        :return: The mtime for the path.
        """
        raise NotImplementedError

    @mtime.setter
    def mtime(self, new_mtime: float) -> None:
        """Set the mtime of this path

        Note that:
         * this operation will fail if `path.is_read_write` returns False.
         * this operation is generally *not* synced to the physical file system (as
           an optimization).

        :param new_mtime: The new mtime of this path. Note that the caller does not need to
          account for `SOURCE_DATE_EPOCH`. Timestamp normalization is handled later.
        """
        raise NotImplementedError

    def readlink(self) -> str:
        """Determine the link target of this path assuming it is a symlink

        For paths where `is_symlink` is True, this already returns a link target even when
        `has_fs_path` is False.

        :return: The link target of the path or an error is this is not a symlink
        """
        raise NotImplementedError()

    @overload
    def open(
        self,
        *,
        byte_io: Literal[False] = False,
        buffering: int = -1,
    ) -> TextIO: ...

    @overload
    def open(
        self,
        *,
        byte_io: Literal[True],
        buffering: int = -1,
    ) -> BinaryIO: ...

    @overload
    def open(
        self,
        *,
        byte_io: bool,
        buffering: int = -1,
    ) -> Union[TextIO, BinaryIO]: ...

    def open(
        self,
        *,
        byte_io: bool = False,
        buffering: int = -1,
    ) -> Union[TextIO, BinaryIO]:
        """Open the file for reading.  Usually used with a context manager

        By default, the file is opened in text mode (utf-8). Binary mode can be requested
        via the `byte_io` parameter.  This operation is only valid for files (`is_file` returns
        `True`). Usage on symlinks and directories will raise exceptions.

        This method *often* requires the `fs_path` to be present.  However, tests as a notable
        case can inject content without having the `fs_path` point to a real file. (To be clear,
        such tests are generally expected to ensure `has_fs_path` returns `True`).


        :param byte_io: If True, open the file in binary mode (like `rb` for `open`)
        :param buffering: Same as open(..., buffering=...) where supported. Notably during
          testing, the content may be purely in memory and use a BytesIO/StringIO
          (which does not accept that parameter, but then it is buffered in a different way)
        :return: The file handle.
        """

        if not self.is_file:
            raise TypeError(f"Cannot open {self.path} for reading: It is not a file")

        if byte_io:
            return open(self.fs_path, "rb", buffering=buffering)
        return open(self.fs_path, "rt", encoding="utf-8", buffering=buffering)

    @property
    def fs_path(self) -> str:
        """Request the underling fs_path of this path

        Only available when `has_fs_path` is True.  Generally this should only be used for files to read
        the contents of the file and do some action based on the parsed result.

        The path should only be used for read-only purposes as debputy may assume that it is safe to have
        multiple paths pointing to the same file system path.

        Note that:
          * This is often *not* available for directories and symlinks.
          * The debputy in-memory file system overrules the physical file system. Attempting to "fix" things
            by using `os.chmod` or `os.unlink`'ing files, etc. will generally not do as you expect. Best case,
            your actions are ignored and worst case it will cause the build to fail as it violates debputy's
            internal invariants.

        :return: The path to the underlying file system object on the build system or an error if no such
        file exist (see `has_fs_path`).
        """
        raise NotImplementedError()

    @property
    def is_dir(self) -> bool:
        """Determine if this path is a directory

        Never follows symlinks.

        :return: True if this path is a directory. False otherwise.
        """
        raise NotImplementedError()

    @property
    def is_file(self) -> bool:
        """Determine if this path is a directory

        Never follows symlinks.

        :return: True if this path is a regular file. False otherwise.
        """
        raise NotImplementedError()

    @property
    def is_symlink(self) -> bool:
        """Determine if this path is a symlink

        :return: True if this path is a symlink. False otherwise.
        """
        raise NotImplementedError()

    @property
    def has_fs_path(self) -> bool:
        """Determine whether this path is backed by a file system path

        :return: True if this path is backed by a file system object on the build system.
        """
        raise NotImplementedError()

    @property
    def is_read_write(self) -> bool:
        """When true, the file system entry may be mutated

        Read-write rules are:

        +--------------------------+-------------------+------------------------+
        | File system              | From / Inside     | Read-Only / Read-Write |
        +--------------------------+-------------------+------------------------+
        | Source directory         | Any context       | Read-Only              |
        | Binary staging directory | Package Processor | Read-Write             |
        | Binary staging directory | Metadata Detector | Read-Only              |
        +--------------------------+-------------------+------------------------+

        These rules apply to the virtual file system (`debputy` cannot enforce
        these rules in the underlying file system). The `debputy` code relies
        on these rules for its logic in multiple places to catch bugs and for
        optimizations.

        As an example, the reason why the file system is read-only when Metadata
        Detectors are run is based the contents of the file system has already
        been committed. New files will not be included, removals of existing
        files will trigger a hard error when the package is assembled, etc.
        To avoid people spending hours debugging why their code does not work
        as intended, `debputy` instead throws a hard error if you try to mutate
        the file system when it is read-only mode to "fail fast".

        :return: Whether file system mutations are permitted.
        """
        return False

    def mkdir(self, name: str) -> "VirtualPath":
        """Create a new subdirectory of the current path

        :param name: Basename of the new directory. The directory must not contain a path
          with this basename.
        :return: The new subdirectory
        """
        raise NotImplementedError

    def mkdirs(self, path: str) -> "VirtualPath":
        """Ensure a given path exists and is a directory.

        :param path: Path to the directory to create. Any parent directories will be
          created as needed. If the path already exists and is a directory, then it
          is returned.  If any part of the path exists and that is not a directory,
          then the `mkdirs` call will raise an error.
        :return: The directory denoted by the given path
        """
        raise NotImplementedError

    def add_file(
        self,
        name: str,
        *,
        unlink_if_exists: bool = True,
        use_fs_path_mode: bool = False,
        mode: int = 0o0644,
        mtime: Optional[float] = None,
    ) -> ContextManager["VirtualPath"]:
        """Add a new regular file as a child of this path

        This method will insert a new file into the virtual file system as a child
        of the current path (which must be a directory).  The caller must use the
        return value as a context manager (see example).  During the life-cycle of
        the managed context, the caller can fill out the contents of the file
        from the new path's `fs_path` attribute. The `fs_path` will exist as an
        empty file when the context manager is entered.

        Once the context manager exits, mutation of the `fs_path` is no longer permitted.

          >>> import subprocess
          >>> path = ...                                                                 # doctest: +SKIP
          >>> with path.add_file("foo") as new_file, open(new_file.fs_path, "w") as fd:  # doctest: +SKIP
          ...     fd.writelines(["Some", "Content", "Here"])

        The caller can replace the provided `fs_path` entirely provided at the end result
        (when the context manager exits) is a regular file with no hard links.

        Note that this operation will fail if `path.is_read_write` returns False.

        :param name: Basename of the new file
        :param unlink_if_exists: If the name was already in use, then either an exception is thrown
           (when `unlink_if_exists` is False) or the path will be removed via ´unlink(recursive=False)`
           (when `unlink_if_exists` is True)
        :param use_fs_path_mode: When True, the file created will have this mode in the physical file
          system. When the context manager exists, `debputy` will refresh its mode to match the mode
          in the physical file system.  This is primarily useful if the caller uses a subprocess to
          mutate the path and the file mode is relevant for this tool (either as input or output).
          When the parameter is false, the new file is guaranteed to be readable and writable for
          the current user. However, no other guarantees are given (not even that it matches the
          `mode` parameter and any changes to the mode in the physical file system will be ignored.
        :param mode: This is the initial file mode. Note the `use_fs_path_mode` parameter for how
          this interacts with the physical file system.
        :param mtime: If the caller has a more accurate mtime than the mtime of the generated file,
          then it can be provided here. Note that all mtimes will later be clamped based on
          `SOURCE_DATE_EPOCH`. This parameter is only for when the conceptual mtime of this path
          should be earlier than `SOURCE_DATE_EPOCH`.
        :return: A Context manager that upon entering provides a `VirtualPath` instance for the
                 new file. The instance remains valid after the context manager exits (assuming it exits
                 successfully), but the file denoted by `fs_path` must not be changed after the context
                 manager exits
        """
        raise NotImplementedError

    def replace_fs_path_content(
        self,
        *,
        use_fs_path_mode: bool = False,
    ) -> ContextManager[str]:
        """Replace the contents of this file via inline manipulation

        Used as a context manager to provide the fs path for manipulation.

        Example:
            >>> import subprocess
            >>> path = ...                                       # doctest: +SKIP
            >>> with path.replace_fs_path_content() as fs_path:  # doctest: +SKIP
            ...    subprocess.check_call(['strip', fs_path])     # doctest: +SKIP

        The provided file system path should be manipulated inline. The debputy framework may
        copy it first as necessary and therefore the provided fs_path may be different from
        `path.fs_path` prior to entering the context manager.

        Note that this operation will fail if `path.is_read_write` returns False.

        If the mutation causes the returned `fs_path` to be a non-file or a hard-linked file
        when the context manager exits, `debputy` will raise an error at that point. To preserve
        the internal invariants of `debputy`, the path will be unlinked as `debputy` cannot
        reliably restore the path.

        :param use_fs_path_mode: If True, any changes to the mode on the physical FS path will be
          recorded as the desired mode of the file when the contextmanager ends.  The provided FS path
          with start with the current mode when `use_fs_path_mode` is True. Otherwise, `debputy` will
          ignore the mode of the file system entry and reuse its own current mode
          definition.
        :return: A Context manager that upon entering provides the path to a muable (copy) of
                 this path's `fs_path` attribute. The file on the underlying path may be mutated however
                 the caller wishes until the context manager exits.
        """
        raise NotImplementedError

    def add_symlink(self, link_name: str, link_target: str) -> "VirtualPath":
        """Add a new regular file as a child of this path

        This will create a new symlink inside the current path. If the path already exists,
        the existing path will be unlinked via `unlink(recursive=False)`.

        Note that this operation will fail if `path.is_read_write` returns False.

        :param link_name: The basename of the link file entry.
        :param link_target: The target of the link.  Link target normalization will
          be handled by `debputy`, so the caller can use relative or absolute paths.
          (At the time of writing, symlink target normalization happens late)
        :return: The newly created symlink.
        """
        raise NotImplementedError

    def unlink(self, *, recursive: bool = False) -> None:
        """Unlink a file or a directory

        This operation will remove the path from the file system (causing `is_detached` to return True).

        When the path is a:

         * symlink, then the symlink itself is removed. The target (if present) is not affected.
         * *non-empty* directory, then the `recursive` parameter decides the outcome. An empty
           directory will be removed regardless of the value of `recursive`.

        Note that:
          * the root directory cannot be deleted.
          * this operation will fail if `path.is_read_write` returns False.

        :param recursive: If True, then non-empty directories will be unlinked as well removing everything inside them
          as well.  When False, an error is raised if the path is a non-empty directory
        """
        raise NotImplementedError

    def interpreter(self) -> Optional[Interpreter]:
        """Determine the interpreter of the file (`#!`-line details)

        Note: this method is only applicable for files (`is_file` is True).

        :return: The detected interpreter if present or None if no interpreter can be detected.
        """
        if not self.is_file:
            raise TypeError("Only files can have interpreters")
        try:
            with self.open(byte_io=True, buffering=4096) as fd:
                return extract_shebang_interpreter_from_file(fd)
        except (PureVirtualPathError, TestPathWithNonExistentFSPathError):
            return None

    def metadata(
        self,
        metadata_type: Type[PMT],
    ) -> PathMetadataReference[PMT]:
        """Fetch the path metadata reference to access the underlying metadata

        Calling this method returns a reference to an arbitrary piece of metadata associated
        with this path. Plugins can store any arbitrary data associated with a given path.
        Keep in mind that the metadata is stored in memory, so keep the size in moderation.

        To store / update the metadata, the path must be in read-write mode. However,
        already stored metadata remains accessible even if the path becomes read-only.

        Note this method is not applicable if the path is detached

        :param metadata_type: Type of the metadata being stored.
        :return: A reference to the metadata.
        """
        raise NotImplementedError


class FlushableSubstvars(Substvars):
    __slots__ = ()

    @contextlib.contextmanager
    def flush(self) -> Iterator[str]:
        """Temporarily write the substvars to a file and then re-read it again

        >>> s = FlushableSubstvars()
        >>> 'Test:Var' in s
        False
        >>> with s.flush() as name, open(name, 'wt', encoding='utf-8') as fobj:
        ...     _ = fobj.write('Test:Var=bar\\n')  # "_ = " is to ignore the return value of write
        >>> 'Test:Var' in s
        True

        Used as a context manager to define when the file is flushed and can be
        accessed via the file system. If the context terminates successfully, the
        file is read and its content replaces the current substvars.

        This is mostly useful if the plugin needs to interface with a third-party
        tool that requires a file as interprocess communication (IPC) for sharing
        the substvars.

        The file may be truncated or completed replaced (change inode) as long as
        the provided path points to a regular file when the context manager
        terminates successfully.

        Note that any manipulation of the substvars via the `Substvars` API while
        the file is flushed will silently be discarded if the context manager completes
        successfully.
        """
        with tempfile.NamedTemporaryFile(mode="w+t", encoding="utf-8") as tmp:
            self.write_substvars(tmp)
            tmp.flush()  # Temping to use close, but then we have to manually delete the file.
            yield tmp.name
            # Re-open; seek did not work when I last tried (if I did it work, feel free to
            # convert back to seek - as long as it works!)
            with open(tmp.name, "rt", encoding="utf-8") as fd:
                self.read_substvars(fd)

    def save(self) -> None:
        # Promote the debputy extension over `save()` for the plugins.
        if self._substvars_path is None:
            raise TypeError(
                "Please use `flush()` extension to temporarily write the substvars to the file system"
            )
        super().save()


class ServiceRegistry(Generic[DSD]):
    __slots__ = ()

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
        """Register a service detected in the package

        All the details will either be provided as-is or used as default when the plugin provided
        integration code is called.

        Two services from different service managers are considered related when:

         1) They are of the same type (`type_of_service`) and has the same scope (`service_scope`), AND
         2) Their plugin provided names has an overlap

        Related services can be covered by the same service definition in the manifest.

        :param path: The path defining this service.
        :param name: The name of the service. Multiple ones can be provided if the service has aliases.
          Note that when providing multiple names, `debputy` will use the first name in the list as the
          default name if it has to choose. Any alternative name provided can be used by the packager
          to identify this service.
        :param type_of_service: The type of service. By default, this is "service", but plugins can
          provide other types (such as "timer" for the systemd timer unit).
        :param service_scope: The scope for this service. By default, this is "system" meaning the
          service is a system-wide service. Service managers can define their own scopes such as
          "user" (which is used by systemd for "per-user" services).
        :param enable_by_default: Whether the service should be enabled by default, assuming the
          packager does not explicitly override this setting.
        :param start_by_default: Whether the service should be started by default on install, assuming
          the packager does not explicitly override this setting.
        :param default_upgrade_rule: The default value for how the service should be processed during
          upgrades. Options are:
              * `do-nothing`: The plugin should not interact with the running service (if any)
                (maintenance of the enabled start, start on install, etc. are still applicable)
              * `reload`: The plugin should attempt to reload the running service (if any).
                 Note: In combination with `auto_start_in_install == False`, be careful to not
                 start the service if not is not already running.
              * `restart`: The plugin should attempt to restart the running service (if any).
                 Note: In combination with `auto_start_in_install == False`, be careful to not
                 start the service if not is not already running.
              * `stop-then-start`: The plugin should stop the service during `prerm upgrade`
                 and start it against in the `postinst` script.

        :param service_context: Any custom data that the detector want to pass along to the
          integrator for this service.
        """
        raise NotImplementedError


@dataclasses.dataclass(slots=True, frozen=True)
class ParserAttributeDocumentation:
    attributes: FrozenSet[str]
    description: Optional[str]

    @property
    def is_hidden(self) -> bool:
        return False


@final
@dataclasses.dataclass(slots=True, frozen=True)
class StandardParserAttributeDocumentation(ParserAttributeDocumentation):
    sort_category: int = 0


def undocumented_attr(attr: str) -> ParserAttributeDocumentation:
    """Describe an attribute as undocumented

    If you for some reason do not want to document a particular attribute, you can mark it as
    undocumented. This is required if you are only documenting a subset of the attributes,
    because `debputy` assumes any omission to be a mistake.

    :param attr: Name of the attribute
    """
    return ParserAttributeDocumentation(
        frozenset({attr}),
        None,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class ParserDocumentation:
    title: Optional[str] = None
    description: Optional[str] = None
    attribute_doc: Optional[Sequence[ParserAttributeDocumentation]] = None
    alt_parser_description: Optional[str] = None
    documentation_reference_url: Optional[str] = None

    def replace(self, **changes: Any) -> "ParserDocumentation":
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(slots=True, frozen=True)
class TypeMappingExample(Generic[S]):
    source_input: S


@dataclasses.dataclass(slots=True, frozen=True)
class TypeMappingDocumentation(Generic[S]):
    description: Optional[str] = None
    examples: Sequence[TypeMappingExample[S]] = tuple()


def type_mapping_example(source_input: S) -> TypeMappingExample[S]:
    return TypeMappingExample(source_input)


def type_mapping_reference_documentation(
    *,
    description: Optional[str] = None,
    examples: Union[TypeMappingExample[S], Iterable[TypeMappingExample[S]]] = tuple(),
) -> TypeMappingDocumentation[S]:
    e = (
        tuple([examples])
        if isinstance(examples, TypeMappingExample)
        else tuple(examples)
    )
    return TypeMappingDocumentation(
        description=description,
        examples=e,
    )


def documented_attr(
    attr: Union[str, Iterable[str]],
    description: str,
) -> ParserAttributeDocumentation:
    """Describe an attribute or a group of attributes

    :param attr: A single attribute or a sequence of attributes. The attribute must be the
      attribute name as used in the source format version of the TypedDict.

      If multiple attributes are provided, they will be documented together. This is often
      useful if these attributes are strongly related (such as different names for the same
      target attribute).
    :param description: The description the user should see for this attribute / these
       attributes. This parameter can be a Python format string with variables listed in
       the description of `reference_documentation`.
    :return: An opaque representation of the documentation,
    """
    attributes = [attr] if isinstance(attr, str) else attr
    return ParserAttributeDocumentation(
        frozenset(attributes),
        description,
    )


def reference_documentation(
    title: str = "Auto-generated reference documentation for {RULE_NAME}",
    description: Optional[str] = textwrap.dedent(
        """\
            This is an automatically generated reference documentation for {RULE_NAME}. It is generated
            from input provided by {PLUGIN_NAME} via the debputy API.

            (If you are the provider of the {PLUGIN_NAME} plugin, you can replace this text with
             your own documentation by providing the `inline_reference_documentation` when registering
             the manifest rule.)
            """
    ),
    attributes: Optional[Sequence[ParserAttributeDocumentation]] = None,
    non_mapping_description: Optional[str] = None,
    reference_documentation_url: Optional[str] = None,
) -> ParserDocumentation:
    """Provide inline reference documentation for the manifest snippet

    For parameters that mention that they are a Python format, the following format variables
    are available:

     * RULE_NAME: Name of the rule. If manifest snippet has aliases, this will be the name of
       the alias provided by the user.
     * MANIFEST_FORMAT_DOC: Path OR URL to the "MANIFEST-FORMAT" reference documentation from
       `debputy`. By using the MANIFEST_FORMAT_DOC variable, you ensure that you point to the
       file that matches the version of `debputy` itself.
     * PLUGIN_NAME: Name of the plugin providing this rule.

    :param title: The text you want the user to see as for your rule. A placeholder is provided by default.
      This parameter can be a Python format string with the above listed variables.
    :param description: The text you want the user to see as a description for the rule. An auto-generated
      placeholder is provided by default saying that no human written documentation was provided.
      This parameter can be a Python format string with the above listed variables.
    :param attributes: A sequence of attribute-related documentation. Each element of the sequence should
      be the result of `documented_attr` or `undocumented_attr`. The sequence must cover all source
      attributes exactly once.
    :param non_mapping_description: The text you want the user to see as the description for your rule when
      `debputy` describes its non-mapping format. Must not be provided for rules that do not have an
      (optional) non-mapping format as source format.  This parameter can be a Python format string with
      the above listed variables.
    :param reference_documentation_url: A URL to the reference documentation.
    :return: An opaque representation of the documentation,
    """
    return ParserDocumentation(
        title,
        description,
        attributes,
        non_mapping_description,
        reference_documentation_url,
    )


class ServiceDefinition(Generic[DSD]):
    __slots__ = ()

    @property
    def name(self) -> str:
        """Name of the service registered by the plugin

        This is always a plugin provided name for this service (that is, `x.name in x.names`
        will always be `True`).  Where possible, this will be the same as the one that the
        packager provided when they provided any configuration related to this service.
        When not possible, this will be the first name provided by the plugin (`x.names[0]`).

        If all the aliases are equal, then using this attribute will provide traceability
        between the manifest and the generated maintscript snippets. When the exact name
        used is important, the plugin should ignore this attribute and pick the name that
        is needed.
        """
        raise NotImplementedError

    @property
    def names(self) -> Sequence[str]:
        """All *plugin provided* names and aliases of the service

        This is the name/sequence of names that the plugin provided when it registered
        the service earlier.
        """
        raise NotImplementedError

    @property
    def path(self) -> VirtualPath:
        """The registered path for this service

        :return: The path that was associated with this service when it was registered
          earlier.
        """
        raise NotImplementedError

    @property
    def type_of_service(self) -> str:
        """Type of the service such as "service" (daemon), "timer", etc.

        :return: The type of service scope. It is the same value as the one as the plugin provided
           when registering the service (if not explicitly provided, it defaults to "service").
        """
        raise NotImplementedError

    @property
    def service_scope(self) -> str:
        """Service scope such as "system" or "user"

        :return: The service scope. It is the same value as the one as the plugin provided
           when registering the service (if not explicitly provided, it defaults to "system")
        """
        raise NotImplementedError

    @property
    def auto_enable_on_install(self) -> bool:
        """Whether the service should be auto-enabled on install

        :return: True if the service should be enabled automatically, false if not.
        """
        raise NotImplementedError

    @property
    def auto_start_on_install(self) -> bool:
        """Whether the service should be auto-started on install

        :return: True if the service should be started automatically, false if not.
        """
        raise NotImplementedError

    @property
    def on_upgrade(self) -> ServiceUpgradeRule:
        """How to handle the service during an upgrade

        Options are:
          * `do-nothing`: The plugin should not interact with the running service (if any)
            (maintenance of the enabled start, start on install, etc. are still applicable)
          * `reload`: The plugin should attempt to reload the running service (if any).
             Note: In combination with `auto_start_in_install == False`, be careful to not
             start the service if not is not already running.
          * `restart`: The plugin should attempt to restart the running service (if any).
             Note: In combination with `auto_start_in_install == False`, be careful to not
             start the service if not is not already running.
          * `stop-then-start`: The plugin should stop the service during `prerm upgrade`
             and start it against in the `postinst` script.

        Note: In all cases, the plugin should still consider what to do in
        `prerm remove`, which is the last point in time where the plugin can rely on the
        service definitions in the file systems to stop the services when the package is
        being uninstalled.

        :return: The service restart rule
        """
        raise NotImplementedError

    @property
    def definition_source(self) -> str:
        """Describes where this definition came from

        If the definition is provided by the packager, then this will reference the part
        of the manifest that made this definition. Otherwise, this will be a reference
        to the plugin providing this definition.

        :return: The source of this definition
        """
        raise NotImplementedError

    @property
    def is_plugin_provided_definition(self) -> bool:
        """Whether the definition source points to the plugin or a package provided definition

        :return: True if definition is 100% from the plugin. False if the definition is partially
          or fully from another source (usually, the packager via the manifest).
        """
        raise NotImplementedError

    @property
    def service_context(self) -> Optional[DSD]:
        """Custom service context (if any) provided by the detector code of the plugin

        :return: If the detection code provided a custom data when registering the
          service, this attribute will reference that data.  If nothing was provided,
          then this attribute will be None.
        """
        raise NotImplementedError
