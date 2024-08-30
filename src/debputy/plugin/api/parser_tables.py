import textwrap

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.installations import InstallRule
from debputy.maintscript_snippet import DpkgMaintscriptHelperCommand
from debputy.manifest_conditions import ManifestCondition
from debputy.plugin.api import reference_documentation
from debputy.plugin.debputy.to_be_api_types import BuildRule
from debputy.transformation_rules import TransformationRule

SUPPORTED_DISPATCHABLE_TABLE_PARSERS = {
    InstallRule: "installations",
    TransformationRule: "packages.{{PACKAGE}}.transformations",
    DpkgMaintscriptHelperCommand: "packages.{{PACKAGE}}.conffile-management",
    ManifestCondition: "*.when",
    BuildRule: "builds",
}

OPARSER_MANIFEST_ROOT = "<ROOT>"
OPARSER_PACKAGES_ROOT = "packages"
OPARSER_PACKAGES = "packages.{{PACKAGE}}"
OPARSER_MANIFEST_DEFINITIONS = "definitions"

SUPPORTED_DISPATCHABLE_OBJECT_PARSERS = {
    OPARSER_MANIFEST_ROOT: reference_documentation(
        reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md",
    ),
    OPARSER_MANIFEST_DEFINITIONS: reference_documentation(
        title="Packager provided definitions",
        description="Reusable packager provided definitions such as manifest variables.",
        reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#packager-provided-definitions",
    ),
    OPARSER_PACKAGES: reference_documentation(
        title="Binary package rules",
        description=textwrap.dedent(
            """\
            Inside the manifest, the `packages` mapping can be used to define requests for the binary packages
            you want `debputy` to produce.  Each key inside `packages` must be the name of a binary package
            defined in `debian/control`.  The value is a dictionary defining which features that `debputy`
            should apply to that binary package.  An example could be:

                packages:
                    foo:
                        transformations:
                            - create-symlink:
                                  path: usr/share/foo/my-first-symlink
                                  target: /usr/share/bar/symlink-target
                            - create-symlink:
                                  path: usr/lib/{{DEB_HOST_MULTIARCH}}/my-second-symlink
                                  target: /usr/lib/{{DEB_HOST_MULTIARCH}}/baz/symlink-target
                    bar:
                        transformations:
                        - create-directories:
                           - some/empty/directory.d
                           - another/empty/integration-point.d
                        - create-directories:
                             path: a/third-empty/directory.d
                             owner: www-data
                             group: www-data

            In this case, `debputy` will create some symlinks inside the `foo` package and some directories for
            the `bar` package.  The following subsections define the keys you can use under each binary package.
        """
        ),
        reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#binary-package-rules",
    ),
}
