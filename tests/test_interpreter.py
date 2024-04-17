import textwrap
from typing import Optional

import pytest

from debputy.highlevel_manifest import HighLevelManifest
from debputy.highlevel_manifest_parser import YAMLManifestParser
from debputy.interpreter import extract_shebang_interpreter
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.test_api import build_virtual_file_system
from debputy.transformation_rules import NormalizeShebangLineTransformation


@pytest.mark.parametrize(
    "raw_shebang,original_command,command_full_basename,command_stem,correct_command,corrected_shebang_line",
    [
        (
            b"#!       /usr/bin/false\r\n",
            "/usr/bin/false",
            "false",
            "false",
            None,
            None,
        ),
        (
            b"#!/usr/bin/python3 -b",
            "/usr/bin/python3",
            "python3",
            "python",
            "/usr/bin/python3",
            None,
        ),
        (
            b"#!/usr/bin/env python3 -b",
            "/usr/bin/env python3",
            "python3",
            "python",
            "/usr/bin/python3",
            "#! /usr/bin/python3 -b",
        ),
        (
            b"#! /bin/env python3.12-dbg -b",
            "/bin/env python3.12-dbg",
            "python3.12-dbg",
            "python",
            "/usr/bin/python3.12-dbg",
            "#! /usr/bin/python3.12-dbg -b",
        ),
        (
            b"#! /usr/bin/bash",
            "/usr/bin/bash",
            "bash",
            "bash",
            "/bin/bash",
            "#! /bin/bash",
        ),
        (
            b"#! /usr/bin/env sh",
            "/usr/bin/env sh",
            "sh",
            "sh",
            "/bin/sh",
            "#! /bin/sh",
        ),
        (
            b"#! /usr/local/bin/perl",
            "/usr/local/bin/perl",
            "perl",
            "perl",
            "/usr/bin/perl",
            "#! /usr/bin/perl",
        ),
    ],
)
def test_interpreter_detection(
    raw_shebang: bytes,
    original_command: str,
    command_full_basename: str,
    command_stem: str,
    correct_command: Optional[str],
    corrected_shebang_line: Optional[str],
) -> None:
    interpreter = extract_shebang_interpreter(raw_shebang)
    # The `and ...` part is just to get the raw line in the error message
    assert interpreter is not None or raw_shebang == b""

    assert interpreter.original_command == original_command
    assert interpreter.command_full_basename == command_full_basename
    assert interpreter.command_stem == command_stem
    assert interpreter.correct_command == correct_command
    assert interpreter.corrected_shebang_line == corrected_shebang_line
    assert interpreter.fixup_needed == (corrected_shebang_line is not None)


@pytest.mark.parametrize(
    "raw_data",
    [
        b"#!#!#!       /usr/bin/false",
        b"#!perl",  # Used in files as an editor hint
        b"\x7FELF/usr/bin/perl",
        b"\x00\01\x02\x03/usr/bin/perl",
        b"PK\x03\x03/usr/bin/perl",
    ],
)
def test_interpreter_negative(raw_data: bytes) -> None:
    assert extract_shebang_interpreter(raw_data) is None


@pytest.fixture
def empty_manifest(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
    source_package,
    package_single_foo_arch_all_cxt_amd64,
    amd64_substitution,
    no_profiles_or_build_options,
    debputy_plugin_feature_set,
) -> HighLevelManifest:
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
    ).build_manifest()


def test_interpreter_rewrite(empty_manifest: HighLevelManifest) -> None:
    condition_context = empty_manifest.condition_context("foo")
    fs_root = build_virtual_file_system(
        [
            virtual_path_def("usr/bin/foo", content="random data"),
            virtual_path_def(
                "usr/bin/foo.sh",
                materialized_content="#!/usr/bin/sh\nset -e\n",
            ),
        ]
    )
    interpreter_normalization = NormalizeShebangLineTransformation()
    interpreter_normalization.transform_file_system(fs_root, condition_context)
    foo = fs_root.lookup("usr/bin/foo")
    foo_sh = fs_root.lookup("usr/bin/foo.sh")

    assert foo.is_file
    with foo.open() as fd:
        assert fd.read() == "random data"

    assert foo_sh.is_file
    with foo_sh.open() as fd:
        expected = textwrap.dedent(
            """\
        #! /bin/sh
        set -e
        """
        )
        assert fd.read() == expected
