from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
    package_metadata_context,
)


def test_grantlee_dependencies(amd64_dpkg_architecture_variables) -> None:
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    context = package_metadata_context(package_fields={"Architecture": "all"})
    metadata = plugin.run_metadata_detector("detect-grantlee-dependencies", fs, context)
    assert "grantlee:Depends" not in metadata.substvars

    context = package_metadata_context(
        package_fields={"Architecture": "any"},
        host_arch=amd64_dpkg_architecture_variables.current_host_arch,
    )
    madir = amd64_dpkg_architecture_variables.current_host_multiarch
    fs = build_virtual_file_system(
        [
            f"usr/lib/{madir}/grantlee/random-dir",
        ]
    )
    metadata = plugin.run_metadata_detector("detect-grantlee-dependencies", fs, context)
    assert "grantlee:Depends" not in metadata.substvars

    fs = build_virtual_file_system(
        [
            f"usr/lib/{madir}/grantlee/5.0/foo.so",
        ]
    )
    metadata = plugin.run_metadata_detector("detect-grantlee-dependencies", fs, context)
    assert metadata.substvars["grantlee:Depends"] == "grantlee5-templates-5-0"
