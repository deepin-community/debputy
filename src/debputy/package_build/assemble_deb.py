import json
import os
import subprocess
from typing import Optional, Sequence, List, Tuple

from debputy import DEBPUTY_ROOT_DIR
from debputy.commands.debputy_cmd.context import CommandContext
from debputy.deb_packaging_support import setup_control_files
from debputy.debhelper_emulation import dhe_dbgsym_root_dir
from debputy.filesystem_scan import FSRootDir
from debputy.highlevel_manifest import HighLevelManifest
from debputy.intermediate_manifest import IntermediateManifest
from debputy.plugin.api.impl_types import PackageDataTable
from debputy.util import (
    escape_shell,
    _error,
    compute_output_filename,
    scratch_dir,
    ensure_dir,
    _warn,
    assume_not_none,
)


_RRR_DEB_ASSEMBLY_KEYWORD = "debputy/deb-assembly"
_WARNED_ABOUT_FALLBACK_ASSEMBLY = False


def _serialize_intermediate_manifest(members: IntermediateManifest) -> str:
    serial_format = [m.to_manifest() for m in members]
    return json.dumps(serial_format)


def determine_assembly_method(
    package: str,
    intermediate_manifest: IntermediateManifest,
) -> Tuple[bool, bool, List[str]]:
    paths_needing_root = (
        tm for tm in intermediate_manifest if tm.owner != "root" or tm.group != "root"
    )
    matched_path = next(paths_needing_root, None)
    if matched_path is None:
        return False, False, []
    rrr = os.environ.get("DEB_RULES_REQUIRES_ROOT")
    if rrr and _RRR_DEB_ASSEMBLY_KEYWORD in rrr:
        gain_root_cmd = os.environ.get("DEB_GAIN_ROOT_CMD")
        if not gain_root_cmd:
            _error(
                "DEB_RULES_REQUIRES_ROOT contains a debputy keyword but DEB_GAIN_ROOT_CMD does not contain a "
                '"gain root" command'
            )
        return True, False, gain_root_cmd.split()
    if rrr == "no":
        global _WARNED_ABOUT_FALLBACK_ASSEMBLY
        if not _WARNED_ABOUT_FALLBACK_ASSEMBLY:
            _warn(
                'Using internal assembly method due to "Rules-Requires-Root" being "no" and dpkg-deb assembly would'
                " require (fake)root for binary packages that needs it."
            )
            _WARNED_ABOUT_FALLBACK_ASSEMBLY = True
        return True, True, []

    _error(
        f'Due to the path "{matched_path.member_path}" in {package}, the package assembly will require (fake)root.'
        " However, this command is not run as root nor was debputy requested to use a root command via"
        f' "Rules-Requires-Root".  Please consider adding "{_RRR_DEB_ASSEMBLY_KEYWORD}" to "Rules-Requires-Root"'
        " in debian/control. Though, due to #1036865, you may have to revert to"
        ' "Rules-Requires-Root: binary-targets" depending on which version of dpkg you need to support.'
        ' Alternatively, you can set "Rules-Requires-Root: no" in debian/control and debputy will assemble'
        " the package anyway. In this case, dpkg-deb will not be used, but the output should be bit-for-bit"
        " compatible with what debputy would have produced with dpkg-deb (and root/fakeroot)."
    )


def assemble_debs(
    context: CommandContext,
    manifest: HighLevelManifest,
    package_data_table: PackageDataTable,
    is_dh_rrr_only_mode: bool,
) -> None:
    parsed_args = context.parsed_args
    output_path = parsed_args.output
    upstream_args = parsed_args.upstream_args
    deb_materialize = str(DEBPUTY_ROOT_DIR / "deb_materialization.py")
    mtime = context.mtime

    for dctrl_bin in manifest.active_packages:
        package = dctrl_bin.name
        dbgsym_package_name = f"{package}-dbgsym"
        dctrl_data = package_data_table[package]
        fs_root = dctrl_data.fs_root
        control_output_dir = assume_not_none(dctrl_data.control_output_dir)
        package_metadata_context = dctrl_data.package_metadata_context
        if (
            dbgsym_package_name in package_data_table
            or "noautodbgsym" in manifest.build_env.deb_build_options
            or "noddebs" in manifest.build_env.deb_build_options
        ):
            # Discard the dbgsym part if it conflicts with a real package, or
            # we were asked not to build it.
            dctrl_data.dbgsym_info.dbgsym_fs_root = FSRootDir()
            dctrl_data.dbgsym_info.dbgsym_ids.clear()
        dbgsym_fs_root = dctrl_data.dbgsym_info.dbgsym_fs_root
        dbgsym_ids = dctrl_data.dbgsym_info.dbgsym_ids
        intermediate_manifest = manifest.finalize_data_tar_contents(
            package, fs_root, mtime
        )

        setup_control_files(
            dctrl_data,
            manifest,
            dbgsym_fs_root,
            dbgsym_ids,
            package_metadata_context,
            allow_ctrl_file_management=not is_dh_rrr_only_mode,
        )

        needs_root, use_fallback_assembly, gain_root_cmd = determine_assembly_method(
            package, intermediate_manifest
        )

        if not dctrl_bin.is_udeb and any(
            f for f in dbgsym_fs_root.all_paths() if f.is_file
        ):
            # We never built udebs due to #797391. We currently do not generate a control
            # file for it either for the same reason.
            dbgsym_root = dhe_dbgsym_root_dir(dctrl_bin)
            if not os.path.isdir(output_path):
                _error(
                    "Cannot produce a dbgsym package when output path is not a directory."
                )
            dbgsym_intermediate_manifest = manifest.finalize_data_tar_contents(
                dbgsym_package_name,
                dbgsym_fs_root,
                mtime,
            )
            _assemble_deb(
                dbgsym_package_name,
                deb_materialize,
                dbgsym_intermediate_manifest,
                mtime,
                os.path.join(dbgsym_root, "DEBIAN"),
                output_path,
                upstream_args,
                is_udeb=dctrl_bin.is_udeb,  # Review this if we ever do dbgsyms for udebs
                use_fallback_assembly=False,
                needs_root=False,
            )

        _assemble_deb(
            package,
            deb_materialize,
            intermediate_manifest,
            mtime,
            control_output_dir,
            output_path,
            upstream_args,
            is_udeb=dctrl_bin.is_udeb,
            use_fallback_assembly=use_fallback_assembly,
            needs_root=needs_root,
            gain_root_cmd=gain_root_cmd,
        )


def _assemble_deb(
    package: str,
    deb_materialize_cmd: str,
    intermediate_manifest: IntermediateManifest,
    mtime: int,
    control_output_dir: str,
    output_path: str,
    upstream_args: Optional[List[str]],
    is_udeb: bool = False,
    use_fallback_assembly: bool = False,
    needs_root: bool = False,
    gain_root_cmd: Optional[Sequence[str]] = None,
) -> None:
    scratch_root_dir = scratch_dir()
    materialization_dir = os.path.join(
        scratch_root_dir, "materialization-dirs", package
    )
    ensure_dir(os.path.dirname(materialization_dir))
    materialize_cmd: List[str] = []
    assert not use_fallback_assembly or not gain_root_cmd
    if needs_root and gain_root_cmd:
        # Only use the gain_root_cmd if we absolutely need it.
        # Note that gain_root_cmd will be empty unless R³ is set to the relevant keyword
        # that would make us use targeted promotion. Therefore, we do not need to check other
        # conditions than the package needing root. (R³: binary-targets implies `needs_root=True`
        # without a gain_root_cmd)
        materialize_cmd.extend(gain_root_cmd)
    materialize_cmd.extend(
        [
            deb_materialize_cmd,
            "materialize-deb",
            "--intermediate-package-manifest",
            "-",
            "--may-move-control-files",
            "--may-move-data-files",
            "--source-date-epoch",
            str(mtime),
            "--discard-existing-output",
            control_output_dir,
            materialization_dir,
        ]
    )
    output = output_path
    if is_udeb:
        materialize_cmd.append("--udeb")
        output = os.path.join(
            output_path, compute_output_filename(control_output_dir, True)
        )

    assembly_method = "debputy" if needs_root and use_fallback_assembly else "dpkg-deb"
    combined_materialization_and_assembly = not needs_root
    if combined_materialization_and_assembly:
        materialize_cmd.extend(
            ["--build-method", assembly_method, "--assembled-deb-output", output]
        )

    if upstream_args:
        materialize_cmd.append("--")
        materialize_cmd.extend(upstream_args)

    if combined_materialization_and_assembly:
        print(
            f"Materializing and assembling {package} via: {escape_shell(*materialize_cmd)}"
        )
    else:
        print(f"Materializing {package} via: {escape_shell(*materialize_cmd)}")
    proc = subprocess.Popen(materialize_cmd, stdin=subprocess.PIPE)
    proc.communicate(
        _serialize_intermediate_manifest(intermediate_manifest).encode("utf-8")
    )
    if proc.returncode != 0:
        _error(f"{escape_shell(deb_materialize_cmd)} exited with a non-zero exit code!")

    if not combined_materialization_and_assembly:
        build_materialization = [
            deb_materialize_cmd,
            "build-materialized-deb",
            materialization_dir,
            assembly_method,
            "--output",
            output,
        ]
        print(f"Assembling {package} via: {escape_shell(*build_materialization)}")
        try:
            subprocess.check_call(build_materialization)
        except subprocess.CalledProcessError as e:
            exit_code = f" with exit code {e.returncode}" if e.returncode else ""
            _error(
                f"Assembly command for {package} failed{exit_code}. Please review the output of the command"
                f" for more details on the problem."
            )
