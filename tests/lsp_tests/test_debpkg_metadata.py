from typing import Optional

import pytest

from debputy.lsp.lsp_debian_control_reference_data import package_name_to_section


@pytest.mark.parametrize(
    "name,guessed_section",
    [
        ("foo-udeb", "debian-installer"),
        ("python-foo", "python"),
        ("python-foo-doc", "doc"),
        ("libfoo-dev", "libdevel"),
        ("php-foo", "php"),
        ("libpam-foo", "admin"),
        ("fonts-foo", "fonts"),
        ("xxx-l10n", "localization"),
        ("xxx-l10n-bar", "localization"),
        ("libfoo4", "libs"),
        ("unknown", None),
    ],
)
def test_package_name_to_section(name: str, guessed_section: Optional[str]) -> None:
    assert package_name_to_section(name) == guessed_section
