import contextlib
import dataclasses
import os.path
import subprocess
from typing import (
    Optional,
    FrozenSet,
    final,
    TYPE_CHECKING,
    Union,
    Annotated,
    List,
    NotRequired,
    Literal,
    Any,
    Type,
    TypeVar,
    Self,
    Sequence,
    Callable,
    Container,
    Iterable,
    is_typeddict,
)

from debputy.exceptions import PluginAPIViolationError, PluginInitializationError
from debputy.manifest_conditions import ManifestCondition
from debputy.manifest_parser.base_types import (
    BuildEnvironmentDefinition,
    DebputyParsedContentStandardConditional,
    FileSystemExactMatchRule,
)
from debputy.manifest_parser.exceptions import (
    ManifestParseException,
    ManifestInvalidUserDataException,
)
from debputy.manifest_parser.parse_hints import DebputyParseHint
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.tagging_types import DebputyDispatchableType
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.plugin.api.spec import (
    ParserDocumentation,
    DebputyIntegrationMode,
    BuildSystemManifestRuleMetadata,
    _DEBPUTY_DISPATCH_METADATA_ATTR_NAME,
    VirtualPath,
)
from debputy.plugin.plugin_state import run_in_context_of_plugin
from debputy.substitution import Substitution
from debputy.types import EnvironmentModification
from debputy.util import run_build_system_command, _debug_log, _info, _warn

if TYPE_CHECKING:
    from debputy.build_support.build_context import BuildContext
    from debputy.highlevel_manifest import HighLevelManifest
    from debputy.plugin.api.impl_types import DIPHandler


AT = TypeVar("AT")
BSR = TypeVar("BSR", bound="BuildSystemRule")
BSPF = TypeVar("BSPF", bound="BuildRuleDefinitionBase")


@dataclasses.dataclass(slots=True, frozen=True)
class BuildSystemCharacteristics:
    out_of_source_builds: Literal[
        "required",
        "supported-and-default",
        "supported-but-not-default",
        "not-supported",
    ]


class CleanHelper:
    def schedule_removal_of_files(self, *args: str) -> None:
        """Schedule removal of these files

        This will remove the provided files in bulk. The files are not guaranteed
        to be deleted in any particular order. If anything needs urgent removal,
        `os.unlink` can be used directly.

        Note: Symlinks will **not** be followed. If a symlink and target must
        be deleted, ensure both are passed.


        :param args: Path names to remove. Each must be removable with
          `os.unlink`
        """
        raise NotImplementedError

    def schedule_removal_of_directories(self, *args: str) -> None:
        """Schedule removal of these directories

        This will remove the provided dirs in bulk. The dirs are not guaranteed
        to be deleted in any particular order. If anything needs urgent removal,
        then it can be done directly instead of passing it to this method.

        If anything needs urgent removal, then it can be removed immediately.

        :param args: Path names to remove.
        """
        raise NotImplementedError


class BuildRuleParsedFormat(DebputyParsedContentStandardConditional):
    name: NotRequired[str]
    for_packages: NotRequired[
        Annotated[
            Union[BinaryPackage, List[BinaryPackage]],
            DebputyParseHint.manifest_attribute("for"),
        ]
    ]
    environment: NotRequired[BuildEnvironmentDefinition]


class OptionalBuildDirectory(BuildRuleParsedFormat):
    build_directory: NotRequired[FileSystemExactMatchRule]


class OptionalInSourceBuild(BuildRuleParsedFormat):
    perform_in_source_build: NotRequired[bool]


class OptionalInstallDirectly(BuildRuleParsedFormat):
    install_directly_to_package: NotRequired[bool]


BuildSystemDefinition = Union[
    BuildRuleParsedFormat,
    OptionalBuildDirectory,
    OptionalInSourceBuild,
    OptionalInstallDirectly,
]


class BuildRule(DebputyDispatchableType):
    __slots__ = (
        "_auto_generated_stem",
        "_name",
        "_for_packages",
        "_manifest_condition",
        "_attribute_path",
        "_environment",
        "_substitution",
    )

    def __init__(
        self,
        attributes: BuildRuleParsedFormat,
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__()

        self._name = attributes.get("name")
        for_packages = attributes.get("for_packages")

        if for_packages is None:
            if isinstance(parser_context, ParserContextData):
                all_binaries = parser_context.binary_packages.values()
            else:
                all_binaries = parser_context.all_packages
            self._for_packages = frozenset(all_binaries)
        else:
            self._for_packages = frozenset(
                for_packages if isinstance(for_packages, list) else [for_packages]
            )
        self._manifest_condition = attributes.get("when")
        self._attribute_path = attribute_path
        self._substitution = parser_context.substitution
        self._auto_generated_stem: Optional[str] = None
        environment = attributes.get("environment")
        if environment is None:
            assert isinstance(parser_context, ParserContextData)
            self._environment = parser_context.resolve_build_environment(
                None,
                attribute_path,
            )
        else:
            self._environment = environment

    @final
    @property
    def name(self) -> Optional[str]:
        return self._name

    @final
    @property
    def attribute_path(self) -> AttributePath:
        return self._attribute_path

    @final
    @property
    def manifest_condition(self) -> Optional[ManifestCondition]:
        return self._manifest_condition

    @final
    @property
    def for_packages(self) -> FrozenSet[BinaryPackage]:
        return self._for_packages

    @final
    @property
    def substitution(self) -> Substitution:
        return self._substitution

    @final
    @property
    def environment(self) -> BuildEnvironmentDefinition:
        return self._environment

    @final
    @property
    def auto_generated_stem(self) -> str:
        stem = self._auto_generated_stem
        if stem is None:
            raise AssertionError(
                "The auto-generated-stem is not available at this time"
            )
        return stem

    @final
    @auto_generated_stem.setter
    def auto_generated_stem(self, value: str) -> None:
        if self._auto_generated_stem is not None:
            raise AssertionError("The auto-generated-stem should only be set once")
        assert value is not None
        self._auto_generated_stem = value

    @final
    def run_build(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        run_in_context_of_plugin(
            self._debputy_plugin,
            self.perform_build,
            context,
            manifest,
            **kwargs,
        )

    def perform_build(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        raise NotImplementedError

    @property
    def is_buildsystem(self) -> bool:
        return False

    @property
    def name_or_tag(self) -> str:
        name = self.name
        if name is None:
            return self.auto_generated_stem
        return name


def _is_type_or_none(v: Optional[Any], expected_type: Type[AT]) -> Optional[AT]:
    if isinstance(v, expected_type):
        return v
    return None


class BuildSystemRule(BuildRule):

    __slots__ = (
        "_build_directory",
        "source_directory",
        "install_directly_to_package",
        "perform_in_source_build",
    )

    def __init__(
        self,
        attributes: BuildSystemDefinition,
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(attributes, attribute_path, parser_context)
        build_directory = _is_type_or_none(
            attributes.get("build_directory"), FileSystemExactMatchRule
        )
        if build_directory is not None:
            self._build_directory = build_directory.match_rule.path
        else:
            self._build_directory = None
        self.source_directory = "."
        self.install_directly_to_package = False
        self.perform_in_source_build = _is_type_or_none(
            attributes.get("perform_in_source_build"), bool
        )
        install_directly_to_package = _is_type_or_none(
            attributes.get("install_directly_to_package"), bool
        )
        if install_directly_to_package is None:
            self.install_directly_to_package = len(self.for_packages) == 1
        elif install_directly_to_package and len(self.for_packages) > 1:
            idtp_path = attribute_path["install_directly_to_package"].path
            raise ManifestParseException(
                f'The attribute "install-directly-to-package" ({idtp_path}) cannot'
                " be true when the build system applies to multiple packages."
            )
        else:
            self.install_directly_to_package = install_directly_to_package

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        """Check if the build system apply automatically.

        This class method is called when the manifest does not declare any build rules at
        all.

        :param source_root: The source root (the directory containing `debian/`). Usually,
          the detection code would look at this for files related to the upstream build system.
        :param args: For future compat, new arguments might appear as positional arguments.
        :param kwargs: For future compat, new arguments might appear as keyword argument.
        :return: True if the build system can be used, False when it would not be useful
          to use the build system (at least with all defaults).
          Note: Be sure to use proper `bool` return values. The calling code does an
          `isinstance` check to ensure that the version of `debputy` supports the
          auto-detector (in case the return type is ever expanded in the future).
        """
        return False

    @property
    def out_of_source_build(self) -> bool:
        build_directory = self.build_directory
        return build_directory != self.source_directory

    @property
    def build_directory(self) -> str:
        directory = self._build_directory
        if directory is None:
            return self.source_directory
        return directory

    @contextlib.contextmanager
    def dump_logs_on_error(self, *logs: str) -> None:
        """Context manager that will dump logs to stdout on error

        :param logs: The logs to be dumped. Relative path names are assumed to be relative to
          the build directory.
        """
        try:
            yield
        except (Exception, KeyboardInterrupt, SystemExit):
            _warn(
                "Error occurred, attempting to provide relevant logs as requested by the build system provider"
            )
            found_any = False
            for log in logs:
                if not os.path.isabs(log):
                    log = self.build_dir_path(log)
                if not os.path.isfile(log):
                    _info(
                        f'Would have pushed "{log}" to stdout, but it does not exist.'
                    )
                    continue
                subprocess.run(["tail", "-v", "-n", "+0", log])
                found_any = True
            if not found_any:
                _warn(
                    f"None of the logs provided were available (relative to build directory): {', '.join(logs)}"
                )
            raise

    @final
    def run_clean(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: CleanHelper,
        **kwargs,
    ) -> None:
        run_in_context_of_plugin(
            self._debputy_plugin,
            self.perform_clean,
            context,
            manifest,
            clean_helper,
            **kwargs,
        )

    def perform_clean(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: CleanHelper,
        **kwargs,
    ) -> None:
        raise NotImplementedError

    def ensure_build_dir_exists(self) -> None:
        build_dir = self.build_directory
        source_dir = self.source_directory
        if build_dir == source_dir:
            return
        os.makedirs(build_dir, mode=0o755, exist_ok=True)

    def build_dir_path(self, /, path: str = "") -> str:
        build_dir = self.build_directory
        if path == "":
            return build_dir
        return os.path.join(build_dir, path)

    def relative_from_builddir_to_source(
        self,
        path_in_source_dir: Optional[str] = None,
    ) -> str:
        build_dir = self.build_directory
        source_dir = self.source_directory
        if build_dir == source_dir:
            return path_in_source_dir
        return os.path.relpath(os.path.join(source_dir, path_in_source_dir), build_dir)

    @final
    @property
    def is_buildsystem(self) -> bool:
        return True


class StepBasedBuildSystemRule(BuildSystemRule):

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        raise NotImplementedError

    @final
    def perform_clean(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: CleanHelper,
        **kwargs,
    ) -> None:
        self._check_characteristics()
        self.before_first_impl_step(stage="clean")
        self.clean_impl(context, manifest, clean_helper, **kwargs)
        if self.out_of_source_build:
            build_directory = self.build_directory
            assert build_directory is not None
            if os.path.lexists(build_directory):
                clean_helper.schedule_removal_of_directories(build_directory)
        dest_dir = self.resolve_dest_dir()
        if not isinstance(dest_dir, BinaryPackage):
            clean_helper.schedule_removal_of_directories(dest_dir)

    @final
    def perform_build(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._check_characteristics()
        self.before_first_impl_step(stage="build")
        self.configure_impl(context, manifest, **kwargs)
        self.build_impl(context, manifest, **kwargs)
        if context.should_run_tests:
            self.test_impl(context, manifest, **kwargs)
        dest_dir = self.resolve_dest_dir()
        if isinstance(dest_dir, BinaryPackage):
            dest_dir = f"debian/{dest_dir.name}"
        # Make it absolute for everyone (that worked for debhelper).
        # At least autoconf's "make install" requires an absolute path, so making is
        # relative would have at least one known issue.
        abs_dest_dir = os.path.abspath(dest_dir)
        self.install_impl(context, manifest, abs_dest_dir, **kwargs)

    def before_first_impl_step(
        self,
        /,
        stage: Literal["build", "clean"],
        **kwargs,
    ) -> None:
        """Called before any `*_impl` method is called.

        This can be used to validate input against data that is not available statically
        (that is, it will be checked during build but not in static checks). An example
        is that the `debhelper` build system uses this to validate the provided `dh-build-system`
        to ensure that `debhelper` knows about the build system. This check cannot be done
        statically since the build system is only required to be available in a chroot build
        and not on the host system.

        The method can also be used to compute common state for all the `*_impl` methods that
        is awkward to do in `__init__`. Note there is no data sharing between the different
        stages. This has to do with how `debputy` will be called (usually `clean` followed by
        a source package assembly in `dpkg` and then `build`).

        The check is done both on build and on clean before the relevant implementation methods
        are invoked.

        Any exception will abort the build. Prefer to raise ManifestInvalidUserDataException
        exceptions for issues related to incorrect data.

        The method is not invoked if the steps are skipped, which can happen with build profiles
        or arch:any vs. arch:all builds.

        :param stage: A discriminator variable to determine which kind of steps will be invoked
        after this method returns. For state initialization, this can be useful if the state
        is somewhat expensive and not needed for `clean`.
        """
        pass

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        """Called to handle the "configure" and "build" part of the build

        This is basically a mix of `dh_auto_configure` and `dh_auto_build` from `debhelper`.
        If the upstream build also runs test as a part of the build, this method should
        check `context.should_run_tests` and pass the relevant flags to disable tests when
        `context.should_run_tests` is false.
        """
        raise NotImplementedError

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        """Called to handle the "configure" and "build" part of the build

        This is basically a mix of `dh_auto_configure` and `dh_auto_build` from `debhelper`.
        If the upstream build also runs test as a part of the build, this method should
        check `context.should_run_tests` and pass the relevant flags to disable tests when
        `context.should_run_tests` is false.
        """
        raise NotImplementedError

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        """Called to handle the "test" part of the build

        This is basically `dh_auto_test` from `debhelper`.

        Note: This will be skipped when `context.should_run_tests` is False. Therefore, the
        method can assume that when invoked then tests must be run.

        It is always run after `configure_and_build_impl`.
        """
        raise NotImplementedError

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        """Called to handle the "install" part of the build

        This is basically `dh_auto_install` from `debhelper`.

        The `dest_dir` attribute is what the upstream should install its data into. It
        follows the `DESTDIR` convention from autoconf/make. The `dest_dir` should not
        be second-guessed since `debputy` will provide automatically as a search path
        for installation rules when relevant.

        It is always run after `configure_and_build_impl` and, if relevant, `test_impl`.
        """
        raise NotImplementedError

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        """Called to handle the "clean" part of the build

        This is basically `dh_auto_clean` from `debhelper`.

        For out-of-source builds, `debputy` will remove the build directory for you
        if it exists (when this method returns). This method is only "in-source" cleaning
        or for "dirty" state left outside the designated build directory.

        Note that state *cannot* be shared between `clean` and other steps due to limitations
        of how the Debian build system works in general.
        """
        raise NotImplementedError

    def _check_characteristics(self) -> None:
        characteristics = self.characteristics()

        _debug_log(f"Characteristics for {self.name_or_tag} {self.__class__.__name__} ")

        if self.out_of_source_build and self.perform_in_source_build:
            raise ManifestInvalidUserDataException(
                f"Cannot use 'build-directory' with 'perform-in-source-build' at {self.attribute_path.path}"
            )
        if (
            characteristics.out_of_source_builds == "required"
            and self.perform_in_source_build
        ):
            path = self.attribute_path["perform_in_source_build"].path_key_lc

            # FIXME: How do I determine the faulty plugin from here.
            raise PluginAPIViolationError(
                f"The build system {self.__class__.__qualname__} had an perform-in-source-build attribute, but claims"
                f" it requires out of source builds. Please file a bug against the provider asking them not to use"
                f' "{OptionalInSourceBuild.__name__}" as base for their build system definition or tweak'
                f" the characteristics of the build system as the current combination is inconsistent."
                f" The offending definition is at {path}."
            )

        if (
            characteristics.out_of_source_builds
            in ("required", "supported-and-default")
            and not self.out_of_source_build
        ):

            if not self.perform_in_source_build:
                self._build_directory = self._pick_build_dir()
            else:
                assert characteristics.out_of_source_builds != "required"
        elif (
            characteristics.out_of_source_builds == "not-supported"
            and self.out_of_source_build
        ):
            path = self.attribute_path["build_directory"].path_key_lc

            # FIXME: How do I determine the faulty plugin from here.
            raise PluginAPIViolationError(
                f"The build system {self.__class__.__qualname__} had a build-directory attribute, but claims it does"
                f" not support out of source builds. Please file a bug against the provider asking them not to use"
                f' "{OptionalBuildDirectory.__name__}" as base for their build system definition or tweak'
                f" the characteristics of the build system as the current combination is inconsistent."
                f" The offending definition is at {path}."
            )

    def _pick_build_dir(self) -> str:
        tag = self.name if self.name is not None else self.auto_generated_stem
        if tag == "":
            return "_build"
        return f"_build-{tag}"

    @final
    def resolve_dest_dir(self) -> Union[str, BinaryPackage]:
        auto_generated_stem = self.auto_generated_stem
        if self.install_directly_to_package:
            assert len(self.for_packages) == 1
            return next(iter(self.for_packages))
        if auto_generated_stem == "":
            return "debian/tmp"
        return f"debian/tmp-{auto_generated_stem}"


# Using the same logic as debhelper for the same reasons.
def _make_target_exists(make_cmd: str, target: str, *, directory: str = ".") -> bool:
    cmd = [
        make_cmd,
        "-s",
        "-n",
        "--no-print-directory",
    ]
    if directory and directory != ".":
        cmd.append("-C")
        cmd.append(directory)
    cmd.append(target)
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            restore_signals=True,
        )
    except FileNotFoundError:
        return False

    options = (
        f"*** No rule to make target '{target}",
        f"*** No rule to make target `{target}",
    )

    stdout = res.stdout.decode("utf-8")
    return not any(o in stdout for o in options)


def _find_first_existing_make_target(
    make_cmd: str,
    targets: Sequence[str],
    *,
    directory: str = ".",
) -> Optional[str]:
    for target in targets:
        if _make_target_exists(make_cmd, target, directory=directory):
            return target
    return None


_UNSET = object()


class NinjaBuildSupport:
    __slots__ = ("_provided_ninja_program", "_build_system_rule")

    def __init__(
        self,
        provided_ninja_program: str,
        build_system_rule: BuildSystemRule,
    ) -> None:
        self._provided_ninja_program = provided_ninja_program
        self._build_system_rule = build_system_rule

    @classmethod
    def from_build_system(
        cls,
        build_system: BuildSystemRule,
        *,
        ninja_program: Optional[str] = None,
    ) -> Self:
        if ninja_program is None:
            ninja_program = "ninja"
        return cls(ninja_program, build_system)

    @property
    def _directory(self) -> str:
        return self._build_system_rule.build_directory

    def _pick_directory(
        self, arg: Union[Optional[str], _UNSET] = _UNSET
    ) -> Optional[str]:
        if arg is _UNSET:
            return self._directory
        return arg

    def run_ninja_build(
        self,
        build_context: "BuildContext",
        *ninja_args: str,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
        enable_parallelization: bool = True,
    ) -> None:
        extra_ninja_args = []
        if not build_context.is_terse_build:
            extra_ninja_args.append("-v")
        self._run_ninja(
            build_context,
            *extra_ninja_args,
            *ninja_args,
            env_mod=env_mod,
            directory=directory,
            enable_parallelization=enable_parallelization,
        )

    def run_ninja_test(
        self,
        build_context: "BuildContext",
        *ninja_args: str,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
        enable_parallelization: bool = True,
    ) -> None:
        self._run_ninja(
            build_context,
            "test",
            *ninja_args,
            env_mod=env_mod,
            directory=directory,
            enable_parallelization=enable_parallelization,
        )

    def run_ninja_install(
        self,
        build_context: "BuildContext",
        dest_dir: str,
        *ninja_args: str,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
        # debhelper never had parallel installs, so we do not have it either for now.
        enable_parallelization: bool = False,
    ) -> None:
        install_env_mod = EnvironmentModification(replacements=(("DESTDIR", dest_dir),))
        if env_mod is not None:
            install_env_mod = install_env_mod.combine(env_mod)
        self._run_ninja(
            build_context,
            "install",
            *ninja_args,
            directory=directory,
            env_mod=install_env_mod,
            enable_parallelization=enable_parallelization,
        )

    def run_ninja_clean(
        self,
        build_context: "BuildContext",
        *ninja_args: str,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
        enable_parallelization: bool = True,
    ) -> None:
        self._run_ninja(
            build_context,
            "clean",
            *ninja_args,
            env_mod=env_mod,
            directory=directory,
            enable_parallelization=enable_parallelization,
        )

    def _run_ninja(
        self,
        build_context: "BuildContext",
        *ninja_args: str,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
        enable_parallelization: bool = True,
    ) -> None:
        extra_ninja_args = []
        limit = (
            build_context.parallelization_limit(support_zero_as_unlimited=True)
            if enable_parallelization
            else 1
        )
        extra_ninja_args.append(f"-j{limit}")
        ninja_env_mod = EnvironmentModification(replacements=(("LC_ALL", "C.UTF-8"),))
        if env_mod is not None:
            ninja_env_mod = ninja_env_mod.combine(env_mod)
        run_build_system_command(
            self._provided_ninja_program,
            *extra_ninja_args,
            *ninja_args,
            cwd=self._pick_directory(directory),
            env_mod=ninja_env_mod,
        )


class MakefileSupport:

    __slots__ = ("_provided_make_program", "_build_system_rule")

    def __init__(
        self,
        make_program: str,
        build_system_rule: BuildSystemRule,
    ) -> None:
        self._provided_make_program = make_program
        self._build_system_rule = build_system_rule

    @classmethod
    def from_build_system(
        cls,
        build_system: BuildSystemRule,
        *,
        make_program: Optional[str] = None,
    ) -> Self:
        if make_program is None:
            make_program = os.environ.get("MAKE", "make")
        return cls(make_program, build_system)

    @property
    def _directory(self) -> str:
        return self._build_system_rule.build_directory

    @property
    def _make_program(self) -> str:
        make_program = self._provided_make_program
        if self._provided_make_program is None:
            return os.environ.get("MAKE", "make")
        return make_program

    def _pick_directory(
        self, arg: Union[Optional[str], _UNSET] = _UNSET
    ) -> Optional[str]:
        if arg is _UNSET:
            return self._directory
        return arg

    def find_first_existing_make_target(
        self,
        targets: Sequence[str],
        *,
        directory: Union[Optional[str], _UNSET] = _UNSET,
    ) -> Optional[str]:
        for target in targets:
            if self.make_target_exists(target, directory=directory):
                return target
        return None

    def make_target_exists(
        self,
        target: str,
        *,
        directory: Union[Optional[str], _UNSET] = _UNSET,
    ) -> bool:
        return _make_target_exists(
            self._make_program,
            target,
            directory=self._pick_directory(directory),
        )

    def run_first_existing_target_if_any(
        self,
        build_context: "BuildContext",
        targets: Sequence[str],
        *make_args: str,
        enable_parallelization: bool = True,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
    ) -> bool:
        target = self.find_first_existing_make_target(targets, directory=directory)
        if target is None:
            return False

        self.run_make(
            build_context,
            target,
            *make_args,
            enable_parallelization=enable_parallelization,
            directory=directory,
            env_mod=env_mod,
        )
        return True

    def run_make(
        self,
        build_context: "BuildContext",
        *make_args: str,
        enable_parallelization: bool = True,
        directory: Union[Optional[str], _UNSET] = _UNSET,
        env_mod: Optional[EnvironmentModification] = None,
    ) -> None:
        limit = (
            build_context.parallelization_limit(support_zero_as_unlimited=True)
            if enable_parallelization
            else 1
        )
        extra_make_args = [f"-j{limit}"] if limit else ["-j"]
        run_build_system_command(
            self._make_program,
            *extra_make_args,
            *make_args,
            cwd=self._pick_directory(directory),
            env_mod=env_mod,
        )


def debputy_build_system(
    # For future self: Before you get ideas about making manifest_keyword accept a list,
    # remember it has consequences for shadowing_build_systems_when_active.
    manifest_keyword: str,
    provider: Type[BSR],
    *,
    expected_debputy_integration_mode: Optional[
        Container[DebputyIntegrationMode]
    ] = None,
    auto_detection_shadows_build_systems: Optional[
        Union[str, Iterable[str]]
    ] = frozenset(),
    online_reference_documentation: Optional[ParserDocumentation] = None,
    apply_standard_attribute_documentation: bool = False,
    source_format: Optional[Any] = None,
) -> Callable[[Type[BSPF]], Type[BSPF]]:
    if not isinstance(provider, type) or not issubclass(provider, BuildSystemRule):
        raise PluginInitializationError(
            f"The provider for @{debputy_build_system.__name__} must be subclass of {BuildSystemRule.__name__}goes on the TypedDict that defines the parsed"
            f" variant of the manifest definition. Not the build system implementation class."
        )

    def _constructor_wrapper(
        _rule_used: str,
        *args,
        **kwargs,
    ) -> BSR:
        return provider(*args, **kwargs)

    if isinstance(auto_detection_shadows_build_systems, str):
        shadows = frozenset([auto_detection_shadows_build_systems])
    else:
        shadows = frozenset(auto_detection_shadows_build_systems)

    metadata = BuildSystemManifestRuleMetadata(
        (manifest_keyword,),
        BuildRule,
        _constructor_wrapper,
        expected_debputy_integration_mode=expected_debputy_integration_mode,
        source_format=source_format,
        online_reference_documentation=online_reference_documentation,
        apply_standard_attribute_documentation=apply_standard_attribute_documentation,
        auto_detection_shadow_build_systems=shadows,
        build_system_impl=provider,
    )

    def _decorator_impl(pf_cls: Type[BSPF]) -> Type[BSPF]:
        if isinstance(pf_cls, type) and issubclass(pf_cls, BuildSystemRule):
            raise PluginInitializationError(
                f"The @{debputy_build_system.__name__} annotation goes on the TypedDict that defines the parsed"
                f" variant of the manifest definition. Not the build system implementation class."
            )

        # TODO: In python3.12 we can check more than just `is_typeddict`. In python3.11, woe is us and
        #  is_typeddict is the only thing that reliably works (cpython#103699)
        if not is_typeddict(pf_cls):
            raise PluginInitializationError(
                f"Expected annotated class to be a subclass of {BuildRuleParsedFormat.__name__},"
                f" but got {pf_cls.__name__} instead"
            )

        setattr(pf_cls, _DEBPUTY_DISPATCH_METADATA_ATTR_NAME, metadata)
        return pf_cls

    return _decorator_impl
