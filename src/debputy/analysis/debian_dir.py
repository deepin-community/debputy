import json
import os
import stat
import subprocess
from typing import (
    AbstractSet,
    List,
    Mapping,
    Iterable,
    Tuple,
    Optional,
    Sequence,
    Dict,
    Any,
    Union,
    Iterator,
    TypedDict,
    NotRequired,
    Container,
)

from debputy.analysis import REFERENCE_DATA_TABLE
from debputy.analysis.analysis_util import flatten_ppfs
from debputy.dh.dh_assistant import (
    resolve_active_and_inactive_dh_commands,
    read_dh_addon_sequences,
    extract_dh_compat_level,
)
from debputy.packager_provided_files import (
    PackagerProvidedFile,
    detect_all_packager_provided_files,
)
from debputy.packages import BinaryPackage
from debputy.plugin.api import (
    VirtualPath,
    packager_provided_file_reference_documentation,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.plugin.api.impl_types import (
    PluginProvidedKnownPackagingFile,
    DebputyPluginMetadata,
    KnownPackagingFileInfo,
    InstallPatternDHCompatRule,
    PackagerProvidedFileClassSpec,
    expand_known_packaging_config_features,
)
from debputy.util import assume_not_none, escape_shell

PackagingFileInfo = TypedDict(
    "PackagingFileInfo",
    {
        "path": str,
        "binary-package": NotRequired[str],
        "install-path": NotRequired[str],
        "install-pattern": NotRequired[str],
        "file-categories": NotRequired[List[str]],
        "config-features": NotRequired[List[str]],
        "pkgfile-is-active-in-build": NotRequired[bool],
        "pkgfile-stem": NotRequired[str],
        "pkgfile-explicit-package-name": NotRequired[bool],
        "pkgfile-name-segment": NotRequired[str],
        "pkgfile-architecture-restriction": NotRequired[str],
        "likely-typo-of": NotRequired[str],
        "likely-generated-from": NotRequired[List[str]],
        "related-tools": NotRequired[List[str]],
        "documentation-uris": NotRequired[List[str]],
        "debputy-cmd-templates": NotRequired[List[List[str]]],
        "generates": NotRequired[str],
        "generated-from": NotRequired[str],
    },
)


def scan_debian_dir(
    feature_set: PluginProvidedFeatureSet,
    binary_packages: Mapping[str, BinaryPackage],
    debian_dir: VirtualPath,
    *,
    uses_dh_sequencer: bool = True,
    dh_sequences: Optional[AbstractSet[str]] = None,
) -> Tuple[List[PackagingFileInfo], List[str], int, Optional[object]]:
    known_packaging_files = feature_set.known_packaging_files
    debputy_plugin_metadata = plugin_metadata_for_debputys_own_plugin()

    reference_data_set_names = [
        "config-features",
        "file-categories",
    ]
    for n in reference_data_set_names:
        assert n in REFERENCE_DATA_TABLE

    annotated: List[PackagingFileInfo] = []
    seen_paths: Dict[str, PackagingFileInfo] = {}

    if dh_sequences is None:
        r = read_dh_addon_sequences(debian_dir)
        if r is not None:
            bd_sequences, dr_sequences, uses_dh_sequencer = r
            dh_sequences = bd_sequences | dr_sequences
        else:
            dh_sequences = set()
            uses_dh_sequencer = False
    is_debputy_package = (
        "debputy" in dh_sequences
        or "zz-debputy" in dh_sequences
        or "zz_debputy" in dh_sequences
        or "zz-debputy-rrr" in dh_sequences
    )
    dh_compat_level, dh_assistant_exit_code = extract_dh_compat_level()
    dh_issues = []

    static_packaging_files = {
        kpf.detection_value: kpf
        for kpf in known_packaging_files.values()
        if kpf.detection_method == "path"
    }
    dh_pkgfile_docs = {
        kpf.detection_value: kpf
        for kpf in known_packaging_files.values()
        if kpf.detection_method == "dh.pkgfile"
    }

    if is_debputy_package:
        all_debputy_ppfs = list(
            flatten_ppfs(
                detect_all_packager_provided_files(
                    feature_set.packager_provided_files,
                    debian_dir,
                    binary_packages,
                    allow_fuzzy_matches=True,
                    detect_typos=True,
                    ignore_paths=static_packaging_files,
                )
            )
        )
    else:
        all_debputy_ppfs = []

    if dh_compat_level is not None:
        (
            all_dh_ppfs,
            dh_issues,
            dh_assistant_exit_code,
        ) = resolve_debhelper_config_files(
            debian_dir,
            binary_packages,
            debputy_plugin_metadata,
            dh_pkgfile_docs,
            dh_sequences,
            dh_compat_level,
            uses_dh_sequencer,
            ignore_paths=static_packaging_files,
        )

    else:
        all_dh_ppfs = []

    for ppf in all_debputy_ppfs:
        key = ppf.path.path
        ref_doc = ppf.definition.reference_documentation
        documentation_uris = (
            ref_doc.format_documentation_uris if ref_doc is not None else None
        )
        details: PackagingFileInfo = {
            "path": key,
            "binary-package": ppf.package_name,
            "pkgfile-stem": ppf.definition.stem,
            "pkgfile-explicit-package-name": ppf.uses_explicit_package_name,
            "pkgfile-is-active-in-build": ppf.definition.has_active_command,
            "debputy-cmd-templates": [
                ["debputy", "plugin", "show", "p-p-f", ppf.definition.stem]
            ],
        }
        if ppf.fuzzy_match and key.endswith(".in"):
            _merge_list(details, "file-categories", ["generic-template"])
            details["generates"] = key[:-3]
        elif assume_not_none(ppf.path.parent_dir).get(ppf.path.name + ".in"):
            _merge_list(details, "file-categories", ["generated"])
            details["generated-from"] = key + ".in"
        name_segment = ppf.name_segment
        arch_restriction = ppf.architecture_restriction
        if name_segment is not None:
            details["pkgfile-name-segment"] = name_segment
        if arch_restriction:
            details["pkgfile-architecture-restriction"] = arch_restriction
        seen_paths[key] = details
        annotated.append(details)
        static_details = static_packaging_files.get(key)
        if static_details is not None:
            # debhelper compat rules does not apply to debputy files
            _add_known_packaging_data(details, static_details, None)
        if documentation_uris:
            details["documentation-uris"] = list(documentation_uris)

    _merge_ppfs(annotated, seen_paths, all_dh_ppfs, dh_pkgfile_docs, dh_compat_level)

    for virtual_path in _scan_debian_dir(debian_dir):
        key = virtual_path.path
        if key in seen_paths:
            continue
        if virtual_path.is_symlink:
            try:
                st = os.stat(virtual_path.fs_path)
            except FileNotFoundError:
                continue
            else:
                if not stat.S_ISREG(st.st_mode):
                    continue
        elif not virtual_path.is_file:
            continue

        static_match = static_packaging_files.get(virtual_path.path)
        if static_match is not None:
            details: PackagingFileInfo = {
                "path": key,
            }
            annotated.append(details)
            if assume_not_none(virtual_path.parent_dir).get(virtual_path.name + ".in"):
                details["generated-from"] = key + ".in"
                _merge_list(details, "file-categories", ["generated"])
            _add_known_packaging_data(details, static_match, dh_compat_level)

    return annotated, reference_data_set_names, dh_assistant_exit_code, dh_issues


def _fake_PPFClassSpec(
    debputy_plugin_metadata: DebputyPluginMetadata,
    stem: str,
    doc_uris: Optional[Sequence[str]],
    install_pattern: Optional[str],
    *,
    default_priority: Optional[int] = None,
    packageless_is_fallback_for_all_packages: bool = False,
    post_formatting_rewrite: Optional[str] = None,
    bug_950723: bool = False,
    has_active_command: bool = False,
) -> PackagerProvidedFileClassSpec:
    if install_pattern is None:
        install_pattern = "not-a-real-ppf"
    if post_formatting_rewrite is not None:
        formatting_hook = _POST_FORMATTING_REWRITE[post_formatting_rewrite]
    else:
        formatting_hook = None
    return PackagerProvidedFileClassSpec(
        debputy_plugin_metadata,
        stem,
        install_pattern,
        allow_architecture_segment=True,
        allow_name_segment=True,
        default_priority=default_priority,
        default_mode=0o644,
        post_formatting_rewrite=formatting_hook,
        packageless_is_fallback_for_all_packages=packageless_is_fallback_for_all_packages,
        reservation_only=False,
        formatting_callback=None,
        bug_950723=bug_950723,
        has_active_command=has_active_command,
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=doc_uris,
        ),
    )


def _relevant_dh_compat_rules(
    compat_level: Optional[int],
    info: KnownPackagingFileInfo,
) -> Iterable[InstallPatternDHCompatRule]:
    if compat_level is None:
        return
    dh_compat_rules = info.get("dh_compat_rules")
    if not dh_compat_rules:
        return
    for dh_compat_rule in dh_compat_rules:
        rule_compat_level = dh_compat_rule.get("starting_with_compat_level")
        if rule_compat_level is not None and compat_level < rule_compat_level:
            continue
        yield dh_compat_rule


def _kpf_install_pattern(
    compat_level: Optional[int],
    ppkpf: PluginProvidedKnownPackagingFile,
) -> Optional[str]:
    for compat_rule in _relevant_dh_compat_rules(compat_level, ppkpf.info):
        install_pattern = compat_rule.get("install_pattern")
        if install_pattern is not None:
            return install_pattern
    return ppkpf.info.get("install_pattern")


def resolve_debhelper_config_files(
    debian_dir: VirtualPath,
    binary_packages: Mapping[str, BinaryPackage],
    debputy_plugin_metadata: DebputyPluginMetadata,
    dh_ppf_docs: Dict[str, PluginProvidedKnownPackagingFile],
    dh_rules_addons: AbstractSet[str],
    dh_compat_level: int,
    saw_dh: bool,
    ignore_paths: Container[str] = frozenset(),
) -> Tuple[List[PackagerProvidedFile], Optional[object], int]:
    dh_ppfs = {}
    commands, exit_code = _relevant_dh_commands(dh_rules_addons)

    cmd = ["dh_assistant", "list-guessed-dh-config-files"]
    if dh_rules_addons:
        addons = ",".join(dh_rules_addons)
        cmd.append(f"--with={addons}")
    try:
        output = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        config_files = []
        issues = None
        if isinstance(e, subprocess.CalledProcessError):
            exit_code = e.returncode
        else:
            exit_code = 127
    else:
        result = json.loads(output)
        config_files: List[Union[Mapping[str, Any], object]] = result.get(
            "config-files", []
        )
        issues = result.get("issues")
    dh_commands = resolve_active_and_inactive_dh_commands(dh_rules_addons)
    for config_file in config_files:
        if not isinstance(config_file, dict):
            continue
        if config_file.get("file-type") != "pkgfile":
            continue
        stem = config_file.get("pkgfile")
        if stem is None:
            continue
        internal = config_file.get("internal")
        if isinstance(internal, dict):
            bug_950723 = internal.get("bug#950723", False) is True
        else:
            bug_950723 = False
        commands = config_file.get("commands")
        documentation_uris = []
        related_tools = []
        seen_commands = set()
        seen_docs = set()
        ppkpf = dh_ppf_docs.get(stem)

        if ppkpf:
            dh_cmds = ppkpf.info.get("debhelper_commands")
            doc_uris = ppkpf.info.get("documentation_uris")
            default_priority = ppkpf.info.get("default_priority")
            if doc_uris is not None:
                seen_docs.update(doc_uris)
                documentation_uris.extend(doc_uris)
            if dh_cmds is not None:
                seen_commands.update(dh_cmds)
                related_tools.extend(dh_cmds)
            install_pattern = _kpf_install_pattern(dh_compat_level, ppkpf)
            post_formatting_rewrite = ppkpf.info.get("post_formatting_rewrite")
            packageless_is_fallback_for_all_packages = ppkpf.info.get(
                "packageless_is_fallback_for_all_packages",
                False,
            )
            # If it is a debhelper PPF, then `has_active_command` is false by default.
            has_active_command = ppkpf.info.get("has_active_command", False)
        else:
            install_pattern = None
            default_priority = None
            post_formatting_rewrite = None
            packageless_is_fallback_for_all_packages = False
            has_active_command = False
        for command in commands:
            if isinstance(command, dict):
                command_name = command.get("command")
                if isinstance(command_name, str) and command_name:
                    if command_name not in seen_commands:
                        related_tools.append(command_name)
                        seen_commands.add(command_name)
                    manpage = f"man:{command_name}(1)"
                    if manpage not in seen_docs:
                        documentation_uris.append(manpage)
                        seen_docs.add(manpage)
                else:
                    continue
                is_active = command.get("is-active", True)
                if is_active is None and command_name in dh_commands.active_commands:
                    is_active = True
                if not isinstance(is_active, bool):
                    continue
                if is_active:
                    has_active_command = True
        dh_ppfs[stem] = _fake_PPFClassSpec(
            debputy_plugin_metadata,
            stem,
            documentation_uris,
            install_pattern,
            default_priority=default_priority,
            post_formatting_rewrite=post_formatting_rewrite,
            packageless_is_fallback_for_all_packages=packageless_is_fallback_for_all_packages,
            bug_950723=bug_950723,
            has_active_command=has_active_command if saw_dh else True,
        )
    for ppkpf in dh_ppf_docs.values():
        stem = ppkpf.detection_value
        if stem in dh_ppfs:
            continue

        default_priority = ppkpf.info.get("default_priority")
        install_pattern = _kpf_install_pattern(dh_compat_level, ppkpf)
        post_formatting_rewrite = ppkpf.info.get("post_formatting_rewrite")
        packageless_is_fallback_for_all_packages = ppkpf.info.get(
            "packageless_is_fallback_for_all_packages",
            False,
        )
        has_active_command = (
            ppkpf.info.get("has_active_command", False) if saw_dh else False
        )
        if not has_active_command:
            dh_cmds = ppkpf.info.get("debhelper_commands")
            if dh_cmds:
                has_active_command = any(
                    c in dh_commands.active_commands for c in dh_cmds
                )
        dh_ppfs[stem] = _fake_PPFClassSpec(
            debputy_plugin_metadata,
            stem,
            ppkpf.info.get("documentation_uris"),
            install_pattern,
            default_priority=default_priority,
            post_formatting_rewrite=post_formatting_rewrite,
            packageless_is_fallback_for_all_packages=packageless_is_fallback_for_all_packages,
            has_active_command=has_active_command,
        )
    all_dh_ppfs = list(
        flatten_ppfs(
            detect_all_packager_provided_files(
                dh_ppfs,
                debian_dir,
                binary_packages,
                allow_fuzzy_matches=True,
                detect_typos=True,
                ignore_paths=ignore_paths,
            )
        )
    )
    return all_dh_ppfs, issues, exit_code


def _merge_list(
    existing_table: Dict[str, Any],
    key: str,
    new_data: Optional[Sequence[str]],
) -> None:
    if not new_data:
        return
    existing_values = existing_table.get(key, [])
    if isinstance(existing_values, tuple):
        existing_values = list(existing_values)
    assert isinstance(existing_values, list)
    seen = set(existing_values)
    existing_values.extend(x for x in new_data if x not in seen)
    existing_table[key] = existing_values


def _merge_ppfs(
    identified: List[PackagingFileInfo],
    seen_paths: Dict[str, PackagingFileInfo],
    ppfs: List[PackagerProvidedFile],
    context: Mapping[str, PluginProvidedKnownPackagingFile],
    dh_compat_level: Optional[int],
) -> None:
    for ppf in ppfs:
        key = ppf.path.path
        ref_doc = ppf.definition.reference_documentation
        documentation_uris = (
            ref_doc.format_documentation_uris if ref_doc is not None else None
        )
        if not ppf.definition.installed_as_format.startswith("not-a-real-ppf"):
            try:
                parts = ppf.compute_dest()
            except RuntimeError:
                dest = None
            else:
                dest = "/".join(parts).lstrip(".")
        else:
            dest = None
        orig_details = seen_paths.get(key)
        if orig_details is None:
            details: PackagingFileInfo = {
                "path": key,
                "pkgfile-stem": ppf.definition.stem,
                "pkgfile-is-active-in-build": ppf.definition.has_active_command,
                "pkgfile-explicit-package-name": ppf.uses_explicit_package_name,
                "binary-package": ppf.package_name,
            }
            if ppf.expected_path is not None:
                details["likely-typo-of"] = ppf.expected_path
            identified.append(details)
        else:
            details = orig_details
            # We do not merge the "is typo" field; if the original
            for k, v in [
                ("pkgfile-stem", ppf.definition.stem),
                ("pkgfile-explicit-package-name", ppf.definition.has_active_command),
                ("binary-package", ppf.package_name),
            ]:
                if k not in details:
                    details[k] = v
            if ppf.definition.has_active_command and details.get(
                "pkgfile-is-active-in-build", False
            ):
                details["pkgfile-is-active-in-build"] = True
            if ppf.expected_path is None and "likely-typo-of" in details:
                del details["likely-typo-of"]

        name_segment = ppf.name_segment
        arch_restriction = ppf.architecture_restriction
        if name_segment is not None and "pkgfile-name-segment" not in details:
            details["pkgfile-name-segment"] = name_segment
        if (
            arch_restriction is not None
            and "pkgfile-architecture-restriction" not in details
        ):
            details["pkgfile-architecture-restriction"] = arch_restriction
        if ppf.fuzzy_match and key.endswith(".in"):
            _merge_list(details, "file-categories", ["generic-template"])
            details["generates"] = key[:-3]
        elif assume_not_none(ppf.path.parent_dir).get(ppf.path.name + ".in"):
            _merge_list(details, "file-categories", ["generated"])
            details["generated-from"] = key + ".in"
        if dest is not None and "install-path" not in details:
            details["install-path"] = dest

        extra_details = context.get(ppf.definition.stem)
        if extra_details is not None:
            _add_known_packaging_data(details, extra_details, dh_compat_level)

        _merge_list(details, "documentation-uris", documentation_uris)


def _relevant_dh_commands(dh_rules_addons: Iterable[str]) -> Tuple[List[str], int]:
    cmd = ["dh_assistant", "list-commands", "--output-format=json"]
    if dh_rules_addons:
        addons = ",".join(dh_rules_addons)
        cmd.append(f"--with={addons}")
    try:
        output = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        exit_code = 127
        if isinstance(e, subprocess.CalledProcessError):
            exit_code = e.returncode
        return [], exit_code
    else:
        data = json.loads(output)
        commands_json = data.get("commands")
        commands = []
        for command in commands_json:
            if isinstance(command, dict):
                command_name = command.get("command")
                if isinstance(command_name, str) and command_name:
                    commands.append(command_name)
        return commands, 0


def _add_known_packaging_data(
    details: PackagingFileInfo,
    plugin_data: PluginProvidedKnownPackagingFile,
    dh_compat_level: Optional[int],
):
    install_pattern = _kpf_install_pattern(
        dh_compat_level,
        plugin_data,
    )
    config_features = plugin_data.info.get("config_features")
    if config_features:
        config_features = expand_known_packaging_config_features(
            dh_compat_level or 0,
            config_features,
        )
        _merge_list(details, "config-features", config_features)

    if dh_compat_level is not None:
        extra_config_features = []
        for dh_compat_rule in _relevant_dh_compat_rules(
            dh_compat_level, plugin_data.info
        ):
            cf = dh_compat_rule.get("add_config_features")
            if cf:
                extra_config_features.extend(cf)
        if extra_config_features:
            extra_config_features = expand_known_packaging_config_features(
                dh_compat_level,
                extra_config_features,
            )
            _merge_list(details, "config-features", extra_config_features)
    if "install-pattern" not in details and install_pattern is not None:
        details["install-pattern"] = install_pattern
    for mk, ok in [
        ("file_categories", "file-categories"),
        ("documentation_uris", "documentation-uris"),
        ("debputy_cmd_templates", "debputy-cmd-templates"),
    ]:
        value = plugin_data.info.get(mk)
        if value and ok == "debputy-cmd-templates":
            value = [escape_shell(*c) for c in value]
        _merge_list(details, ok, value)


def _scan_debian_dir(debian_dir: VirtualPath) -> Iterator[VirtualPath]:
    for p in debian_dir.iterdir:
        yield p
        if p.is_dir and p.path in ("debian/source", "debian/tests"):
            yield from p.iterdir


_POST_FORMATTING_REWRITE = {
    "period-to-underscore": lambda n: n.replace(".", "_"),
}
