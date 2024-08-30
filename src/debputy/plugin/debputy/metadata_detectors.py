import itertools
import os
import re
import textwrap
from typing import Iterable, Iterator

from debputy.plugin.api import (
    VirtualPath,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.plugin.debputy.paths import (
    INITRAMFS_HOOK_DIR,
    SYSTEMD_TMPFILES_DIR,
    GSETTINGS_SCHEMA_DIR,
    SYSTEMD_SYSUSERS_DIR,
)
from debputy.plugin.debputy.types import DebputyCapability
from debputy.util import assume_not_none, _warn

DPKG_ROOT = '"${DPKG_ROOT}"'
DPKG_ROOT_UNQUOTED = "${DPKG_ROOT}"

KERNEL_MODULE_EXTENSIONS = tuple(
    f"{ext}{comp_ext}"
    for ext, comp_ext in itertools.product(
        (".o", ".ko"),
        ("", ".gz", ".bz2", ".xz"),
    )
)


def detect_initramfs_hooks(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    hook_dir = fs_root.lookup(INITRAMFS_HOOK_DIR)
    if not hook_dir:
        return
    for _ in hook_dir.iterdir:
        # Only add the trigger if the directory is non-empty. It is unlikely to matter a lot,
        # but we do this to match debhelper.
        break
    else:
        return

    ctrl.dpkg_trigger("activate-noawait", "update-initramfs")


def _all_tmpfiles_conf(fs_root: VirtualPath) -> Iterable[VirtualPath]:
    seen_tmpfiles = set()
    tmpfiles_dirs = [
        SYSTEMD_TMPFILES_DIR,
        "./etc/tmpfiles.d",
    ]
    for tmpfiles_dir_path in tmpfiles_dirs:
        tmpfiles_dir = fs_root.lookup(tmpfiles_dir_path)
        if not tmpfiles_dir:
            continue
        for path in tmpfiles_dir.iterdir:
            if (
                not path.is_file
                or not path.name.endswith(".conf")
                or path.name in seen_tmpfiles
            ):
                continue
            seen_tmpfiles.add(path.name)
            yield path


def detect_systemd_tmpfiles(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    tmpfiles_confs = [
        x.name for x in sorted(_all_tmpfiles_conf(fs_root), key=lambda x: x.name)
    ]
    if not tmpfiles_confs:
        return

    tmpfiles_escaped = ctrl.maintscript.escape_shell_words(*tmpfiles_confs)

    snippet = textwrap.dedent(
        f"""\
            if [ -x "$(command -v systemd-tmpfiles)" ]; then
                systemd-tmpfiles ${{DPKG_ROOT:+--root="$DPKG_ROOT"}} --create {tmpfiles_escaped} || true
            fi
    """
    )

    ctrl.maintscript.on_configure(snippet)


def _all_sysusers_conf(fs_root: VirtualPath) -> Iterable[VirtualPath]:
    sysusers_dir = fs_root.lookup(SYSTEMD_SYSUSERS_DIR)
    if not sysusers_dir:
        return
    for child in sysusers_dir.iterdir:
        if not child.name.endswith(".conf"):
            continue
        yield child


def detect_systemd_sysusers(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    sysusers_confs = [p.name for p in _all_sysusers_conf(fs_root)]
    if not sysusers_confs:
        return

    sysusers_escaped = ctrl.maintscript.escape_shell_words(*sysusers_confs)

    snippet = textwrap.dedent(
        f"""\
            systemd-sysusers ${{DPKG_ROOT:+--root="$DPKG_ROOT"}} --create {sysusers_escaped} || true
    """
    )

    ctrl.substvars.add_dependency(
        "misc:Depends", "systemd | systemd-standalone-sysusers | systemd-sysusers"
    )
    ctrl.maintscript.on_configure(snippet)


def detect_icons(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    icons_root_dir = fs_root.lookup("./usr/share/icons")
    if not icons_root_dir:
        return
    icon_dirs = []
    for subdir in icons_root_dir.iterdir:
        if subdir.name in ("gnome", "hicolor"):
            # dh_icons skips this for some reason.
            continue
        for p in subdir.all_paths():
            if p.is_file and p.name.endswith((".png", ".svg", ".xpm", ".icon")):
                icon_dirs.append(subdir.absolute)
                break
    if not icon_dirs:
        return

    icon_dir_list_escaped = ctrl.maintscript.escape_shell_words(*icon_dirs)

    postinst_snippet = textwrap.dedent(
        f"""\
        if command -v update-icon-caches >/dev/null; then
            update-icon-caches {icon_dir_list_escaped}
        fi
    """
    )

    postrm_snippet = textwrap.dedent(
        f"""\
        if command -v update-icon-caches >/dev/null; then
            update-icon-caches {icon_dir_list_escaped}
        fi
    """
    )

    ctrl.maintscript.on_configure(postinst_snippet)
    ctrl.maintscript.unconditionally_in_script("postrm", postrm_snippet)


def detect_gsettings_dependencies(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    gsettings_schema_dir = fs_root.lookup(GSETTINGS_SCHEMA_DIR)
    if not gsettings_schema_dir:
        return

    for path in gsettings_schema_dir.all_paths():
        if path.is_file and path.name.endswith((".xml", ".override")):
            ctrl.substvars.add_dependency(
                "misc:Depends", "dconf-gsettings-backend | gsettings-backend"
            )
            break


def detect_kernel_modules(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _unused: PackageProcessingContext,
) -> None:
    for prefix in [".", "./usr"]:
        module_root_dir = fs_root.lookup(f"{prefix}/lib/modules")

        if not module_root_dir:
            continue

        module_version_dirs = []

        for module_version_dir in module_root_dir.iterdir:
            if not module_version_dir.is_dir:
                continue

            for fs_path in module_version_dir.all_paths():
                if fs_path.name.endswith(KERNEL_MODULE_EXTENSIONS):
                    module_version_dirs.append(module_version_dir.name)
                    break

        for module_version in module_version_dirs:
            module_version_escaped = ctrl.maintscript.escape_shell_words(module_version)
            postinst_snippet = textwrap.dedent(
                f"""\
                    if [ -e /boot/System.map-{module_version_escaped} ]; then
                        depmod -a -F /boot/System.map-{module_version_escaped} {module_version_escaped} || true
                    fi
            """
            )

            postrm_snippet = textwrap.dedent(
                f"""\
                if [ -e /boot/System.map-{module_version_escaped} ]; then
                    depmod -a -F /boot/System.map-{module_version_escaped} {module_version_escaped} || true
                fi
            """
            )

            ctrl.maintscript.on_configure(postinst_snippet)
            # TODO: This should probably be on removal. However, this is what debhelper did and we should
            # do the same until we are sure (not that it matters a lot).
            ctrl.maintscript.unconditionally_in_script("postrm", postrm_snippet)


def detect_xfonts(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    xfonts_root_dir = fs_root.lookup("./usr/share/fonts/X11/")
    if not xfonts_root_dir:
        return

    cmds = []
    cmds_postinst = []
    cmds_postrm = []
    escape_shell_words = ctrl.maintscript.escape_shell_words
    package_name = context.binary_package.name

    for xfonts_dir in xfonts_root_dir.iterdir:
        xfonts_dirname = xfonts_dir.name
        if not xfonts_dir.is_dir or xfonts_dirname.startswith("."):
            continue
        if fs_root.lookup(f"./etc/X11/xfonts/{xfonts_dirname}/{package_name}.scale"):
            cmds.append(escape_shell_words("update-fonts-scale", xfonts_dirname))
        cmds.append(
            escape_shell_words("update-fonts-dir", "--x11r7-layout", xfonts_dirname)
        )
        alias_file = fs_root.lookup(
            f"./etc/X11/xfonts/{xfonts_dirname}/{package_name}.alias"
        )
        if alias_file:
            cmds_postinst.append(
                escape_shell_words(
                    "update-fonts-alias",
                    "--include",
                    alias_file.absolute,
                    xfonts_dirname,
                )
            )
            cmds_postrm.append(
                escape_shell_words(
                    "update-fonts-alias",
                    "--exclude",
                    alias_file.absolute,
                    xfonts_dirname,
                )
            )

    if not cmds:
        return

    postinst_snippet = textwrap.dedent(
        f"""\
        if command -v update-fonts-dir >/dev/null; then
            {';'.join(itertools.chain(cmds, cmds_postinst))}
        fi
    """
    )

    postrm_snippet = textwrap.dedent(
        f"""\
        if [ -x "`command -v update-fonts-dir`" ]; then
            {';'.join(itertools.chain(cmds, cmds_postrm))}
        fi
    """
    )

    ctrl.maintscript.unconditionally_in_script("postinst", postinst_snippet)
    ctrl.maintscript.unconditionally_in_script("postrm", postrm_snippet)
    ctrl.substvars.add_dependency("misc:Depends", "xfonts-utils")


# debputy does not support python2, so we do not list python / python2.
_PYTHON_PUBLIC_DIST_DIR_NAMES = re.compile(r"(?:pypy|python)3(?:[.]\d+)?")


def _public_python_dist_dirs(fs_root: VirtualPath) -> Iterator[VirtualPath]:
    usr_lib = fs_root.lookup("./usr/lib")
    root_dirs = []
    if usr_lib:
        root_dirs.append(usr_lib)

    dbg_root = fs_root.lookup("./usr/lib/debug/usr/lib")
    if dbg_root:
        root_dirs.append(dbg_root)

    for root_dir in root_dirs:
        python_dirs = (
            path
            for path in root_dir.iterdir
            if path.is_dir and _PYTHON_PUBLIC_DIST_DIR_NAMES.match(path.name)
        )
        for python_dir in python_dirs:
            dist_packages = python_dir.get("dist-packages")
            if not dist_packages:
                continue
            yield dist_packages


def _has_py_file_in_dir(d: VirtualPath) -> bool:
    return any(f.is_file and f.name.endswith(".py") for f in d.all_paths())


def detect_pycompile_files(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    package = context.binary_package.name
    # TODO: Support configurable list of private dirs
    private_search_dirs = [
        fs_root.lookup(os.path.join(d, package))
        for d in [
            "./usr/share",
            "./usr/share/games",
            "./usr/lib",
            f"./usr/lib/{context.binary_package.deb_multiarch}",
            "./usr/lib/games",
        ]
    ]
    private_search_dirs_with_py_files = [
        p for p in private_search_dirs if p is not None and _has_py_file_in_dir(p)
    ]
    public_search_dirs_has_py_files = any(
        p is not None and _has_py_file_in_dir(p)
        for p in _public_python_dist_dirs(fs_root)
    )

    if not public_search_dirs_has_py_files and not private_search_dirs_with_py_files:
        return

    # The dh_python3 helper also supports -V and -X.  We do not use them. They can be
    # replaced by bcep support instead, which is how we will be supporting this kind
    # of configuration down the line.
    ctrl.maintscript.unconditionally_in_script(
        "prerm",
        textwrap.dedent(
            f"""\
        if command -v py3clean >/dev/null 2>&1; then
            py3clean -p {package}
        else
            dpkg -L {package} | sed -En -e '/^(.*)\\/(.+)\\.py$/s,,rm "\\1/__pycache__/\\2".*,e'
            find /usr/lib/python3/dist-packages/ -type d -name __pycache__ -empty -print0 | xargs --null --no-run-if-empty rmdir
        fi
        """
        ),
    )
    if public_search_dirs_has_py_files:
        ctrl.maintscript.on_configure(
            textwrap.dedent(
                f"""\
            if command -v py3compile >/dev/null 2>&1; then
                py3compile -p {package}
            fi
            if command -v pypy3compile >/dev/null 2>&1; then
                pypy3compile -p {package} || true
            fi
            """
            )
        )
    for private_dir in private_search_dirs_with_py_files:
        escaped_dir = ctrl.maintscript.escape_shell_words(private_dir.absolute)
        ctrl.maintscript.on_configure(
            textwrap.dedent(
                f"""\
            if command -v py3compile >/dev/null 2>&1; then
                py3compile -p {package} {escaped_dir}
            fi
            if command -v pypy3compile >/dev/null 2>&1; then
                pypy3compile -p {package} {escaped_dir} || true
            fi
            """
            )
        )


def translate_capabilities(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    caps = []
    maintscript = ctrl.maintscript
    for p in fs_root.all_paths():
        if not p.is_file:
            continue
        metadata_ref = p.metadata(DebputyCapability)
        capability = metadata_ref.value
        if capability is None:
            continue

        abs_path = maintscript.escape_shell_words(p.absolute)

        cap_script = "".join(
            [
                "    # Triggered by: {DEFINITION_SOURCE}\n"
                "    _TPATH=$(dpkg-divert --truename {ABS_PATH})\n",
                '    if setcap {CAP} "{DPKG_ROOT_UNQUOTED}${{_TPATH}}"; then\n',
                '        chmod {MODE} "{DPKG_ROOT_UNQUOTED}${{_TPATH}}"\n',
                '        echo "Successfully applied capabilities {CAP} on ${{_TPATH}}"\n',
                "    else\n",
                # We do not reset the mode here; generally a re-install or upgrade would re-store both mode,
                # and remove the capabilities.
                '        echo "The setcap failed to processes {CAP} on ${{_TPATH}}; falling back to no capability support" >&2\n',
                "    fi\n",
            ]
        ).format(
            CAP=maintscript.escape_shell_words(capability.capabilities).replace(
                "\\+", "+"
            ),
            DPKG_ROOT_UNQUOTED=DPKG_ROOT_UNQUOTED,
            ABS_PATH=abs_path,
            MODE=maintscript.escape_shell_words(str(capability.capability_mode)),
            DEFINITION_SOURCE=capability.definition_source.replace("\n", "\\n"),
        )
        assert cap_script.endswith("\n")
        caps.append(cap_script)

    if not caps:
        return

    maintscript.on_configure(
        textwrap.dedent(
            """\
        if command -v setcap > /dev/null; then
        {SET_CAP_COMMANDS}
            unset _TPATH
        else
            echo "The setcap utility is not installed available; falling back to no capability support" >&2
        fi
        """
        ).format(
            SET_CAP_COMMANDS="".join(caps).rstrip("\n"),
        )
    )


def pam_auth_update(
    fs_root: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    pam_configs = fs_root.lookup("/usr/share/pam-configs")
    if not pam_configs:
        return
    maintscript = ctrl.maintscript
    for pam_config in pam_configs.iterdir:
        if not pam_config.is_file:
            continue
        maintscript.on_configure("pam-auth-update --package\n")
        maintscript.on_before_removal(
            textwrap.dedent(
                f"""\
                if [ "${{DPKG_MAINTSCRIPT_PACKAGE_REFCOUNT:-1}}" = 1 ]; then
                    pam-auth-update --package --remove {maintscript.escape_shell_words(pam_config.name)}
                fi
                """
            )
        )


def auto_depends_arch_any_solink(
    fs_foot: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    package = context.binary_package
    if package.is_arch_all:
        return
    libbasedir = fs_foot.lookup("usr/lib")
    if not libbasedir:
        return
    libmadir = libbasedir.get(package.deb_multiarch)
    if libmadir:
        libdirs = [libmadir, libbasedir]
    else:
        libdirs = [libbasedir]
    targets = []
    for libdir in libdirs:
        for path in libdir.iterdir:
            if not path.is_symlink or not path.name.endswith(".so"):
                continue
            target = path.readlink()
            resolved = assume_not_none(path.parent_dir).lookup(target)
            if resolved is not None:
                continue
            targets.append((libdir.path, target))

    roots = list(context.accessible_package_roots())
    if not roots:
        return

    for libdir_path, target in targets:
        final_path = os.path.join(libdir_path, target)
        matches = []
        for opkg, ofs_root in roots:
            m = ofs_root.lookup(final_path)
            if not m:
                continue
            matches.append(opkg)
        if not matches or len(matches) > 1:
            if matches:
                all_matches = ", ".join(p.name for p in matches)
                _warn(
                    f"auto-depends-solink: The {final_path} was found in multiple packages ({all_matches}):"
                    f" Not generating a dependency."
                )
            else:
                _warn(
                    f"auto-depends-solink: The {final_path} was NOT found in any accessible package:"
                    " Not generating a dependency. This detection only works when both packages are arch:any"
                    " and they have the same build-profiles."
                )
            continue
        pkg_dep = matches[0]
        # The debputy API should not allow this constraint to fail
        assert pkg_dep.is_arch_all == package.is_arch_all
        # If both packages are arch:all or both are arch:any, we can generate a tight dependency
        relation = f"{pkg_dep.name} (= ${{binary:Version}})"
        ctrl.substvars.add_dependency("misc:Depends", relation)
