import textwrap

from debputy.plugin.api import (
    DebputyPluginInitializer,
    packager_provided_file_reference_documentation,
)
from debputy.plugin.debputy.metadata_detectors import (
    detect_systemd_tmpfiles,
    detect_kernel_modules,
    detect_icons,
    detect_gsettings_dependencies,
    detect_xfonts,
    detect_initramfs_hooks,
    detect_systemd_sysusers,
    detect_pycompile_files,
    translate_capabilities,
    pam_auth_update,
    auto_depends_arch_any_solink,
)
from debputy.plugin.debputy.paths import (
    SYSTEMD_TMPFILES_DIR,
    INITRAMFS_HOOK_DIR,
    GSETTINGS_SCHEMA_DIR,
    SYSTEMD_SYSUSERS_DIR,
)
from debputy.plugin.debputy.private_api import initialize_via_private_api


def initialize_debputy_features(api: DebputyPluginInitializer) -> None:
    initialize_via_private_api(api)
    declare_manifest_variables(api)
    register_packager_provided_files(api)
    register_package_metadata_detectors(api)


def declare_manifest_variables(api: DebputyPluginInitializer) -> None:
    api.manifest_variable(
        "path:BASH_COMPLETION_DIR",
        "/usr/share/bash-completion/completions",
        variable_reference_documentation="Directory to install bash completions into",
    )
    api.manifest_variable(
        "path:GNU_INFO_DIR",
        "/usr/share/info",
        variable_reference_documentation="Directory to install GNU INFO files into",
    )

    api.manifest_variable(
        "token:NL",
        "\n",
        variable_reference_documentation="Literal newline (linefeed) character",
    )
    api.manifest_variable(
        "token:NEWLINE",
        "\n",
        variable_reference_documentation="Literal newline (linefeed) character",
    )
    api.manifest_variable(
        "token:TAB",
        "\t",
        variable_reference_documentation="Literal tab character",
    )
    api.manifest_variable(
        "token:OPEN_CURLY_BRACE",
        "{",
        variable_reference_documentation='Literal "{" character',
    )
    api.manifest_variable(
        "token:CLOSE_CURLY_BRACE",
        "}",
        variable_reference_documentation='Literal "}" character',
    )
    api.manifest_variable(
        "token:DOUBLE_OPEN_CURLY_BRACE",
        "{{",
        variable_reference_documentation='Literal "{{" character - useful to avoid triggering a substitution',
    )
    api.manifest_variable(
        "token:DOUBLE_CLOSE_CURLY_BRACE",
        "}}",
        variable_reference_documentation='Literal "}}" string - useful to avoid triggering a substitution',
    )


def register_package_metadata_detectors(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector("systemd-tmpfiles", detect_systemd_tmpfiles)
    api.metadata_or_maintscript_detector("systemd-sysusers", detect_systemd_sysusers)
    api.metadata_or_maintscript_detector("kernel-modules", detect_kernel_modules)
    api.metadata_or_maintscript_detector("icon-cache", detect_icons)
    api.metadata_or_maintscript_detector(
        "gsettings-dependencies",
        detect_gsettings_dependencies,
    )
    api.metadata_or_maintscript_detector("xfonts", detect_xfonts)
    api.metadata_or_maintscript_detector("initramfs-hooks", detect_initramfs_hooks)
    api.metadata_or_maintscript_detector("pycompile-files", detect_pycompile_files)
    api.metadata_or_maintscript_detector(
        "translate-capabilities",
        translate_capabilities,
    )
    api.metadata_or_maintscript_detector("pam-auth-update", pam_auth_update)
    api.metadata_or_maintscript_detector(
        "auto-depends-arch-any-solink",
        auto_depends_arch_any_solink,
    )


def register_packager_provided_files(api: DebputyPluginInitializer) -> None:
    api.packager_provided_file(
        "tmpfiles",
        f"{SYSTEMD_TMPFILES_DIR}/{{name}}.conf",
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=["man:tmpfiles.d(5)"]
        ),
    )
    api.packager_provided_file(
        "sysusers",
        f"{SYSTEMD_SYSUSERS_DIR}/{{name}}.conf",
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=["man:sysusers.d(5)"]
        ),
    )
    api.packager_provided_file(
        "bash-completion", "/usr/share/bash-completion/completions/{name}"
    )
    api.packager_provided_file(
        "bug-script",
        "./usr/share/bug/{name}/script",
        default_mode=0o0755,
        allow_name_segment=False,
    )
    api.packager_provided_file(
        "bug-control",
        "/usr/share/bug/{name}/control",
        allow_name_segment=False,
    )

    api.packager_provided_file(
        "bug-presubj",
        "/usr/share/bug/{name}/presubj",
        allow_name_segment=False,
    )

    api.packager_provided_file("pam", "/usr/lib/pam.d/{name}")
    api.packager_provided_file(
        "ppp.ip-up",
        "/etc/ppp/ip-up.d/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "ppp.ip-down",
        "/etc/ppp/ip-down.d/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "lintian-overrides",
        "/usr/share/lintian/overrides/{name}",
        allow_name_segment=False,
    )
    api.packager_provided_file("logrotate", "/etc/logrotate.d/{name}")
    api.packager_provided_file(
        "logcheck.cracking",
        "/etc/logcheck/cracking.d/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )
    api.packager_provided_file(
        "logcheck.violations",
        "/etc/logcheck/violations.d/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )
    api.packager_provided_file(
        "logcheck.violations.ignore",
        "/etc/logcheck/violations.ignore.d/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )
    api.packager_provided_file(
        "logcheck.ignore.workstation",
        "/etc/logcheck/ignore.d.workstation/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )
    api.packager_provided_file(
        "logcheck.ignore.server",
        "/etc/logcheck/ignore.d.server/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )
    api.packager_provided_file(
        "logcheck.ignore.paranoid",
        "/etc/logcheck/ignore.d.paranoid/{name}",
        post_formatting_rewrite=_replace_dot_with_underscore,
    )

    api.packager_provided_file("mime", "/usr/lib/mime/packages/{name}")
    api.packager_provided_file("sharedmimeinfo", "/usr/share/mime/packages/{name}.xml")

    api.packager_provided_file(
        "if-pre-up",
        "/etc/network/if-pre-up.d/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "if-up",
        "/etc/network/if-up.d/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "if-down",
        "/etc/network/if-down.d/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "if-post-down",
        "/etc/network/if-post-down.d/{name}",
        default_mode=0o0755,
    )

    api.packager_provided_file(
        "cron.hourly",
        "/etc/cron.hourly/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "cron.daily",
        "/etc/cron.daily/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "cron.weekly",
        "/etc/cron.weekly/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "cron.monthly",
        "./etc/cron.monthly/{name}",
        default_mode=0o0755,
    )
    api.packager_provided_file(
        "cron.yearly",
        "/etc/cron.yearly/{name}",
        default_mode=0o0755,
    )
    # cron.d uses 0644 unlike the others
    api.packager_provided_file(
        "cron.d",
        "/etc/cron.d/{name}",
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=["man:crontab(5)"]
        ),
    )

    api.packager_provided_file(
        "initramfs-hook", f"{INITRAMFS_HOOK_DIR}/{{name}}", default_mode=0o0755
    )

    api.packager_provided_file("modprobe", "/etc/modprobe.d/{name}.conf")

    api.packager_provided_file(
        "init",
        "/etc/init.d/{name}",
        default_mode=0o755,
    )
    api.packager_provided_file("default", "/etc/default/{name}")

    for stem in [
        "mount",
        "path",
        "service",
        "socket",
        "target",
        "timer",
    ]:
        api.packager_provided_file(
            stem,
            f"/usr/lib/systemd/system/{{name}}.{stem}",
            reference_documentation=packager_provided_file_reference_documentation(
                format_documentation_uris=[f"man:systemd.{stem}(5)"]
            ),
        )

    for stem in [
        "path",
        "service",
        "socket",
        "target",
        "timer",
    ]:
        api.packager_provided_file(
            f"@{stem}", f"/usr/lib/systemd/system/{{name}}@.{stem}"
        )

    # api.packager_provided_file(
    #     "udev",
    #     "./lib/udev/rules.d/{priority:02}-{name}.rules",
    #     default_priority=60,
    # )

    api.packager_provided_file(
        "gsettings-override",
        f"{GSETTINGS_SCHEMA_DIR}/{{priority:02}}_{{name}}.gschema.override",
        default_priority=10,
    )

    # Special-cases that will probably not be a good example for other plugins
    api.packager_provided_file(
        "changelog",
        # The "changelog.Debian" gets renamed to "changelog" for native packages elsewhere.
        # Also, the changelog trimming is also done elsewhere.
        "/usr/share/doc/{name}/changelog.Debian",
        allow_name_segment=False,
        packageless_is_fallback_for_all_packages=True,
        reference_documentation=packager_provided_file_reference_documentation(
            description=textwrap.dedent(
                """\
                This file is the changelog of the package and is mandatory.

                The changelog contains the version of the source package and is mandatory for all
                packages.

                Use `dch --create` to create the changelog.

                In theory, the binary package can have a different changelog than the source
                package (by having `debian/binary-package.changelog`). However, it is generally
                not useful and leads to double administration. It has not been used in practice.
            """
            ),
            format_documentation_uris=[
                "man:deb-changelog(5)",
                "https://www.debian.org/doc/debian-policy/ch-source.html#debian-changelog-debian-changelog",
                "man:dch(1)",
            ],
        ),
    )
    api.packager_provided_file(
        "copyright",
        "/usr/share/doc/{name}/copyright",
        allow_name_segment=False,
        packageless_is_fallback_for_all_packages=True,
        reference_documentation=packager_provided_file_reference_documentation(
            description=textwrap.dedent(
                """\
                This file documents the license and copyright information of the binary package.
                Packages aimed at the Debian archive (and must derivatives thereof) must have this file.

                For packages not aimed at Debian, the file can still be useful to convey the license
                terms of the package (which is often a requirement in many licenses). However, it is
                not a strict *technical* requirement. Whether it is a legal requirement depends on
                license.

                Often, the same file can be used for all packages. In the extremely rare case where
                one binary package has a "vastly different" license than the other packages, you can
                provide a package specific version for that package.
            """
            ),
            format_documentation_uris=[
                "https://www.debian.org/doc/debian-policy/ch-source.html#copyright-debian-copyright",
                "https://www.debian.org/doc/debian-policy/ch-docs.html#s-copyrightfile",
                "https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/",
            ],
        ),
    )
    api.packager_provided_file(
        "NEWS",
        "/usr/share/doc/{name}/NEWS.Debian",
        allow_name_segment=False,
        packageless_is_fallback_for_all_packages=True,
        reference_documentation=packager_provided_file_reference_documentation(
            description=textwrap.dedent(
                """\
            Important news that should be shown to the user/admin when upgrading. If a system has
            apt-listchanges installed, then contents of this file will be shown prior to upgrading
            the package.

            Uses a similar format to that of debian/changelog (create with `dch --news --create`).
            """
            ),
            format_documentation_uris=[
                "https://www.debian.org/doc/manuals/developers-reference/best-pkging-practices.en.html#supplementing-changelogs-with-news-debian-files",
                "man:dch(1)",
            ],
        ),
    )
    api.packager_provided_file(
        "README.Debian",
        "/usr/share/doc/{name}/README.Debian",
        allow_name_segment=False,
    )
    api.packager_provided_file(
        "TODO",
        "/usr/share/doc/{name}/TODO.Debian",
        allow_name_segment=False,
    )
    # From dh-python / dh_python3
    # api.packager_provided_file(
    #     "bcep",
    #     "/usr/share/python3/bcep/{name}",
    #     allow_name_segment=False,
    # )


def _replace_dot_with_underscore(x: str) -> str:
    return x.replace(".", "_")
