import pytest

from debputy.util import debian_policy_normalize_symlink_target


@pytest.mark.parametrize(
    "link_path,link_target,expected",
    [
        ("usr/share/doc/pkg/my-symlink", "/etc/foo.conf", "/etc/foo.conf"),
        ("usr/share/doc/pkg/my-symlink", "/usr/share/doc/pkg", "."),
        ("usr/share/doc/pkg/my-symlink", "/usr/share/doc/pkg/.", "."),
        ("usr/share/doc/pkg/my-symlink", "/usr/share/bar/../doc/pkg/.", "."),
        (
            "usr/share/doc/pkg/my-symlink",
            "/usr/share/bar/../doc/pkg/../other-pkg",
            "../other-pkg",
        ),
        ("usr/share/doc/pkg/my-symlink", "/usr/share/doc/other-pkg/.", "../other-pkg"),
        ("usr/share/doc/pkg/my-symlink", "../other-pkg/.", "../other-pkg"),
        ("usr/share/doc/pkg/my-symlink", "/usr/share/doc/other-pkg", "../other-pkg"),
        ("usr/share/doc/pkg/my-symlink", "../other-pkg", "../other-pkg"),
        (
            "usr/share/doc/pkg/my-symlink",
            "/usr/share/doc/pkg/../../../../etc/foo.conf",
            "/etc/foo.conf",
        ),
    ],
)
def test_symlink_normalization(link_path: str, link_target: str, expected: str) -> None:
    actual = debian_policy_normalize_symlink_target(
        link_path,
        link_target,
        normalize_link_path=True,
    )
    assert actual == expected
