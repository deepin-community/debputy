import argparse
import dataclasses
import errno
import os
from typing import (
    Optional,
    Tuple,
    Mapping,
    FrozenSet,
    Set,
    Union,
    Sequence,
    Iterable,
    Callable,
    Dict,
    TYPE_CHECKING,
)

from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import (
    DpkgArchitectureBuildProcessValuesTable,
    dpkg_architecture_table,
)
from debputy.exceptions import DebputyRuntimeError
from debputy.filesystem_scan import FSROOverlay
from debputy.highlevel_manifest import HighLevelManifest
from debputy.highlevel_manifest_parser import YAMLManifestParser
from debputy.packages import (
    SourcePackage,
    BinaryPackage,
    DctrlParser,
)
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.impl import load_plugin_features
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.substitution import (
    Substitution,
    VariableContext,
    SubstitutionImpl,
    NULL_SUBSTITUTION,
)
from debputy.util import _error, PKGNAME_REGEX, resolve_source_date_epoch, setup_logging

if TYPE_CHECKING:
    from argparse import _SubParsersAction


CommandHandler = Callable[["CommandContext"], None]
ArgparserConfigurator = Callable[[argparse.ArgumentParser], None]


def add_arg(
    *name_or_flags: str,
    **kwargs,
) -> Callable[[argparse.ArgumentParser], None]:
    def _configurator(argparser: argparse.ArgumentParser) -> None:
        argparser.add_argument(
            *name_or_flags,
            **kwargs,
        )

    return _configurator


@dataclasses.dataclass(slots=True, frozen=True)
class CommandArg:
    parsed_args: argparse.Namespace
    plugin_search_dirs: Sequence[str]


@dataclasses.dataclass
class Command:
    handler: Callable[["CommandContext"], None]
    require_substitution: bool = True
    requested_plugins_only: bool = False


class CommandContext:
    def __init__(
        self,
        parsed_args: argparse.Namespace,
        plugin_search_dirs: Sequence[str],
        require_substitution: bool = True,
        requested_plugins_only: bool = False,
    ) -> None:
        self.parsed_args = parsed_args
        self.plugin_search_dirs = plugin_search_dirs
        self._require_substitution = require_substitution
        self._requested_plugins_only = requested_plugins_only
        self._debputy_plugin_feature_set: PluginProvidedFeatureSet = (
            PluginProvidedFeatureSet()
        )
        self._debian_dir = FSROOverlay.create_root_dir("debian", "debian")
        self._mtime: Optional[int] = None
        self._source_variables: Optional[Mapping[str, str]] = None
        self._substitution: Optional[Substitution] = None
        self._requested_plugins: Optional[Sequence[str]] = None
        self._plugins_loaded = False
        self._dctrl_parser: Optional[DctrlParser] = None
        self._dctrl_data: Optional[
            Tuple[
                "SourcePackage",
                Mapping[str, "BinaryPackage"],
            ]
        ] = None

    @property
    def debian_dir(self) -> VirtualPath:
        return self._debian_dir

    @property
    def mtime(self) -> int:
        if self._mtime is None:
            self._mtime = resolve_source_date_epoch(
                None,
                substitution=self.substitution,
            )
        return self._mtime

    @property
    def dctrl_parser(self) -> DctrlParser:
        parser = self._dctrl_parser
        if parser is None:
            packages: Union[Set[str], FrozenSet[str]] = frozenset()
            if hasattr(self.parsed_args, "packages"):
                packages = self.parsed_args.packages

            parser = DctrlParser(
                packages,  # -p/--package
                set(),  # -N/--no-package
                False,  # -i
                False,  # -a
                build_env=DebBuildOptionsAndProfiles.instance(),
                dpkg_architecture_variables=dpkg_architecture_table(),
                dpkg_arch_query_table=DpkgArchTable.load_arch_table(),
            )
            self._dctrl_parser = parser
        return parser

    def source_package(self) -> SourcePackage:
        source, _ = self._parse_dctrl()
        return source

    def binary_packages(self) -> Mapping[str, "BinaryPackage"]:
        _, binary_package_table = self._parse_dctrl()
        return binary_package_table

    def requested_plugins(self) -> Sequence[str]:
        if self._requested_plugins is None:
            self._requested_plugins = self._resolve_requested_plugins()
        return self._requested_plugins

    def required_plugins(self) -> Set[str]:
        return set(getattr(self.parsed_args, "required_plugins") or [])

    @property
    def deb_build_options_and_profiles(self) -> "DebBuildOptionsAndProfiles":
        return self.dctrl_parser.build_env

    @property
    def deb_build_options(self) -> Mapping[str, Optional[str]]:
        return self.deb_build_options_and_profiles.deb_build_options

    def _create_substitution(
        self,
        parsed_args: argparse.Namespace,
        plugin_feature_set: PluginProvidedFeatureSet,
        debian_dir: VirtualPath,
    ) -> Substitution:
        requested_subst = self._require_substitution
        if hasattr(parsed_args, "substitution"):
            requested_subst = parsed_args.substitution
        if requested_subst is False and self._require_substitution:
            _error(f"--no-substitution cannot be used with {parsed_args.command}")
        if self._require_substitution or requested_subst is not False:
            variable_context = VariableContext(debian_dir)
            return SubstitutionImpl(
                plugin_feature_set=plugin_feature_set,
                unresolvable_substitutions=frozenset(["PACKAGE"]),
                variable_context=variable_context,
            )
        return NULL_SUBSTITUTION

    def load_plugins(self) -> PluginProvidedFeatureSet:
        if not self._plugins_loaded:
            requested_plugins = None
            required_plugins = self.required_plugins()
            if self._requested_plugins_only:
                requested_plugins = self.requested_plugins()
            debug_mode = getattr(self.parsed_args, "debug_mode", False)
            load_plugin_features(
                self.plugin_search_dirs,
                self.substitution,
                requested_plugins_only=requested_plugins,
                required_plugins=required_plugins,
                plugin_feature_set=self._debputy_plugin_feature_set,
                debug_mode=debug_mode,
            )
            self._plugins_loaded = True
        return self._debputy_plugin_feature_set

    @staticmethod
    def _plugin_from_dependency_field(dep_field: str) -> Iterable[str]:
        package_prefix = "debputy-plugin-"
        for dep_clause in (d.strip() for d in dep_field.split(",")):
            dep = dep_clause.split("|")[0].strip()
            if not dep.startswith(package_prefix):
                continue
            m = PKGNAME_REGEX.search(dep)
            assert m
            package_name = m.group(0)
            plugin_name = package_name[len(package_prefix) :]
            yield plugin_name

    def _resolve_requested_plugins(self) -> Sequence[str]:
        source_package, _ = self._parse_dctrl()
        bd = source_package.fields.get("Build-Depends", "")
        plugins = list(self._plugin_from_dependency_field(bd))
        for field_name in ("Build-Depends-Arch", "Build-Depends-Indep"):
            f = source_package.fields.get(field_name)
            if not f:
                continue
            for plugin in self._plugin_from_dependency_field(f):
                raise DebputyRuntimeError(
                    f"Cannot load plugins via {field_name}:"
                    f" Please move debputy-plugin-{plugin} dependency to Build-Depends."
                )

        return plugins

    @property
    def substitution(self) -> Substitution:
        if self._substitution is None:
            self._substitution = self._create_substitution(
                self.parsed_args,
                self._debputy_plugin_feature_set,
                self.debian_dir,
            )
        return self._substitution

    def must_be_called_in_source_root(self) -> None:
        if self.debian_dir.get("control") is None:
            _error(
                "This subcommand must be run from a source package root; expecting debian/control to exist."
            )

    def _parse_dctrl(
        self,
    ) -> Tuple[
        "SourcePackage",
        Mapping[str, "BinaryPackage"],
    ]:
        if self._dctrl_data is None:
            try:
                debian_control = self.debian_dir.get("control")
                if debian_control is None:
                    raise FileNotFoundError(
                        errno.ENOENT,
                        os.strerror(errno.ENOENT),
                        os.path.join(self.debian_dir.fs_path, "control"),
                    )
                with debian_control.open() as fd:
                    source_package, binary_packages = (
                        self.dctrl_parser.parse_source_debian_control(
                            fd,
                        )
                    )
            except FileNotFoundError:
                # We are not using `must_be_called_in_source_root`, because we (in this case) require
                # the file to be readable (that is, parse_source_debian_control can also raise a
                # FileNotFoundError when trying to open the file).
                _error(
                    "This subcommand must be run from a source package root; expecting debian/control to exist."
                )

            self._dctrl_data = (
                source_package,
                binary_packages,
            )

        return self._dctrl_data

    @property
    def has_dctrl_file(self) -> bool:
        debian_control = self.debian_dir.get("control")
        return debian_control is not None

    def manifest_parser(
        self,
        *,
        manifest_path: Optional[str] = None,
    ) -> YAMLManifestParser:
        substitution = self.substitution
        dctrl_parser = self.dctrl_parser

        source_package, binary_packages = self._parse_dctrl()

        if self.parsed_args.debputy_manifest is not None:
            manifest_path = self.parsed_args.debputy_manifest
        if manifest_path is None:
            manifest_path = os.path.join(self.debian_dir.fs_path, "debputy.manifest")
        return YAMLManifestParser(
            manifest_path,
            source_package,
            binary_packages,
            substitution,
            dctrl_parser.dpkg_architecture_variables,
            dctrl_parser.dpkg_arch_query_table,
            dctrl_parser.build_env,
            self.load_plugins(),
            debian_dir=self.debian_dir,
        )

    def parse_manifest(
        self,
        *,
        manifest_path: Optional[str] = None,
    ) -> HighLevelManifest:
        substitution = self.substitution
        manifest_required = False

        if self.parsed_args.debputy_manifest is not None:
            manifest_path = self.parsed_args.debputy_manifest
            manifest_required = True
        if manifest_path is None:
            manifest_path = os.path.join(self.debian_dir.fs_path, "debputy.manifest")
        parser = self.manifest_parser(manifest_path=manifest_path)

        os.environ["SOURCE_DATE_EPOCH"] = substitution.substitute(
            "{{SOURCE_DATE_EPOCH}}",
            "Internal resolution",
        )
        if os.path.isfile(manifest_path):
            return parser.parse_manifest()
        if manifest_required:
            _error(f'The path "{manifest_path}" is not a file!')
        return parser.build_manifest()


class CommandBase:
    __slots__ = ()

    def configure(self, argparser: argparse.ArgumentParser) -> None:
        # Does nothing by default
        pass

    def __call__(self, command_arg: CommandArg) -> None:
        raise NotImplementedError


class SubcommandBase(CommandBase):
    __slots__ = ("name", "aliases", "help_description")

    def __init__(
        self,
        name: str,
        *,
        aliases: Sequence[str] = tuple(),
        help_description: Optional[str] = None,
    ) -> None:
        self.name = name
        self.aliases = aliases
        self.help_description = help_description

    def add_subcommand_to_subparser(
        self,
        subparser: "_SubParsersAction",
    ) -> argparse.ArgumentParser:
        parser = subparser.add_parser(
            self.name,
            aliases=self.aliases,
            help=self.help_description,
            allow_abbrev=False,
        )
        self.configure(parser)
        return parser


class GenericSubCommand(SubcommandBase):
    __slots__ = (
        "_handler",
        "_configure_handler",
        "_require_substitution",
        "_requested_plugins_only",
        "_log_only_to_stderr",
    )

    def __init__(
        self,
        name: str,
        handler: Callable[[CommandContext], None],
        *,
        aliases: Sequence[str] = tuple(),
        help_description: Optional[str] = None,
        configure_handler: Optional[Callable[[argparse.ArgumentParser], None]] = None,
        require_substitution: bool = True,
        requested_plugins_only: bool = False,
        log_only_to_stderr: bool = False,
    ) -> None:
        super().__init__(name, aliases=aliases, help_description=help_description)
        self._handler = handler
        self._configure_handler = configure_handler
        self._require_substitution = require_substitution
        self._requested_plugins_only = requested_plugins_only
        self._log_only_to_stderr = log_only_to_stderr

    def configure_handler(
        self,
        handler: Callable[[argparse.ArgumentParser], None],
    ) -> None:
        if self._configure_handler is not None:
            raise TypeError("Only one argument handler can be provided")
        self._configure_handler = handler

    def configure(self, argparser: argparse.ArgumentParser) -> None:
        handler = self._configure_handler
        if handler is not None:
            handler(argparser)

    def __call__(self, command_arg: CommandArg) -> None:
        context = CommandContext(
            command_arg.parsed_args,
            command_arg.plugin_search_dirs,
            self._require_substitution,
            self._requested_plugins_only,
        )
        if self._log_only_to_stderr:
            setup_logging(reconfigure_logging=True, log_only_to_stderr=True)
        return self._handler(context)


class DispatchingCommandMixin(CommandBase):
    __slots__ = ()

    def add_subcommand(self, subcommand: SubcommandBase) -> None:
        raise NotImplementedError

    def add_dispatching_subcommand(
        self,
        name: str,
        dest: str,
        *,
        aliases: Sequence[str] = tuple(),
        help_description: Optional[str] = None,
        metavar: str = "command",
        default_subcommand: Optional[str] = None,
    ) -> "DispatcherCommand":
        ds = DispatcherCommand(
            name,
            dest,
            aliases=aliases,
            help_description=help_description,
            metavar=metavar,
            default_subcommand=default_subcommand,
        )
        self.add_subcommand(ds)
        return ds

    def register_subcommand(
        self,
        name: Union[str, Sequence[str]],
        *,
        help_description: Optional[str] = None,
        argparser: Optional[
            Union[ArgparserConfigurator, Sequence[ArgparserConfigurator]]
        ] = None,
        require_substitution: bool = True,
        requested_plugins_only: bool = False,
        log_only_to_stderr: bool = False,
    ) -> Callable[[CommandHandler], GenericSubCommand]:
        if isinstance(name, str):
            cmd_name = name
            aliases = []
        else:
            cmd_name = name[0]
            aliases = name[1:]

        if argparser is not None and not callable(argparser):
            args = argparser

            def _wrapper(parser: argparse.ArgumentParser) -> None:
                for configurator in args:
                    configurator(parser)

            argparser = _wrapper

        def _annotation_impl(func: CommandHandler) -> GenericSubCommand:
            subcommand = GenericSubCommand(
                cmd_name,
                func,
                aliases=aliases,
                help_description=help_description,
                require_substitution=require_substitution,
                requested_plugins_only=requested_plugins_only,
                log_only_to_stderr=log_only_to_stderr,
            )
            self.add_subcommand(subcommand)
            if argparser is not None:
                subcommand.configure_handler(argparser)

            return subcommand

        return _annotation_impl


class DispatcherCommand(SubcommandBase, DispatchingCommandMixin):
    __slots__ = (
        "_subcommands",
        "_aliases",
        "_dest",
        "_metavar",
        "_required",
        "_default_subcommand",
        "_argparser",
    )

    def __init__(
        self,
        name: str,
        dest: str,
        *,
        aliases: Sequence[str] = tuple(),
        help_description: Optional[str] = None,
        metavar: str = "command",
        default_subcommand: Optional[str] = None,
    ) -> None:
        super().__init__(name, aliases=aliases, help_description=help_description)
        self._aliases: Dict[str, SubcommandBase] = {}
        self._subcommands: Dict[str, SubcommandBase] = {}
        self._dest = dest
        self._metavar = metavar
        self._default_subcommand = default_subcommand
        self._argparser: Optional[argparse.ArgumentParser] = None

    def add_subcommand(self, subcommand: SubcommandBase) -> None:
        all_names = [subcommand.name]
        if subcommand.aliases:
            all_names.extend(subcommand.aliases)
        aliases = self._aliases
        for n in all_names:
            if n in aliases:
                raise ValueError(
                    f"Internal error: Multiple handlers for {n} on topic {self.name}"
                )

            aliases[n] = subcommand
        self._subcommands[subcommand.name] = subcommand

    def configure(self, argparser: argparse.ArgumentParser) -> None:
        if self._argparser is not None:
            raise TypeError("Cannot configure twice!")
        self._argparser = argparser
        subcommands = self._subcommands
        if not subcommands:
            raise ValueError(
                f"Internal error: No subcommands for subcommand {self.name} (then why do we have it?)"
            )
        default_subcommand = self._default_subcommand
        required = default_subcommand is None
        if (
            default_subcommand is not None
            and default_subcommand not in ("--help", "-h")
            and default_subcommand not in subcommands
        ):
            raise ValueError(
                f"Internal error: Subcommand {self.name} should have {default_subcommand} as default,"
                " but it was not registered?"
            )
        subparser = argparser.add_subparsers(
            dest=self._dest,
            required=required,
            metavar=self._metavar,
        )
        for subcommand in subcommands.values():
            subcommand.add_subcommand_to_subparser(subparser)

    def has_command(self, command: str) -> bool:
        return command in self._aliases

    def __call__(self, command_arg: CommandArg) -> None:
        argparser = self._argparser
        assert argparser is not None
        v = getattr(command_arg.parsed_args, self._dest, None)
        if v is None:
            v = self._default_subcommand
            if v in ("--help", "-h"):
                argparser.parse_args([v])
                _error("Missing command", prog=argparser.prog)

        assert (
            v is not None
        ), f"Internal error: No default subcommand and argparse did not provide the required subcommand {self._dest}?"
        assert (
            v in self._aliases
        ), f"Internal error: {v} was accepted as a topic, but it was not registered?"
        self._aliases[v](command_arg)


ROOT_COMMAND = DispatcherCommand(
    "root",
    dest="command",
    metavar="COMMAND",
)
