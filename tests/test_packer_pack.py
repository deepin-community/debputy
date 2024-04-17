import argparse
import json
from pathlib import Path

from debputy.commands import deb_packer
from debputy.intermediate_manifest import TarMember, PathType


def write_unpacked_deb(root: Path, package: str, version: str, arch: str):
    debian = root / "DEBIAN"
    debian.mkdir(mode=0o755)
    (debian / "control").write_text(
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n"
    )


def test_pack_smoke(tmp_path):
    mtime = 1668973695

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    write_unpacked_deb(root_dir, "fake", "1.0", "amd64")
    output_path = tmp_path / "out"
    output_path.mkdir()
    deb_file = Path(output_path) / "output.deb"

    parsed_args = argparse.Namespace(
        is_udeb=False, compression_level=None, compression_strategy=None
    )

    data_compression = deb_packer.COMPRESSIONS["xz"]
    data_compression_cmd = data_compression.as_cmdline(parsed_args)
    ctrl_compression = data_compression
    ctrl_compression_cmd = data_compression_cmd

    package_manifest = tmp_path / "temporary-manifest.json"
    package_manifest.write_text(
        json.dumps(
            [
                TarMember.virtual_path(
                    "./", PathType.DIRECTORY, mode=0o755, mtime=1668973695
                ).to_manifest()
            ]
        )
    )

    deb_packer.pack(
        str(deb_file),
        ctrl_compression,
        data_compression,
        str(root_dir),
        str(package_manifest),
        mtime,
        ctrl_compression_cmd,
        data_compression_cmd,
        prefer_raw_exceptions=True,
    )

    binary = deb_file.read_bytes()

    assert binary == (
        b"!<arch>\n"
        b"debian-binary   1668973695  0     0     100644  4         `\n"
        b"2.0\n"
        b"control.tar.xz  1668973695  0     0     100644  244       `\n"
        b"\xfd7zXZ\x00\x00\x04\xe6\xd6\xb4F\x04\xc0\xb4\x01\x80P!\x01\x16\x00\x00\x00"
        b"\x00\x00\x00\x00\x19\x87 E\xe0'\xff\x00\xac]\x00\x17\x0b\xbc\x1c}"
        b"\x01\x95\xc0\x1dJ>y\x15\xc2\xcc&\xa3^\x11\xb5\x81\xa6\x8cI\xd2\xf0m\xdd\x04"
        b"M\xb2|Tdy\xf5\x00H\xab\xa6B\x11\x8d2\x0e\x1d\xf8F\x9e\x9a\xb0\xb8_]\xa3;M"
        b"t\x90\x9a\xe3)\xeb\xadF\xfet'b\x05\x85\xd5\x04g\x7f\x89\xeb=(\xfd\xf6"
        b'"p\xc3\x91\xf2\xd3\xd2\xb3\xed%i\x9a\xfa\\\xde7\xd5\x01\x18I\x14D\x10E'
        b"\xba\xdf\xfb\x12{\x84\xc4\x10\x08,\xbc\x9e\xac+w\x07\r`|\xcfFL#\xbb"
        b"S\x91\xb4\\\x9b\x80&\x1d\x9ej\x13\xe3\x13\x02=\xe9\xd5\xcf\xb0\xdf?L\xf1\x96"
        b"\xd2\xc6bh\x19|?\xc2j\xe58If\xb7Y\xb9\x18:\x00\x00|\xfb\xcf\x82e/\xd05"
        b"\x00\x01\xd0\x01\x80P\x00\x00\xc9y\xeem\xb1\xc4g\xfb\x02\x00\x00\x00"
        b"\x00\x04YZ"
        b"data.tar.xz     1668973695  0     0     100644  168       `\n"
        b"\xfd7zXZ\x00\x00\x04\xe6\xd6\xb4F\x04\xc0h\x80P!\x01\x16\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\xc2bi\xe8\xe0'\xff\x00`]\x00\x17\x0b\xbc\x1c}"
        b"\x01\x95\xc0\x1dJ>y\x15\xc2\xcc&\xa3^\x11\xb5\x81\xa6\x8cI\xd2\xf0m\xdd\x04"
        b"M\xb2|Tdy\xf5\x00H\xab\xa6B\x11\x8d2\x0e\x1d\xf8F\x9e\x9a\xb0\xb8_]\xa4W%"
        b"\xa2\x14N\xb9\xe7\xbd\xf3a\x16\xe5\xb7\xe6\x80\x95\xcc\xe6+\xe1;I"
        b"\xf2\x1f\xed\x08\xac\xd7UZ\xc0P\x0b\xfb\nK\xef~\xcb\x8f\x80\x00\x9b\x19\xf8A"
        b"Q_\xe7\xeb\x00\x01\x84\x01\x80P\x00\x00(3\xf1\xfa\xb1\xc4g\xfb"
        b"\x02\x00\x00\x00\x00\x04YZ"
    )
