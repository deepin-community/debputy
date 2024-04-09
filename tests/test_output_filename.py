from pathlib import Path

import pytest

from debputy.util import compute_output_filename


def write_unpacked_deb(root: Path, package: str, version: str, arch: str):
    (root / "control").write_text(
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n"
    )


@pytest.mark.parametrize(
    "package,version,arch,is_udeb,expected",
    [
        ("fake", "1.0", "amd64", False, "fake_1.0_amd64.deb"),
        ("fake", "1.0", "amd64", True, "fake_1.0_amd64.udeb"),
        ("fake", "2:1.0", "amd64", False, "fake_1.0_amd64.deb"),
        ("fake", "2:1.0", "amd64", True, "fake_1.0_amd64.udeb"),
        ("fake", "3.0", "all", False, "fake_3.0_all.deb"),
        ("fake", "3.0", "all", True, "fake_3.0_all.udeb"),
    ],
)
def test_generate_deb_filename(tmp_path, package, version, arch, is_udeb, expected):
    write_unpacked_deb(tmp_path, package, version, arch)
    assert compute_output_filename(str(tmp_path), is_udeb) == expected
