from typing import Callable, List, Mapping, Protocol, Optional

from debputy.commands.debputy_cmd.output import OutputStylingBase
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
    detect_dh_addons_with_zz_integration,
    migrate_not_installed_file,
    migrate_installman_file,
    migrate_bash_completion,
    migrate_installinfo_file,
    migrate_dh_installsystemd_files,
    detect_obsolete_substvars,
    detect_dh_addons_zz_debputy_rrr,
    detect_dh_addons_with_full_integration,
)
from debputy.dh_migration.models import AcceptableMigrationIssues, FeatureMigration
from debputy.highlevel_manifest import HighLevelManifest
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.spec import (
    DebputyIntegrationMode,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
    INTEGRATION_MODE_DH_DEBPUTY,
    INTEGRATION_MODE_FULL,
)

Migrator = Callable[
    [
        VirtualPath,
        HighLevelManifest,
        AcceptableMigrationIssues,
        FeatureMigration,
        DebputyIntegrationMode,
    ],
    None,
]

_DH_DEBPUTY_MIGRATORS = [
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
    detect_obsolete_substvars,
    # not-installed should go last, so its rules appear after other installations
    # It is not perfect, but it is a start.
    migrate_not_installed_file,
]

MIGRATORS: Mapping[DebputyIntegrationMode, List[Migrator]] = {
    INTEGRATION_MODE_DH_DEBPUTY_RRR: [
        migrate_dh_hook_targets,
        migrate_misspelled_readme_debian_files,
        detect_dh_addons_zz_debputy_rrr,
        detect_obsolete_substvars,
    ],
    INTEGRATION_MODE_DH_DEBPUTY: [
        *_DH_DEBPUTY_MIGRATORS,
        detect_dh_addons_with_zz_integration,
    ],
    INTEGRATION_MODE_FULL: [
        *_DH_DEBPUTY_MIGRATORS,
        detect_dh_addons_with_full_integration,
    ],
}
del _DH_DEBPUTY_MIGRATORS
