#!/usr/bin/python3 -B
import argparse
import collections
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime
from typing import Optional, List, Iterator, Dict, Tuple

from debputy import DEBPUTY_ROOT_DIR
from debputy.intermediate_manifest import (
    TarMember,
    PathType,
    output_intermediate_manifest,
    output_intermediate_manifest_to_fd,
)
from debputy.util import (
    _error,
    _info,
    compute_output_filename,
    resolve_source_date_epoch,
    ColorizedArgumentParser,
    setup_logging,
    detect_fakeroot,
    print_command,
    program_name,
)
from debputy.version import __version__


def parse_args() -> argparse.Namespace:
    description = textwrap.dedent(
        """\
    This is a low level tool for materializing deb packages from intermediate debputy manifests or assembling
    the deb from a materialization.

    The tool is not intended to be run directly by end users.
    """
    )

    parser = ColorizedArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        prog=program_name(),
    )

    parser.add_argument("--version", action="version", version=__version__)

    subparsers = parser.add_subparsers(dest="command", required=True)

    materialize_deb_parser = subparsers.add_parser(
        "materialize-deb",
        allow_abbrev=False,
        help="Generate .deb/.udebs structure from a root directory and"
        " a *intermediate* debputy manifest",
    )
    materialize_deb_parser.add_argument(
        "control_root_dir",
        metavar="control-root-dir",
        help="A directory that contains the control files (usually debian/<pkg>/DEBIAN)",
    )
    materialize_deb_parser.add_argument(
        "materialization_output",
        metavar="materialization_output",
        help="Where to place the resulting structure should be placed. Should not exist",
    )
    materialize_deb_parser.add_argument(
        "--discard-existing-output",
        dest="discard_existing_output",
        default=False,
        action="store_true",
        help="If passed, then the output location may exist."
        " If it does, it will be *deleted*.",
    )
    materialize_deb_parser.add_argument(
        "--source-date-epoch",
        dest="source_date_epoch",
        action="store",
        type=int,
        default=None,
        help="Source date epoch (can also be given via the SOURCE_DATE_EPOCH environ"
        " variable",
    )
    materialize_deb_parser.add_argument(
        "--may-move-control-files",
        dest="may_move_control_files",
        action="store_true",
        default=False,
        help="Whether the command may optimize by moving (rather than copying) DEBIAN files",
    )
    materialize_deb_parser.add_argument(
        "--may-move-data-files",
        dest="may_move_data_files",
        action="store_true",
        default=False,
        help="Whether the command may optimize by moving (rather than copying) when materializing",
    )

    materialize_deb_parser.add_argument(
        "--intermediate-package-manifest",
        dest="package_manifest",
        metavar="JSON_FILE",
        action="store",
        default=None,
        help="INTERMEDIATE package manifest (JSON!)",
    )

    materialize_deb_parser.add_argument(
        "--udeb",
        dest="udeb",
        default=False,
        action="store_true",
        help="Whether this is udeb package.  Affects extension and default compression",
    )

    materialize_deb_parser.add_argument(
        "--build-method",
        dest="build_method",
        choices=["debputy", "dpkg-deb"],
        type=str,
        default=None,
        help="Immediately assemble the deb as well using the selected method",
    )
    materialize_deb_parser.add_argument(
        "--assembled-deb-output",
        dest="assembled_deb_output",
        type=str,
        default=None,
        help="Where to place the resulting deb. Only applicable with --build-method",
    )

    # Added for "help only" - you cannot trigger this option in practice
    materialize_deb_parser.add_argument(
        "--",
        metavar="DPKG_DEB_ARGS",
        action="extend",
        nargs="+",
        dest="unused",
        help="Arguments to be passed to dpkg-deb"
        " (same as you might pass to dh_builddeb).",
    )

    build_deb_structure = subparsers.add_parser(
        "build-materialized-deb",
        allow_abbrev=False,
        help="Produce a .deb from a directory produced by the"
        " materialize-deb-structure command",
    )
    build_deb_structure.add_argument(
        "materialized_deb_root_dir",
        metavar="materialized-deb-root-dir",
        help="The output directory of the materialize-deb-structure command",
    )
    build_deb_structure.add_argument(
        "build_method",
        metavar="build-method",
        choices=["debputy", "dpkg-deb"],
        type=str,
        default="dpkg-deb",
        help="Which tool should assemble the deb",
    )
    build_deb_structure.add_argument(
        "--output", type=str, default=None, help="Where to place the resulting deb"
    )

    argv = sys.argv
    try:
        i = argv.index("--")
        upstream_args = argv[i + 1 :]
        argv = argv[:i]
    except (IndexError, ValueError):
        upstream_args = []
    parsed_args = parser.parse_args(argv[1:])
    setattr(parsed_args, "upstream_args", upstream_args)

    return parsed_args


def _run(cmd: List[str]) -> None:
    print_command(*cmd)
    subprocess.check_call(cmd)


def strip_path_prefix(member_path: str) -> str:
    if not member_path.startswith("./"):
        _error(
            f'Invalid manifest: "{member_path}" does not start with "./", but all paths should'
        )
    return member_path[2:]


def _perform_data_tar_materialization(
    output_packaging_root: str,
    intermediate_manifest: List[TarMember],
    may_move_data_files: bool,
) -> List[Tuple[str, TarMember]]:
    start_time = datetime.now()
    replacement_manifest_paths = []
    _info("Materializing data.tar part of the deb:")

    directories = ["mkdir"]
    symlinks = []
    bulk_copies: Dict[str, List[str]] = collections.defaultdict(list)
    copies = []
    renames = []

    for tar_member in intermediate_manifest:
        member_path = strip_path_prefix(tar_member.member_path)
        new_fs_path = (
            os.path.join("deb-root", member_path) if member_path else "deb-root"
        )
        materialization_path = (
            f"{output_packaging_root}/{member_path}"
            if member_path
            else output_packaging_root
        )
        replacement_tar_member = tar_member
        materialization_parent_dir = os.path.dirname(materialization_path.rstrip("/"))
        if tar_member.path_type == PathType.DIRECTORY:
            directories.append(materialization_path)
        elif tar_member.path_type == PathType.SYMLINK:
            symlinks.append((tar_member.link_target, materialization_path))
        elif tar_member.fs_path is not None:
            if tar_member.link_target:
                # Not sure if hardlinks gets here yet as we do not support hardlinks
                _error("Internal error; hardlink not supported")

            if may_move_data_files and tar_member.may_steal_fs_path:
                renames.append((tar_member.fs_path, materialization_path))
            elif os.path.basename(tar_member.fs_path) == os.path.basename(
                materialization_path
            ):
                bulk_copies[materialization_parent_dir].append(tar_member.fs_path)
            else:
                copies.append((tar_member.fs_path, materialization_path))
        else:
            _error(f"Internal error; unsupported path type {tar_member.path_type}")

        if tar_member.fs_path is not None:
            replacement_tar_member = tar_member.clone_and_replace(
                fs_path=new_fs_path, may_steal_fs_path=False
            )

        replacement_manifest_paths.append(
            (materialization_path, replacement_tar_member)
        )

    if len(directories) > 1:
        _run(directories)

    for dest_dir, files in bulk_copies.items():
        cmd = ["cp", "--reflink=auto", "-t", dest_dir]
        cmd.extend(files)
        _run(cmd)

    for source, dest in copies:
        _run(["cp", "--reflink=auto", source, dest])

    for source, dest in renames:
        print_command("mv", source, dest)
        os.rename(source, dest)

    for link_target, link_path in symlinks:
        print_command("ln", "-s", link_target, link_path)
        os.symlink(link_target, link_path)

    end_time = datetime.now()

    _info(f"Materialization of data.tar finished, took: {end_time - start_time}")

    return replacement_manifest_paths


def materialize_deb(
    control_root_dir: str,
    intermediate_manifest_path: Optional[str],
    source_date_epoch: int,
    dpkg_deb_options: List[str],
    is_udeb: bool,
    output_dir: str,
    may_move_control_files: bool,
    may_move_data_files: bool,
) -> None:
    if not os.path.isfile(f"{control_root_dir}/control"):
        _error(
            f'The directory "{control_root_dir}" does not look like a package root dir (there is no control file)'
        )
    intermediate_manifest: List[TarMember] = parse_manifest(intermediate_manifest_path)

    output_packaging_root = os.path.join(output_dir, "deb-root")
    os.mkdir(output_dir)

    replacement_manifest_paths = _perform_data_tar_materialization(
        output_packaging_root, intermediate_manifest, may_move_data_files
    )
    for materialization_path, tar_member in reversed(replacement_manifest_paths):
        # TODO: Hardlinks should probably skip these commands
        if tar_member.path_type != PathType.SYMLINK:
            os.chmod(materialization_path, tar_member.mode, follow_symlinks=False)
        os.utime(
            materialization_path,
            (tar_member.mtime, tar_member.mtime),
            follow_symlinks=False,
        )

    materialized_ctrl_dir = f"{output_packaging_root}/DEBIAN"
    if may_move_control_files:
        print_command("mv", control_root_dir, materialized_ctrl_dir)
        os.rename(control_root_dir, materialized_ctrl_dir)
    else:
        os.mkdir(materialized_ctrl_dir)
        copy_cmd = ["cp", "-a"]
        copy_cmd.extend(
            os.path.join(control_root_dir, f) for f in os.listdir(control_root_dir)
        )
        copy_cmd.append(materialized_ctrl_dir)
        _run(copy_cmd)

    output_intermediate_manifest(
        os.path.join(output_dir, "deb-structure-intermediate-manifest.json"),
        [t[1] for t in replacement_manifest_paths],
    )

    with open(os.path.join(output_dir, "env-and-cli.json"), "w") as fd:
        serial_format = {
            "env": {
                "SOURCE_DATE_EPOCH": str(source_date_epoch),
                "DPKG_DEB_COMPRESSOR_LEVEL": os.environ.get(
                    "DPKG_DEB_COMPRESSOR_LEVEL"
                ),
                "DPKG_DEB_COMPRESSOR_TYPE": os.environ.get("DPKG_DEB_COMPRESSOR_TYPE"),
                "DPKG_DEB_THREADS_MAX": os.environ.get("DPKG_DEB_THREADS_MAX"),
            },
            "cli": {"dpkg-deb": dpkg_deb_options},
            "udeb": is_udeb,
        }
        json.dump(serial_format, fd)


def apply_fs_metadata(
    materialized_path: str,
    tar_member: TarMember,
    apply_ownership: bool,
    is_using_fakeroot: bool,
) -> None:
    if apply_ownership:
        os.chown(
            materialized_path, tar_member.uid, tar_member.gid, follow_symlinks=False
        )
    # To avoid surprises, align these with the manifest. Just in case the transport did not preserve the metadata.
    # Also, unsure whether metadata changes cause directory mtimes to change, so resetting them unconditionally
    # also prevents that problem.
    if tar_member.path_type != PathType.SYMLINK:
        os.chmod(materialized_path, tar_member.mode, follow_symlinks=False)
    os.utime(
        materialized_path, (tar_member.mtime, tar_member.mtime), follow_symlinks=False
    )
    if is_using_fakeroot:
        st = os.stat(materialized_path, follow_symlinks=False)
        if st.st_uid != tar_member.uid or st.st_gid != tar_member.gid:
            _error(
                'Change of ownership failed. The chown call "succeeded" but stat does not give the right result.'
                " Most likely a fakeroot bug. Note, when verifying this, use os.chown + os.stat from python"
                " (the chmod/stat shell commands might use a different syscall that fakeroot accurately emulates)"
            )


def _dpkg_deb_root_requirements(
    intermediate_manifest: List[TarMember],
) -> Tuple[List[str], bool, bool]:
    needs_root = any(tm.uid != 0 or tm.gid != 0 for tm in intermediate_manifest)
    if needs_root:
        if os.getuid() != 0:
            _error(
                'Must be run as root/fakeroot when using the method "dpkg-deb" due to the contents'
            )
        is_using_fakeroot = detect_fakeroot()
        deb_cmd = ["dpkg-deb"]
        _info("Applying ownership, mode, and utime from the intermediate manifest...")
    else:
        # fakeroot does not matter in this case
        is_using_fakeroot = False
        deb_cmd = ["dpkg-deb", "--root-owner-group"]
        _info("Applying mode and utime from the intermediate manifest...")
    return deb_cmd, needs_root, is_using_fakeroot


@contextlib.contextmanager
def maybe_with_materialized_manifest(
    content: Optional[List[TarMember]],
) -> Iterator[Optional[str]]:
    if content is not None:
        with tempfile.NamedTemporaryFile(
            prefix="debputy-mat-build",
            mode="w+t",
            suffix=".json",
            encoding="utf-8",
        ) as fd:
            output_intermediate_manifest_to_fd(fd, content)
            fd.flush()
            yield fd.name
    else:
        yield None


def _prep_assembled_deb_output_path(
    output_path: Optional[str],
    materialized_deb_structure: str,
    deb_root: str,
    method: str,
    is_udeb: bool,
) -> str:
    if output_path is None:
        ext = "udeb" if is_udeb else "deb"
        output_dir = os.path.join(materialized_deb_structure, "output")
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)
        output = os.path.join(output_dir, f"{method}.{ext}")
    elif os.path.isdir(output_path):
        output = os.path.join(
            output_path,
            compute_output_filename(os.path.join(deb_root, "DEBIAN"), is_udeb),
        )
    else:
        output = output_path
    return output


def _apply_env(env: Dict[str, Optional[str]]) -> None:
    for name, value in env.items():
        if value is not None:
            os.environ[name] = value
        else:
            try:
                del os.environ[name]
            except KeyError:
                pass


def assemble_deb(
    materialized_deb_structure: str,
    method: str,
    output_path: Optional[str],
    combined_materialization_and_assembly: bool,
) -> None:
    deb_root = os.path.join(materialized_deb_structure, "deb-root")

    with open(os.path.join(materialized_deb_structure, "env-and-cli.json"), "r") as fd:
        serial_format = json.load(fd)

    env = serial_format.get("env") or {}
    cli = serial_format.get("cli") or {}
    is_udeb = serial_format.get("udeb")
    source_date_epoch = env.get("SOURCE_DATE_EPOCH")
    dpkg_deb_options = cli.get("dpkg-deb") or []
    intermediate_manifest_path = os.path.join(
        materialized_deb_structure, "deb-structure-intermediate-manifest.json"
    )
    original_intermediate_manifest = TarMember.parse_intermediate_manifest(
        intermediate_manifest_path
    )
    _info(
        "Rebasing relative paths in the intermediate manifest so they are relative to current working directory ..."
    )
    intermediate_manifest = [
        (
            tar_member.clone_and_replace(
                fs_path=os.path.join(materialized_deb_structure, tar_member.fs_path)
            )
            if tar_member.fs_path is not None and not tar_member.fs_path.startswith("/")
            else tar_member
        )
        for tar_member in original_intermediate_manifest
    ]
    materialized_manifest = None
    if method == "debputy":
        materialized_manifest = intermediate_manifest

    if source_date_epoch is None:
        _error(
            "Cannot reproduce the deb. No source date epoch provided in the materialized deb root."
        )
    _apply_env(env)

    output = _prep_assembled_deb_output_path(
        output_path,
        materialized_deb_structure,
        deb_root,
        method,
        is_udeb,
    )

    with maybe_with_materialized_manifest(materialized_manifest) as tmp_file:
        if method == "dpkg-deb":
            deb_cmd, needs_root, is_using_fakeroot = _dpkg_deb_root_requirements(
                intermediate_manifest
            )
            if needs_root or not combined_materialization_and_assembly:
                for tar_member in reversed(intermediate_manifest):
                    p = os.path.join(
                        deb_root, strip_path_prefix(tar_member.member_path)
                    )
                    apply_fs_metadata(p, tar_member, needs_root, is_using_fakeroot)
        elif method == "debputy":
            deb_packer = os.path.join(DEBPUTY_ROOT_DIR, "deb_packer.py")
            assert tmp_file is not None
            deb_cmd = [
                deb_packer,
                "--intermediate-package-manifest",
                tmp_file,
                "--source-date-epoch",
                source_date_epoch,
            ]
        else:
            _error(f"Internal error: Unsupported assembly method: {method}")

        if is_udeb:
            deb_cmd.extend(["-z6", "-Zxz", "-Sextreme"])
        deb_cmd.extend(dpkg_deb_options)
        deb_cmd.extend(["--build", deb_root, output])
        start_time = datetime.now()
        _run(deb_cmd)
        end_time = datetime.now()
        _info(f"  - assembly command took {end_time - start_time}")


def parse_manifest(manifest_path: "Optional[str]") -> "List[TarMember]":
    if manifest_path is None:
        _error("--intermediate-package-manifest is mandatory for now")
    return TarMember.parse_intermediate_manifest(manifest_path)


def main() -> None:
    setup_logging()
    parsed_args = parse_args()
    if parsed_args.command == "materialize-deb":
        mtime = resolve_source_date_epoch(parsed_args.source_date_epoch)
        dpkg_deb_args = parsed_args.upstream_args or []
        output_dir = parsed_args.materialization_output
        if os.path.exists(output_dir):
            if not parsed_args.discard_existing_output:
                _error(
                    "The output path already exists. Please either choose a non-existing path, delete the path"
                    " or use --discard-existing-output (to have this command remove it as necessary)."
                )
            _info(
                f'Removing existing path "{output_dir}" as requested by --discard-existing-output'
            )
            _run(["rm", "-fr", output_dir])

        materialize_deb(
            parsed_args.control_root_dir,
            parsed_args.package_manifest,
            mtime,
            dpkg_deb_args,
            parsed_args.udeb,
            output_dir,
            parsed_args.may_move_control_files,
            parsed_args.may_move_data_files,
        )

        if parsed_args.build_method is not None:
            assemble_deb(
                output_dir,
                parsed_args.build_method,
                parsed_args.assembled_deb_output,
                True,
            )

    elif parsed_args.command == "build-materialized-deb":
        assemble_deb(
            parsed_args.materialized_deb_root_dir,
            parsed_args.build_method,
            parsed_args.output,
            False,
        )
    else:
        _error(f'Internal error: Unimplemented command "{parsed_args.command}"')


if __name__ == "__main__":
    main()
