import pytest

from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
    package_metadata_context,
)


@pytest.mark.parametrize(
    "version,expected_version,expected_next_version",
    [
        (
            "1:3.36.1",
            "1:3.36",
            "1:3.38",
        ),
        (
            "3.38.2",
            "3.38",
            "40",
        ),
        (
            "40.2.0",
            "40~",
            "41~",
        ),
        (
            "40",
            "40~",
            "41~",
        ),
    ],
)
def test_gnome_plugin(
    version: str,
    expected_version: str,
    expected_next_version: str,
) -> None:
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    context = package_metadata_context(binary_package_version=version)
    metadata = plugin.run_metadata_detector("gnome-versions", fs, context)
    assert metadata.substvars["gnome:Version"] == expected_version
    assert metadata.substvars["gnome:NextVersion"] == expected_next_version
