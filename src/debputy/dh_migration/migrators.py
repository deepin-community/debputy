from typing import Callable, List, Mapping

from debputy.dh_migration.migrators_impl import (
    migrate_links_files,
    migrate_maintscript,
    migrate_tmpfile,
    migrate_install_file,
    migrate_installdocs_file,
    migrate_installexamples_file,
    migrate_dh_hook_targets,
    migrate_misspelled_readme_debian_files,
    migrate_doc_base_files,
    migrate_lintian_overrides_files,
    detect_unsupported_zz_debputy_features,
    detect_pam_files,
    detect_dh_addons,
    migrate_not_installed_file,
    migrate_installman_file,
    migrate_bash_completion,
    migrate_installinfo_file,
    migrate_dh_installsystemd_files,
    detect_obsolete_substvars,
    detect_dh_addons_zz_debputy_rrr,
    MIGRATION_TARGET_DH_DEBPUTY,
    MIGRATION_TARGET_DH_DEBPUTY_RRR,
)
from debputy.dh_migration.models import AcceptableMigrationIssues, FeatureMigration
from debputy.highlevel_manifest import HighLevelManifest
from debputy.plugin.api import VirtualPath

Migrator = Callable[
    [VirtualPath, HighLevelManifest, AcceptableMigrationIssues, FeatureMigration, str],
    None,
]


MIGRATORS: Mapping[str, List[Migrator]] = {
    MIGRATION_TARGET_DH_DEBPUTY_RRR: [
        migrate_dh_hook_targets,
        migrate_misspelled_readme_debian_files,
        detect_dh_addons_zz_debputy_rrr,
        detect_obsolete_substvars,
    ],
    MIGRATION_TARGET_DH_DEBPUTY: [
        detect_unsupported_zz_debputy_features,
        detect_pam_files,
        migrate_dh_hook_targets,
        migrate_dh_installsystemd_files,
        migrate_install_file,
        migrate_installdocs_file,
        migrate_installexamples_file,
        migrate_installman_file,
        migrate_installinfo_file,
        migrate_misspelled_readme_debian_files,
        migrate_doc_base_files,
        migrate_links_files,
        migrate_maintscript,
        migrate_tmpfile,
        migrate_lintian_overrides_files,
        migrate_bash_completion,
        detect_dh_addons,
        detect_obsolete_substvars,
        # not-installed should go last, so its rules appear after other installations
        # It is not perfect, but it is a start.
        migrate_not_installed_file,
    ],
}
