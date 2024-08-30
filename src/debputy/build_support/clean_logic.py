import os.path
from typing import (
    Set,
    cast,
    List,
)

from debputy.build_support.build_context import BuildContext
from debputy.build_support.build_logic import (
    in_build_env,
    assign_stems,
)
from debputy.build_support.buildsystem_detection import auto_detect_buildsystem
from debputy.commands.debputy_cmd.context import CommandContext
from debputy.highlevel_manifest import HighLevelManifest
from debputy.plugin.debputy.to_be_api_types import BuildSystemRule, CleanHelper
from debputy.util import _info, print_command, _error, _debug_log, _warn
from debputy.util import (
    run_build_system_command,
)

_REMOVE_DIRS = frozenset(
    [
        "__pycache__",
        "autom4te.cache",
    ]
)
_IGNORE_DIRS = frozenset(
    [
        ".git",
        ".svn",
        ".bzr",
        ".hg",
        "CVS",
        ".pc",
        "_darcs",
    ]
)
DELETE_FILE_EXT = (
    "~",
    ".orig",
    ".rej",
    ".bak",
)
DELETE_FILE_BASENAMES = {
    "DEADJOE",
    ".SUMS",
    "TAGS",
}


def _debhelper_left_overs() -> bool:
    if os.path.lexists("debian/.debhelper") or os.path.lexists(
        "debian/debhelper-build-stamp"
    ):
        return True
    with os.scandir(".") as root_dir:
        for child in root_dir:
            if child.is_file(follow_symlinks=False) and (
                child.name.endswith(".debhelper.log")
                or child.name.endswith(".debhelper")
            ):
                return True
    return False


class CleanHelperImpl(CleanHelper):

    def __init__(self) -> None:
        self.files_to_remove: Set[str] = set()
        self.dirs_to_remove: Set[str] = set()

    def schedule_removal_of_files(self, *args: str) -> None:
        self.files_to_remove.update(args)

    def schedule_removal_of_directories(self, *args: str) -> None:
        if any(p == "/" for p in args):
            raise ValueError("Refusing to delete '/'")
        self.dirs_to_remove.update(args)


def _scan_for_standard_removals(clean_helper: CleanHelperImpl) -> None:
    remove_files = clean_helper.files_to_remove
    remove_dirs = clean_helper.dirs_to_remove
    with os.scandir(".") as root_dir:
        for child in root_dir:
            if child.is_file(follow_symlinks=False) and child.name.endswith("-stamp"):
                remove_files.add(child.path)
    for current_dir, subdirs, files in os.walk("."):
        for remove_dir in [d for d in subdirs if d in _REMOVE_DIRS]:
            path = os.path.join(current_dir, remove_dir)
            remove_dirs.add(path)
            subdirs.remove(remove_dir)
        for skip_dir in [d for d in subdirs if d in _IGNORE_DIRS]:
            subdirs.remove(skip_dir)

        for basename in files:
            if (
                basename.endswith(DELETE_FILE_EXT)
                or basename in DELETE_FILE_BASENAMES
                or (basename.startswith("#") and basename.endswith("#"))
            ):
                path = os.path.join(current_dir, basename)
                remove_files.add(path)


def perform_clean(
    context: CommandContext,
    manifest: HighLevelManifest,
) -> None:
    clean_helper = CleanHelperImpl()

    build_rules = manifest.build_rules
    if build_rules is not None:
        if not build_rules:
            # Defined but empty disables the auto-detected build system
            return
        active_packages = frozenset(manifest.active_packages)
        condition_context = manifest.source_condition_context
        build_context = BuildContext.from_command_context(context)
        assign_stems(build_rules, manifest)
        for step_no, build_rule in enumerate(build_rules):
            step_ref = (
                f"step {step_no} [{build_rule.auto_generated_stem}]"
                if build_rule.name is None
                else f"step {step_no} [{build_rule.name}]"
            )
            if not build_rule.is_buildsystem:
                _debug_log(f"Skipping clean for {step_ref}: Not a build system")
                continue
            build_system_rule: BuildSystemRule = cast("BuildSystemRule", build_rule)
            if build_system_rule.for_packages.isdisjoint(active_packages):
                _info(
                    f"Skipping build for {step_ref}: None of the relevant packages are being built"
                )
                continue
            manifest_condition = build_system_rule.manifest_condition
            if manifest_condition is not None and not manifest_condition.evaluate(
                condition_context
            ):
                _info(
                    f"Skipping clean for {step_ref}: The condition clause evaluated to false"
                )
                continue
            _info(f"Starting clean for {step_ref}.")
            with in_build_env(build_rule.environment):
                try:
                    build_system_rule.run_clean(
                        build_context,
                        manifest,
                        clean_helper,
                    )
                except (RuntimeError, AttributeError) as e:
                    if context.parsed_args.debug_mode:
                        raise e
                    _error(
                        f"An error occurred during clean at {step_ref} (defined at {build_rule.attribute_path.path}): {str(e)}"
                    )
            _info(f"Completed clean for {step_ref}.")
    else:
        build_system = auto_detect_buildsystem(manifest)
        if build_system:
            _info(f"Auto-detected build system: {build_system.__class__.__name__}")
            build_context = BuildContext.from_command_context(context)
            with in_build_env(build_system.environment):
                build_system.run_clean(
                    build_context,
                    manifest,
                    clean_helper,
                )
        else:
            _info("No build system was detected from the current plugin set.")

    dh_autoreconf_used = os.path.lexists("debian/autoreconf.before")
    debhelper_used = False

    if dh_autoreconf_used or _debhelper_left_overs():
        debhelper_used = True

    _scan_for_standard_removals(clean_helper)

    for package in manifest.all_packages:
        package_staging_dir = os.path.join("debian", package.name)
        if os.path.lexists(package_staging_dir):
            clean_helper.schedule_removal_of_directories(package_staging_dir)

    remove_files = clean_helper.files_to_remove
    remove_dirs = clean_helper.dirs_to_remove
    if remove_files:
        print_command("rm", "-f", *remove_files)
        _remove_files_if_exists(*remove_files)
    if remove_dirs:
        run_build_system_command("rm", "-fr", *remove_dirs)

    if debhelper_used:
        _info(
            "Noted traces of debhelper commands being used; invoking dh_clean to clean up after them"
        )
        if dh_autoreconf_used:
            run_build_system_command("dh_autoreconf_clean")
        run_build_system_command("dh_clean")

    try:
        run_build_system_command("dpkg-buildtree", "clean")
    except FileNotFoundError:
        _warn("The dpkg-buildtree command is not present. Emulating it")
        # This is from the manpage of dpkg-buildtree for 1.22.11.
        _remove_files_if_exists(
            "debian/files",
            "debian/files.new",
            "debian/substvars",
            "debian/substvars.new",
        )
        run_build_system_command("rm", "-fr", "debian/tmp")
    # Remove debian/.debputy as a separate step. While `rm -fr` should process things in order,
    # it will continue on error, which could cause our manifests of things to delete to be deleted
    # while leaving things half-removed unless we do this extra step.
    run_build_system_command("rm", "-fr", "debian/.debputy")


def _remove_files_if_exists(*args: str) -> None:
    for path in args:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            if os.path.isdir(path):
                _error(
                    f"Failed to remove {path}: It is a directory, but it should have been a non-directory."
                    " Please verify everything is as expected and, if it is, remove it manually."
                )
            _error(f"Failed to remove {path}: {str(e)}")
