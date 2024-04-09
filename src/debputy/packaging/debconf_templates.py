import os.path
import shutil
import subprocess
import textwrap
from typing import List, Dict

from debputy.maintscript_snippet import MaintscriptSnippetContainer, MaintscriptSnippet
from debputy.packager_provided_files import PackagerProvidedFile
from debputy.packages import BinaryPackage
from debputy.packaging.makeshlibs import resolve_reserved_provided_file
from debputy.plugin.api.spec import FlushableSubstvars
from debputy.util import _error, escape_shell

# Match debhelper (minus one space in each end, which comes
# via join).
LINE_PREFIX = "\\\n            "


def process_debconf_templates(
    binary_package: BinaryPackage,
    reserved_packager_provided_files: Dict[str, List[PackagerProvidedFile]],
    maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
    substvars: FlushableSubstvars,
    control_output_dir: str,
) -> None:
    provided_templates_file = resolve_reserved_provided_file(
        "templates",
        reserved_packager_provided_files,
    )
    if provided_templates_file is None:
        return

    templates_file = os.path.join(control_output_dir, "templates")
    debian_dir = provided_templates_file.parent_dir
    po_template_dir = debian_dir.get("po") if debian_dir is not None else None
    if po_template_dir is not None and po_template_dir.is_dir:
        with open(templates_file, "wb") as fd:
            cmd = [
                "po2debconf",
                provided_templates_file.fs_path,
            ]
            print(f"   {escape_shell(*cmd)} > {templates_file}")
            try:
                subprocess.check_call(
                    cmd,
                    stdout=fd.fileno(),
                )
            except subprocess.CalledProcessError:
                _error(
                    f"Failed to generate the templates files for {binary_package.name}. Please review "
                    f" the output of {escape_shell('po-debconf', provided_templates_file.fs_path)}"
                    " to understand the issue."
                )
    else:
        shutil.copyfile(provided_templates_file.fs_path, templates_file)

    dependency = (
        "cdebconf-udeb" if binary_package.is_udeb else "debconf (>= 0.5) | debconf-2.0"
    )
    substvars.add_dependency("misc:Depends", dependency)
    if not binary_package.is_udeb:
        # udebs do not have `postrm` scripts
        maintscript_snippets["postrm"].append(
            MaintscriptSnippet(
                f"debputy (due to {provided_templates_file.fs_path})",
                # FIXME: `debconf` sourcing should be an overarching feature
                snippet=textwrap.dedent(
                    """\
                    if [ "$1" = purge ] && [ -e /usr/share/debconf/confmodule ]; then
                        . /usr/share/debconf/confmodule
                        db_purge
                        db_stop
                    fi
                """
                ),
            )
        )
