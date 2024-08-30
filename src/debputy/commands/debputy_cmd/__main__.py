#!/usr/bin/python3 -B
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import traceback
from tempfile import TemporaryDirectory
from typing import (
    List,
    Dict,
    Any,
    Tuple,
    Optional,
    NoReturn,
    NamedTuple,
    Literal,
    cast,
)

from debputy import DEBPUTY_ROOT_DIR, DEBPUTY_PLUGIN_ROOT_DIR
from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.analysis import REFERENCE_DATA_TABLE
from debputy.analysis.debian_dir import scan_debian_dir
from debputy.build_support import perform_clean, perform_builds
from debputy.commands.debputy_cmd.context import (
    CommandContext,
    add_arg,
    ROOT_COMMAND,
    CommandArg,
)
from debputy.commands.debputy_cmd.output import _stream_to_pager, _output_styling
from debputy.dh_migration.migrators import MIGRATORS
from debputy.exceptions import (
    DebputyRuntimeError,
    PluginNotFoundError,
    PluginAPIViolationError,
    PluginInitializationError,
    UnhandledOrUnexpectedErrorFromPluginError,
    SymlinkLoopError,
)
from debputy.highlevel_manifest import HighLevelManifest
from debputy.package_build.assemble_deb import (
    assemble_debs,
)
from debputy.plugin.api.spec import (
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
    DebputyIntegrationMode,
    INTEGRATION_MODE_FULL,
)

try:
    from argcomplete import autocomplete
except ImportError:

    def autocomplete(_parser: argparse.ArgumentParser) -> None:
        pass


from debputy.version import __version__
from debputy.filesystem_scan import (
    FSROOverlay,
    FSRootDir,
)
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
)
from debputy.plugin.api.impl import (
    find_json_plugin,
    find_tests_for_plugin,
    find_related_implementation_files_for_plugin,
    parse_json_plugin_desc,
)
from debputy.dh_migration.migration import migrate_from_dh, _check_migration_target
from debputy.dh_migration.models import AcceptableMigrationIssues
from debputy.dh.debhelper_emulation import (
    dhe_pkgdir,
)

from debputy.deb_packaging_support import (
    usr_local_transformation,
    handle_perl_code,
    detect_systemd_user_service_files,
    fixup_debian_changelog_and_news_file,
    install_upstream_changelog,
    relocate_dwarves_into_dbgsym_packages,
    run_package_processors,
    cross_package_control_files,
)
from debputy.util import (
    _error,
    _warn,
    ColorizedArgumentParser,
    setup_logging,
    _info,
    escape_shell,
    program_name,
    integrated_with_debhelper,
    PRINT_BUILD_SYSTEM_COMMAND,
    PRINT_COMMAND,
    change_log_level,
)


class SharedArgument(NamedTuple):
    """
    Information about an argument shared between a parser and its subparsers
    """

    action: argparse.Action
    args: Tuple[Any, ...]
    kwargs: Dict[str, Any]


class Namespace(argparse.Namespace):
    """
    Hacks around a namespace to allow merging of values set multiple times

    Based on: https://www.enricozini.org/blog/2022/python/sharing-argparse-arguments-with-subcommands/
    """

    def __setattr__(self, name: str, value: Any) -> None:
        arg = self._shared_args.get(name)
        if arg is not None:
            action_type = arg.kwargs.get("action")
            if action_type == "store_true":
                # OR values
                old = getattr(self, name, False)
                super().__setattr__(name, old or value)
            elif action_type == "store_false":
                # AND values
                old = getattr(self, name, True)
                super().__setattr__(name, old and value)
            elif action_type == "append":
                old = getattr(self, name, None)
                if old is None:
                    old = []
                    super().__setattr__(name, old)
                if isinstance(value, list):
                    old.extend(value)
                elif value is not None:
                    old.append(value)
            elif action_type == "store":
                old = getattr(self, name, None)
                if old is None:
                    super().__setattr__(name, value)
                elif old != value and value is not None:
                    raise argparse.ArgumentError(
                        None,
                        f"conflicting values provided for {arg.action.dest!r} ({old!r} and {value!r})",
                    )
            else:
                raise NotImplementedError(
                    f"Action {action_type!r} for {arg.action.dest!r} is not supported"
                )
        else:
            return super().__setattr__(name, value)


class DebputyArgumentParser(ColorizedArgumentParser):
    """
    Hacks around a standard ArgumentParser to allow to have a limited set of
    options both outside and inside subcommands

    Based on: https://www.enricozini.org/blog/2022/python/sharing-argparse-arguments-with-subcommands/
    """

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)

        if not hasattr(self, "shared_args"):
            self.shared_args: dict[str, SharedArgument] = {}

        # Add arguments from the shared ones
        for a in self.shared_args.values():
            super().add_argument(*a.args, **a.kwargs)

    def add_argument(self, *args: Any, **kw: Any) -> Any:
        shared = kw.pop("shared", False)
        res = super().add_argument(*args, **kw)
        if shared:
            action = kw.get("action")
            if action not in ("store", "store_true", "store_false", "append"):
                raise NotImplementedError(
                    f"Action {action!r} for {args!r} is not supported"
                )
            # Take note of the argument if it was marked as shared
            self.shared_args[res.dest] = SharedArgument(res, args, kw)
        return res

    def add_subparsers(self, *args: Any, **kw: Any) -> Any:
        if "parser_class" not in kw:
            kw["parser_class"] = type(
                "ArgumentParser",
                (self.__class__,),
                {"shared_args": dict(self.shared_args)},
            )
        return super().add_subparsers(*args, **kw)

    def parse_args(self, *args: Any, **kw: Any) -> Any:
        if "namespace" not in kw:
            # Use a subclass to pass the special action list without making it
            # appear as an argument
            kw["namespace"] = type(
                "Namespace", (Namespace,), {"_shared_args": self.shared_args}
            )()
        return super().parse_args(*args, **kw)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--debputy-manifest",
        dest="debputy_manifest",
        action="store",
        default=None,
        help="Specify another `debputy` manifest (default: debian/debputy.manifest)",
        shared=True,
    )

    parser.add_argument(
        "-d",
        "--debug",
        dest="debug_mode",
        action="store_true",
        default=False,
        help="Enable debug logging and raw stack traces on errors. Some warnings become errors as a consequence.",
        shared=True,
    )

    parser.add_argument(
        "--no-pager",
        dest="pager",
        action="store_false",
        default=True,
        help="For subcommands that can use a pager, disable the use of pager. Some output formats implies --no-pager",
        shared=True,
    )

    parser.add_argument(
        "--plugin",
        dest="required_plugins",
        action="append",
        type=str,
        default=[],
        help="Request the plugin to be loaded. Can be used multiple time."
        " Ignored for some commands (such as autopkgtest-test-runner)",
        shared=True,
    )


def _add_packages_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p",
        "--package",
        dest="packages",
        action="append",
        type=str,
        default=[],
        help="The package(s) to act on.  Affects default permission normalization rules",
    )


def _build_subcommand_log_level(context: CommandContext) -> int:
    parsed_args = context.parsed_args
    log_level: Optional[int] = None
    if os.environ.get("DH_VERBOSE", "") != "":
        log_level = PRINT_COMMAND
    if parsed_args.debug_mode:
        log_level = logging.INFO
    if log_level is not None:
        change_log_level(log_level)
    return PRINT_BUILD_SYSTEM_COMMAND


internal_commands = ROOT_COMMAND.add_dispatching_subcommand(
    "internal-command",
    dest="internal_command",
    metavar="command",
    help_description="Commands used for internal purposes. These are implementation details and subject to change",
)
tool_support_commands = ROOT_COMMAND.add_dispatching_subcommand(
    "tool-support",
    help_description="Tool integration commands. These are intended to have stable output and behaviour",
    dest="tool_subcommand",
    metavar="command",
)


def parse_args() -> argparse.Namespace:
    description = textwrap.dedent(
        """\
    The `debputy` program is a manifest-based Debian packaging tool.

    It is used as a part of compiling a source package and transforming it into one or
    more binary (.deb) packages.

    If you are using a screen reader, consider exporting setting the environment variable
    OPTIMIZE_FOR_SCREEN_READER=1. This will remove some of the visual formatting and some
    commands will render the output in a purely textual manner rather than visual layout.
    """
    )

    parser: argparse.ArgumentParser = DebputyArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        prog=program_name(),
    )

    parser.add_argument("--version", action="version", version=__version__)

    _add_common_args(parser)
    from debputy.commands.debputy_cmd.plugin_cmds import (
        ensure_plugin_commands_are_loaded,
    )
    from debputy.commands.debputy_cmd.lint_and_lsp_cmds import (
        ensure_lint_and_lsp_commands_are_loaded,
    )

    ensure_plugin_commands_are_loaded()
    ensure_lint_and_lsp_commands_are_loaded()

    ROOT_COMMAND.configure(parser)

    autocomplete(parser)

    argv = sys.argv
    try:
        i = argv.index("--")
        upstream_args = argv[i + 1 :]
        argv = argv[:i]
    except (IndexError, ValueError):
        upstream_args = []
    parsed_args: argparse.Namespace = parser.parse_args(argv[1:])

    setattr(parsed_args, "upstream_args", upstream_args)
    if hasattr(parsed_args, "packages"):
        setattr(parsed_args, "packages", frozenset(parsed_args.packages))

    return parsed_args


@ROOT_COMMAND.register_subcommand(
    "check-manifest",
    help_description="Check the manifest for obvious errors, but do not run anything",
    requested_plugins_only=True,
)
def _check_manifest(context: CommandContext) -> None:
    context.parse_manifest()


def _install_plugin_from_plugin_metadata(
    plugin_metadata: DebputyPluginMetadata,
    dest_dir: str,
) -> None:
    related_files = find_related_implementation_files_for_plugin(plugin_metadata)
    install_dir = os.path.join(
        f"{dest_dir}/{DEBPUTY_PLUGIN_ROOT_DIR}".replace("//", "/"),
        "debputy",
        "plugins",
    )

    os.umask(0o022)
    os.makedirs(install_dir, exist_ok=True)
    cmd = ["cp", "--reflink=auto", "-t", install_dir]
    cmd.extend(related_files)
    cmd.append(plugin_metadata.plugin_path)
    _info(f"   {escape_shell(*cmd)}")
    subprocess.check_call(
        cmd,
        stdin=subprocess.DEVNULL,
    )


@internal_commands.register_subcommand(
    "install-plugin",
    help_description="[Internal command] Install a plugin and related files",
    requested_plugins_only=True,
    argparser=[
        add_arg("target_plugin", metavar="PLUGIN", action="store"),
        add_arg(
            "--dest-dir",
            dest="dest_dir",
            default="",
            action="store",
        ),
    ],
)
def _install_plugin(context: CommandContext) -> None:
    target_plugin = context.parsed_args.target_plugin
    if not os.path.isfile(target_plugin):
        _error(
            f'The value "{target_plugin}" must be a file. It should be the JSON descriptor of'
            f" the plugin."
        )
    plugin_metadata = parse_json_plugin_desc(target_plugin)
    _install_plugin_from_plugin_metadata(
        plugin_metadata,
        context.parsed_args.dest_dir,
    )


_DH_PLUGIN_PKG_DIR = "debputy-plugins"


def _find_plugins_and_tests_in_source_package(
    context: CommandContext,
) -> Tuple[bool, List[Tuple[DebputyPluginMetadata, str]], List[str]]:
    debian_dir = context.debian_dir
    binary_packages = context.binary_packages()
    installs = []
    all_tests = []
    had_plugin_dir = False
    for binary_package in binary_packages.values():
        if not binary_package.should_be_acted_on:
            continue
        debputy_plugins_dir = dhe_pkgdir(debian_dir, binary_package, _DH_PLUGIN_PKG_DIR)
        if debputy_plugins_dir is None:
            continue
        if not debputy_plugins_dir.is_dir:
            continue
        had_plugin_dir = True
        dest_dir = os.path.join("debian", binary_package.name)
        for path in debputy_plugins_dir.iterdir:
            if not path.is_file or not path.name.endswith((".json", ".json.in")):
                continue
            plugin_metadata = parse_json_plugin_desc(path.path)
            if (
                plugin_metadata.plugin_name.startswith("debputy-")
                or plugin_metadata.plugin_name == "debputy"
            ):
                _error(
                    f"The plugin name {plugin_metadata.plugin_name} is reserved by debputy. Please rename"
                    " the plugin to something else."
                )
            installs.append((plugin_metadata, dest_dir))
            all_tests.extend(find_tests_for_plugin(plugin_metadata))
    return had_plugin_dir, installs, all_tests


@ROOT_COMMAND.register_subcommand(
    "autopkgtest-test-runner",
    requested_plugins_only=True,
    help_description="Detect tests in the debian dir and run them against installed plugins",
)
def _autodep8_test_runner(context: CommandContext) -> None:
    ad_hoc_run = "AUTOPKGTEST_TMP" not in os.environ
    _a, _b, all_tests = _find_plugins_and_tests_in_source_package(context)

    source_package = context.source_package()
    explicit_test = (
        "autopkgtest-pkg-debputy" in source_package.fields.get("Testsuite", "").split()
    )

    if not shutil.which("py.test"):
        if ad_hoc_run:
            extra_context = ""
            if not explicit_test:
                extra_context = (
                    " Remember to add python3-pytest to the Depends field of your autopkgtests field if"
                    " you are writing your own test case for autopkgtest. Note you can also add"
                    ' "autopkgtest-pkg-debputy" to the "Testsuite" field in debian/control if you'
                    " want the test case autogenerated."
                )
            _error(
                f"Please install the py.test command (apt-get install python3-pytest).{extra_context}"
            )
        _error("Please add python3-pytest to the Depends field of your autopkgtests.")

    if not all_tests:
        extra_context = ""
        if explicit_test:
            extra_context = (
                " If the package no longer provides any plugin or tests, please remove the "
                ' "autopkgtest-pkg-debputy" test from the "Testsuite" in debian/control'
            )
        _error(
            "There are no tests to be run. The autodep8 feature should not have generated a test for"
            f" this case.{extra_context}"
        )

    if _run_tests(
        context,
        all_tests,
        test_plugin_location="installed",
        on_error_return=False,
    ):
        return
    extra_context = ""
    if not ad_hoc_run:
        extra_context = (
            ' These tests can be run manually via the "debputy autopkgtest-test-runner" command without any'
            ' autopkgtest layering. To do so, install "dh-debputy python3-pytest" plus the packages'
            " being tested and relevant extra dependencies required for the tests. Then open a shell in"
            f' the unpacked source directory of {source_package.name} and run "debputy autopkgtest-test-runner"'
        )
    _error(f"The tests were not successful.{extra_context}")


@internal_commands.register_subcommand(
    "dh-integration-install-plugin",
    help_description="[Internal command] Install a plugin and related files via debhelper integration",
    requested_plugins_only=True,
    argparser=_add_packages_args,
)
def _dh_integration_install_plugin(context: CommandContext) -> None:
    had_plugin_dir, installs, all_tests = _find_plugins_and_tests_in_source_package(
        context
    )

    if not installs:
        if had_plugin_dir:
            _warn(
                "There were plugin dirs, but no plugins were detected inside them. Please ensure that "
                f" the plugin dirs (debian/<pkg>.{_DH_PLUGIN_PKG_DIR} or debian/{_DH_PLUGIN_PKG_DIR})"
                f" contains a .json or .json.in file, or remove them (plus drop the"
                f" dh-sequence-installdebputy build dependency) if they are no longer useful."
            )
        else:
            _info(
                f"No plugin directories detected (debian/<pkg>.{_DH_PLUGIN_PKG_DIR} or debian/{_DH_PLUGIN_PKG_DIR})"
            )
        return

    if all_tests:
        if "nocheck" in context.deb_build_options_and_profiles.deb_build_options:
            _info("Skipping tests due to DEB_BUILD_OPTIONS=nocheck")
        elif not shutil.which("py.test"):
            _warn("Skipping tests because py.test is not available")
        else:
            _run_tests(context, all_tests)
    else:
        _info("No tests detected for any of the plugins. Skipping running tests.")

    for plugin_metadata, dest_dir in installs:
        _info(f"Installing plugin {plugin_metadata.plugin_name} into {dest_dir}")
        _install_plugin_from_plugin_metadata(plugin_metadata, dest_dir)


def _run_tests(
    context: CommandContext,
    test_paths: List[str],
    *,
    cwd: Optional[str] = None,
    tmpdir_root: Optional[str] = None,
    test_plugin_location: Literal["installed", "uninstalled"] = "uninstalled",
    on_error_return: Optional[Any] = None,
    on_success_return: Optional[Any] = True,
) -> Any:
    env = dict(os.environ)
    env["DEBPUTY_TEST_PLUGIN_LOCATION"] = test_plugin_location
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = f"{DEBPUTY_ROOT_DIR}:{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = str(DEBPUTY_ROOT_DIR)

    env["PYTHONDONTWRITEBYTECODE"] = "1"
    _info("Running debputy plugin tests.")
    _info("")
    _info("Environment settings:")
    for envname in [
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "DEBPUTY_TEST_PLUGIN_LOCATION",
    ]:
        _info(f"    {envname}={env[envname]}")

    with TemporaryDirectory(dir=tmpdir_root) as tmpdir:
        cmd = [
            "py.test",
            "-vvvvv" if context.parsed_args.debug_mode else "-v",
            "--config-file=/dev/null",
            f"--rootdir={cwd if cwd is not None else '.'}",
            "-o",
            f"cache_dir={tmpdir}",
        ]
        cmd.extend(test_paths)

        _info(f"Test Command: {escape_shell(*cmd)}")
        try:
            subprocess.check_call(
                cmd,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=cwd,
            )
        except subprocess.CalledProcessError:
            if on_error_return is None:
                _error("The tests were not successful.")
            return on_error_return
    return True


@internal_commands.register_subcommand(
    "run-tests-for-plugin",
    help_description="[Internal command] Run tests for a plugin",
    requested_plugins_only=True,
    argparser=[
        add_arg("target_plugin", metavar="PLUGIN", action="store"),
        add_arg(
            "--require-tests",
            dest="require_tests",
            default=True,
            action=argparse.BooleanOptionalAction,
        ),
    ],
)
def _run_tests_for_plugin(context: CommandContext) -> None:
    target_plugin = context.parsed_args.target_plugin
    if not os.path.isfile(target_plugin):
        _error(
            f'The value "{target_plugin}" must be a file. It should be the JSON descriptor of'
            f" the plugin."
        )
    try:
        plugin_metadata = find_json_plugin(
            context.plugin_search_dirs,
            target_plugin,
        )
    except PluginNotFoundError as e:
        _error(e.message)

    tests = find_tests_for_plugin(plugin_metadata)

    if not tests:
        if context.parsed_args.require_tests:
            plugin_name = plugin_metadata.plugin_name
            plugin_dir = os.path.dirname(plugin_metadata.plugin_path)

            _error(
                f"Cannot find any tests for {plugin_name}: Expected them to be in "
                f' "{plugin_dir}". Use --no-require-tests to consider missing tests'
                " a non-error."
            )
        _info(
            f"No tests found for {plugin_metadata.plugin_name}. Use --require-tests to turn"
            " this into an error."
        )
        return

    if not shutil.which("py.test"):
        _error(
            f"Cannot run the tests for {plugin_metadata.plugin_name}: This feature requires py.test"
            f" (apt-get install python3-pytest)"
        )
    _run_tests(context, tests, cwd="/")


@internal_commands.register_subcommand(
    "dpkg-build-driver-run-task",
    help_description="[Internal command] Perform a given Dpkg::BuildDriver task (Not stable API)",
    requested_plugins_only=True,
    default_log_level=_build_subcommand_log_level,
    argparser=[
        add_arg(
            "task_name",
            metavar="task-name",
            choices=[
                "clean",
                "build",
                "build-arch",
                "build-indep",
                "binary",
                "binary-arch",
                "binary-indep",
            ],
            help="The task to run",
        ),
        add_arg(
            "output",
            nargs="?",
            default="..",
            metavar="output",
            help="Where to place the resulting packages. Should be a directory",
        ),
    ],
)
def _dpkg_build_driver_integration(context: CommandContext) -> None:
    parsed_args = context.parsed_args
    log_level = context.set_log_level_for_build_subcommand()
    task_name = parsed_args.task_name

    if task_name.endswith("-indep"):
        context.package_set = "indep"
    elif task_name.endswith("arch"):
        context.package_set = "arch"

    manifest = context.parse_manifest()

    plugins = context.load_plugins().plugin_data
    for plugin in plugins.values():
        if not plugin.is_bundled:
            _info(f"Loaded plugin {plugin.plugin_name}")
    if task_name == "clean":
        perform_clean(context, manifest)
    elif task_name in ("build", "build-indep", "build-arch"):
        perform_builds(context, manifest)
    elif task_name in ("binary", "binary-indep", "binary-arch"):
        perform_builds(context, manifest)
        assemble(
            context,
            manifest,
            INTEGRATION_MODE_FULL,
            debug_materialization=log_level is not None,
        )
    else:
        _error(f"Unsupported Dpkg::BuildDriver task: {task_name}.")


@internal_commands.register_subcommand(
    "dh-integration-generate-debs",
    help_description="[Internal command] Generate .deb/.udebs packages from debian/<pkg> (Not stable API)",
    requested_plugins_only=True,
    default_log_level=_build_subcommand_log_level,
    argparser=[
        _add_packages_args,
        add_arg(
            "output",
            metavar="output",
            help="Where to place the resulting packages. Should be a directory",
        ),
        # Added for "help only" - you cannot trigger this option in practice
        add_arg(
            "--",
            metavar="UPSTREAM_ARGS",
            action="extend",
            nargs="+",
            dest="unused",
        ),
    ],
)
def _dh_integration_generate_debs(context: CommandContext) -> None:
    integrated_with_debhelper()
    log_level = context.set_log_level_for_build_subcommand()
    integration_mode = context.resolve_integration_mode()
    is_dh_rrr_only_mode = integration_mode == INTEGRATION_MODE_DH_DEBPUTY_RRR
    if is_dh_rrr_only_mode:
        problematic_plugins = list(context.requested_plugins())
        problematic_plugins.extend(context.required_plugins())
        if problematic_plugins:
            plugin_names = ", ".join(problematic_plugins)
            _error(
                f"Plugins are not supported in the zz-debputy-rrr sequence. Detected plugins: {plugin_names}"
            )

    plugins = context.load_plugins().plugin_data
    for plugin in plugins.values():
        if not plugin.is_bundled:
            _info(f"Loaded plugin {plugin.plugin_name}")
    manifest = context.parse_manifest()

    assemble(
        context,
        manifest,
        integration_mode,
        debug_materialization=log_level is not None,
    )


def assemble(
    context: CommandContext,
    manifest: HighLevelManifest,
    integration_mode: DebputyIntegrationMode,
    *,
    debug_materialization: bool = False,
) -> None:
    source_fs = FSROOverlay.create_root_dir("..", ".")
    source_version = manifest.source_version()
    is_native = "-" not in source_version
    is_dh_rrr_only_mode = integration_mode == INTEGRATION_MODE_DH_DEBPUTY_RRR
    package_data_table = manifest.perform_installations(integration_mode)
    if not is_dh_rrr_only_mode:
        for dctrl_bin in manifest.active_packages:
            package = dctrl_bin.name
            dctrl_data = package_data_table[package]
            fs_root = dctrl_data.fs_root
            package_metadata_context = dctrl_data.package_metadata_context

            assert dctrl_bin.should_be_acted_on

            detect_systemd_user_service_files(dctrl_bin, fs_root)
            usr_local_transformation(dctrl_bin, fs_root)
            handle_perl_code(
                dctrl_bin,
                manifest.dpkg_architecture_variables,
                fs_root,
                dctrl_data.substvars,
            )
            if "nostrip" not in manifest.deb_options_and_profiles.deb_build_options:
                dbgsym_ids = relocate_dwarves_into_dbgsym_packages(
                    dctrl_bin,
                    fs_root,
                    dctrl_data.dbgsym_info.dbgsym_fs_root,
                )
                dctrl_data.dbgsym_info.dbgsym_ids = dbgsym_ids

            fixup_debian_changelog_and_news_file(
                dctrl_bin,
                fs_root,
                is_native,
                manifest.deb_options_and_profiles,
            )
            if not is_native:
                install_upstream_changelog(
                    dctrl_bin,
                    fs_root,
                    source_fs,
                )
            run_package_processors(manifest, package_metadata_context, fs_root)

        cross_package_control_files(package_data_table, manifest)
    for binary_data in package_data_table:
        if not binary_data.binary_package.should_be_acted_on:
            continue
        # Ensure all fs's are read-only before we enable cross package checks.
        # This ensures that no metadata detector will never see a read-write FS
        pkg_fs_root: "FSRootDir" = cast("FSRootDir", binary_data.fs_root)
        pkg_fs_root.is_read_write = False

    package_data_table.enable_cross_package_checks = True
    assemble_debs(
        context,
        manifest,
        package_data_table,
        is_dh_rrr_only_mode,
        debug_materialization=debug_materialization,
    )


@tool_support_commands.register_subcommand(
    "supports-tool-command",
    help_description="Test where a given tool-support command exists",
    argparser=add_arg(
        "test_command",
        metavar="name",
        default=None,
        help="The name of the command",
    ),
)
def _supports_tool_command(context: CommandContext) -> None:
    command_name = context.parsed_args.test_command
    if tool_support_commands.has_command(command_name):
        sys.exit(0)
    else:
        sys.exit(2)


@tool_support_commands.register_subcommand(
    "export-reference-data",
    help_description="Export reference data for other tool-support commands",
    argparser=[
        add_arg(
            "--output-format",
            default="text",
            choices=["text", "json"],
            help="Output format of the reference data",
        ),
        add_arg(
            "dataset",
            metavar="name",
            default=None,
            nargs="?",
            help="The dataset to export (if any)",
            choices=REFERENCE_DATA_TABLE,
        ),
    ],
)
def _export_reference_data(context: CommandContext) -> None:
    dataset_name = context.parsed_args.dataset
    output_format = context.parsed_args.output_format
    if dataset_name is not None:
        subdata_set = REFERENCE_DATA_TABLE.get(dataset_name)
        if subdata_set is None:
            _error(f"Unknown data set: {dataset_name}")
        reference_data = {
            dataset_name: subdata_set,
        }
    else:
        subdata_set = None
        reference_data = REFERENCE_DATA_TABLE
    if output_format == "text":
        if subdata_set is None:
            _error(
                "When output format is text, then the dataset name is required (it is optional for JSON formats)."
            )
        with _stream_to_pager(context.parsed_args) as (fd, fo):
            header = ["key", "description"]
            rows = [(k, v["description"]) for k, v in subdata_set.items()]
            fo.print_list_table(header, rows)
            fo.print()
            fo.print("If you wanted this as JSON, please use --output-format=json")
    elif output_format == "json":
        _json_output(
            {
                "reference-data": reference_data,
            }
        )
    else:
        raise AssertionError(f"Unsupported output format {output_format}")


@tool_support_commands.register_subcommand(
    "annotate-debian-directory",
    log_only_to_stderr=True,
    help_description="Scan debian/* for known package files and annotate them with information."
    " Output is evaluated and may change. Please get in touch if you want to use it"
    " or want additional features.",
)
def _annotate_debian_directory(context: CommandContext) -> None:
    # Validates that we are run from a debian directory as a side effect
    binary_packages = context.binary_packages()
    feature_set = context.load_plugins()

    result = scan_debian_dir(
        feature_set,
        binary_packages,
        context.debian_dir,
    )
    annotated, reference_data_set_names, dh_assistant_exit_code, dh_issues = result

    data = {
        "result": annotated,
        "reference-datasets": reference_data_set_names,
    }
    if dh_issues is not None or dh_assistant_exit_code != 0:
        data["issues"] = [
            {
                "source": "dh_assistant",
                "exit-code": dh_assistant_exit_code,
                "issue-data": dh_issues,
            }
        ]
    _json_output(data)


def _json_output(data: Any) -> None:
    if sys.stdout.isatty():
        # sort_keys might be tempting but generally insert order makes more sense in practice.
        json.dump(data, sys.stdout, indent=4)
    else:
        json.dump(data, sys.stdout)
    if sys.stdout.isatty():
        # Looks better with a final newline.
        print()


@ROOT_COMMAND.register_subcommand(
    "migrate-from-dh",
    help_description='Generate/update manifest from a "dh $@" using package',
    argparser=[
        add_arg(
            "--acceptable-migration-issues",
            dest="acceptable_migration_issues",
            action="append",
            type=str,
            default=[],
            help="Continue the migration even if this/these issues are detected."
            " Can be set to ALL (in all upper-case) to accept all issues",
        ),
        add_arg(
            "--migration-target",
            dest="migration_target",
            action="store",
            choices=MIGRATORS,
            type=str,
            default=None,
            help="Continue the migration even if this/these issues are detected."
            " Can be set to ALL (in all upper-case) to accept all issues",
        ),
        add_arg(
            "--no-act",
            "--no-apply-changes",
            dest="destructive",
            action="store_false",
            default=None,
            help="Do not perform changes.  Existing manifest will not be overridden",
        ),
        add_arg(
            "--apply-changes",
            dest="destructive",
            action="store_true",
            default=None,
            help="Perform changes.  The debian/debputy.manifest will updated in place if exists",
        ),
    ],
)
def _migrate_from_dh(context: CommandContext) -> None:
    context.must_be_called_in_source_root()
    parsed_args = context.parsed_args
    resolved_migration_target = _check_migration_target(
        context,
        parsed_args.migration_target,
    )
    context.debputy_integration_mode = resolved_migration_target
    manifest = context.parse_manifest()
    acceptable_migration_issues = AcceptableMigrationIssues(
        frozenset(
            i for x in parsed_args.acceptable_migration_issues for i in x.split(",")
        )
    )
    migrate_from_dh(
        _output_styling(context.parsed_args, sys.stdout),
        manifest,
        acceptable_migration_issues,
        parsed_args.destructive,
        resolved_migration_target,
        lambda p: context.parse_manifest(manifest_path=p),
    )


def _setup_and_parse_args() -> argparse.Namespace:
    is_arg_completing = "_ARGCOMPLETE" in os.environ
    if not is_arg_completing:
        setup_logging()
    parsed_args = parse_args()
    if is_arg_completing:
        # We could be asserting at this point; but lets just recover gracefully.
        setup_logging()
    return parsed_args


def main() -> None:
    parsed_args = _setup_and_parse_args()
    plugin_search_dirs = [str(DEBPUTY_PLUGIN_ROOT_DIR)]
    try:
        cmd_arg = CommandArg(
            parsed_args,
            plugin_search_dirs,
        )
        ROOT_COMMAND(cmd_arg)
    except PluginInitializationError as e:
        _error_w_stack_trace(
            "Failed to load a plugin - full stack strace:",
            e.message,
            e,
            parsed_args.debug_mode,
            follow_warning=[
                "Please consider filing a bug against the plugin in question"
            ],
        )
    except UnhandledOrUnexpectedErrorFromPluginError as e:
        trace = e.__cause__ if e.__cause__ is not None else e
        # TODO: Reframe this as an internal error if `debputy` is the misbehaving plugin
        if isinstance(trace, SymlinkLoopError):
            _error_w_stack_trace(
                "Error in `debputy`:",
                e.message,
                trace,
                parsed_args.debug_mode,
                orig_exception=e,
                follow_warning=[
                    "Please consider filing a bug against `debputy` in question"
                ],
            )
        else:
            _error_w_stack_trace(
                "A plugin misbehaved:",
                e.message,
                trace,
                parsed_args.debug_mode,
                orig_exception=e,
                follow_warning=[
                    "Please consider filing a bug against the plugin in question"
                ],
            )
    except PluginAPIViolationError as e:
        trace = e.__cause__ if e.__cause__ is not None else e
        # TODO: Reframe this as an internal error if `debputy` is the misbehaving plugin
        _error_w_stack_trace(
            "A plugin misbehaved:",
            e.message,
            trace,
            parsed_args.debug_mode,
            orig_exception=e,
            follow_warning=[
                "Please consider filing a bug against the plugin in question"
            ],
        )
    except DebputyRuntimeError as e:
        if parsed_args.debug_mode:
            _warn(
                "Re-raising original exception to show the full stack trace due to debug mode being active"
            )
            raise e
        _error(e.message)
    except AssertionError as e:
        _error_w_stack_trace(
            "Internal error in debputy",
            str(e),
            e,
            parsed_args.debug_mode,
            orig_exception=e,
            follow_warning=["Please file a bug against debputy with the full output."],
        )
    except subprocess.CalledProcessError as e:
        cmd = escape_shell(*e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        _error_w_stack_trace(
            f"The command << {cmd} >> failed and the code did not explicitly handle that exception.",
            str(e),
            e,
            parsed_args.debug_mode,
            orig_exception=e,
            follow_warning=[
                "The output above this error and the stacktrace may provide context to why the command failed.",
                "Please file a bug against debputy with the full output.",
            ],
        )
    except Exception as e:
        _error_w_stack_trace(
            "Unhandled exception (Re-run with --debug to see the raw stack trace)",
            str(e),
            e,
            parsed_args.debug_mode,
            orig_exception=e,
            follow_warning=["Please file a bug against debputy with the full output."],
        )


def _error_w_stack_trace(
    warning: str,
    error_msg: str,
    stacktrace: BaseException,
    debug_mode: bool,
    orig_exception: Optional[BaseException] = None,
    follow_warning: Optional[List[str]] = None,
) -> "NoReturn":
    if debug_mode:
        _warn(
            "Re-raising original exception to show the full stack trace due to debug mode being active"
        )
        raise orig_exception if orig_exception is not None else stacktrace
    _warn(warning)
    _warn("  ----- 8< ---- BEGIN STACK TRACE ---- 8< -----")
    traceback.print_exception(stacktrace)
    _warn("  ----- 8< ---- END STACK TRACE ---- 8< -----")
    if follow_warning:
        for line in follow_warning:
            _warn(line)
    _error(error_msg)


if __name__ == "__main__":
    main()
