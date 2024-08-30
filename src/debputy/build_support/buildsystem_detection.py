from typing import (
    Optional,
)

from debputy.exceptions import (
    DebputyPluginRuntimeError,
    PluginBaseError,
)
from debputy.filesystem_scan import FSRootDir, FSROOverlay
from debputy.highlevel_manifest import HighLevelManifest
from debputy.manifest_parser.base_types import BuildEnvironmentDefinition
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.debputy.to_be_api_types import (
    BuildSystemRule,
)
from debputy.plugin.plugin_state import run_in_context_of_plugin_wrap_errors
from debputy.util import (
    _error,
    _debug_log,
)


def default_build_environment_only(
    manifest: HighLevelManifest,
) -> BuildEnvironmentDefinition:
    build_envs = manifest.build_environments
    if build_envs.environments:
        _error(
            'When automatic build system detection is used, the manifest cannot use "build-environments"'
        )
    build_env = build_envs.default_environment
    assert build_env is not None
    return build_env


def auto_detect_buildsystem(
    manifest: HighLevelManifest,
) -> Optional[BuildSystemRule]:
    auto_detectable_build_systems = (
        manifest.plugin_provided_feature_set.auto_detectable_build_systems
    )
    excludes = set()
    options = []
    _debug_log("Auto-detecting build systems.")
    source_root = FSROOverlay.create_root_dir("", ".")
    for ppadbs in auto_detectable_build_systems.values():
        detected = ppadbs.detector(source_root)
        if not isinstance(detected, bool):
            _error(
                f'The auto-detector for the build system {ppadbs.manifest_keyword} returned a "non-bool"'
                f" ({detected!r}), which could be a bug in the plugin or the plugin relying on a newer"
                " version of `debputy` that changed the auto-detection protocol."
            )
        if not detected:
            _debug_log(
                f"Skipping build system {ppadbs.manifest_keyword}: Detector returned False!"
            )
            continue
        _debug_log(
            f"Considering build system {ppadbs.manifest_keyword} as its Detector returned True!"
        )
        if ppadbs.auto_detection_shadow_build_systems:
            names = ", ".join(
                sorted(x for x in ppadbs.auto_detection_shadow_build_systems)
            )
            _debug_log(f"Build system {ppadbs.manifest_keyword} excludes: {names}!")
        excludes.update(ppadbs.auto_detection_shadow_build_systems)
        options.append(ppadbs)

    if not options:
        _debug_log("Zero candidates; continuing without a build system")
        return None

    if excludes:
        names = ", ".join(sorted(x for x in excludes))
        _debug_log(f"The following build systems have been excluded: {names}!")
        remaining_options = [o for o in options if o.manifest_keyword not in excludes]
    else:
        remaining_options = options

    if len(remaining_options) > 1:
        names = ", ".join(o.manifest_keyword for o in remaining_options)
        # TODO: This means adding an auto-detectable build system to an existing plugin causes FTBFS
        # We need a better way of handling this. Probably the build systems should include
        # a grace timer based on d/changelog. Anything before the changelog date is in
        # "grace mode" and will not conflict with a build system that is. If all choices
        # are in "grace mode", "oldest one" wins.
        _error(
            f"Multiple build systems match, please pick one explicitly (under `builds:`): {names}"
        )

    if not remaining_options:
        names = ", ".join(o.build_system_rule_type.__name__ for o in options)
        # TODO: Detect at registration time
        _error(
            f"Multiple build systems matched but they all shadowed each other: {names}."
            f" There is a bug in at least one of them!"
        )

    chosen_build_system = remaining_options[0]
    environment = default_build_environment_only(manifest)
    bs = run_in_context_of_plugin_wrap_errors(
        chosen_build_system.plugin_metadata.plugin_name,
        chosen_build_system.constructor,
        {
            "environment": environment,
        },
        AttributePath.builtin_path(),
        manifest,
    )
    bs.auto_generated_stem = ""
    return bs
