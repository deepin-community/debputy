import textwrap

from debputy.plugin.api import (
    DebputyPluginInitializer,
    VirtualPath,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.util import POSTINST_DEFAULT_CONDITION


def _maintscript_generator(
    _path: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    maintscript = ctrl.maintscript

    # When `debputy` becomes a stand-alone package, it should have these maintscripts instead of dh-debputy
    # Admittedly, I hope to get rid of this plugin before then, but ...
    assert context.binary_package.name != "debputy", "Update the self-hosting plugin"
    dirname = "/usr/share/debputy"

    if context.binary_package.name == "dh-debputy":
        ctrl.dpkg_trigger("interest-noawait", dirname)
        maintscript.unconditionally_in_script(
            "postinst",
            textwrap.dedent(
                f"""\
            if {POSTINST_DEFAULT_CONDITION} || [ "$1" = "triggered" ] ; then
                # Ensure all plugins are byte-compiled (plus uninstalled plugins are cleaned up)
                py3clean {dirname}
                if command -v py3compile >/dev/null 2>&1; then
                    py3compile {dirname}
                fi
                if command -v pypy3compile >/dev/null 2>&1; then
                    pypy3compile {dirname} || true
                fi
            fi
        """
            ),
        )
        maintscript.unconditionally_in_script(
            "prerm",
            textwrap.dedent(
                f"""\
        if command -v py3clean >/dev/null 2>&1; then
            py3clean {dirname}
        else
            find {dirname}/ -type d -name __pycache__ -empty -print0 | xargs --null --no-run-if-empty rmdir
        fi
        """
            ),
        )


def initializer(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector(
        "debputy-self-hosting",
        _maintscript_generator,
    )
