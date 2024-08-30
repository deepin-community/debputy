import collections
import dataclasses
import functools
import json
import os
import re
import subprocess
from typing import (
    Iterable,
    Optional,
    Tuple,
    List,
    Set,
    Mapping,
    Any,
    Union,
    Callable,
    TypeVar,
    Dict,
    Container,
)

from debian.deb822 import Deb822

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.architecture_support import dpkg_architecture_table
from debputy.commands.debputy_cmd.output import OutputStylingBase
from debputy.deb_packaging_support import dpkg_field_list_pkg_dep
from debputy.dh.debhelper_emulation import (
    dhe_filedoublearray,
    DHConfigFileLine,
    dhe_pkgfile,
)
from debputy.dh.dh_assistant import (
    read_dh_addon_sequences,
)
from debputy.dh_migration.models import (
    ConflictingChange,
    FeatureMigration,
    UnsupportedFeature,
    AcceptableMigrationIssues,
    DHMigrationSubstitution,
)
from debputy.highlevel_manifest import (
    MutableYAMLSymlink,
    HighLevelManifest,
    MutableYAMLConffileManagementItem,
    AbstractMutableYAMLInstallRule,
)
from debputy.installations import MAN_GUESS_FROM_BASENAME, MAN_GUESS_LANG_FROM_PATH
from debputy.packages import BinaryPackage
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.spec import (
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
    INTEGRATION_MODE_DH_DEBPUTY,
    DebputyIntegrationMode,
    INTEGRATION_MODE_FULL,
)
from debputy.util import (
    _error,
    PKGVERSION_REGEX,
    PKGNAME_REGEX,
    _normalize_path,
    assume_not_none,
    has_glob_magic,
)


class ContainsEverything:

    def __contains__(self, item: str) -> bool:
        return True


# Align with debputy.py
DH_COMMANDS_REPLACED: Mapping[DebputyIntegrationMode, Container[str]] = {
    INTEGRATION_MODE_DH_DEBPUTY_RRR: frozenset(
        {
            "dh_fixperms",
            "dh_shlibdeps",
            "dh_gencontrol",
            "dh_md5sums",
            "dh_builddeb",
        }
    ),
    INTEGRATION_MODE_DH_DEBPUTY: frozenset(
        {
            "dh_install",
            "dh_installdocs",
            "dh_installchangelogs",
            "dh_installexamples",
            "dh_installman",
            "dh_installcatalogs",
            "dh_installcron",
            "dh_installdebconf",
            "dh_installemacsen",
            "dh_installifupdown",
            "dh_installinfo",
            "dh_installinit",
            "dh_installsysusers",
            "dh_installtmpfiles",
            "dh_installsystemd",
            "dh_installsystemduser",
            "dh_installmenu",
            "dh_installmime",
            "dh_installmodules",
            "dh_installlogcheck",
            "dh_installlogrotate",
            "dh_installpam",
            "dh_installppp",
            "dh_installudev",
            "dh_installgsettings",
            "dh_installinitramfs",
            "dh_installalternatives",
            "dh_bugfiles",
            "dh_ucf",
            "dh_lintian",
            "dh_icons",
            "dh_usrlocal",
            "dh_perl",
            "dh_link",
            "dh_installwm",
            "dh_installxfonts",
            "dh_strip_nondeterminism",
            "dh_compress",
            "dh_fixperms",
            "dh_dwz",
            "dh_strip",
            "dh_makeshlibs",
            "dh_shlibdeps",
            "dh_missing",
            "dh_installdeb",
            "dh_gencontrol",
            "dh_md5sums",
            "dh_builddeb",
        }
    ),
    INTEGRATION_MODE_FULL: ContainsEverything(),
}

_GS_DOC = f"{DEBPUTY_DOC_ROOT_DIR}/GETTING-STARTED-WITH-dh-debputy.md"
MIGRATION_AID_FOR_OVERRIDDEN_COMMANDS = {
    "dh_installinit": f"{_GS_DOC}#covert-your-overrides-for-dh_installsystemd-dh_installinit-if-any",
    "dh_installsystemd": f"{_GS_DOC}#covert-your-overrides-for-dh_installsystemd-dh_installinit-if-any",
    "dh_fixperms": f"{_GS_DOC}#convert-your-overrides-or-excludes-for-dh_fixperms-if-any",
    "dh_gencontrol": f"{_GS_DOC}#convert-your-overrides-for-dh_gencontrol-if-any",
}


@dataclasses.dataclass(frozen=True, slots=True)
class UnsupportedDHConfig:
    dh_config_basename: str
    dh_tool: str
    bug_950723_prefix_matching: bool = False
    is_missing_migration: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class DHSequenceMigration:
    debputy_plugin: str
    remove_dh_sequence: bool = True
    must_use_zz_debputy: bool = False


UNSUPPORTED_DH_CONFIGS_AND_TOOLS_FOR_ZZ_DEBPUTY = [
    UnsupportedDHConfig("config", "dh_installdebconf"),
    UnsupportedDHConfig("templates", "dh_installdebconf"),
    UnsupportedDHConfig("emacsen-compat", "dh_installemacsen"),
    UnsupportedDHConfig("emacsen-install", "dh_installemacsen"),
    UnsupportedDHConfig("emacsen-remove", "dh_installemacsen"),
    UnsupportedDHConfig("emacsen-startup", "dh_installemacsen"),
    # The `upstart` file should be long dead, but we might as well detect it.
    UnsupportedDHConfig("upstart", "dh_installinit"),
    # dh_installsystemduser
    UnsupportedDHConfig(
        "user.path", "dh_installsystemduser", bug_950723_prefix_matching=False
    ),
    UnsupportedDHConfig(
        "user.path", "dh_installsystemduser", bug_950723_prefix_matching=True
    ),
    UnsupportedDHConfig(
        "user.service", "dh_installsystemduser", bug_950723_prefix_matching=False
    ),
    UnsupportedDHConfig(
        "user.service", "dh_installsystemduser", bug_950723_prefix_matching=True
    ),
    UnsupportedDHConfig(
        "user.socket", "dh_installsystemduser", bug_950723_prefix_matching=False
    ),
    UnsupportedDHConfig(
        "user.socket", "dh_installsystemduser", bug_950723_prefix_matching=True
    ),
    UnsupportedDHConfig(
        "user.target", "dh_installsystemduser", bug_950723_prefix_matching=False
    ),
    UnsupportedDHConfig(
        "user.target", "dh_installsystemduser", bug_950723_prefix_matching=True
    ),
    UnsupportedDHConfig(
        "user.timer", "dh_installsystemduser", bug_950723_prefix_matching=False
    ),
    UnsupportedDHConfig(
        "user.timer", "dh_installsystemduser", bug_950723_prefix_matching=True
    ),
    UnsupportedDHConfig("udev", "dh_installudev"),
    UnsupportedDHConfig("menu", "dh_installmenu"),
    UnsupportedDHConfig("menu-method", "dh_installmenu"),
    UnsupportedDHConfig("ucf", "dh_ucf"),
    UnsupportedDHConfig("wm", "dh_installwm"),
    UnsupportedDHConfig("triggers", "dh_installdeb"),
    UnsupportedDHConfig("postinst", "dh_installdeb"),
    UnsupportedDHConfig("postrm", "dh_installdeb"),
    UnsupportedDHConfig("preinst", "dh_installdeb"),
    UnsupportedDHConfig("prerm", "dh_installdeb"),
    UnsupportedDHConfig("menutest", "dh_installdeb"),
    UnsupportedDHConfig("isinstallable", "dh_installdeb"),
]
SUPPORTED_DH_ADDONS = frozenset(
    {
        # debputy's own
        "debputy",
        "zz-debputy",
        # debhelper provided sequences that should work.
        "single-binary",
    }
)
DH_ADDONS_TO_REMOVE = frozenset(
    [
        # Sequences debputy directly replaces
        "dwz",
        "elf-tools",
        "installinitramfs",
        "installsysusers",
        "doxygen",
        # Sequences that are embedded fully into debputy
        "bash-completion",
        "sodeps",
    ]
)
DH_ADDONS_TO_PLUGINS = {
    "gnome": DHSequenceMigration(
        "gnome",
        # The sequence still provides a command for the clean sequence
        remove_dh_sequence=False,
        must_use_zz_debputy=True,
    ),
    "grantlee": DHSequenceMigration(
        "grantlee",
        remove_dh_sequence=True,
        must_use_zz_debputy=True,
    ),
    "numpy3": DHSequenceMigration(
        "numpy3",
        # The sequence provides (build-time) dependencies that we cannot provide
        remove_dh_sequence=False,
        must_use_zz_debputy=True,
    ),
    "perl-openssl": DHSequenceMigration(
        "perl-openssl",
        # The sequence provides (build-time) dependencies that we cannot provide
        remove_dh_sequence=False,
        must_use_zz_debputy=True,
    ),
}


def _dh_config_file(
    debian_dir: VirtualPath,
    dctrl_bin: BinaryPackage,
    basename: str,
    helper_name: str,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    manifest: HighLevelManifest,
    support_executable_files: bool = False,
    allow_dh_exec_rename: bool = False,
    pkgfile_lookup: bool = True,
    remove_on_migration: bool = True,
) -> Union[Tuple[None, None], Tuple[VirtualPath, Iterable[DHConfigFileLine]]]:
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    dh_config_file = (
        dhe_pkgfile(debian_dir, dctrl_bin, basename)
        if pkgfile_lookup
        else debian_dir.get(basename)
    )
    if dh_config_file is None or dh_config_file.is_dir:
        return None, None
    if dh_config_file.is_executable and not support_executable_files:
        primary_key = f"executable-{helper_name}-config"
        if (
            primary_key in acceptable_migration_issues
            or "any-executable-dh-configs" in acceptable_migration_issues
        ):
            feature_migration.warn(
                f'TODO: MANUAL MIGRATION of executable dh config "{dh_config_file}" is required.'
            )
            return None, None
        raise UnsupportedFeature(
            f"Executable configuration files not supported (found: {dh_config_file}).",
            [primary_key, "any-executable-dh-configs"],
        )

    if remove_on_migration:
        feature_migration.remove_on_success(dh_config_file.fs_path)
    substitution = DHMigrationSubstitution(
        dpkg_architecture_table(),
        acceptable_migration_issues,
        feature_migration,
        mutable_manifest,
    )
    content = dhe_filedoublearray(
        dh_config_file,
        substitution,
        allow_dh_exec_rename=allow_dh_exec_rename,
    )
    return dh_config_file, content


def _validate_rm_mv_conffile(
    package: str,
    config_line: DHConfigFileLine,
) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    cmd, *args = config_line.tokens
    if "--" in config_line.tokens:
        raise ValueError(
            f'The maintscripts file "{config_line.config_file.path}" for {package} includes a "--" in line'
            f" {config_line.line_no}. The offending line is: {config_line.original_line}"
        )
    if cmd == "rm_conffile":
        min_args = 1
        max_args = 3
    else:
        min_args = 2
        max_args = 4
    if len(args) > max_args or len(args) < min_args:
        raise ValueError(
            f'The "{cmd}" command takes at least {min_args} and at most {max_args} arguments.  However,'
            f' in "{config_line.config_file.path}" line {config_line.line_no} (for {package}), there'
            f" are {len(args)} arguments. The offending line is: {config_line.original_line}"
        )

    obsolete_conffile = args[0]
    new_conffile = args[1] if cmd == "mv_conffile" else None
    prior_version = args[min_args] if len(args) > min_args else None
    owning_package = args[min_args + 1] if len(args) > min_args + 1 else None
    if not obsolete_conffile.startswith("/"):
        raise ValueError(
            f'The (old-)conffile parameter for {cmd} must be absolute (i.e., start with "/").  However,'
            f' in "{config_line.config_file.path}" line {config_line.line_no} (for {package}), it was specified'
            f' as "{obsolete_conffile}". The offending line is: {config_line.original_line}'
        )
    if new_conffile is not None and not new_conffile.startswith("/"):
        raise ValueError(
            f'The new-conffile parameter for {cmd} must be absolute (i.e., start with "/").  However,'
            f' in "{config_line.config_file.path}" line {config_line.line_no} (for {package}), it was specified'
            f' as "{new_conffile}". The offending line is: {config_line.original_line}'
        )
    if prior_version is not None and not PKGVERSION_REGEX.fullmatch(prior_version):
        raise ValueError(
            f"The prior-version parameter for {cmd} must be a valid package version (i.e., match"
            f' {PKGVERSION_REGEX}).  However, in "{config_line.config_file.path}" line {config_line.line_no}'
            f' (for {package}), it was specified as "{prior_version}". The offending line is:'
            f" {config_line.original_line}"
        )
    if owning_package is not None and not PKGNAME_REGEX.fullmatch(owning_package):
        raise ValueError(
            f"The package parameter for {cmd} must be a valid package name (i.e., match {PKGNAME_REGEX})."
            f'  However, in "{config_line.config_file.path}" line {config_line.line_no} (for {package}), it'
            f' was specified as "{owning_package}". The offending line is: {config_line.original_line}'
        )
    return cmd, obsolete_conffile, new_conffile, prior_version, owning_package


_BASH_COMPLETION_RE = re.compile(
    r"""
      (^|[|&;])\s*complete.*-[A-Za-z].*
    | \$\(.*\)
    | \s*compgen.*-[A-Za-z].*
    | \s*if.*;.*then/
""",
    re.VERBOSE,
)


def migrate_bash_completion(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_bash-completion files"
    is_single_binary = sum(1 for _ in manifest.all_packages) == 1
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)

    for dctrl_bin in manifest.all_packages:
        dh_file = dhe_pkgfile(debian_dir, dctrl_bin, "bash-completion")
        if dh_file is None:
            continue
        is_bash_completion_file = False
        with dh_file.open() as fd:
            for line in fd:
                line = line.strip()
                if not line or line[0] == "#":
                    continue
                if _BASH_COMPLETION_RE.search(line):
                    is_bash_completion_file = True
                    break
        if not is_bash_completion_file:
            _, content = _dh_config_file(
                debian_dir,
                dctrl_bin,
                "bash-completion",
                "dh_bash-completion",
                acceptable_migration_issues,
                feature_migration,
                manifest,
                support_executable_files=True,
            )
        else:
            content = None

        if content:
            install_dest_sources: List[str] = []
            install_as_rules: List[Tuple[str, str]] = []
            for dhe_line in content:
                if len(dhe_line.tokens) > 2:
                    raise UnsupportedFeature(
                        f"The dh_bash-completion file {dh_file.path} more than two words on"
                        f' line {dhe_line.line_no} (line: "{dhe_line.original_line}").'
                    )
                source = dhe_line.tokens[0]
                dest_basename = (
                    dhe_line.tokens[1]
                    if len(dhe_line.tokens) > 1
                    else os.path.basename(source)
                )
                if source.startswith("debian/") and not has_glob_magic(source):
                    if dctrl_bin.name != dest_basename:
                        dest_path = (
                            f"debian/{dctrl_bin.name}.{dest_basename}.bash-completion"
                        )
                    else:
                        dest_path = f"debian/{dest_basename}.bash-completion"
                    feature_migration.rename_on_success(source, dest_path)
                elif len(dhe_line.tokens) == 1:
                    install_dest_sources.append(source)
                else:
                    install_as_rules.append((source, dest_basename))

            if install_dest_sources:
                sources: Union[List[str], str] = (
                    install_dest_sources
                    if len(install_dest_sources) > 1
                    else install_dest_sources[0]
                )
                installations.append(
                    AbstractMutableYAMLInstallRule.install_dest(
                        sources=sources,
                        dest_dir="{{path:BASH_COMPLETION_DIR}}",
                        into=dctrl_bin.name if not is_single_binary else None,
                    )
                )

            for source, dest_basename in install_as_rules:
                installations.append(
                    AbstractMutableYAMLInstallRule.install_as(
                        source=source,
                        install_as="{{path:BASH_COMPLETION_DIR}}/" + dest_basename,
                        into=dctrl_bin.name if not is_single_binary else None,
                    )
                )


def migrate_dh_installsystemd_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installsystemd files"
    for dctrl_bin in manifest.all_packages:
        for stem in [
            "path",
            "service",
            "socket",
            "target",
            "timer",
        ]:
            pkgfile = dhe_pkgfile(
                debian_dir, dctrl_bin, stem, bug_950723_prefix_matching=True
            )
            if not pkgfile:
                continue
            if not pkgfile.name.endswith(f".{stem}") or "@." not in pkgfile.name:
                raise UnsupportedFeature(
                    f'Unable to determine the correct name for {pkgfile.fs_path}. It should be a ".@{stem}"'
                    f" file now (foo@.service => foo.@service)"
                )
            newname = pkgfile.name.replace("@.", ".")
            newname = newname[: -len(stem)] + f"@{stem}"
            feature_migration.rename_on_success(
                pkgfile.fs_path, os.path.join(debian_dir.fs_path, newname)
            )


def migrate_maintscript(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installdeb files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    for dctrl_bin in manifest.all_packages:
        mainscript_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "maintscript",
            "dh_installdeb",
            acceptable_migration_issues,
            feature_migration,
            manifest,
        )

        if mainscript_file is None:
            continue
        assert content is not None

        package_definition = mutable_manifest.package(dctrl_bin.name)
        conffiles = {
            it.obsolete_conffile: it
            for it in package_definition.conffile_management_items()
        }
        seen_conffiles = set()

        for dhe_line in content:
            cmd = dhe_line.tokens[0]
            if cmd not in {"rm_conffile", "mv_conffile"}:
                raise UnsupportedFeature(
                    f"The dh_installdeb file {mainscript_file.path} contains the (currently)"
                    f' unsupported command "{cmd}" on line {dhe_line.line_no}'
                    f' (line: "{dhe_line.original_line}")'
                )

            try:
                (
                    _,
                    obsolete_conffile,
                    new_conffile,
                    prior_to_version,
                    owning_package,
                ) = _validate_rm_mv_conffile(dctrl_bin.name, dhe_line)
            except ValueError as e:
                _error(
                    f"Validation error in {mainscript_file} on line {dhe_line.line_no}. The error was: {e.args[0]}."
                )

            if obsolete_conffile in seen_conffiles:
                raise ConflictingChange(
                    f'The {mainscript_file} file defines actions for "{obsolete_conffile}" twice!'
                    f" Please ensure that it is defined at most once in that file."
                )
            seen_conffiles.add(obsolete_conffile)

            if cmd == "rm_conffile":
                item = MutableYAMLConffileManagementItem.rm_conffile(
                    obsolete_conffile,
                    prior_to_version,
                    owning_package,
                )
            else:
                assert cmd == "mv_conffile"
                item = MutableYAMLConffileManagementItem.mv_conffile(
                    obsolete_conffile,
                    assume_not_none(new_conffile),
                    prior_to_version,
                    owning_package,
                )

            existing_def = conffiles.get(item.obsolete_conffile)
            if existing_def is not None:
                if not (
                    item.command == existing_def.command
                    and item.new_conffile == existing_def.new_conffile
                    and item.prior_to_version == existing_def.prior_to_version
                    and item.owning_package == existing_def.owning_package
                ):
                    raise ConflictingChange(
                        f"The maintscript defines the action {item.command} for"
                        f' "{obsolete_conffile}" in {mainscript_file}, but there is another'
                        f" conffile management definition for same path defined already (in the"
                        f" existing manifest or an migration e.g., inside {mainscript_file})"
                    )
                feature_migration.already_present += 1
                continue

            package_definition.add_conffile_management(item)
            feature_migration.successful_manifest_changes += 1


@dataclasses.dataclass(slots=True)
class SourcesAndConditional:
    dest_dir: Optional[str] = None
    sources: List[str] = dataclasses.field(default_factory=list)
    conditional: Optional[Union[str, Mapping[str, Any]]] = None


def _strip_d_tmp(p: str) -> str:
    if p.startswith("debian/tmp/") and len(p) > 11:
        return p[11:]
    return p


def migrate_install_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_install config files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)
    priority_lines = []
    remaining_install_lines = []
    warn_about_fixmes_in_dest_dir = False

    is_single_binary = sum(1 for _ in manifest.all_packages) == 1

    for dctrl_bin in manifest.all_packages:
        install_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "install",
            "dh_install",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
            allow_dh_exec_rename=True,
        )
        if not install_file or not content:
            continue
        current_sources = []
        sources_by_destdir: Dict[Tuple[str, Tuple[str, ...]], SourcesAndConditional] = (
            {}
        )
        install_as_rules = []
        multi_dest = collections.defaultdict(list)
        seen_sources = set()
        multi_dest_sources: Set[str] = set()

        for dhe_line in content:
            special_rule = None
            if "=>" in dhe_line.tokens:
                if dhe_line.tokens[0] == "=>" and len(dhe_line.tokens) == 2:
                    # This rule must be as early as possible to retain the semantics
                    path = _strip_d_tmp(
                        _normalize_path(dhe_line.tokens[1], with_prefix=False)
                    )
                    special_rule = AbstractMutableYAMLInstallRule.install_dest(
                        path,
                        dctrl_bin.name if not is_single_binary else None,
                        dest_dir=None,
                        when=dhe_line.conditional(),
                    )
                elif len(dhe_line.tokens) != 3:
                    _error(
                        f"Validation error in {install_file.path} on line {dhe_line.line_no}. Cannot migrate dh-exec"
                        ' renames that is not exactly "SOURCE => TARGET" or "=> TARGET".'
                    )
                else:
                    install_rule = AbstractMutableYAMLInstallRule.install_as(
                        _strip_d_tmp(
                            _normalize_path(dhe_line.tokens[0], with_prefix=False)
                        ),
                        _normalize_path(dhe_line.tokens[2], with_prefix=False),
                        dctrl_bin.name if not is_single_binary else None,
                        when=dhe_line.conditional(),
                    )
                    install_as_rules.append(install_rule)
            else:
                if len(dhe_line.tokens) > 1:
                    sources = list(
                        _strip_d_tmp(_normalize_path(w, with_prefix=False))
                        for w in dhe_line.tokens[:-1]
                    )
                    dest_dir = _normalize_path(dhe_line.tokens[-1], with_prefix=False)
                else:
                    sources = list(
                        _strip_d_tmp(_normalize_path(w, with_prefix=False))
                        for w in dhe_line.tokens
                    )
                    dest_dir = None

                multi_dest_sources.update(s for s in sources if s in seen_sources)
                seen_sources.update(sources)

                if dest_dir is None and dhe_line.conditional() is None:
                    current_sources.extend(sources)
                    continue
                key = (dest_dir, dhe_line.conditional_key())
                ctor = functools.partial(
                    SourcesAndConditional,
                    dest_dir=dest_dir,
                    conditional=dhe_line.conditional(),
                )
                md = _fetch_or_create(
                    sources_by_destdir,
                    key,
                    ctor,
                )
                md.sources.extend(sources)

            if special_rule:
                priority_lines.append(special_rule)

        remaining_install_lines.extend(install_as_rules)

        for md in sources_by_destdir.values():
            if multi_dest_sources:
                sources = [s for s in md.sources if s not in multi_dest_sources]
                already_installed = (s for s in md.sources if s in multi_dest_sources)
                for s in already_installed:
                    # The sources are ignored, so we can reuse the object as-is
                    multi_dest[s].append(md)
                if not sources:
                    continue
            else:
                sources = md.sources
            install_rule = AbstractMutableYAMLInstallRule.install_dest(
                sources[0] if len(sources) == 1 else sources,
                dctrl_bin.name if not is_single_binary else None,
                dest_dir=md.dest_dir,
                when=md.conditional,
            )
            remaining_install_lines.append(install_rule)

        if current_sources:
            if multi_dest_sources:
                sources = [s for s in current_sources if s not in multi_dest_sources]
                already_installed = (
                    s for s in current_sources if s in multi_dest_sources
                )
                for s in already_installed:
                    # The sources are ignored, so we can reuse the object as-is
                    dest_dir = os.path.dirname(s)
                    if has_glob_magic(dest_dir):
                        warn_about_fixmes_in_dest_dir = True
                        dest_dir = f"FIXME: {dest_dir} (could not reliably compute the dest dir)"
                    multi_dest[s].append(
                        SourcesAndConditional(
                            dest_dir=dest_dir,
                            conditional=None,
                        )
                    )
            else:
                sources = current_sources

            if sources:
                install_rule = AbstractMutableYAMLInstallRule.install_dest(
                    sources[0] if len(sources) == 1 else sources,
                    dctrl_bin.name if not is_single_binary else None,
                    dest_dir=None,
                )
                remaining_install_lines.append(install_rule)

        if multi_dest:
            for source, dest_and_conditionals in multi_dest.items():
                dest_dirs = [dac.dest_dir for dac in dest_and_conditionals]
                # We assume the conditional is the same.
                conditional = next(
                    iter(
                        dac.conditional
                        for dac in dest_and_conditionals
                        if dac.conditional is not None
                    ),
                    None,
                )
                remaining_install_lines.append(
                    AbstractMutableYAMLInstallRule.multi_dest_install(
                        source,
                        dest_dirs,
                        dctrl_bin.name if not is_single_binary else None,
                        when=conditional,
                    )
                )

    if priority_lines:
        installations.extend(priority_lines)

    if remaining_install_lines:
        installations.extend(remaining_install_lines)

    feature_migration.successful_manifest_changes += len(priority_lines) + len(
        remaining_install_lines
    )
    if warn_about_fixmes_in_dest_dir:
        feature_migration.warn(
            "TODO: FIXME left in dest-dir(s) of some installation rules."
            " Please review these and remove the FIXME (plus correct as necessary)"
        )


def migrate_installdocs_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installdocs config files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)

    is_single_binary = sum(1 for _ in manifest.all_packages) == 1

    for dctrl_bin in manifest.all_packages:
        install_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "docs",
            "dh_installdocs",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
        )
        if not install_file:
            continue
        assert content is not None
        docs: List[str] = []
        for dhe_line in content:
            if dhe_line.arch_filter or dhe_line.build_profile_filter:
                _error(
                    f"Unable to migrate line {dhe_line.line_no} of {install_file.path}."
                    " Missing support for conditions."
                )
            docs.extend(_normalize_path(w, with_prefix=False) for w in dhe_line.tokens)

        if not docs:
            continue
        feature_migration.successful_manifest_changes += 1
        install_rule = AbstractMutableYAMLInstallRule.install_docs(
            docs if len(docs) > 1 else docs[0],
            dctrl_bin.name if not is_single_binary else None,
        )
        installations.create_definition_if_missing()
        installations.append(install_rule)


def migrate_installexamples_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installexamples config files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)
    is_single_binary = sum(1 for _ in manifest.all_packages) == 1

    for dctrl_bin in manifest.all_packages:
        install_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "examples",
            "dh_installexamples",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
        )
        if not install_file:
            continue
        assert content is not None
        examples: List[str] = []
        for dhe_line in content:
            if dhe_line.arch_filter or dhe_line.build_profile_filter:
                _error(
                    f"Unable to migrate line {dhe_line.line_no} of {install_file.path}."
                    " Missing support for conditions."
                )
            examples.extend(
                _normalize_path(w, with_prefix=False) for w in dhe_line.tokens
            )

        if not examples:
            continue
        feature_migration.successful_manifest_changes += 1
        install_rule = AbstractMutableYAMLInstallRule.install_examples(
            examples if len(examples) > 1 else examples[0],
            dctrl_bin.name if not is_single_binary else None,
        )
        installations.create_definition_if_missing()
        installations.append(install_rule)


@dataclasses.dataclass(slots=True)
class InfoFilesDefinition:
    sources: List[str] = dataclasses.field(default_factory=list)
    conditional: Optional[Union[str, Mapping[str, Any]]] = None


def migrate_installinfo_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installinfo config files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)
    is_single_binary = sum(1 for _ in manifest.all_packages) == 1

    for dctrl_bin in manifest.all_packages:
        info_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "info",
            "dh_installinfo",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
        )
        if not info_file:
            continue
        assert content is not None
        info_files_by_condition: Dict[Tuple[str, ...], InfoFilesDefinition] = {}
        for dhe_line in content:
            key = dhe_line.conditional_key()
            ctr = functools.partial(
                InfoFilesDefinition, conditional=dhe_line.conditional()
            )
            info_def = _fetch_or_create(
                info_files_by_condition,
                key,
                ctr,
            )
            info_def.sources.extend(
                _normalize_path(w, with_prefix=False) for w in dhe_line.tokens
            )

        if not info_files_by_condition:
            continue
        feature_migration.successful_manifest_changes += 1
        installations.create_definition_if_missing()
        for info_def in info_files_by_condition.values():
            info_files = info_def.sources
            install_rule = AbstractMutableYAMLInstallRule.install_docs(
                info_files if len(info_files) > 1 else info_files[0],
                dctrl_bin.name if not is_single_binary else None,
                dest_dir="{{path:GNU_INFO_DIR}}",
                when=info_def.conditional,
            )
            installations.append(install_rule)


@dataclasses.dataclass(slots=True)
class ManpageDefinition:
    sources: List[str] = dataclasses.field(default_factory=list)
    language: Optional[str] = None
    conditional: Optional[Union[str, Mapping[str, Any]]] = None


DK = TypeVar("DK")
DV = TypeVar("DV")


def _fetch_or_create(d: Dict[DK, DV], key: DK, factory: Callable[[], DV]) -> DV:
    v = d.get(key)
    if v is None:
        v = factory()
        d[key] = v
    return v


def migrate_installman_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installman config files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)
    is_single_binary = sum(1 for _ in manifest.all_packages) == 1
    warn_about_basename = False

    for dctrl_bin in manifest.all_packages:
        manpages_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "manpages",
            "dh_installman",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
            allow_dh_exec_rename=True,
        )
        if not manpages_file:
            continue
        assert content is not None

        vanilla_definitions = []
        install_as_rules = []
        complex_definitions: Dict[
            Tuple[Optional[str], Tuple[str, ...]], ManpageDefinition
        ] = {}
        install_rule: AbstractMutableYAMLInstallRule
        for dhe_line in content:
            if "=>" in dhe_line.tokens:
                # dh-exec allows renaming features.  For `debputy`, we degenerate it into an `install` (w. `as`) feature
                # without any of the `install-man` features.
                if dhe_line.tokens[0] == "=>" and len(dhe_line.tokens) == 2:
                    _error(
                        f'Unsupported "=> DEST" rule for error in {manpages_file.path} on line {dhe_line.line_no}."'
                        f' Cannot migrate dh-exec renames that is not exactly "SOURCE => TARGET" for d/manpages files.'
                    )
                elif len(dhe_line.tokens) != 3:
                    _error(
                        f"Validation error in {manpages_file.path} on line {dhe_line.line_no}. Cannot migrate dh-exec"
                        ' renames that is not exactly "SOURCE => TARGET" or "=> TARGET".'
                    )
                else:
                    install_rule = AbstractMutableYAMLInstallRule.install_doc_as(
                        _normalize_path(dhe_line.tokens[0], with_prefix=False),
                        _normalize_path(dhe_line.tokens[2], with_prefix=False),
                        dctrl_bin.name if not is_single_binary else None,
                        when=dhe_line.conditional(),
                    )
                    install_as_rules.append(install_rule)
                continue

            sources = [_normalize_path(w, with_prefix=False) for w in dhe_line.tokens]
            needs_basename = any(
                MAN_GUESS_FROM_BASENAME.search(x)
                and not MAN_GUESS_LANG_FROM_PATH.search(x)
                for x in sources
            )
            if needs_basename or dhe_line.conditional() is not None:
                if needs_basename:
                    warn_about_basename = True
                    language = "derive-from-basename"
                else:
                    language = None
                key = (language, dhe_line.conditional_key())
                ctor = functools.partial(
                    ManpageDefinition,
                    language=language,
                    conditional=dhe_line.conditional(),
                )
                manpage_def = _fetch_or_create(
                    complex_definitions,
                    key,
                    ctor,
                )
                manpage_def.sources.extend(sources)
            else:
                vanilla_definitions.extend(sources)

        if not install_as_rules and not vanilla_definitions and not complex_definitions:
            continue
        feature_migration.successful_manifest_changes += 1
        installations.create_definition_if_missing()
        installations.extend(install_as_rules)
        if vanilla_definitions:
            man_source = (
                vanilla_definitions
                if len(vanilla_definitions) > 1
                else vanilla_definitions[0]
            )
            install_rule = AbstractMutableYAMLInstallRule.install_man(
                man_source,
                dctrl_bin.name if not is_single_binary else None,
                None,
            )
            installations.append(install_rule)
        for manpage_def in complex_definitions.values():
            sources = manpage_def.sources
            install_rule = AbstractMutableYAMLInstallRule.install_man(
                sources if len(sources) > 1 else sources[0],
                dctrl_bin.name if not is_single_binary else None,
                manpage_def.language,
                when=manpage_def.conditional,
            )
            installations.append(install_rule)

    if warn_about_basename:
        feature_migration.warn(
            'Detected man pages that might rely on "derive-from-basename" logic.  Please double check'
            " that the generated `install-man` rules are correct"
        )


def migrate_not_installed_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_missing's not-installed config file"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    installations = mutable_manifest.installations(create_if_absent=False)
    main_binary = [p for p in manifest.all_packages if p.is_main_package][0]

    missing_file, content = _dh_config_file(
        debian_dir,
        main_binary,
        "not-installed",
        "dh_missing",
        acceptable_migration_issues,
        feature_migration,
        manifest,
        support_executable_files=False,
        pkgfile_lookup=False,
    )
    discard_rules: List[str] = []
    if missing_file:
        assert content is not None
        for dhe_line in content:
            discard_rules.extend(
                _normalize_path(w, with_prefix=False) for w in dhe_line.tokens
            )

    if discard_rules:
        feature_migration.successful_manifest_changes += 1
        install_rule = AbstractMutableYAMLInstallRule.discard(
            discard_rules if len(discard_rules) > 1 else discard_rules[0],
        )
        installations.create_definition_if_missing()
        installations.append(install_rule)


def detect_pam_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "detect dh_installpam files (min dh compat)"
    for dctrl_bin in manifest.all_packages:
        dh_config_file = dhe_pkgfile(debian_dir, dctrl_bin, "pam")
        if dh_config_file is not None:
            feature_migration.assumed_compat = 14
            break


def migrate_tmpfile(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_installtmpfiles config files"
    for dctrl_bin in manifest.all_packages:
        dh_config_file = dhe_pkgfile(debian_dir, dctrl_bin, "tmpfile")
        if dh_config_file is not None:
            target = (
                dh_config_file.name.replace(".tmpfile", ".tmpfiles")
                if "." in dh_config_file.name
                else "tmpfiles"
            )
            _rename_file_if_exists(
                debian_dir,
                dh_config_file.name,
                target,
                feature_migration,
            )


def migrate_lintian_overrides_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_lintian config files"
    for dctrl_bin in manifest.all_packages:
        # We do not support executable lintian-overrides and `_dh_config_file` handles all of that.
        # Therefore, the return value is irrelevant to us.
        _dh_config_file(
            debian_dir,
            dctrl_bin,
            "lintian-overrides",
            "dh_lintian",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=False,
            remove_on_migration=False,
        )


def migrate_links_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh_link files"
    mutable_manifest = assume_not_none(manifest.mutable_manifest)
    for dctrl_bin in manifest.all_packages:
        links_file, content = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "links",
            "dh_link",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=True,
        )

        if links_file is None:
            continue
        assert content is not None

        package_definition = mutable_manifest.package(dctrl_bin.name)
        defined_symlink = {
            symlink.symlink_path: symlink.symlink_target
            for symlink in package_definition.symlinks()
        }

        seen_symlinks: Set[str] = set()

        for dhe_line in content:
            if len(dhe_line.tokens) != 2:
                raise UnsupportedFeature(
                    f"The dh_link file {links_file.fs_path} did not have exactly two paths on line"
                    f' {dhe_line.line_no} (line: "{dhe_line.original_line}"'
                )
            target, source = dhe_line.tokens
            if source in seen_symlinks:
                # According to #934499, this has happened in the wild already
                raise ConflictingChange(
                    f"The {links_file.fs_path} file defines the link path {source} twice! Please ensure"
                    " that it is defined at most once in that file"
                )
            seen_symlinks.add(source)
            # Symlinks in .links are always considered absolute, but you were not required to have a leading slash.
            # However, in the debputy manifest, you can have relative links, so we should ensure it is explicitly
            # absolute.
            if not target.startswith("/"):
                target = "/" + target
            existing_target = defined_symlink.get(source)
            if existing_target is not None:
                if existing_target != target:
                    raise ConflictingChange(
                        f'The symlink "{source}" points to "{target}" in {links_file}, but there is'
                        f' another symlink with same path pointing to "{existing_target}" defined'
                        " already (in the existing manifest or an migration e.g., inside"
                        f" {links_file.fs_path})"
                    )
                feature_migration.already_present += 1
                continue
            condition = dhe_line.conditional()
            package_definition.add_symlink(
                MutableYAMLSymlink.new_symlink(
                    source,
                    target,
                    condition,
                )
            )
            feature_migration.successful_manifest_changes += 1


def migrate_misspelled_readme_debian_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "misspelled README.Debian files"
    for dctrl_bin in manifest.all_packages:
        readme, _ = _dh_config_file(
            debian_dir,
            dctrl_bin,
            "README.debian",
            "dh_installdocs",
            acceptable_migration_issues,
            feature_migration,
            manifest,
            support_executable_files=False,
            remove_on_migration=False,
        )
        if readme is None:
            continue
        new_name = readme.name.replace("README.debian", "README.Debian")
        assert readme.name != new_name
        _rename_file_if_exists(
            debian_dir,
            readme.name,
            new_name,
            feature_migration,
        )


def migrate_doc_base_files(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    _: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "doc-base files"
    # ignore the dh_make ".EX" file if one should still be present. The dh_installdocs tool ignores it too.
    possible_effected_doc_base_files = [
        f
        for f in debian_dir.iterdir
        if (
            (".doc-base." in f.name or f.name.startswith("doc-base."))
            and not f.name.endswith("doc-base.EX")
        )
    ]
    known_packages = {d.name: d for d in manifest.all_packages}
    main_package = [d for d in manifest.all_packages if d.is_main_package][0]
    for doc_base_file in possible_effected_doc_base_files:
        parts = doc_base_file.name.split(".")
        owning_package = known_packages.get(parts[0])
        if owning_package is None:
            owning_package = main_package
            package_part = None
        else:
            package_part = parts[0]
            parts = parts[1:]

        if not parts or parts[0] != "doc-base":
            # Not a doc-base file after all
            continue

        if len(parts) > 1:
            name_part = ".".join(parts[1:])
            if package_part is None:
                # Named files must have a package prefix
                package_part = owning_package.name
        else:
            # No rename needed
            continue

        new_basename = ".".join(filter(None, (package_part, name_part, "doc-base")))
        _rename_file_if_exists(
            debian_dir,
            doc_base_file.name,
            new_basename,
            feature_migration,
        )


def migrate_dh_hook_targets(
    debian_dir: VirtualPath,
    _: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "dh hook targets"
    source_root = os.path.dirname(debian_dir.fs_path)
    if source_root == "":
        source_root = "."
    detected_hook_targets = json.loads(
        subprocess.check_output(
            ["dh_assistant", "detect-hook-targets"],
            cwd=source_root,
        ).decode("utf-8")
    )
    sample_hook_target: Optional[str] = None
    replaced_commands = DH_COMMANDS_REPLACED[migration_target]

    for hook_target_def in detected_hook_targets["hook-targets"]:
        if hook_target_def["is-empty"]:
            continue
        command = hook_target_def["command"]
        if command not in replaced_commands:
            continue
        hook_target = hook_target_def["target-name"]
        advice = MIGRATION_AID_FOR_OVERRIDDEN_COMMANDS.get(command)
        if advice is None:
            if sample_hook_target is None:
                sample_hook_target = hook_target
            feature_migration.warn(
                f"TODO: MANUAL MIGRATION required for hook target {hook_target}"
            )
        else:
            feature_migration.warn(
                f"TODO: MANUAL MIGRATION required for hook target {hook_target}. Please see {advice}"
                f" for migration advice."
            )
    if (
        feature_migration.warnings
        and "dh-hook-targets" not in acceptable_migration_issues
        and sample_hook_target is not None
    ):
        raise UnsupportedFeature(
            f"The debian/rules file contains one or more non empty dh hook targets that will not"
            f" be run with the requested debputy dh sequence with no known migration advice. One of these would be"
            f" {sample_hook_target}.",
            ["dh-hook-targets"],
        )


def detect_unsupported_zz_debputy_features(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "Known unsupported features"

    for unsupported_config in UNSUPPORTED_DH_CONFIGS_AND_TOOLS_FOR_ZZ_DEBPUTY:
        _unsupported_debhelper_config_file(
            debian_dir,
            manifest,
            unsupported_config,
            acceptable_migration_issues,
            feature_migration,
        )


def detect_obsolete_substvars(
    debian_dir: VirtualPath,
    _manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = (
        "Check for obsolete ${foo:var} variables in debian/control"
    )
    ctrl_file = debian_dir.get("control")
    if not ctrl_file:
        feature_migration.warn(
            "Cannot find debian/control. Detection of obsolete substvars could not be performed."
        )
        return
    with ctrl_file.open() as fd:
        ctrl = list(Deb822.iter_paragraphs(fd))

    relationship_fields = dpkg_field_list_pkg_dep()
    relationship_fields_lc = frozenset(x.lower() for x in relationship_fields)

    for p in ctrl[1:]:
        seen_obsolete_relationship_substvars = set()
        obsolete_fields = set()
        is_essential = p.get("Essential") == "yes"
        for df in relationship_fields:
            field: Optional[str] = p.get(df)
            if field is None:
                continue
            df_lc = df.lower()
            number_of_relations = 0
            obsolete_substvars_in_field = set()
            for d in (d.strip() for d in field.strip().split(",")):
                if not d:
                    continue
                number_of_relations += 1
                if not d.startswith("${"):
                    continue
                try:
                    end_idx = d.index("}")
                except ValueError:
                    continue
                substvar_name = d[2:end_idx]
                if ":" not in substvar_name:
                    continue
                _, field = substvar_name.rsplit(":", 1)
                field_lc = field.lower()
                if field_lc not in relationship_fields_lc:
                    continue
                is_obsolete = field_lc == df_lc
                if (
                    not is_obsolete
                    and is_essential
                    and substvar_name.lower() == "shlibs:depends"
                    and df_lc == "pre-depends"
                ):
                    is_obsolete = True

                if is_obsolete:
                    obsolete_substvars_in_field.add(d)

            if number_of_relations == len(obsolete_substvars_in_field):
                obsolete_fields.add(df)
            else:
                seen_obsolete_relationship_substvars.update(obsolete_substvars_in_field)

        package = p.get("Package", "(Missing package name!?)")
        fo = feature_migration.fo
        if obsolete_fields:
            fields = ", ".join(obsolete_fields)
            feature_migration.warn(
                f"The following relationship fields can be removed from {package}: {fields}."
                f"  (The content in them would be applied automatically. Note: {fo.bts('1067653')})"
            )
        if seen_obsolete_relationship_substvars:
            v = ", ".join(sorted(seen_obsolete_relationship_substvars))
            feature_migration.warn(
                f"The following relationship substitution variables can be removed from {package}: {v}"
                f" (Note: {fo.bts('1067653')})"
            )


def detect_dh_addons_zz_debputy_rrr(
    debian_dir: VirtualPath,
    _manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "Check for dh-sequence-addons"
    r = read_dh_addon_sequences(debian_dir)
    if r is None:
        feature_migration.warn(
            "Cannot find debian/control. Detection of unsupported/missing dh-sequence addon"
            " could not be performed. Please ensure the package will Build-Depend on dh-sequence-zz-debputy-rrr."
        )
        return

    bd_sequences, dr_sequences, _ = r

    remaining_sequences = bd_sequences | dr_sequences
    saw_dh_debputy = "zz-debputy-rrr" in remaining_sequences

    if not saw_dh_debputy:
        feature_migration.warn("Missing Build-Depends on dh-sequence-zz-debputy-rrr")


def detect_dh_addons_with_full_integration(
    _debian_dir: VirtualPath,
    _manifest: HighLevelManifest,
    _acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "Check for dh-sequence-addons and Build-Depends"
    feature_migration.warn(
        "TODO: Not implemented: Please remove any dh-sequence Build-Dependency"
    )
    feature_migration.warn(
        "TODO: Not implemented: Please ensure there is a Build-Dependency on `debputy (>= 0.1.45~)"
    )
    feature_migration.warn(
        "TODO: Not implemented: Please ensure there is a Build-Dependency on `dpkg-dev (>= 1.22.7~)"
    )


def detect_dh_addons_with_zz_integration(
    debian_dir: VirtualPath,
    _manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
    _migration_target: DebputyIntegrationMode,
) -> None:
    feature_migration.tagline = "Check for dh-sequence-addons"
    r = read_dh_addon_sequences(debian_dir)
    if r is None:
        feature_migration.warn(
            "Cannot find debian/control. Detection of unsupported/missing dh-sequence addon"
            " could not be performed. Please ensure the package will Build-Depend on dh-sequence-zz-debputy"
            " and not rely on any other debhelper sequence addons except those debputy explicitly supports."
        )
        return

    assert _migration_target != INTEGRATION_MODE_FULL

    bd_sequences, dr_sequences, _ = r

    remaining_sequences = bd_sequences | dr_sequences
    saw_dh_debputy = (
        "debputy" in remaining_sequences or "zz-debputy" in remaining_sequences
    )
    saw_zz_debputy = "zz-debputy" in remaining_sequences
    must_use_zz_debputy = False
    remaining_sequences -= SUPPORTED_DH_ADDONS
    for sequence in remaining_sequences & DH_ADDONS_TO_PLUGINS.keys():
        migration = DH_ADDONS_TO_PLUGINS[sequence]
        feature_migration.require_plugin(migration.debputy_plugin)
        if migration.remove_dh_sequence:
            if migration.must_use_zz_debputy:
                must_use_zz_debputy = True
            if sequence in bd_sequences:
                feature_migration.warn(
                    f"TODO: MANUAL MIGRATION - Remove build-dependency on dh-sequence-{sequence}"
                    f" (replaced by debputy-plugin-{migration.debputy_plugin})"
                )
            else:
                feature_migration.warn(
                    f"TODO: MANUAL MIGRATION - Remove --with {sequence} from dh in d/rules"
                    f" (replaced by debputy-plugin-{migration.debputy_plugin})"
                )

    remaining_sequences -= DH_ADDONS_TO_PLUGINS.keys()

    alt_key = "unsupported-dh-sequences"
    for sequence in remaining_sequences & DH_ADDONS_TO_REMOVE:
        if sequence in bd_sequences:
            feature_migration.warn(
                f"TODO: MANUAL MIGRATION - Remove build dependency on dh-sequence-{sequence}"
            )
        else:
            feature_migration.warn(
                f"TODO: MANUAL MIGRATION - Remove --with {sequence} from dh in d/rules"
            )

    remaining_sequences -= DH_ADDONS_TO_REMOVE

    for sequence in remaining_sequences:
        key = f"unsupported-dh-sequence-{sequence}"
        msg = f'The dh addon "{sequence}" is not known to work with dh-debputy and might malfunction'
        if (
            key not in acceptable_migration_issues
            and alt_key not in acceptable_migration_issues
        ):
            raise UnsupportedFeature(msg, [key, alt_key])
        feature_migration.warn(msg)

    if not saw_dh_debputy:
        feature_migration.warn("Missing Build-Depends on dh-sequence-zz-debputy")
    elif must_use_zz_debputy and not saw_zz_debputy:
        feature_migration.warn(
            "Please use the zz-debputy sequence rather than the debputy (needed due to dh add-on load order)"
        )


def _rename_file_if_exists(
    debian_dir: VirtualPath,
    source: str,
    dest: str,
    feature_migration: FeatureMigration,
) -> None:
    source_path = debian_dir.get(source)
    dest_path = debian_dir.get(dest)
    spath = (
        source_path.path
        if source_path is not None
        else os.path.join(debian_dir.path, source)
    )
    dpath = (
        dest_path.path if dest_path is not None else os.path.join(debian_dir.path, dest)
    )
    if source_path is not None and source_path.is_file:
        if dest_path is not None:
            if not dest_path.is_file:
                feature_migration.warnings.append(
                    f'TODO: MANUAL MIGRATION - there is a "{spath}" (file) and "{dpath}" (not a file).'
                    f' The migration wanted to replace "{spath}" with "{dpath}", but since "{dpath}" is not'
                    " a file, this step is left as a manual migration."
                )
                return
            if (
                subprocess.call(["cmp", "-s", source_path.fs_path, dest_path.fs_path])
                != 0
            ):
                feature_migration.warnings.append(
                    f'TODO: MANUAL MIGRATION - there is a "{source_path.path}" and "{dest_path.path}"'
                    f" file. Normally these files are for the same package and there would only be one of"
                    f" them. In this case, they both exist but their content differs. Be advised that"
                    f' debputy tool will use the "{dest_path.path}".'
                )
            else:
                feature_migration.remove_on_success(dest_path.fs_path)
        else:
            feature_migration.rename_on_success(
                source_path.fs_path,
                os.path.join(debian_dir.fs_path, dest),
            )
    elif source_path is not None:
        feature_migration.warnings.append(
            f'TODO: MANUAL MIGRATION - The migration would normally have renamed "{spath}" to "{dpath}".'
            f' However, the migration assumed "{spath}" would be a file and it is not. Therefore, this step'
            " as a manual migration."
        )


def _find_dh_config_file_for_any_pkg(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    unsupported_config: UnsupportedDHConfig,
) -> Iterable[VirtualPath]:
    for dctrl_bin in manifest.all_packages:
        dh_config_file = dhe_pkgfile(
            debian_dir,
            dctrl_bin,
            unsupported_config.dh_config_basename,
            bug_950723_prefix_matching=unsupported_config.bug_950723_prefix_matching,
        )
        if dh_config_file is not None:
            yield dh_config_file


def _unsupported_debhelper_config_file(
    debian_dir: VirtualPath,
    manifest: HighLevelManifest,
    unsupported_config: UnsupportedDHConfig,
    acceptable_migration_issues: AcceptableMigrationIssues,
    feature_migration: FeatureMigration,
) -> None:
    dh_config_files = list(
        _find_dh_config_file_for_any_pkg(debian_dir, manifest, unsupported_config)
    )
    if not dh_config_files:
        return
    dh_tool = unsupported_config.dh_tool
    basename = unsupported_config.dh_config_basename
    file_stem = (
        f"@{basename}" if unsupported_config.bug_950723_prefix_matching else basename
    )
    dh_config_file = dh_config_files[0]
    if unsupported_config.is_missing_migration:
        feature_migration.warn(
            f'Missing migration support for the "{dh_config_file.path}" debhelper config file'
            f" (used by {dh_tool}). Manual migration may be feasible depending on the exact features"
            " required."
        )
        return
    primary_key = f"unsupported-dh-config-file-{file_stem}"
    secondary_key = "any-unsupported-dh-config-file"
    if (
        primary_key not in acceptable_migration_issues
        and secondary_key not in acceptable_migration_issues
    ):
        msg = (
            f'The "{dh_config_file.path}" debhelper config file (used by {dh_tool} is currently not'
            " supported by debputy."
        )
        raise UnsupportedFeature(
            msg,
            [primary_key, secondary_key],
        )
    for dh_config_file in dh_config_files:
        feature_migration.warn(
            f'TODO: MANUAL MIGRATION - Use of unsupported "{dh_config_file.path}" file (used by {dh_tool})'
        )
