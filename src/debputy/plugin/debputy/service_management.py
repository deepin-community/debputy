import collections
import dataclasses
import os
import textwrap
from typing import Dict, List, Literal, Iterable, Sequence

from debputy.packages import BinaryPackage
from debputy.plugin.api.spec import (
    ServiceRegistry,
    VirtualPath,
    PackageProcessingContext,
    BinaryCtrlAccessor,
    ServiceDefinition,
)
from debputy.util import _error, assume_not_none

DPKG_ROOT = '"${DPKG_ROOT}"'
EMPTY_DPKG_ROOT_CONDITION = '[ -z "${DPKG_ROOT}" ]'
SERVICE_MANAGER_IS_SYSTEMD_CONDITION = "[ -d /run/systemd/system ]"


@dataclasses.dataclass(slots=True)
class SystemdServiceContext:
    had_install_section: bool


@dataclasses.dataclass(slots=True)
class SystemdUnit:
    path: VirtualPath
    names: List[str]
    type_of_service: str
    service_scope: str
    enable_by_default: bool
    start_by_default: bool
    had_install_section: bool


def detect_systemd_service_files(
    fs_root: VirtualPath,
    service_registry: ServiceRegistry[SystemdServiceContext],
    context: PackageProcessingContext,
) -> None:
    pkg = context.binary_package
    systemd_units = _find_and_analyze_systemd_service_files(pkg, fs_root, "system")
    for unit in systemd_units:
        service_registry.register_service(
            unit.path,
            unit.names,
            type_of_service=unit.type_of_service,
            service_scope=unit.service_scope,
            enable_by_default=unit.enable_by_default,
            start_by_default=unit.start_by_default,
            default_upgrade_rule="restart" if unit.start_by_default else "do-nothing",
            service_context=SystemdServiceContext(
                unit.had_install_section,
            ),
        )


def generate_snippets_for_systemd_units(
    services: Sequence[ServiceDefinition[SystemdServiceContext]],
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    stop_before_upgrade: List[str] = []
    stop_then_start_scripts = []
    on_purge = []
    start_on_install = []
    action_on_upgrade = collections.defaultdict(list)
    assert services

    for service_def in services:
        if service_def.auto_enable_on_install:
            template = """\
                if deb-systemd-helper debian-installed {UNITFILE}; then
                    # The following line should be removed in trixie or trixie+1
                    deb-systemd-helper unmask {UNITFILE} >/dev/null || true

                    if deb-systemd-helper --quiet was-enabled {UNITFILE}; then
                        # Create new symlinks, if any.
                        deb-systemd-helper enable {UNITFILE} >/dev/null || true
                    fi
                fi

                # Update the statefile to add new symlinks (if any), which need to be cleaned
                # up on purge. Also remove old symlinks.
                deb-systemd-helper update-state {UNITFILE} >/dev/null || true
            """
        else:
            template = """\
                # The following line should be removed in trixie or trixie+1
                deb-systemd-helper unmask {UNITFILE} >/dev/null || true

                # was-enabled defaults to true, so new installations run enable.
                if deb-systemd-helper --quiet was-enabled {UNITFILE}; then
                    # Enables the unit on first installation, creates new
                    # symlinks on upgrades if the unit file has changed.
                    deb-systemd-helper enable {UNITFILE} >/dev/null || true
                else
                    # Update the statefile to add new symlinks (if any), which need to be
                    # cleaned up on purge. Also remove old symlinks.
                    deb-systemd-helper update-state {UNITFILE} >/dev/null || true
                fi
            """
        service_name = service_def.name

        if assume_not_none(service_def.service_context).had_install_section:
            ctrl.maintscript.on_configure(
                template.format(
                    UNITFILE=ctrl.maintscript.escape_shell_words(service_name),
                )
            )
            on_purge.append(service_name)
        elif service_def.auto_enable_on_install:
            _error(
                f'The service "{service_name}" cannot be enabled under "systemd" as'
                f' it has no "[Install]" section. Please correct {service_def.definition_source}'
                f' so that it does not enable the service or does not apply to "systemd"'
            )

        if service_def.auto_start_on_install:
            start_on_install.append(service_name)
        if service_def.on_upgrade == "stop-then-start":
            stop_then_start_scripts.append(service_name)
        elif service_def.on_upgrade in ("restart", "reload"):
            action: str = service_def.on_upgrade
            action_on_upgrade[action].append(service_name)
        elif service_def.on_upgrade != "do-nothing":
            raise AssertionError(
                f"Missing support for on_upgrade rule: {service_def.on_upgrade}"
            )

    if start_on_install or action_on_upgrade:
        lines = [
            "if {EMPTY_DPKG_ROOT_CONDITION} && {SERVICE_MANAGER_IS_SYSTEMD_CONDITION}; then".format(
                EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                SERVICE_MANAGER_IS_SYSTEMD_CONDITION=SERVICE_MANAGER_IS_SYSTEMD_CONDITION,
            ),
            "    systemctl --system daemon-reload >/dev/null || true",
        ]
        if stop_then_start_scripts:
            unit_files = ctrl.maintscript.escape_shell_words(*stop_then_start_scripts)
            lines.append(
                "        deb-systemd-invoke start {UNITFILES} >/dev/null || true".format(
                    UNITFILES=unit_files,
                )
            )
        if start_on_install:
            lines.append('    if [ -z "$2" ]; then')
            lines.append(
                "        deb-systemd-invoke start {UNITFILES} >/dev/null || true".format(
                    UNITFILES=ctrl.maintscript.escape_shell_words(*start_on_install),
                )
            )
            lines.append("    fi")
        if action_on_upgrade:
            lines.append('    if [ -n "$2" ]; then')
            for action, units in action_on_upgrade.items():
                lines.append(
                    "        deb-systemd-invoke {ACTION} {UNITFILES} >/dev/null || true".format(
                        ACTION=action,
                        UNITFILES=ctrl.maintscript.escape_shell_words(*units),
                    )
                )
            lines.append("    fi")
        lines.append("fi")
        combined = "".join(x if x.endswith("\n") else f"{x}\n" for x in lines)
        ctrl.maintscript.on_configure(combined)

    if stop_then_start_scripts:
        ctrl.maintscript.unconditionally_in_script(
            "preinst",
            textwrap.dedent(
                """\
            if {EMPTY_DPKG_ROOT_CONDITION} && [ "$1" = upgrade ] && {SERVICE_MANAGER_IS_SYSTEMD_CONDITION} ; then
                deb-systemd-invoke stop {UNIT_FILES} >/dev/null || true
            fi
            """.format(
                    EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                    SERVICE_MANAGER_IS_SYSTEMD_CONDITION=SERVICE_MANAGER_IS_SYSTEMD_CONDITION,
                    UNIT_FILES=ctrl.maintscript.escape_shell_words(
                        *stop_then_start_scripts
                    ),
                )
            ),
        )

    if stop_before_upgrade:
        ctrl.maintscript.on_before_removal(
            """\
        if {EMPTY_DPKG_ROOT_CONDITION} && {SERVICE_MANAGER_IS_SYSTEMD_CONDITION} ; then
            deb-systemd-invoke stop {UNIT_FILES} >/dev/null || true
        fi
        """.format(
                EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                SERVICE_MANAGER_IS_SYSTEMD_CONDITION=SERVICE_MANAGER_IS_SYSTEMD_CONDITION,
                UNIT_FILES=ctrl.maintscript.escape_shell_words(*stop_before_upgrade),
            )
        )
    if on_purge:
        ctrl.maintscript.on_purge(
            """\
        if [ -x "/usr/bin/deb-systemd-helper" ]; then
            deb-systemd-helper purge {UNITFILES} >/dev/null || true
        fi
        """.format(
                UNITFILES=ctrl.maintscript.escape_shell_words(*stop_before_upgrade),
            )
        )
    ctrl.maintscript.on_removed(
        textwrap.dedent(
            """\
        if {SERVICE_MANAGER_IS_SYSTEMD_CONDITION} ; then
            systemctl --system daemon-reload >/dev/null || true
        fi
        """.format(
                SERVICE_MANAGER_IS_SYSTEMD_CONDITION=SERVICE_MANAGER_IS_SYSTEMD_CONDITION
            )
        )
    )


def _remove_quote(v: str) -> str:
    if v and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _find_and_analyze_systemd_service_files(
    pkg: BinaryPackage,
    fs_root: VirtualPath,
    systemd_service_dir: Literal["system", "user"],
) -> Iterable[SystemdUnit]:
    service_dirs = [
        f"./usr/lib/systemd/{systemd_service_dir}",
        f"./lib/systemd/{systemd_service_dir}",
    ]
    had_install_sections = set()
    aliases: Dict[str, List[str]] = collections.defaultdict(list)
    seen = set()
    all_files = []
    expected_units = set()
    expected_units_required_by = collections.defaultdict(list)

    for d in service_dirs:
        system_dir = fs_root.lookup(d)
        if not system_dir:
            continue
        for child in system_dir.iterdir:
            if child.is_symlink:
                dest = os.path.basename(child.readlink())
                aliases[dest].append(child.name)
            elif child.is_file and child.name not in seen:
                seen.add(child.name)
                all_files.append(child)
                if "@" in child.name:
                    # dh_installsystemd does not check the contents of templated services,
                    # and we match that.
                    continue
                with child.open() as fd:
                    for line in fd:
                        line = line.strip()
                        line_lc = line.lower()
                        if line_lc == "[install]":
                            had_install_sections.add(child.name)
                        elif line_lc.startswith("alias="):
                            # This code assumes service names cannot contain spaces (as in
                            # if you copy-paste it for another field it might not work)
                            aliases[child.name].extend(
                                _remove_quote(x) for x in line[6:].split()
                            )
                        elif line_lc.startswith("also="):
                            # This code assumes service names cannot contain spaces (as in
                            # if you copy-paste it for another field it might not work)
                            for unit in (_remove_quote(x) for x in line[5:].split()):
                                expected_units_required_by[unit].append(child.absolute)
                                expected_units.add(unit)
    for path in all_files:
        if "@" in path.name:
            # Match dh_installsystemd, which skips templated services
            continue
        names = aliases[path.name]
        _, type_of_service = path.name.rsplit(".", 1)
        expected_units.difference_update(names)
        expected_units.discard(path.name)
        names.extend(x[:-8] for x in list(names) if x.endswith(".service"))
        names.insert(0, path.name)
        if path.name.endswith(".service"):
            names.insert(1, path.name[:-8])
        yield SystemdUnit(
            path,
            names,
            type_of_service,
            systemd_service_dir,
            # Bug (?) compat with dh_installsystemd. All units are started, but only
            # enable those with an `[Install]` section.
            # Possibly related bug #1055599
            enable_by_default=path.name in had_install_sections,
            start_by_default=True,
            had_install_section=path.name in had_install_sections,
        )

    if expected_units:
        for unit_name in expected_units:
            required_by = expected_units_required_by[unit_name]
            required_names = ", ".join(required_by)
            _error(
                f"The unit {unit_name} was required by {required_names} (via Also=...)"
                f" but was not present in the package {pkg.name}"
            )


def generate_snippets_for_init_scripts(
    services: Sequence[ServiceDefinition[None]],
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    for service_def in services:
        script_name = service_def.path.name
        script_installed_path = service_def.path.absolute

        update_rcd_params = (
            "defaults" if service_def.auto_enable_on_install else "defaults-disabled"
        )

        ctrl.maintscript.unconditionally_in_script(
            "preinst",
            textwrap.dedent(
                """\
            if [ "$1" = "install" ] && [ -n "$2" ] && [ -x {DPKG_ROOT}{SCRIPT_PATH} ] ; then
                chmod +x {DPKG_ROOT}{SCRIPT_PATH} >/dev/null || true
            fi
               """.format(
                    DPKG_ROOT=DPKG_ROOT,
                    SCRIPT_PATH=ctrl.maintscript.escape_shell_words(
                        script_installed_path
                    ),
                )
            ),
        )

        lines = [
            "if {EMPTY_DPKG_ROOT_CONDITION} && [ -x {SCRIPT_PATH} ]; then",
            "    update-rc.d {SCRIPT_NAME} {UPDATE_RCD_PARAMS} >/dev/null || exit 1",
        ]

        if (
            service_def.auto_start_on_install
            and service_def.on_upgrade != "stop-then-start"
        ):
            lines.append('    if [ -z "$2" ]; then')
            lines.append(
                "        invoke-rc.d --skip-systemd-native {SCRIPT_NAME} start >/dev/null || exit 1".format(
                    SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
                )
            )
            lines.append("    fi")

        if service_def.on_upgrade in ("restart", "reload"):
            lines.append('    if [ -n "$2" ]; then')
            lines.append(
                "        invoke-rc.d --skip-systemd-native {SCRIPT_NAME} {ACTION} >/dev/null || exit 1".format(
                    SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
                    ACTION=service_def.on_upgrade,
                )
            )
            lines.append("    fi")
        elif service_def.on_upgrade == "stop-then-start":
            lines.append(
                "    invoke-rc.d --skip-systemd-native {SCRIPT_NAME} start >/dev/null || exit 1".format(
                    SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
                )
            )
            ctrl.maintscript.unconditionally_in_script(
                "preinst",
                textwrap.dedent(
                    """\
                if {EMPTY_DPKG_ROOT_CONDITION} && [ "$1" = "upgrade" ] && [ -x {SCRIPT_PATH} ]; then
                    invoke-rc.d --skip-systemd-native {SCRIPT_NAME} stop > /dev/null || true
                fi
                """.format(
                        EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                        SCRIPT_PATH=ctrl.maintscript.escape_shell_words(
                            script_installed_path
                        ),
                        SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
                    )
                ),
            )
        elif service_def.on_upgrade != "do-nothing":
            raise AssertionError(
                f"Missing support for on_upgrade rule: {service_def.on_upgrade}"
            )

        lines.append("fi")
        combined = "".join(x if x.endswith("\n") else f"{x}\n" for x in lines)
        ctrl.maintscript.on_configure(
            combined.format(
                EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                DPKG_ROOT=DPKG_ROOT,
                UPDATE_RCD_PARAMS=update_rcd_params,
                SCRIPT_PATH=ctrl.maintscript.escape_shell_words(script_installed_path),
                SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
            )
        )

        ctrl.maintscript.on_removed(
            textwrap.dedent(
                """\
            if [ -x {DPKG_ROOT}{SCRIPT_PATH} ]; then
                chmod -x {DPKG_ROOT}{SCRIPT_PATH} > /dev/null || true
            fi
            """.format(
                    DPKG_ROOT=DPKG_ROOT,
                    SCRIPT_PATH=ctrl.maintscript.escape_shell_words(
                        script_installed_path
                    ),
                )
            )
        )
        ctrl.maintscript.on_purge(
            textwrap.dedent(
                """\
            if {EMPTY_DPKG_ROOT_CONDITION} ; then
                update-rc.d {SCRIPT_NAME} remove >/dev/null
            fi
            """.format(
                    SCRIPT_NAME=ctrl.maintscript.escape_shell_words(script_name),
                    EMPTY_DPKG_ROOT_CONDITION=EMPTY_DPKG_ROOT_CONDITION,
                )
            )
        )


def detect_sysv_init_service_files(
    fs_root: VirtualPath,
    service_registry: ServiceRegistry[None],
    _context: PackageProcessingContext,
) -> None:
    etc_init = fs_root.lookup("/etc/init.d")
    if not etc_init:
        return
    for path in etc_init.iterdir:
        if path.is_dir or not path.is_executable:
            continue

        service_registry.register_service(
            path,
            path.name,
        )
