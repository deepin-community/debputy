import collections
import contextlib
import os
from typing import (
    Iterator,
    Mapping,
    List,
    Dict,
    Optional,
)

from debputy.build_support.build_context import BuildContext
from debputy.build_support.buildsystem_detection import (
    auto_detect_buildsystem,
)
from debputy.commands.debputy_cmd.context import CommandContext
from debputy.highlevel_manifest import HighLevelManifest
from debputy.manifest_parser.base_types import BuildEnvironmentDefinition
from debputy.plugin.debputy.to_be_api_types import BuildRule
from debputy.util import (
    _error,
    _info,
    _non_verbose_info,
)


@contextlib.contextmanager
def in_build_env(build_env: BuildEnvironmentDefinition):
    remove_unnecessary_env()
    # Should possibly be per build
    with _setup_build_env(build_env):
        yield


def _set_stem_if_absent(stems: List[Optional[str]], idx: int, stem: str) -> None:
    if stems[idx] is None:
        stems[idx] = stem


def assign_stems(
    build_rules: List[BuildRule],
    manifest: HighLevelManifest,
) -> None:
    if not build_rules:
        return
    if len(build_rules) == 1:
        build_rules[0].auto_generated_stem = ""
        return

    debs = {p.name for p in manifest.all_packages if p.package_type == "deb"}
    udebs = {p.name for p in manifest.all_packages if p.package_type == "udeb"}
    deb_only_builds: List[int] = []
    udeb_only_builds: List[int] = []
    by_name_only_builds: Dict[str, List[int]] = collections.defaultdict(list)
    stems = [rule.name for rule in build_rules]
    reserved_stems = set(n for n in stems if n is not None)

    for idx, rule in enumerate(build_rules):
        stem = stems[idx]
        if stem is not None:
            continue
        pkg_names = {p.name for p in rule.for_packages}
        if pkg_names == debs:
            deb_only_builds.append(idx)
        elif pkg_names == udebs:
            udeb_only_builds.append(idx)

        if len(pkg_names) == 1:
            pkg_name = next(iter(pkg_names))
            by_name_only_builds[pkg_name].append(idx)

    if "deb" not in reserved_stems and len(deb_only_builds) == 1:
        _set_stem_if_absent(stems, deb_only_builds[0], "deb")

    if "udeb" not in reserved_stems and len(udeb_only_builds) == 1:
        _set_stem_if_absent(stems, udeb_only_builds[0], "udeb")

    for pkg, idxs in by_name_only_builds.items():
        if len(idxs) != 1 or pkg in reserved_stems:
            continue
        _set_stem_if_absent(stems, idxs[0], pkg)

    for idx, rule in enumerate(build_rules):
        stem = stems[idx]
        if stem is None:
            stem = f"bno_{idx}"
        rule.auto_generated_stem = stem
        _info(f"Assigned {rule.auto_generated_stem} [{stem}] to step {idx}")


def perform_builds(
    context: CommandContext,
    manifest: HighLevelManifest,
) -> None:
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
            if build_rule.for_packages.isdisjoint(active_packages):
                _info(
                    f"Skipping build for {step_ref}: None of the relevant packages are being built"
                )
                continue
            manifest_condition = build_rule.manifest_condition
            if manifest_condition is not None and not manifest_condition.evaluate(
                condition_context
            ):
                _info(
                    f"Skipping build for {step_ref}: The condition clause evaluated to false"
                )
                continue
            _info(f"Starting build for {step_ref}.")
            with in_build_env(build_rule.environment):
                try:
                    build_rule.run_build(build_context, manifest)
                except (RuntimeError, AttributeError) as e:
                    if context.parsed_args.debug_mode:
                        raise e
                    _error(
                        f"An error occurred during build/install at {step_ref} (defined at {build_rule.attribute_path.path}): {str(e)}"
                    )
            _info(f"Completed build for {step_ref}.")

    else:
        build_system = auto_detect_buildsystem(manifest)
        if build_system:
            _info(f"Auto-detected build system: {build_system.__class__.__name__}")
            build_context = BuildContext.from_command_context(context)
            with in_build_env(build_system.environment):
                with in_build_env(build_system.environment):
                    build_system.run_build(
                        build_context,
                        manifest,
                    )

            _non_verbose_info("Upstream builds completed successfully")
        else:
            _info("No build system was detected from the current plugin set.")


def remove_unnecessary_env() -> None:
    vs = [
        "XDG_CACHE_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_RUNTIME_DIR",
    ]
    for v in vs:
        if v in os.environ:
            del os.environ[v]

    # FIXME: Add custom HOME + XDG_RUNTIME_DIR


@contextlib.contextmanager
def _setup_build_env(build_env: BuildEnvironmentDefinition) -> Iterator[None]:
    env_backup = dict(os.environ)
    env = dict(env_backup)
    had_delta = False
    build_env.update_env(env)
    if env != env_backup:
        _set_env(env)
        had_delta = True
    _info("Updated environment to match build")
    yield
    if had_delta or env != env_backup:
        _set_env(env_backup)


def _set_env(desired_env: Mapping[str, str]) -> None:
    os_env = os.environ
    for key in os_env.keys() | desired_env.keys():
        desired_value = desired_env.get(key)
        if desired_value is None:
            try:
                del os_env[key]
            except KeyError:
                pass
        else:
            os_env[key] = desired_value
