#!/usr/bin/python3 -B
import argparse
import errno
import operator
import os
import stat
import subprocess
import tarfile
import textwrap
from typing import Optional, List, FrozenSet, Iterable, Callable, BinaryIO, cast

from debputy.intermediate_manifest import TarMember, PathType
from debputy.util import (
    _error,
    compute_output_filename,
    resolve_source_date_epoch,
    ColorizedArgumentParser,
    setup_logging,
    program_name,
    assume_not_none,
)
from debputy.version import __version__


# AR header / start of a deb file for reference
# 00000000  21 3c 61 72 63 68 3e 0a  64 65 62 69 61 6e 2d 62  |!<arch>.debian-b|
# 00000010  69 6e 61 72 79 20 20 20  31 36 36 38 39 37 33 36  |inary   16689736|
# 00000020  39 35 20 20 30 20 20 20  20 20 30 20 20 20 20 20  |95  0     0     |
# 00000030  31 30 30 36 34 34 20 20  34 20 20 20 20 20 20 20  |100644  4       |
# 00000040  20 20 60 0a 32 2e 30 0a  63 6f 6e 74 72 6f 6c 2e  |  `.2.0.control.|
# 00000050  74 61 72 2e 78 7a 20 20  31 36 36 38 39 37 33 36  |tar.xz  16689736|
# 00000060  39 35 20 20 30 20 20 20  20 20 30 20 20 20 20 20  |95  0     0     |
# 00000070  31 30 30 36 34 34 20 20  39 33 36 38 20 20 20 20  |100644  9368    |
# 00000080  20 20 60 0a fd 37 7a 58  5a 00 00 04 e6 d6 b4 46  |  `..7zXZ......F|


class ArMember:
    def __init__(
        self,
        name: str,
        mtime: int,
        fixed_binary: Optional[bytes] = None,
        write_to_impl: Optional[Callable[[BinaryIO], None]] = None,
    ) -> None:
        self.name = name
        self._mtime = mtime
        self._write_to_impl = write_to_impl
        self.fixed_binary = fixed_binary

    @property
    def is_fixed_binary(self) -> bool:
        return self.fixed_binary is not None

    @property
    def mtime(self) -> int:
        return self.mtime

    def write_to(self, fd: BinaryIO) -> None:
        writer = self._write_to_impl
        assert writer is not None
        writer(fd)


AR_HEADER_LEN = 60
AR_HEADER = b" " * AR_HEADER_LEN


def write_header(
    fd: BinaryIO,
    member: ArMember,
    member_len: int,
    mtime: int,
) -> None:
    header = b"%-16s%-12d0     0     100644  %-10d\x60\n" % (
        member.name.encode("ascii"),
        mtime,
        member_len,
    )
    fd.write(header)


def generate_ar_archive(
    output_filename: str,
    mtime: int,
    members: Iterable[ArMember],
    prefer_raw_exceptions: bool,
) -> None:
    try:
        with open(output_filename, "wb", buffering=0) as fd:
            fd.write(b"!<arch>\n")
            for member in members:
                if member.is_fixed_binary:
                    fixed_binary = assume_not_none(member.fixed_binary)
                    write_header(fd, member, len(fixed_binary), mtime)
                    fd.write(fixed_binary)
                else:
                    header_pos = fd.tell()
                    fd.write(AR_HEADER)
                    member.write_to(fd)
                    current_pos = fd.tell()
                    fd.seek(header_pos, os.SEEK_SET)
                    content_len = current_pos - header_pos - AR_HEADER_LEN
                    assert content_len >= 0
                    write_header(fd, member, content_len, mtime)
                    fd.seek(current_pos, os.SEEK_SET)
    except OSError as e:
        if prefer_raw_exceptions:
            raise
        if e.errno == errno.ENOSPC:
            _error(
                f"Unable to write {output_filename}.  The file system device reported disk full: {str(e)}"
            )
        elif e.errno == errno.EIO:
            _error(
                f"Unable to write {output_filename}.  The file system reported a generic I/O error: {str(e)}"
            )
        elif e.errno == errno.EROFS:
            _error(
                f"Unable to write {output_filename}.  The file system is read-only: {str(e)}"
            )
        raise
    print(f"Generated {output_filename}")


def _generate_tar_file(
    tar_members: Iterable[TarMember],
    compression_cmd: List[str],
    write_to: BinaryIO,
) -> None:
    with (
        subprocess.Popen(
            compression_cmd, stdin=subprocess.PIPE, stdout=write_to
        ) as compress_proc,
        tarfile.open(
            mode="w|",
            fileobj=compress_proc.stdin,
            format=tarfile.GNU_FORMAT,
            errorlevel=1,
        ) as tar_fd,
    ):
        for tar_member in tar_members:
            tar_info: tarfile.TarInfo = tar_member.create_tar_info(tar_fd)
            if tar_member.path_type == PathType.FILE:
                with open(assume_not_none(tar_member.fs_path), "rb") as mfd:
                    tar_fd.addfile(tar_info, fileobj=mfd)
            else:
                tar_fd.addfile(tar_info)
    compress_proc.wait()
    if compress_proc.returncode != 0:
        _error(
            f"Compression command {compression_cmd} failed with code {compress_proc.returncode}"
        )


def generate_tar_file_member(
    tar_members: Iterable[TarMember],
    compression_cmd: List[str],
) -> Callable[[BinaryIO], None]:
    def _impl(fd: BinaryIO) -> None:
        _generate_tar_file(
            tar_members,
            compression_cmd,
            fd,
        )

    return _impl


def _xz_cmdline(
    compression_rule: "Compression",
    parsed_args: Optional[argparse.Namespace],
) -> List[str]:
    compression_level = compression_rule.effective_compression_level(parsed_args)
    cmdline = ["xz", "-T2", "-" + str(compression_level)]
    strategy = None if parsed_args is None else parsed_args.compression_strategy
    if strategy is None:
        strategy = "none"
    if strategy != "none":
        cmdline.append("--" + strategy)
    cmdline.append("--no-adjust")
    return cmdline


def _gzip_cmdline(
    compression_rule: "Compression",
    parsed_args: Optional[argparse.Namespace],
) -> List[str]:
    compression_level = compression_rule.effective_compression_level(parsed_args)
    cmdline = ["gzip", "-n" + str(compression_level)]
    strategy = None if parsed_args is None else parsed_args.compression_strategy
    if strategy is not None and strategy != "none":
        raise ValueError(
            f"Not implemented: Compression strategy {strategy}"
            " for gzip is currently unsupported (but dpkg-deb does)"
        )
    return cmdline


def _uncompressed_cmdline(
    _unused_a: "Compression",
    _unused_b: Optional[argparse.Namespace],
) -> List[str]:
    return ["cat"]


class Compression:
    def __init__(
        self,
        default_compression_level: int,
        extension: str,
        allowed_strategies: FrozenSet[str],
        cmdline_builder: Callable[
            ["Compression", Optional[argparse.Namespace]], List[str]
        ],
    ) -> None:
        self.default_compression_level = default_compression_level
        self.extension = extension
        self.allowed_strategies = allowed_strategies
        self.cmdline_builder = cmdline_builder

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.extension}>"

    def effective_compression_level(
        self, parsed_args: Optional[argparse.Namespace]
    ) -> int:
        if parsed_args and parsed_args.compression_level is not None:
            return cast("int", parsed_args.compression_level)
        return self.default_compression_level

    def as_cmdline(self, parsed_args: Optional[argparse.Namespace]) -> List[str]:
        return self.cmdline_builder(self, parsed_args)

    def with_extension(self, filename: str) -> str:
        return filename + self.extension


COMPRESSIONS = {
    "xz": Compression(6, ".xz", frozenset({"none", "extreme"}), _xz_cmdline),
    "gzip": Compression(
        9,
        ".gz",
        frozenset({"none", "filtered", "huffman", "rle", "fixed"}),
        _gzip_cmdline,
    ),
    "none": Compression(0, "", frozenset({"none"}), _uncompressed_cmdline),
}


def _normalize_compression_args(parsed_args: argparse.Namespace) -> argparse.Namespace:
    if (
        parsed_args.compression_level == 0
        and parsed_args.compression_algorithm == "gzip"
    ):
        print(
            "Note: Mapping compression algorithm to none for compatibility with dpkg-deb (due to -Zgzip -z0)"
        )
        setattr(parsed_args, "compression_algorithm", "none")

    compression = COMPRESSIONS[parsed_args.compression_algorithm]
    strategy = parsed_args.compression_strategy
    if strategy is not None and strategy not in compression.allowed_strategies:
        _error(
            f'Compression algorithm "{parsed_args.compression_algorithm}" does not support compression strategy'
            f' "{strategy}".  Allowed values: {", ".join(sorted(compression.allowed_strategies))}'
        )
    return parsed_args


def parse_args() -> argparse.Namespace:
    try:
        compression_level_default = int(os.environ["DPKG_DEB_COMPRESSOR_LEVEL"])
    except (KeyError, ValueError):
        compression_level_default = None

    try:
        compression_type = os.environ["DPKG_DEB_COMPRESSOR_TYPE"]
    except (KeyError, ValueError):
        compression_type = "xz"

    try:
        threads_max = int(os.environ["DPKG_DEB_THREADS_MAX"])
    except (KeyError, ValueError):
        threads_max = None

    description = textwrap.dedent(
        """\
    THIS IS A PROTOTYPE "dpkg-deb -b" emulator with basic manifest support

    DO NOT USE THIS TOOL DIRECTLY.  It has not stability guarantees and will be removed as
    soon as "dpkg-deb -b" grows support for the relevant features.

    This tool is a prototype "dpkg-deb -b"-like interface for compiling a Debian package
    without requiring root even for static ownership.  It is a temporary stand-in for
    "dpkg-deb -b" until "dpkg-deb -b" will get support for a manifest.

    The tool operates on an internal JSON based manifest for now, because it was faster
    than building an mtree parser (which is the format that dpkg will likely end up
    using).

    As the tool is not meant to be used directly, it is full of annoying paper cuts that
    I refuse to fix or maintain. Use the high level tool instead.

    """
    )

    parser = ColorizedArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        prog=program_name(),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "package_root_dir",
        metavar="PACKAGE_ROOT_DIR",
        help="Root directory of the package. Must contain a DEBIAN directory",
    )
    parser.add_argument(
        "package_output_path",
        metavar="PATH",
        help="Path where the package should be placed.  If it is directory,"
        " the base name will be determined from the package metadata",
    )

    parser.add_argument(
        "--intermediate-package-manifest",
        dest="package_manifest",
        metavar="JSON_FILE",
        action="store",
        default=None,
        help="INTERMEDIATE package manifest (JSON!)",
    )
    parser.add_argument(
        "--root-owner-group",
        dest="root_owner_group",
        action="store_true",
        help="Ignored. Accepted for compatibility with dpkg-deb -b",
    )
    parser.add_argument(
        "-b",
        "--build",
        dest="build_param",
        action="store_true",
        help="Ignored. Accepted for compatibility with dpkg-deb",
    )
    parser.add_argument(
        "--source-date-epoch",
        dest="source_date_epoch",
        action="store",
        type=int,
        default=None,
        help="Source date epoch (can also be given via the SOURCE_DATE_EPOCH environ variable",
    )
    parser.add_argument(
        "-Z",
        dest="compression_algorithm",
        choices=COMPRESSIONS,
        default=compression_type,
        help="The compression algorithm to be used",
    )
    parser.add_argument(
        "-z",
        dest="compression_level",
        metavar="{0-9}",
        choices=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        default=compression_level_default,
        type=int,
        help="The compression level to be used",
    )
    parser.add_argument(
        "-S",
        dest="compression_strategy",
        # We have a different default for xz when strategy is unset and we are building a udeb
        action="store",
        default=None,
        help="The compression algorithm to be used. Concrete values depend on the compression"
        ' algorithm, but the value "none" is always allowed',
    )
    parser.add_argument(
        "--uniform-compression",
        dest="uniform_compression",
        action="store_true",
        default=True,
        help="Whether to use the same compression for the control.tar and the data.tar."
        " The default is to use uniform compression.",
    )
    parser.add_argument(
        "--no-uniform-compression",
        dest="uniform_compression",
        action="store_false",
        default=True,
        help="Disable uniform compression (see --uniform-compression)",
    )
    parser.add_argument(
        "--threads-max",
        dest="threads_max",
        default=threads_max,
        # TODO: Support this properly
        type=int,
        help="Ignored; accepted for compatibility",
    )
    parser.add_argument(
        "-d",
        "--debug",
        dest="debug_mode",
        action="store_true",
        default=False,
        help="Enable debug logging and raw stack traces on errors",
    )

    parsed_args = parser.parse_args()
    parsed_args = _normalize_compression_args(parsed_args)

    return parsed_args


def _ctrl_member(
    member_path: str,
    fs_path: Optional[str] = None,
    path_type: PathType = PathType.FILE,
    mode: int = 0o644,
    mtime: int = 0,
) -> TarMember:
    if fs_path is None:
        assert member_path.startswith("./")
        fs_path = "DEBIAN" + member_path[1:]
    return TarMember(
        member_path=member_path,
        path_type=path_type,
        fs_path=fs_path,
        mode=mode,
        owner="root",
        uid=0,
        group="root",
        gid=0,
        mtime=mtime,
    )


CTRL_MEMBER_SCRIPTS = {
    "postinst",
    "preinst",
    "postrm",
    "prerm",
    "config",
    "isinstallable",
}


def _ctrl_tar_members(package_root_dir: str, mtime: int) -> Iterable[TarMember]:
    debian_root = os.path.join(package_root_dir, "DEBIAN")
    dir_st = os.stat(debian_root)
    dir_mtime = int(dir_st.st_mtime)
    yield _ctrl_member(
        "./",
        debian_root,
        path_type=PathType.DIRECTORY,
        mode=0o0755,
        mtime=min(mtime, dir_mtime),
    )
    with os.scandir(debian_root) as dir_iter:
        for ctrl_member in sorted(dir_iter, key=operator.attrgetter("name")):
            st = os.stat(ctrl_member)
            if not stat.S_ISREG(st.st_mode):
                _error(
                    f"{ctrl_member.path} is not a file and all control.tar members ought to be files!"
                )
            file_mtime = int(st.st_mtime)
            yield _ctrl_member(
                f"./{ctrl_member.name}",
                path_type=PathType.FILE,
                fs_path=ctrl_member.path,
                mode=0o0755 if ctrl_member.name in CTRL_MEMBER_SCRIPTS else 0o0644,
                mtime=min(mtime, file_mtime),
            )


def parse_manifest(manifest_path: "Optional[str]") -> "List[TarMember]":
    if manifest_path is None:
        _error(f"--intermediate-package-manifest is mandatory for now")
    return TarMember.parse_intermediate_manifest(manifest_path)


def main() -> None:
    setup_logging()
    parsed_args = parse_args()
    root_dir: str = parsed_args.package_root_dir
    output_path: str = parsed_args.package_output_path
    mtime = resolve_source_date_epoch(parsed_args.source_date_epoch)

    data_compression: Compression = COMPRESSIONS[parsed_args.compression_algorithm]
    data_compression_cmd = data_compression.as_cmdline(parsed_args)
    if parsed_args.uniform_compression:
        ctrl_compression = data_compression
        ctrl_compression_cmd = data_compression_cmd
    else:
        ctrl_compression = COMPRESSIONS["gzip"]
        ctrl_compression_cmd = COMPRESSIONS["gzip"].as_cmdline(None)

    if output_path.endswith("/") or os.path.isdir(output_path):
        deb_file = os.path.join(
            output_path,
            compute_output_filename(os.path.join(root_dir, "DEBIAN"), False),
        )
    else:
        deb_file = output_path

    pack(
        deb_file,
        ctrl_compression,
        data_compression,
        root_dir,
        parsed_args.package_manifest,
        mtime,
        ctrl_compression_cmd,
        data_compression_cmd,
        prefer_raw_exceptions=not parsed_args.debug_mode,
    )


def pack(
    deb_file: str,
    ctrl_compression: Compression,
    data_compression: Compression,
    root_dir: str,
    package_manifest: "Optional[str]",
    mtime: int,
    ctrl_compression_cmd: List[str],
    data_compression_cmd: List[str],
    prefer_raw_exceptions: bool = False,
) -> None:
    data_tar_members = parse_manifest(package_manifest)
    members = [
        ArMember("debian-binary", mtime, fixed_binary=b"2.0\n"),
        ArMember(
            ctrl_compression.with_extension("control.tar"),
            mtime,
            write_to_impl=generate_tar_file_member(
                _ctrl_tar_members(root_dir, mtime),
                ctrl_compression_cmd,
            ),
        ),
        ArMember(
            data_compression.with_extension("data.tar"),
            mtime,
            write_to_impl=generate_tar_file_member(
                data_tar_members,
                data_compression_cmd,
            ),
        ),
    ]
    generate_ar_archive(deb_file, mtime, members, prefer_raw_exceptions)


if __name__ == "__main__":
    main()
