from typing import Optional

import pytest

from debputy.plugin.api.test_api import package_metadata_context
from debputy.util import package_cross_check_precheck


@pytest.mark.parametrize(
    "a_arch,b_arch,a_bp,b_bp,act_on_a,act_on_b,ex_res_a2b,ex_res_b2a",
    [
        # Both way OK
        ("any", "any", None, None, True, True, True, True),
        ("all", "all", None, None, True, True, True, True),
        ("any", "any", "<!noudeb>", "<!noudeb>", True, True, True, True),
        # OK as well. Same BPs just reordered
        (
            "any",
            "any",
            "<!noudeb !noinsttests>",
            "<!noinsttests !noudeb>",
            True,
            True,
            True,
            True,
        ),
        (
            "any",
            "any",
            "<!noudeb> <!noinsttests>",
            "<!noinsttests> <!noudeb>",
            True,
            True,
            True,
            True,
        ),
        # One way OK
        ("any", "any", None, "<!noudeb>", True, True, True, False),
        ("any", "any", None, "<pkg.foo.positive-build>", True, True, True, False),
        # One way OK - BP is clearly a subset of the other
        (
            "any",
            "any",
            "<!noudeb>",
            "<!noudeb> <!noinsttests>",
            True,
            True,
            True,
            False,
        ),
        (
            "any",
            "any",
            "<pos>",
            "<pos> <pkg.foo.positive-build>",
            True,
            True,
            True,
            False,
        ),
        # Currently fails but should probably allow one way
        (
            "any",
            "any",
            "<!nopython>",
            "<!noudeb> <!notestests>",
            True,
            True,
            False,
            False,
        ),
        (
            "any",
            "any",
            "<!nopython>",
            "<!noudeb> <pkg.foo.positive-build>",
            True,
            True,
            False,
            False,
        ),
        # Negative tests
        ("any", "all", None, None, True, True, False, False),
        ("all", "all", None, None, True, False, False, False),
        ("any", "any", None, None, False, True, False, False),
        ("i386", "amd64", None, None, True, True, False, False),
    ],
)
def test_generate_deb_filename(
    a_arch: str,
    b_arch: str,
    a_bp: Optional[str],
    b_bp: Optional[str],
    act_on_a: bool,
    act_on_b: bool,
    ex_res_a2b: bool,
    ex_res_b2a: bool,
):
    pkg_a_fields = {
        "Package": "pkg-a",
        "Architecture": a_arch,
    }
    if a_bp is not None:
        pkg_a_fields["Build-Profiles"] = a_bp

    pkg_b_fields = {
        "Package": "pkg-b",
        "Architecture": b_arch,
    }
    if b_bp is not None:
        pkg_b_fields["Build-Profiles"] = b_bp

    pkg_a = package_metadata_context(
        package_fields=pkg_a_fields,
        should_be_acted_on=act_on_a,
    ).binary_package
    pkg_b = package_metadata_context(
        package_fields=pkg_b_fields,
        should_be_acted_on=act_on_b,
    ).binary_package

    assert package_cross_check_precheck(pkg_a, pkg_b) == (ex_res_a2b, ex_res_b2a)
    # Inverted should functionally give the same answer
    assert package_cross_check_precheck(pkg_b, pkg_a) == (ex_res_b2a, ex_res_a2b)
