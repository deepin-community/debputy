import textwrap
from typing import Type, Sequence, Mapping, Container, Iterable, Any

from debputy.manifest_parser.base_types import DebputyParsedContentStandardConditional
from debputy.manifest_parser.tagging_types import DebputyParsedContent
from debputy.plugin.api.spec import (
    ParserAttributeDocumentation,
    StandardParserAttributeDocumentation,
)
from debputy.plugin.debputy.to_be_api_types import (
    OptionalInstallDirectly,
    OptionalInSourceBuild,
    OptionalBuildDirectory,
    BuildRuleParsedFormat,
)

_STD_ATTR_DOCS: Mapping[
    Type[DebputyParsedContent],
    Sequence[ParserAttributeDocumentation],
] = {
    BuildRuleParsedFormat: [
        StandardParserAttributeDocumentation(
            frozenset(["name"]),
            textwrap.dedent(
                """\
          The name of the build step.

          The name is used for multiple things, such as:
            1) If you ever need to reference the build elsewhere, the name will be used.
            2) When `debputy` references the build in log output and error, it will use the name.
            3) It is used as defaults for when `debputy` derives build and `DESTDIR` directories
               for the build.
        """
            ),
            # Put in top,
            sort_category=-1000,
        ),
        StandardParserAttributeDocumentation(
            frozenset(["for_packages"]),
            textwrap.dedent(
                """\
          Which package or packages this build step applies to.

          Either a package name or a list of package names.
        """
            ),
        ),
        StandardParserAttributeDocumentation(
            frozenset(["environment"]),
            textwrap.dedent(
                """\
          Specify that this build step uses the named environment

          If omitted, the default environment will be used. If no default environment is present,
          then this option is mandatory.
        """
            ),
        ),
    ],
    OptionalBuildDirectory: [
        StandardParserAttributeDocumentation(
            frozenset(["build_directory"]),
            textwrap.dedent(
                """\
          The build directory to use for the build.

          By default, `debputy` will derive a build directory automatically if the build system needs
          it. However, it can be useful if you need to reference the directory name from other parts
          of the manifest or want a "better" name than `debputy` comes up with.
        """
            ),
        ),
    ],
    OptionalInSourceBuild: [
        StandardParserAttributeDocumentation(
            frozenset(["perform_in_source_build"]),
            textwrap.dedent(
                """\
          Whether the build system should use "in source" or "out of source" build.

          This is mostly useful for forcing "in source" builds for build systems that default to
          "out of source" builds like `autoconf`.

          The default depends on the build system and the value of the `build-directory` attribute
          (if supported by the build system).
        """
            ),
            # Late
            sort_category=500,
        ),
    ],
    OptionalInstallDirectly: [
        StandardParserAttributeDocumentation(
            frozenset(["install_directly_to_package"]),
            textwrap.dedent(
                """\
          Whether the build system should install all upstream content directly into the package.

          This option is mostly useful for disabling said behavior by setting the attribute to `false`.
          The attribute conditionally defaults to `true` when the build only applies to one package.
          If explicitly set to `true`, then this build step must apply to exactly one package (usually
          implying that `for` is set to that package when the source builds multiple packages).

          When `true`, this behaves similar to `dh_auto_install --destdir=debian/PACKAGE`.
        """
            ),
        ),
    ],
    DebputyParsedContentStandardConditional: [
        StandardParserAttributeDocumentation(
            frozenset(["when"]),
            textwrap.dedent(
                """\
            A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).

            The conditional will disable the entire rule when the conditional evaluates to false.
        """
            ),
            # Last
            sort_category=9999,
        ),
    ],
}


def docs_from(
    *ts: Any,
    exclude_attributes: Container[str] = frozenset(),
) -> Iterable[ParserAttributeDocumentation]:
    """Provide standard attribute documentation from existing types

    This is a work-around for `apply_standard_attribute_documentation` requiring python3.12.
    If you can assume python3.12, use `apply_standard_attribute_documentation` instead.
    """
    for t in ts:
        attrs = _STD_ATTR_DOCS.get(t)
        if attrs is None:
            raise ValueError(f"No standard documentation for {str(t)}")
        for attr in attrs:
            if any(a in exclude_attributes for a in attrs):
                continue
            yield attr
