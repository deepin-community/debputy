import json
import os
import re
import subprocess
from itertools import chain
from typing import Optional, List, Callable, Set, Container, Mapping, FrozenSet

from debian.deb822 import Deb822

from debputy.commands.debputy_cmd.context import CommandContext
from debputy.commands.debputy_cmd.output import OutputStylingBase
from debputy.dh.debhelper_emulation import CannotEmulateExecutableDHConfigFile
from debputy.dh_migration.migrators import MIGRATORS
from debputy.dh_migration.migrators_impl import (
    INTEGRATION_MODE_DH_DEBPUTY,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
)
from debputy.dh.dh_assistant import read_dh_addon_sequences
from debputy.dh_migration.models import (
    FeatureMigration,
    AcceptableMigrationIssues,
    UnsupportedFeature,
    ConflictingChange,
)
from debputy.highlevel_manifest import HighLevelManifest
from debputy.integration_detection import determine_debputy_integration_mode
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.spec import DebputyIntegrationMode, INTEGRATION_MODE_FULL
from debputy.util import _error, _warn, _info, escape_shell, assume_not_none

SUPPORTED_MIGRATIONS: Mapping[
    DebputyIntegrationMode, FrozenSet[DebputyIntegrationMode]
] = {
    INTEGRATION_MODE_FULL: frozenset([INTEGRATION_MODE_FULL]),
    INTEGRATION_MODE_DH_DEBPUTY: frozenset(
        [INTEGRATION_MODE_DH_DEBPUTY, INTEGRATION_MODE_FULL]
    ),
    INTEGRATION_MODE_DH_DEBPUTY_RRR: frozenset(
        [
            INTEGRATION_MODE_DH_DEBPUTY_RRR,
            INTEGRATION_MODE_DH_DEBPUTY,
            INTEGRATION_MODE_FULL,
        ]
    ),
}


def _print_migration_summary(
    fo: OutputStylingBase,
    migrations: List[FeatureMigration],
    compat: int,
    min_compat_level: int,
    required_plugins: Set[str],
    requested_plugins: Optional[Set[str]],
) -> None:
    warning_count = 0

    for migration in migrations:
        if not migration.anything_to_do:
            continue
        underline = "-" * len(migration.tagline)
        if migration.warnings:
            if warning_count:
                _warn("")
            _warn(f"Summary for migration: {migration.tagline}")
            if not fo.optimize_for_screen_reader:
                _warn(f"-----------------------{underline}")
            warning_count += len(migration.warnings)
            for warning in migration.warnings:
                _warn(f"    * {warning}")

    if compat < min_compat_level:
        if warning_count:
            _warn("")
        _warn("Supported debhelper compat check")
        if not fo.optimize_for_screen_reader:
            _warn("--------------------------------")
        warning_count += 1
        _warn(
            f"The migration tool assumes debhelper compat {min_compat_level}+ semantics, but this package"
            f" is using compat {compat}.  Consider upgrading the package to compat {min_compat_level}"
            " first."
        )

    if required_plugins:
        if requested_plugins is None:
            warning_count += 1
            needed_plugins = ", ".join(f"debputy-plugin-{n}" for n in required_plugins)
            if warning_count:
                _warn("")
            _warn("Missing debputy plugin check")
            if not fo.optimize_for_screen_reader:
                _warn("----------------------------")
            _warn(
                f"The migration tool could not read d/control and therefore cannot tell if all the required"
                f" plugins have been requested.  Please ensure that the package Build-Depends on: {needed_plugins}"
            )
        else:
            missing_plugins = required_plugins - requested_plugins
            if missing_plugins:
                warning_count += 1
                needed_plugins = ", ".join(
                    f"debputy-plugin-{n}" for n in missing_plugins
                )
                if warning_count:
                    _warn("")
                _warn("Missing debputy plugin check")
                if not fo.optimize_for_screen_reader:
                    _warn("----------------------------")
                _warn(
                    f"The migration tool asserted that the following `debputy` plugins would be required, which"
                    f" are not explicitly requested.  Please add the following to Build-Depends: {needed_plugins}"
                )

    if warning_count:
        _warn("")
        _warn(
            f"/!\\ Total number of warnings or manual migrations required: {warning_count}"
        )


def _dh_compat_level() -> Optional[int]:
    try:
        res = subprocess.check_output(
            ["dh_assistant", "active-compat-level"], stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        compat = None
    else:
        try:
            compat = json.loads(res)["declared-compat-level"]
        except RuntimeError:
            compat = None
        else:
            if not isinstance(compat, int):
                compat = None
    return compat


def _requested_debputy_plugins(debian_dir: VirtualPath) -> Optional[Set[str]]:
    ctrl_file = debian_dir.get("control")
    if not ctrl_file:
        return None

    dep_regex = re.compile("^([a-z0-9][-+.a-z0-9]+)", re.ASCII)
    plugins = set()

    with ctrl_file.open() as fd:
        ctrl = list(Deb822.iter_paragraphs(fd))
    source_paragraph = ctrl[0] if ctrl else {}

    for f in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch"):
        field = source_paragraph.get(f)
        if not field:
            continue

        for dep_clause in (d.strip() for d in field.split(",")):
            match = dep_regex.match(dep_clause.strip())
            if not match:
                continue
            dep = match.group(1)
            if not dep.startswith("debputy-plugin-"):
                continue
            plugins.add(dep[15:])
    return plugins


def _check_migration_target(
    context: CommandContext,
    migration_target: Optional[DebputyIntegrationMode],
) -> DebputyIntegrationMode:
    r = read_dh_addon_sequences(context.debian_dir)
    if r is not None:
        bd_sequences, dr_sequences, _ = r
        all_sequences = bd_sequences | dr_sequences
        detected_migration_target = determine_debputy_integration_mode(
            context.source_package().fields,
            all_sequences,
        )
    else:
        detected_migration_target = None

    if migration_target is not None and detected_migration_target is not None:
        supported_migrations = SUPPORTED_MIGRATIONS.get(
            detected_migration_target,
            frozenset([detected_migration_target]),
        )

        if (
            migration_target != detected_migration_target
            and migration_target not in supported_migrations
        ):
            _error(
                f"Cannot migrate from {detected_migration_target} to {migration_target}"
            )

    if migration_target is not None:
        resolved_migration_target = migration_target
        _info(f'Using "{resolved_migration_target}" as migration target as requested')
    else:
        if detected_migration_target is not None:
            _info(
                f'Using "{detected_migration_target}" as migration target based on the packaging'
            )
        else:
            detected_migration_target = INTEGRATION_MODE_DH_DEBPUTY
            _info(
                f'Using "{detected_migration_target}" as default migration target. Use --migration-target to choose!'
            )
        resolved_migration_target = detected_migration_target

    return resolved_migration_target


def migrate_from_dh(
    fo: OutputStylingBase,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    permit_destructive_changes: Optional[bool],
    migration_target: DebputyIntegrationMode,
    manifest_parser_factory: Callable[[str], HighLevelManifest],
) -> None:
    migrations = []
    compat = _dh_compat_level()
    if compat is None:
        _error(
            'Cannot detect declared compat level (try running "dh_assistant active-compat-level")'
        )

    debian_dir = manifest.debian_dir
    mutable_manifest = assume_not_none(manifest.mutable_manifest)

    try:
        for migrator in MIGRATORS[migration_target]:
            feature_migration = FeatureMigration(migrator.__name__, fo)
            migrator(
                debian_dir,
                manifest,
                acceptable_migration_issues,
                feature_migration,
                migration_target,
            )
            migrations.append(feature_migration)
    except CannotEmulateExecutableDHConfigFile as e:
        _error(
            f"Unable to process the executable dh config file {e.config_file().fs_path}: {e.message()}"
        )
    except UnsupportedFeature as e:
        msg = (
            f"Unable to migrate automatically due to missing features in debputy. The feature is:"
            f"\n\n  * {e.message}"
        )
        keys = e.issue_keys
        if keys:
            primary_key = keys[0]
            alt_keys = ""
            if len(keys) > 1:
                alt_keys = (
                    f' Alternatively you can also use one of: {", ".join(keys[1:])}.  Please note that some'
                    " of these may cover more cases."
                )
            msg += (
                f"\n\nUse --acceptable-migration-issues={primary_key} to convert this into a warning and try again."
                " However, you should only do that if you believe you can replace the functionality manually"
                f" or the usage is obsolete / can be removed. {alt_keys}"
            )
        _error(msg)
    except ConflictingChange as e:
        _error(
            "The migration tool detected a conflict data being migrated and data already migrated / in the existing"
            "manifest."
            f"\n\n   * {e.message}"
            "\n\nPlease review the situation and resolve the conflict manually."
        )

    # We start on compat 12 for arch:any due to the new dh_makeshlibs and dh_installinit default
    min_compat = 12
    min_compat = max(
        (m.assumed_compat for m in migrations if m.assumed_compat is not None),
        default=min_compat,
    )

    if compat < min_compat and "min-compat-level" not in acceptable_migration_issues:
        # The migration summary special-cases the compat mismatch and warns for us.
        _error(
            f"The migration tool assumes debhelper compat {min_compat} or later but the package is only on"
            f" compat {compat}.  This may lead to incorrect result."
            f"\n\nUse --acceptable-migration-issues=min-compat-level to convert this into a warning and"
            f" try again, if you want to continue regardless."
        )

    requested_plugins = _requested_debputy_plugins(debian_dir)
    required_plugins: Set[str] = set()
    required_plugins.update(
        chain.from_iterable(
            m.required_plugins for m in migrations if m.required_plugins
        )
    )

    _print_migration_summary(
        fo,
        migrations,
        compat,
        min_compat,
        required_plugins,
        requested_plugins,
    )
    migration_count = sum((m.performed_changes for m in migrations), 0)

    if not migration_count:
        _info(
            "debputy was not able to find any (supported) migrations that it could perform for you."
        )
        return

    if any(m.successful_manifest_changes for m in migrations):
        new_manifest_path = manifest.manifest_path + ".new"

        with open(new_manifest_path, "w") as fd:
            mutable_manifest.write_to(fd)

        try:
            _info("Verifying the generating manifest")
            manifest_parser_factory(new_manifest_path)
        except ManifestParseException as e:
            raise AssertionError(
                "Could not parse the manifest generated from the migrator"
            ) from e

        if permit_destructive_changes:
            if os.path.isfile(manifest.manifest_path):
                os.rename(manifest.manifest_path, manifest.manifest_path + ".orig")
            os.rename(new_manifest_path, manifest.manifest_path)
            _info(f"Updated manifest {manifest.manifest_path}")
        else:
            _info(
                f'Created draft manifest "{new_manifest_path}" (rename to "{manifest.manifest_path}"'
                " to activate it)"
            )
    else:
        _info("No manifest changes detected; skipping update of manifest.")

    removals: int = sum((len(m.remove_paths_on_success) for m in migrations), 0)
    renames: int = sum((len(m.rename_paths_on_success) for m in migrations), 0)

    if renames:
        if permit_destructive_changes:
            _info("Paths being renamed:")
        else:
            _info("Migration *would* rename the following paths:")
        for previous_path, new_path in (
            p for m in migrations for p in m.rename_paths_on_success
        ):
            _info(f"   mv {escape_shell(previous_path, new_path)}")

    if removals:
        if permit_destructive_changes:
            _info("Removals:")
        else:
            _info("Migration *would* remove the following files:")
        for path in (p for m in migrations for p in m.remove_paths_on_success):
            _info(f"  rm -f {escape_shell(path)}")

    if permit_destructive_changes is None:
        print()
        _info(
            "If you would like to perform the migration, please re-run with --apply-changes."
        )
    elif permit_destructive_changes:
        for previous_path, new_path in (
            p for m in migrations for p in m.rename_paths_on_success
        ):
            os.rename(previous_path, new_path)
        for path in (p for m in migrations for p in m.remove_paths_on_success):
            os.unlink(path)

        print()
        _info("Migrations performed successfully")
        print()
        _info(
            "Remember to validate the resulting binary packages after rebuilding with debputy"
        )
    else:
        print()
        _info("No migrations performed as requested")
