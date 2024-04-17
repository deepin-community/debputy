import os
import textwrap
from typing import Sequence

import pytest

from debputy.exceptions import (
    DebputyManifestVariableRequiresDebianDirError,
    DebputySubstitutionError,
)
from debputy.manifest_parser.base_types import SymbolicMode
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.spec import DSD
from debputy.plugin.api.test_api import (
    build_virtual_file_system,
    package_metadata_context,
)
from debputy.plugin.api.test_api import manifest_variable_resolution_context
from debputy.plugin.api.test_api.test_impl import initialize_plugin_under_test_preloaded
from debputy.plugin.api.test_api.test_spec import DetectedService
from debputy.plugin.debputy.debputy_plugin import initialize_debputy_features
from debputy.plugin.debputy.private_api import load_libcap
from debputy.plugin.debputy.service_management import SystemdServiceContext
from debputy.plugin.debputy.types import DebputyCapability


def test_debputy_packager_provided_files():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    ppf_by_stem = plugin.packager_provided_files_by_stem()
    # Verify that all the files are loaded
    assert set(ppf_by_stem.keys()) == {
        "tmpfiles",
        "sysusers",
        "bash-completion",
        "pam",
        "ppp.ip-up",
        "ppp.ip-down",
        "logrotate",
        "logcheck.cracking",
        "logcheck.violations",
        "logcheck.violations.ignore",
        "logcheck.ignore.workstation",
        "logcheck.ignore.server",
        "logcheck.ignore.paranoid",
        "mime",
        "sharedmimeinfo",
        "if-pre-up",
        "if-up",
        "if-down",
        "if-post-down",
        "cron.hourly",
        "cron.daily",
        "cron.weekly",
        "cron.monthly",
        "cron.yearly",
        "cron.d",
        "initramfs-hook",
        "modprobe",
        "gsettings-override",
        "lintian-overrides",
        "bug-script",
        "bug-control",
        "bug-presubj",
        "changelog",
        "NEWS",
        "copyright",
        "README.Debian",
        "TODO",
        "doc-base",
        "shlibs",
        "symbols",
        "alternatives",
        "init",
        "default",
        "templates",
        # dh_installsytemd
        "mount",
        "path",
        "service",
        "socket",
        "target",
        "timer",
        "@path",
        "@service",
        "@socket",
        "@target",
        "@timer",
    }
    # Verify the post_rewrite_hook
    assert (
        ppf_by_stem["logcheck.ignore.paranoid"].compute_dest("foo.bar")[1] == "foo_bar"
    )
    # Verify custom formats work
    assert ppf_by_stem["tmpfiles"].compute_dest("foo.bar")[1] == "foo.bar.conf"
    assert ppf_by_stem["sharedmimeinfo"].compute_dest("foo.bar")[1] == "foo.bar.xml"
    assert ppf_by_stem["modprobe"].compute_dest("foo.bar")[1] == "foo.bar.conf"
    assert (
        ppf_by_stem["gsettings-override"].compute_dest("foo.bar", assigned_priority=20)[
            1
        ]
        == "20_foo.bar.gschema.override"
    )


def test_debputy_docbase_naming() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    doc_base_pff = plugin.packager_provided_files_by_stem()["doc-base"]
    fs_root = build_virtual_file_system(
        [virtual_path_def("foo.doc-base", content="Document: bar")]
    )
    _, basename = doc_base_pff.compute_dest("foo", path=fs_root["foo.doc-base"])
    assert basename == "foo.bar"


def test_debputy_adr_examples() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    issues = plugin.automatic_discard_rules_examples_with_issues()
    assert not issues


def test_debputy_metadata_detector_gsettings_dependencies():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    # By default, the plugin will not add a substvars
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector("gsettings-dependencies", fs_root)
    assert "misc:Depends" not in metadata.substvars

    # It will not react if there is only directories or non-files
    fs_root = build_virtual_file_system(["./usr/share/glib-2.0/schemas/some-dir/"])
    metadata = plugin.run_metadata_detector("gsettings-dependencies", fs_root)
    assert "misc:Depends" not in metadata.substvars

    # However, it will if there is a file beneath the schemas dir
    fs_root = build_virtual_file_system(["./usr/share/glib-2.0/schemas/foo.xml"])
    metadata = plugin.run_metadata_detector("gsettings-dependencies", fs_root)
    assert (
        metadata.substvars["misc:Depends"]
        == "dconf-gsettings-backend | gsettings-backend"
    )


def test_debputy_metadata_detector_initramfs_hooks():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "initramfs-hooks"

    # By default, the plugin will not add a trigger
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.triggers == []

    # It will not react if the directory is empty
    fs_root = build_virtual_file_system(
        [
            # Use an absolute path to verify that also work (it should and third-party plugin are likely
            # use absolute paths)
            "/usr/share/initramfs-tools/hooks/"
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.triggers == []

    # However, it will if there is a file beneath the schemas dir
    fs_root = build_virtual_file_system(["./usr/share/initramfs-tools/hooks/some-hook"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    result = [t.serialized_format() for t in metadata.triggers]
    assert result == ["activate-noawait update-initramfs"]


def test_debputy_metadata_detector_systemd_tmpfiles():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "systemd-tmpfiles"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    # It only reacts to ".conf" files
    fs_root = build_virtual_file_system(["./usr/lib/tmpfiles.d/foo"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            "./usr/lib/tmpfiles.d/foo.conf",
            "./etc/tmpfiles.d/foo.conf",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet.maintscript == "postinst"
    assert snippet.registration_method == "on_configure"
    # The snippet should use "systemd-tmpfiles [...] --create foo.conf ..."
    assert "--create foo.conf" in snippet.plugin_provided_script
    # The "foo.conf" should only be listed once
    assert snippet.plugin_provided_script.count("foo.conf") == 1


def test_debputy_metadata_detector_systemd_sysusers():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "systemd-sysusers"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    # It only reacts to ".conf" files
    fs_root = build_virtual_file_system(["./usr/lib/sysusers.d/foo"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(["./usr/lib/sysusers.d/foo.conf"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet.maintscript == "postinst"
    assert snippet.registration_method == "on_configure"
    # The snippet should use "systemd-sysusers [...] foo.conf ..."
    assert "systemd-sysusers" in snippet.plugin_provided_script
    assert "foo.conf" in snippet.plugin_provided_script
    # The "foo.conf" should only be listed once
    assert snippet.plugin_provided_script.count("foo.conf") == 1


def test_debputy_metadata_detector_xfonts():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "xfonts"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []
    assert "misc:Depends" not in metadata.substvars

    # It ignores files in the X11 dir and directories starting with ".".
    fs_root = build_virtual_file_system(
        ["./usr/share/fonts/X11/foo", "./usr/share/fonts/X11/.a/"]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []
    assert "misc:Depends" not in metadata.substvars

    fs_root = build_virtual_file_system(
        [
            "./usr/share/fonts/X11/some-font-dir/",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert metadata.substvars["misc:Depends"] == "xfonts-utils"
    assert len(snippets) == 2
    assert set(s.maintscript for s in snippets) == {"postinst", "postrm"}
    postinst_snippet = metadata.maintscripts(maintscript="postinst")[0]
    postrm_snippet = metadata.maintscripts(maintscript="postrm")[0]

    assert postinst_snippet.maintscript == "postinst"
    assert postinst_snippet.registration_method == "unconditionally_in_script"
    assert (
        "update-fonts-scale some-font-dir"
        not in postinst_snippet.plugin_provided_script
    )
    assert "--x11r7-layout some-font-dir" in postinst_snippet.plugin_provided_script
    assert (
        f"update-fonts-alias --include" not in postinst_snippet.plugin_provided_script
    )

    assert postrm_snippet.maintscript == "postrm"
    assert postrm_snippet.registration_method == "unconditionally_in_script"
    assert (
        "update-fonts-scale some-font-dir" not in postrm_snippet.plugin_provided_script
    )
    assert "--x11r7-layout some-font-dir" in postrm_snippet.plugin_provided_script
    assert f"update-fonts-alias --exclude" not in postrm_snippet.plugin_provided_script


def test_debputy_metadata_detector_xfonts_scale_and_alias():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )

    metadata_detector_id = "xfonts"
    package_name = "bar"
    fs_root = build_virtual_file_system(
        [
            "./usr/share/fonts/X11/some-font-dir/",
            f"./etc/X11/xfonts/some-font-dir/{package_name}.scale",
            f"./etc/X11/xfonts/some-font-dir/{package_name}.alias",
        ]
    )
    metadata = plugin.run_metadata_detector(
        metadata_detector_id,
        fs_root,
        package_metadata_context(
            package_fields={
                "Package": package_name,
            }
        ),
    )
    snippets = metadata.maintscripts()
    assert metadata.substvars["misc:Depends"] == "xfonts-utils"
    assert len(snippets) == 2
    assert set(s.maintscript for s in snippets) == {"postinst", "postrm"}
    postinst_snippet = metadata.maintscripts(maintscript="postinst")[0]
    postrm_snippet = metadata.maintscripts(maintscript="postrm")[0]

    assert postinst_snippet.maintscript == "postinst"
    assert postinst_snippet.registration_method == "unconditionally_in_script"
    assert "update-fonts-scale some-font-dir" in postinst_snippet.plugin_provided_script
    assert "--x11r7-layout some-font-dir" in postinst_snippet.plugin_provided_script
    assert (
        f"update-fonts-alias --include /etc/X11/xfonts/some-font-dir/{package_name}.alias some-font-dir"
        in postinst_snippet.plugin_provided_script
    )

    assert postrm_snippet.maintscript == "postrm"
    assert postrm_snippet.registration_method == "unconditionally_in_script"
    assert "update-fonts-scale some-font-dir" in postrm_snippet.plugin_provided_script
    assert "--x11r7-layout some-font-dir" in postrm_snippet.plugin_provided_script
    assert (
        f"update-fonts-alias --exclude /etc/X11/xfonts/some-font-dir/{package_name}.alias some-font-dir"
        in postrm_snippet.plugin_provided_script
    )


def test_debputy_metadata_detector_icon_cache():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "icon-cache"
    icon_dir = "usr/share/icons"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            # Ignored subdirs (dh_icons ignores them too)
            f"./{icon_dir}/gnome/foo.png",
            f"./{icon_dir}/hicolor/foo.png",
            # Unknown image format, so it does not trigger the update-icon-caches call
            f"./{icon_dir}/subdir-a/unknown-image-format.img",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            f"./{icon_dir}/subdir-a/foo.png",
            f"./{icon_dir}/subdir-b/subsubdir/bar.svg",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 2
    assert set(s.maintscript for s in snippets) == {"postinst", "postrm"}
    postinst_snippet = metadata.maintscripts(maintscript="postinst")[0]
    postrm_snippet = metadata.maintscripts(maintscript="postrm")[0]

    assert postinst_snippet.registration_method == "on_configure"
    assert postrm_snippet.registration_method == "unconditionally_in_script"

    # Directory order is stable according to the BinaryPackagePath API.
    assert (
        f"update-icon-caches /{icon_dir}/subdir-a /{icon_dir}/subdir-b"
        in postinst_snippet.plugin_provided_script
    )
    assert (
        f"update-icon-caches /{icon_dir}/subdir-a /{icon_dir}/subdir-b"
        in postrm_snippet.plugin_provided_script
    )


def test_debputy_metadata_detector_kernel_modules():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "kernel-modules"
    module_dir = "lib/modules"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            # Ignore files directly in the path or with wrong extension
            f"./{module_dir}/README",
            f"./{module_dir}/3.11/ignored-file.txt",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            f"./{module_dir}/3.11/foo.ko",
            f"./usr/{module_dir}/3.12/bar.ko.xz",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 4  # Two for each version
    assert set(s.maintscript for s in snippets) == {"postinst", "postrm"}
    postinst_snippets = metadata.maintscripts(maintscript="postinst")
    postrm_snippets = metadata.maintscripts(maintscript="postrm")

    assert len(postinst_snippets) == 2
    assert len(postrm_snippets) == 2
    assert {s.registration_method for s in postinst_snippets} == {"on_configure"}
    assert {s.registration_method for s in postrm_snippets} == {
        "unconditionally_in_script"
    }

    assert (
        "depmod -a -F /boot/System.map-3.11 3.11"
        in postinst_snippets[0].plugin_provided_script
    )
    assert (
        "depmod -a -F /boot/System.map-3.12 3.12"
        in postinst_snippets[1].plugin_provided_script
    )

    assert (
        "depmod -a -F /boot/System.map-3.11 3.11"
        in postrm_snippets[0].plugin_provided_script
    )
    assert (
        "depmod -a -F /boot/System.map-3.12 3.12"
        in postrm_snippets[1].plugin_provided_script
    )


def test_debputy_metadata_detector_dpkg_shlibdeps():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "dpkg-shlibdeps"
    skip_root_dir = "usr/lib/debug/"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(
        [
            "./usr/share/doc/foo/copyright",
            virtual_path_def("./usr/lib/debputy/test.py", fs_path=__file__),
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert "shlibs:Depends" not in metadata.substvars

    fs_root = build_virtual_file_system(
        [
            # Verify that certain directories are skipped as promised
            virtual_path_def(f"./{skip_root_dir}/bin/ls", fs_path="/bin/ls")
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert "shlibs:Depends" not in metadata.substvars

    # But we detect ELF binaries elsewhere
    fs_root = build_virtual_file_system(
        [virtual_path_def(f"./bin/ls", fs_path="/bin/ls")]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    # Do not make assertions about the content of `shlibs:Depends` as
    # package name and versions change over time.
    assert "shlibs:Depends" in metadata.substvars

    # Re-run to verify it runs for udebs as well
    metadata = plugin.run_metadata_detector(
        metadata_detector_id,
        fs_root,
        context=package_metadata_context(
            package_fields={"Package-Type": "udeb"},
        ),
    )
    assert "shlibs:Depends" in metadata.substvars


def test_debputy_metadata_detector_pycompile_files():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "pycompile-files"
    module_dir = "usr/lib/python3/dist-packages"

    # By default, the plugin will not add anything
    fs_root = build_virtual_file_system(["./bin/ls"])
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            # Ignore files in unknown directories by default
            "./random-dir/foo.py",
            # Must be in "dist-packages" to count
            "./usr/lib/python3/foo.py",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    assert metadata.maintscripts() == []

    fs_root = build_virtual_file_system(
        [
            f"./{module_dir}/debputy/foo.py",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 2
    assert set(s.maintscript for s in snippets) == {"postinst", "prerm"}
    postinst_snippets = metadata.maintscripts(maintscript="postinst")
    prerm_snippets = metadata.maintscripts(maintscript="prerm")

    assert len(postinst_snippets) == 1
    assert len(prerm_snippets) == 1
    assert {s.registration_method for s in postinst_snippets} == {"on_configure"}
    assert {s.registration_method for s in prerm_snippets} == {
        "unconditionally_in_script"
    }

    assert "py3compile -p foo" in postinst_snippets[0].plugin_provided_script

    assert "py3clean -p foo" in prerm_snippets[0].plugin_provided_script


def test_debputy_metadata_detector_pycompile_files_private_package_dir():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    metadata_detector_id = "pycompile-files"
    module_dir = "usr/share/foo"

    fs_root = build_virtual_file_system(
        [
            f"./{module_dir}/debputy/foo.py",
        ]
    )
    metadata = plugin.run_metadata_detector(metadata_detector_id, fs_root)
    snippets = metadata.maintscripts()
    assert len(snippets) == 2
    assert set(s.maintscript for s in snippets) == {"postinst", "prerm"}
    postinst_snippets = metadata.maintscripts(maintscript="postinst")
    prerm_snippets = metadata.maintscripts(maintscript="prerm")

    assert len(postinst_snippets) == 1
    assert len(prerm_snippets) == 1
    assert {s.registration_method for s in postinst_snippets} == {"on_configure"}
    assert {s.registration_method for s in prerm_snippets} == {
        "unconditionally_in_script"
    }

    assert (
        f"py3compile -p foo /{module_dir}"
        in postinst_snippets[0].plugin_provided_script
    )

    assert "py3clean -p foo" in prerm_snippets[0].plugin_provided_script


def _extract_service(
    services: Sequence[DetectedService[DSD]], name: str
) -> DetectedService[DSD]:
    v = [s for s in services if name in s.names]
    assert len(v) == 1
    return v[0]


def test_system_service_detection() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    systemd_service_root_dir = "usr/lib/systemd"
    systemd_service_system_dir = f"{systemd_service_root_dir}/system"
    systemd_service_user_dir = f"{systemd_service_root_dir}/user"

    services, _ = plugin.run_service_detection_and_integrations(
        "systemd", build_virtual_file_system([])
    )
    assert not services

    services, _ = plugin.run_service_detection_and_integrations(
        "systemd",
        build_virtual_file_system(
            [f"{systemd_service_system_dir}/", f"{systemd_service_user_dir}/"]
        ),
    )
    assert not services

    fs_root = build_virtual_file_system(
        [
            virtual_path_def(
                f"{systemd_service_system_dir}/foo.service",
                content=textwrap.dedent(
                    """\
            Alias="myname.service"
            [Install]
            """
                ),
            ),
            virtual_path_def(
                f"{systemd_service_system_dir}/foo@.service",
                content=textwrap.dedent(
                    """\
            # dh_installsystemd ignores template services - we do for now as well.
            Alias="ignored.service"
            [Install]
            """
                ),
            ),
            virtual_path_def(
                f"{systemd_service_system_dir}/alias.service", link_target="foo.service"
            ),
            virtual_path_def(f"{systemd_service_system_dir}/bar.timer", content=""),
        ]
    )
    services, metadata = plugin.run_service_detection_and_integrations(
        "systemd",
        fs_root,
        service_context_type_hint=SystemdServiceContext,
    )
    assert len(services) == 2
    assert {s.names[0] for s in services} == {"foo.service", "bar.timer"}
    foo_service = _extract_service(services, "foo.service")
    assert set(foo_service.names) == {
        "foo.service",
        "foo",
        "alias",
        "alias.service",
        "myname.service",
        "myname",
    }
    assert foo_service.type_of_service == "service"
    assert foo_service.service_scope == "system"
    assert foo_service.enable_by_default
    assert foo_service.start_by_default
    assert foo_service.default_upgrade_rule == "restart"
    assert foo_service.service_context.had_install_section

    bar_timer = _extract_service(services, "bar.timer")
    assert set(bar_timer.names) == {"bar.timer"}
    assert bar_timer.type_of_service == "timer"
    assert bar_timer.service_scope == "system"
    assert not bar_timer.enable_by_default
    assert bar_timer.start_by_default
    assert bar_timer.default_upgrade_rule == "restart"
    assert not bar_timer.service_context.had_install_section

    snippets = metadata.maintscripts()
    assert len(snippets) == 4
    postinsts = metadata.maintscripts(maintscript="postinst")
    assert len(postinsts) == 2
    enable_postinst, start_postinst = postinsts
    assert (
        "deb-systemd-helper debian-installed foo.service"
        in enable_postinst.plugin_provided_script
    )
    assert (
        "deb-systemd-invoke start foo.service" in start_postinst.plugin_provided_script
    )
    assert (
        "deb-systemd-invoke restart foo.service"
        in start_postinst.plugin_provided_script
    )


def test_sysv_service_detection() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    init_dir = "etc/init.d"

    services, _ = plugin.run_service_detection_and_integrations(
        "sysvinit", build_virtual_file_system([])
    )
    assert not services

    services, _ = plugin.run_service_detection_and_integrations(
        "sysvinit",
        build_virtual_file_system(
            [
                f"{init_dir}/",
            ]
        ),
    )
    assert not services

    services, _ = plugin.run_service_detection_and_integrations(
        "sysvinit",
        build_virtual_file_system(
            [
                virtual_path_def(
                    f"{init_dir}/README",
                    mode=0o644,
                ),
            ]
        ),
    )
    assert not services

    fs_root = build_virtual_file_system(
        [
            virtual_path_def(
                f"{init_dir}/foo",
                mode=0o755,
            ),
        ]
    )
    services, metadata = plugin.run_service_detection_and_integrations(
        "sysvinit", fs_root
    )
    assert len(services) == 1
    assert {s.names[0] for s in services} == {"foo"}
    foo_service = _extract_service(services, "foo")
    assert set(foo_service.names) == {"foo"}
    assert foo_service.type_of_service == "service"
    assert foo_service.service_scope == "system"
    assert foo_service.enable_by_default
    assert foo_service.start_by_default
    assert foo_service.default_upgrade_rule == "restart"

    snippets = metadata.maintscripts()
    assert len(snippets) == 4
    postinsts = metadata.maintscripts(maintscript="postinst")
    assert len(postinsts) == 1
    postinst = postinsts[0]
    assert postinst.registration_method == "on_configure"
    assert "" in postinst.plugin_provided_script
    assert "update-rc.d foo defaults" in postinst.plugin_provided_script
    assert (
        "invoke-rc.d --skip-systemd-native foo start" in postinst.plugin_provided_script
    )
    assert (
        "invoke-rc.d --skip-systemd-native foo restart"
        in postinst.plugin_provided_script
    )


def test_debputy_manifest_variables() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    manifest_variables_no_dch = plugin.manifest_variables()
    assert manifest_variables_no_dch.keys() == {
        "DEB_BUILD_ARCH",
        "DEB_BUILD_ARCH_ABI",
        "DEB_BUILD_ARCH_BITS",
        "DEB_BUILD_ARCH_CPU",
        "DEB_BUILD_ARCH_ENDIAN",
        "DEB_BUILD_ARCH_LIBC",
        "DEB_BUILD_ARCH_OS",
        "DEB_BUILD_GNU_CPU",
        "DEB_BUILD_GNU_SYSTEM",
        "DEB_BUILD_GNU_TYPE",
        "DEB_BUILD_MULTIARCH",
        "DEB_HOST_ARCH",
        "DEB_HOST_ARCH_ABI",
        "DEB_HOST_ARCH_BITS",
        "DEB_HOST_ARCH_CPU",
        "DEB_HOST_ARCH_ENDIAN",
        "DEB_HOST_ARCH_LIBC",
        "DEB_HOST_ARCH_OS",
        "DEB_HOST_GNU_CPU",
        "DEB_HOST_GNU_SYSTEM",
        "DEB_HOST_GNU_TYPE",
        "DEB_HOST_MULTIARCH",
        "DEB_SOURCE",
        "DEB_TARGET_ARCH",
        "DEB_TARGET_ARCH_ABI",
        "DEB_TARGET_ARCH_BITS",
        "DEB_TARGET_ARCH_CPU",
        "DEB_TARGET_ARCH_ENDIAN",
        "DEB_TARGET_ARCH_LIBC",
        "DEB_TARGET_ARCH_OS",
        "DEB_TARGET_GNU_CPU",
        "DEB_TARGET_GNU_SYSTEM",
        "DEB_TARGET_GNU_TYPE",
        "DEB_TARGET_MULTIARCH",
        "DEB_VERSION",
        "DEB_VERSION_EPOCH_UPSTREAM",
        "DEB_VERSION_UPSTREAM",
        "DEB_VERSION_UPSTREAM_REVISION",
        "PACKAGE",
        "SOURCE_DATE_EPOCH",
        "_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE",
        "_DEBPUTY_SND_SOURCE_DATE_EPOCH",
        "path:BASH_COMPLETION_DIR",
        "path:GNU_INFO_DIR",
        "token:CLOSE_CURLY_BRACE",
        "token:DOUBLE_CLOSE_CURLY_BRACE",
        "token:DOUBLE_OPEN_CURLY_BRACE",
        "token:NEWLINE",
        "token:NL",
        "token:OPEN_CURLY_BRACE",
        "token:TAB",
    }

    for v in [
        "DEB_SOURCE",
        "DEB_VERSION",
        "DEB_VERSION_EPOCH_UPSTREAM",
        "DEB_VERSION_UPSTREAM",
        "DEB_VERSION_UPSTREAM_REVISION",
        "SOURCE_DATE_EPOCH",
        "_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE",
        "_DEBPUTY_SND_SOURCE_DATE_EPOCH",
    ]:
        with pytest.raises(DebputyManifestVariableRequiresDebianDirError):
            manifest_variables_no_dch[v]

    with pytest.raises(DebputySubstitutionError):
        manifest_variables_no_dch["PACKAGE"]

    dch_content = textwrap.dedent(
        """\
        mscgen (1:0.20-15) unstable; urgency=medium

          * Irrelevant stuff here...
          * Also, some details have been tweaked for better testing

         -- Niels Thykier <niels@thykier.net>  Mon, 09 Oct 2023 14:50:06 +0000
        """
    )

    debian_dir = build_virtual_file_system(
        [virtual_path_def("changelog", content=dch_content)]
    )
    resolution_context = manifest_variable_resolution_context(debian_dir=debian_dir)
    manifest_variables = plugin.manifest_variables(
        resolution_context=resolution_context
    )

    assert manifest_variables["DEB_SOURCE"] == "mscgen"
    assert manifest_variables["DEB_VERSION"] == "1:0.20-15"
    assert manifest_variables["_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE"] == "1:0.20-15"

    assert manifest_variables["DEB_VERSION_EPOCH_UPSTREAM"] == "1:0.20"
    assert manifest_variables["DEB_VERSION_UPSTREAM"] == "0.20"
    assert manifest_variables["DEB_VERSION_UPSTREAM_REVISION"] == "0.20-15"
    assert manifest_variables["SOURCE_DATE_EPOCH"] == "1696863006"
    assert manifest_variables["_DEBPUTY_SND_SOURCE_DATE_EPOCH"] == "1696863006"

    # This one remains unresolvable
    with pytest.raises(DebputySubstitutionError):
        manifest_variables["PACKAGE"]

    static_values = {
        "path:BASH_COMPLETION_DIR": "/usr/share/bash-completion/completions",
        "path:GNU_INFO_DIR": "/usr/share/info",
    }

    for k, v in static_values.items():
        assert manifest_variables[k] == v

    dch_content_bin_nmu = textwrap.dedent(
        """\
        mscgen (1:0.20-15+b4) unstable; urgency=medium, binary-only=yes

          * Some binNMU entry here

         -- Niels Thykier <niels@thykier.net>  Mon, 10 Nov 2023 16:01:17 +0000

        mscgen (1:0.20-15) unstable; urgency=medium

          * Irrelevant stuff here...
          * Also, some details have been tweaked for better testing

         -- Niels Thykier <niels@thykier.net>  Mon, 09 Oct 2023 14:50:06 +0000
        """
    )

    debian_dir_bin_nmu = build_virtual_file_system(
        [virtual_path_def("changelog", content=dch_content_bin_nmu)]
    )
    resolution_context_bin_nmu = manifest_variable_resolution_context(
        debian_dir=debian_dir_bin_nmu
    )
    manifest_variables_bin_nmu = plugin.manifest_variables(
        resolution_context=resolution_context_bin_nmu
    )

    assert manifest_variables_bin_nmu["DEB_SOURCE"] == "mscgen"
    assert manifest_variables_bin_nmu["DEB_VERSION"] == "1:0.20-15+b4"
    assert (
        manifest_variables_bin_nmu["_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE"] == "1:0.20-15"
    )

    assert manifest_variables_bin_nmu["DEB_VERSION_EPOCH_UPSTREAM"] == "1:0.20"
    assert manifest_variables_bin_nmu["DEB_VERSION_UPSTREAM"] == "0.20"
    assert manifest_variables_bin_nmu["DEB_VERSION_UPSTREAM_REVISION"] == "0.20-15+b4"
    assert manifest_variables_bin_nmu["SOURCE_DATE_EPOCH"] == "1699632077"
    assert manifest_variables_bin_nmu["_DEBPUTY_SND_SOURCE_DATE_EPOCH"] == "1696863006"


def test_cap_validator() -> None:
    has_libcap, _, is_valid_cap = load_libcap()

    if not has_libcap:
        if os.environ.get("DEBPUTY_REQUIRE_LIBCAP", "") != "":
            pytest.fail("Could not load libcap, but DEBPUTY_REQUIRE_CAP was non-empty")
        pytest.skip("Could not load libcap.so")
    assert not is_valid_cap("foo")
    assert is_valid_cap("cap_dac_override,cap_bpf,cap_net_admin=ep")


def test_clean_la_files() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    fs_root = build_virtual_file_system(
        [virtual_path_def("usr/bin/foo", content="#!/bin/sh\n")]
    )
    # Does nothing by default
    plugin.run_package_processor(
        "clean-la-files",
        fs_root,
    )

    la_file_content = textwrap.dedent(
        """\
    dependency_libs = 'foo bar'
    another_line = 'foo bar'
    """
    )
    expected_content = textwrap.dedent(
        """\
        dependency_libs = ''
        another_line = 'foo bar'
        """
    )
    la_file_content_no_change = expected_content
    expected_content = textwrap.dedent(
        """\
        dependency_libs = ''
        another_line = 'foo bar'
        """
    )

    fs_root = build_virtual_file_system(
        [
            virtual_path_def("usr/lib/libfoo.la", materialized_content=la_file_content),
            virtual_path_def(
                "usr/lib/libfoo-unchanged.la",
                content=la_file_content_no_change,
            ),
        ]
    )

    plugin.run_package_processor(
        "clean-la-files",
        fs_root,
    )
    for basename in ("libfoo.la", "libfoo-unchanged.la"):
        la_file = fs_root.lookup(f"usr/lib/{basename}")
        assert la_file is not None and la_file.is_file
        if basename == "libfoo-unchanged.la":
            # it should never have been rewritten
            assert not la_file.has_fs_path
        else:
            assert la_file.has_fs_path
        with la_file.open() as fd:
            rewritten_content = fd.read()
            assert rewritten_content == expected_content


def test_strip_nondeterminism() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )

    fs_root = build_virtual_file_system(
        [
            # Note, we are only testing a negative example as a positive example crashes
            # because we do not have a SOURCE_DATE_EPOCH value/substitution
            virtual_path_def("test/not-really-a-png.png", content="Not a PNG")
        ]
    )

    plugin.run_package_processor(
        "strip-nondeterminism",
        fs_root,
    )


def test_translate_capabilities() -> None:
    attribute_path = AttributePath.test_path()
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )

    fs_root = build_virtual_file_system([virtual_path_def("usr/bin/foo", mode=0o4755)])

    foo = fs_root.lookup("usr/bin/foo")
    assert foo is not None
    assert foo.is_file
    assert foo.is_read_write

    metadata_no_cap = plugin.run_metadata_detector(
        "translate-capabilities",
        fs_root,
    )

    assert not metadata_no_cap.maintscripts(maintscript="postinst")

    cap = foo.metadata(DebputyCapability)
    assert not cap.is_present
    assert cap.can_write
    cap.value = DebputyCapability(
        capabilities="cap_net_raw+ep",
        capability_mode=SymbolicMode.parse_filesystem_mode(
            "u-s",
            attribute_path["cap_mode"],
        ),
        definition_source="test",
    )
    metadata_w_cap = plugin.run_metadata_detector(
        "translate-capabilities",
        fs_root,
    )

    postinsts = metadata_w_cap.maintscripts(maintscript="postinst")
    assert len(postinsts) == 1
    postinst = postinsts[0]
    assert postinst.registration_method == "on_configure"
    assert "setcap cap_net_raw+ep " in postinst.plugin_provided_script
    assert "chmod u-s " in postinst.plugin_provided_script


def test_pam_auth_update() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )

    fs_root = build_virtual_file_system(["usr/bin/foo"])

    empty_metadata = plugin.run_metadata_detector("pam-auth-update", fs_root)
    assert not empty_metadata.maintscripts()

    fs_root = build_virtual_file_system(["/usr/share/pam-configs/foo-pam"])

    pam_metadata = plugin.run_metadata_detector("pam-auth-update", fs_root)
    postinsts = pam_metadata.maintscripts(maintscript="postinst")
    assert len(postinsts) == 1
    prerms = pam_metadata.maintscripts(maintscript="prerm")
    assert len(prerms) == 1

    postinst = postinsts[0]
    assert postinst.registration_method == "on_configure"
    assert "pam-auth-update --package" in postinst.plugin_provided_script

    prerms = prerms[0]
    assert prerms.registration_method == "on_before_removal"
    assert "pam-auth-update --package --remove foo-pam" in prerms.plugin_provided_script


def test_auto_depends_solink() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        initialize_debputy_features,
        plugin_name="debputy",
        load_debputy_plugin=False,
    )

    fs_root = build_virtual_file_system(["usr/bin/foo"])

    empty_metadata = plugin.run_metadata_detector(
        "auto-depends-arch-any-solink",
        fs_root,
    )
    assert "misc:Depends" not in empty_metadata.substvars
    fs_root = build_virtual_file_system(
        [
            "usr/lib/x86_64-linux-gnu/libfoo.la",
            virtual_path_def(
                "usr/lib/x86_64-linux-gnu/libfoo.so", link_target="libfoo.so.1"
            ),
        ]
    )

    still_empty_metadata = plugin.run_metadata_detector(
        "auto-depends-arch-any-solink",
        fs_root,
    )
    assert "misc:Depends" not in still_empty_metadata.substvars

    libfoo1_fs_root = build_virtual_file_system(
        [
            virtual_path_def(
                "usr/lib/x86_64-linux-gnu/libfoo.so.1", link_target="libfoo.so.1.0.0"
            ),
        ]
    )

    context_correct = package_metadata_context(
        package_fields={
            "Package": "libfoo-dev",
        },
        accessible_package_roots=[
            (
                {
                    "Package": "libfoo1",
                    "Architecture": "any",
                },
                libfoo1_fs_root,
            )
        ],
    )
    sodep_metadata = plugin.run_metadata_detector(
        "auto-depends-arch-any-solink",
        fs_root,
        context=context_correct,
    )
    assert "misc:Depends" in sodep_metadata.substvars
    assert sodep_metadata.substvars["misc:Depends"] == "libfoo1 (= ${binary:Version})"

    context_incorrect = package_metadata_context(
        package_fields={"Package": "libfoo-dev", "Architecture": "all"},
        accessible_package_roots=[
            (
                {
                    "Package": "foo",
                    "Architecture": "all",
                },
                build_virtual_file_system([]),
            )
        ],
    )
    sodep_metadata = plugin.run_metadata_detector(
        "auto-depends-arch-any-solink",
        fs_root,
        context=context_incorrect,
    )
    assert "misc:Depends" not in sodep_metadata.substvars

    context_too_many_matches = package_metadata_context(
        package_fields={"Package": "libfoo-dev"},
        accessible_package_roots=[
            (
                {
                    "Package": "libfoo1-a",
                    "Architecture": "any",
                },
                libfoo1_fs_root,
            ),
            (
                {
                    "Package": "libfoo1-b",
                    "Architecture": "any",
                },
                libfoo1_fs_root,
            ),
        ],
    )
    sodep_metadata = plugin.run_metadata_detector(
        "auto-depends-arch-any-solink",
        fs_root,
        context=context_too_many_matches,
    )
    assert "misc:Depends" not in sodep_metadata.substvars
