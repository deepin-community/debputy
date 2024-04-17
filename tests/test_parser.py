import textwrap

import pytest

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.exceptions import DebputySubstitutionError
from debputy.highlevel_manifest_parser import YAMLManifestParser
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.plugin.api.test_api import build_virtual_file_system


def normalize_doc_link(message) -> str:
    return message.replace(DEBPUTY_DOC_ROOT_DIR, "{{DEBPUTY_DOC_ROOT_DIR}}")


@pytest.fixture()
def manifest_parser_pkg_foo(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
    source_package,
    package_single_foo_arch_all_cxt_amd64,
    amd64_substitution,
    no_profiles_or_build_options,
    debputy_plugin_feature_set,
) -> YAMLManifestParser:
    # We need an empty directory to avoid triggering packager provided files.
    debian_dir = build_virtual_file_system([])
    return YAMLManifestParser(
        "debian/test-debputy.manifest",
        source_package,
        package_single_foo_arch_all_cxt_amd64,
        amd64_substitution,
        amd64_dpkg_architecture_variables,
        dpkg_arch_query,
        no_profiles_or_build_options,
        debputy_plugin_feature_set,
        debian_dir=debian_dir,
    )


def test_parsing_no_manifest(manifest_parser_pkg_foo):
    manifest = manifest_parser_pkg_foo.build_manifest()

    assert [p.name for p in manifest.all_packages] == ["foo"]
    assert [p.name for p in manifest.active_packages] == ["foo"]


def test_parsing_version_only(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    """
    )

    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert [p.name for p in manifest.all_packages] == ["foo"]
    assert [p.name for p in manifest.active_packages] == ["foo"]


def test_parsing_empty_installations(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    installations: []
    """
    )

    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert [p.name for p in manifest.all_packages] == ["foo"]
    assert [p.name for p in manifest.active_packages] == ["foo"]


def test_parsing_variables(manifest_parser_pkg_foo):
    # https://salsa.debian.org/debian/debputy/-/issues/58
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    definitions:
      variables:
        LIBPATH: "/usr/lib/{{DEB_HOST_MULTIARCH}}"
        SONAME: "1"
        LETTER_O: "o"
    installations:
      - install:
           source: build/libfoo.so.{{SONAME}}
           dest-dir: "{{LIBPATH}}"
           into: f{{LETTER_O}}{{LETTER_O}}
    packages:
      f{{LETTER_O}}{{LETTER_O}}:
        transformations:
          - create-symlink:
              path: "{{LIBPATH}}/libfoo.so.{{SONAME}}.0.0"
              target: "{{LIBPATH}}/libfoo.so.{{SONAME}}"
    """
    )
    manifest_parser_pkg_foo.parse_manifest(fd=content)
    # TODO: Verify that the substitution is applied correctly throughout
    # (currently, the test just verifies that we do not reject the manifest)


@pytest.mark.parametrize(
    "varname",
    [
        "PACKAGE",
        "DEB_HOST_ARCH",
        "DEB_BLAH_ARCH",
        "env:UNEXISTING",
        "token:TAB",
    ],
)
def test_parsing_variables_reserved(manifest_parser_pkg_foo, varname):
    content = textwrap.dedent(
        f"""\
    manifest-version: '0.1'
    definitions:
      variables:
        '{varname}': "test"
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = f'The variable "{varname}" is already reserved/defined. Error triggered by definitions.variables.{varname}.'
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_parsing_variables_interdependent_ok(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    definitions:
      variables:
        DOC_PATH: "/usr/share/doc/foo"
        EXAMPLE_PATH: "{{DOC_PATH}}/examples"
    installations:
    - install:
        source: foo.example
        dest-dir: "{{EXAMPLE_PATH}}"
    """
    )

    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    resolved = manifest.substitution.substitute("{{EXAMPLE_PATH}}", "test")
    assert resolved == "/usr/share/doc/foo/examples"


def test_parsing_variables_unused(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        f"""\
    manifest-version: '0.1'
    definitions:
      variables:
        UNUSED: "test"
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        'The variable "UNUSED" is unused. Either use it or remove it.'
        " The variable was declared at definitions.variables.UNUSED."
    )
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_parsing_package_foo_empty(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        "The attribute packages.foo must be a non-empty mapping. Please see"
        " {{DEBPUTY_DOC_ROOT_DIR}}/MANIFEST-FORMAT.md#binary-package-rules for the documentation."
    )
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_parsing_package_bar_empty(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        bar:
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert 'package "bar" is not present' in e_info.value.args[0]


def test_transformations_no_list(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              create-symlinks:
                 path: a
                 target: b
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert "packages.foo.transformations" in e_info.value.args[0]
    assert "must be a list" in e_info.value.args[0]


def test_create_symlinks_missing_path(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  target: b
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        "The following keys were required but not present at packages.foo.transformations[0].create-symlink: 'path'"
        " (Documentation: "
        "{{DEBPUTY_DOC_ROOT_DIR}}/MANIFEST-FORMAT.md#create-symlinks-transformation-rule-create-symlink)"
    )
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_create_symlinks_unknown_replacement_rule(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  path: usr/share/foo
                  target: /usr/share/bar
                  replacement-rule: golf
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        'The attribute "packages.foo.transformations[0].create-symlink.replacement-rule <Search for: usr/share/foo>"'
        " did not have a valid structure/type: Value (golf) must be one of the following literal values:"
        ' "error-if-exists", "error-if-directory", "abort-on-non-empty-directory", "discard-existing"'
    )
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_create_symlinks_missing_target(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  path: a
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        "The following keys were required but not present at packages.foo.transformations[0].create-symlink: 'target'"
        " (Documentation: "
        "{{DEBPUTY_DOC_ROOT_DIR}}/MANIFEST-FORMAT.md#create-symlinks-transformation-rule-create-symlink)"
    )
    assert normalize_doc_link(e_info.value.args[0]) == msg


def test_create_symlinks_not_normalized_path(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  path: ../bar
                  target: b
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    expected = (
        'The path "../bar" provided in packages.foo.transformations[0].create-symlink.path <Search for: ../bar>'
        ' should be relative to the root of the package and not use any ".." or "." segments.'
    )
    assert e_info.value.args[0] == expected


def test_unresolvable_subst_in_source_context(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    installations:
    - install:
       source: "foo.sh"
       as: "usr/bin/{{PACKAGE}}"
    """
    )

    with pytest.raises(DebputySubstitutionError) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    expected = (
        "The variable {{PACKAGE}} is not available while processing installations[0].install.as"
        " <Search for: foo.sh>."
    )

    assert e_info.value.args[0] == expected


def test_yaml_error_duplicate_key(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  path: ../bar
                  target: b
                  # Duplicate key error
                  path: ../foo
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert "duplicate key" in e_info.value.args[0]


def test_yaml_error_tab_start(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-symlink:
                  path: ../bar
                  target: b
    # Tab is not allowed here in this case.
    \ta
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    assert "'\\t' that cannot start any token" in e_info.value.args[0]


def test_yaml_octal_mode_int(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - path-metadata:
                  path: usr/share/bar
                  mode: 0755
    """
    )

    with pytest.raises(ManifestParseException) as e_info:
        manifest_parser_pkg_foo.parse_manifest(fd=content)

    msg = (
        'The attribute "packages.foo.transformations[0].path-metadata.mode <Search for: usr/share/bar>" did not'
        " have a valid structure/type: The attribute must be a FileSystemMode (string)"
    )

    assert e_info.value.args[0] == msg


def test_yaml_clean_after_removal(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            clean-after-removal:
            - /var/log/foo/*.log
            - /var/log/foo/*.log.gz
            - path: /var/log/foo/
              ignore-non-empty-dir: true
            - /etc/non-conffile-configuration.conf
            - path: /var/cache/foo
              recursive: true

    """
    )

    manifest_parser_pkg_foo.parse_manifest(fd=content)


def test_binary_version(manifest_parser_pkg_foo):
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            binary-version: 1:2.3

    """
    )

    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    assert manifest.package_state_for("foo").binary_version == "1:2.3"


@pytest.mark.parametrize(
    "path,is_accepted",
    [
        ("usr/bin/foo", False),
        ("var/cache/foo*", False),
        ("var/cache/foo", True),
        ("var/cache/foo/", True),
        ("var/cache/foo/*", True),
        ("var/cache/foo/*.*", True),
        ("var/cache/foo/*.txt", True),
        ("var/cache/foo/cache.*", True),
        ("etc/foo*", False),
        ("etc/foo/*", True),
        ("etc/foo/", True),
        # /var/log is special-cased
        ("/var/log/foo*", True),
        ("/var/log/foo/*.*", True),
        ("/var/log/foo/", True),
        # Unsupported pattern at the time of writing
        ("/var/log/foo/*.*.*", False),
        # Questionable rules
        ("*", False),
        ("*.la", False),
        ("*/foo/*", False),
    ],
)
def test_yaml_clean_after_removal_unsafe_path(
    manifest_parser_pkg_foo,
    path: str,
    is_accepted: bool,
) -> None:
    content = textwrap.dedent(
        f"""\
    manifest-version: '0.1'
    packages:
        foo:
            clean-after-removal:
            - {path}
    """
    )

    if is_accepted:
        manifest_parser_pkg_foo.parse_manifest(fd=content)
    else:
        with pytest.raises(ManifestParseException) as e_info:
            manifest_parser_pkg_foo.parse_manifest(fd=content)
