from debputy.architecture_support import faked_arch_table


# Ensure our mocks seem to be working reasonably
def test_mock_arch_table():
    amd64_native_table = faked_arch_table("amd64")
    amd64_cross_table = faked_arch_table("amd64", build_arch="i386")
    amd64_cross_target_table = faked_arch_table("amd64", target_arch="arm64")
    all_differ_table = faked_arch_table("amd64", build_arch="i386", target_arch="arm64")

    for var_stem in ["ARCH", "MULTIARCH"]:
        host_var = f"DEB_HOST_{var_stem}"
        build_var = f"DEB_BUILD_{var_stem}"
        target_var = f"DEB_TARGET_{var_stem}"

        assert (
            amd64_cross_table.current_host_arch == amd64_native_table.current_host_arch
        )
        assert amd64_native_table[host_var] == amd64_native_table[build_var]
        assert amd64_native_table[host_var] == amd64_native_table[target_var]

        # HOST_ARCH differ in a cross build, but the rest remain the same
        assert amd64_cross_table[host_var] == amd64_native_table[host_var]
        assert amd64_cross_table[target_var] == amd64_native_table[target_var]
        assert amd64_cross_table[build_var] != amd64_native_table[build_var]
        assert amd64_cross_table[target_var] == amd64_native_table[target_var]
        assert (
            amd64_cross_table.current_host_multiarch
            == amd64_native_table.current_host_multiarch
        )

        # TARGET_ARCH differ in a cross-compiler build, but the rest remain the same
        assert amd64_cross_target_table[host_var] == amd64_native_table[host_var]
        assert amd64_cross_target_table[target_var] != amd64_native_table[target_var]
        assert amd64_cross_target_table[build_var] == amd64_native_table[build_var]
        assert (
            amd64_cross_target_table.current_host_multiarch
            == amd64_native_table.current_host_multiarch
        )

        # TARGET_ARCH differ in a cross-compiler build, but the rest remain the same
        assert all_differ_table[host_var] == amd64_native_table[host_var]
        assert all_differ_table[target_var] != amd64_native_table[target_var]
        assert all_differ_table[build_var] != amd64_native_table[build_var]
        assert all_differ_table[build_var] == amd64_cross_table[build_var]
        assert all_differ_table[target_var] == amd64_cross_target_table[target_var]
        assert (
            all_differ_table.current_host_arch == amd64_native_table.current_host_arch
        )
        assert (
            all_differ_table.current_host_multiarch
            == amd64_native_table.current_host_multiarch
        )

    # Finally, check is_cross_compiling
    assert not amd64_native_table.is_cross_compiling
    assert amd64_cross_table.is_cross_compiling
    assert not amd64_cross_target_table.is_cross_compiling
    assert all_differ_table.is_cross_compiling
