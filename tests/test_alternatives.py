import textwrap
from typing import Mapping

from debputy.maintscript_snippet import MaintscriptSnippetContainer
from debputy.packager_provided_files import PackagerProvidedFile
from debputy.packages import BinaryPackage
from debputy.packaging.alternatives import process_alternatives
from debputy.plugin.api import virtual_path_def, VirtualPath
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.plugin.api.impl_types import PackagerProvidedFileClassSpec
from debputy.plugin.api.test_api import build_virtual_file_system


def _ppf_for(pkg: BinaryPackage, path: VirtualPath) -> PackagerProvidedFile:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    return PackagerProvidedFile(
        path,
        pkg.name,
        "irrelevant",
        "irrelevant",
        PackagerProvidedFileClassSpec(
            plugin_metadata,
            "alternatives",
            "DEBIAN/alternatives",
            0o0644,
            None,
            False,
            False,
            None,
            False,
            True,
            None,
            None,
            False,
        ),
    )


def test_alternatives(
    package_single_foo_arch_all_cxt_amd64: Mapping[str, BinaryPackage],
) -> None:
    pkg = package_single_foo_arch_all_cxt_amd64["foo"]
    debian_dir = build_virtual_file_system(
        [
            virtual_path_def(
                "alternatives",
                fs_path="debian/alternatives",
                content=textwrap.dedent(
                    """\
        Name: x-terminal-emulator
        Link: /usr/bin/x-terminal-emulator
        Alternative: /usr/bin/xterm
        Dependents:
          /usr/share/man/man1/x-terminal-emulator.1.gz x-terminal-emulator.1.gz /usr/share/man/man1/xterm.1.gz
        Priority: 20
        """
                ),
            ),
        ]
    )
    fs_root = build_virtual_file_system(
        [
            "./usr/bin/xterm",
            "./usr/share/man/man1/xterm.1.gz",
        ]
    )
    reserved_ppfs = {"alternatives": [_ppf_for(pkg, debian_dir["alternatives"])]}
    maintscript_snippets = {
        "prerm": MaintscriptSnippetContainer(),
        "postinst": MaintscriptSnippetContainer(),
    }
    process_alternatives(
        pkg,
        fs_root,
        reserved_ppfs,
        maintscript_snippets,
    )

    prerm = maintscript_snippets["prerm"].generate_snippet(reverse=True)
    postinst = maintscript_snippets["postinst"].generate_snippet(reverse=True)

    assert "--remove x-terminal-emulator /usr/bin/xterm" in prerm
    assert (
        "--install /usr/bin/x-terminal-emulator x-terminal-emulator /usr/bin/xterm 20"
    ) in postinst
