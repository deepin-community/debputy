import json
import os.path
from typing import List, Tuple, Type, cast

import pytest

from debputy.exceptions import DebputyFSIsROError
from debputy.plugin.api import (
    DebputyPluginInitializer,
    BinaryCtrlAccessor,
    PackageProcessingContext,
    VirtualPath,
    virtual_path_def,
)
from debputy.exceptions import PluginConflictError, PluginAPIViolationError
from debputy.plugin.api.impl import DebputyPluginInitializerProvider
from debputy.plugin.api.impl_types import automatic_discard_rule_example
from debputy.plugin.api.test_api import (
    build_virtual_file_system,
    package_metadata_context,
    initialize_plugin_under_test,
)
from debputy.plugin.api.test_api.test_impl import (
    initialize_plugin_under_test_preloaded,
    initialize_plugin_under_test_from_inline_json,
)

CUSTOM_PLUGIN_JSON_FILE = os.path.join(
    os.path.dirname(__file__), "data", "custom-plugin.json.in"
)


def bad_metadata_detector_fs_rw(
    path: VirtualPath,
    _ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    del path["foo"]


def conflicting_plugin(api: DebputyPluginInitializer) -> None:
    api.packager_provided_file(
        "logrotate",
        "/some/where/that/is/not/etc/logrotate.d/{name}",
    )


def bad_plugin(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector("fs_rw", bad_metadata_detector_fs_rw)


def adr_inconsistent_example_plugin(api: DebputyPluginInitializerProvider) -> None:
    api.automatic_discard_rule(
        "adr-example-test",
        lambda p: p.name == "discard-me",
        examples=automatic_discard_rule_example(
            "foo/discard-me",
            ("bar/discard-me", False),
            "baz/something",
            ("discard-me/foo", False),
        ),
    )


def test_conflict_with_debputy():
    with pytest.raises(PluginConflictError) as e_info:
        initialize_plugin_under_test_preloaded(
            1,
            conflicting_plugin,
            plugin_name="conflicting-plugin",
        )
    message = (
        'The stem "logrotate" is registered twice for packager provided files.'
        " Once by debputy and once by conflicting-plugin"
    )
    assert message == e_info.value.args[0]


def test_metadata_read_only():
    plugin = initialize_plugin_under_test_preloaded(
        1,
        bad_plugin,
        plugin_name="bad-plugin",
    )
    fs = build_virtual_file_system(["./foo"])
    with pytest.raises(PluginAPIViolationError) as e_info:
        plugin.run_metadata_detector("fs_rw", fs)

    assert isinstance(e_info.value.__cause__, DebputyFSIsROError)


def test_packager_provided_files():
    plugin = initialize_plugin_under_test(plugin_desc_file=CUSTOM_PLUGIN_JSON_FILE)
    assert plugin.packager_provided_files_by_stem().keys() == {
        "my-file",
        "test-file-from-json",
    }
    my_file = [p for p in plugin.packager_provided_files() if p.stem == "my-file"][0]

    assert my_file.stem == "my-file"
    assert my_file.compute_dest("g++-3.1")[1] == "g__-3.1.conf"


def test_path_checks():
    symlink_path = "./foo"
    with pytest.raises(ValueError) as e_info:
        virtual_path_def(symlink_path, link_target="/bar", mode=0o0755)
    assert (
        e_info.value.args[0]
        == f'Please do not provide mode for symlinks. Triggered by "{symlink_path}"'
    )
    with pytest.raises(ValueError) as e_info:
        virtual_path_def(symlink_path + "/", link_target="/bar")
    msg = (
        "Path name looks like a directory, but a symlink target was also provided."
        f' Please remove the trailing slash OR the symlink_target. Triggered by "{symlink_path}/"'
    )
    assert e_info.value.args[0] == msg


def test_metadata_detector_applies_to_check():
    plugin_name = "custom-plugin"
    metadata_detector_id = "udeb-only"
    plugin = initialize_plugin_under_test(plugin_desc_file=CUSTOM_PLUGIN_JSON_FILE)
    with pytest.raises(ValueError) as e_info:
        plugin.run_metadata_detector(
            metadata_detector_id,
            build_virtual_file_system(["./usr/share/doc/foo/copyright"]),
        )
    msg = f'The detector "{metadata_detector_id}" from {plugin_name} does not apply to the given package.'
    assert e_info.value.args[0].startswith(msg)

    metadata = plugin.run_metadata_detector(
        metadata_detector_id,
        build_virtual_file_system(["./usr/share/doc/foo/copyright"]),
        context=package_metadata_context(package_fields={"Package-Type": "udeb"}),
    )
    assert metadata.substvars["Test:Udeb-Metadata-Detector"] == "was-run"


@pytest.mark.parametrize(
    "variables,exec_type",
    [
        (
            [("DEBPUTY_VAR", "RESERVED")],
            ValueError,
        ),
        (
            [("_DEBPUTY_VAR", "RESERVED")],
            ValueError,
        ),
        (
            [("_FOO", "RESERVED")],
            ValueError,
        ),
        (
            [("path:_var", "RESERVED")],
            ValueError,
        ),
        (
            [("path:DEBPUTY_VAR", "RESERVED")],
            ValueError,
        ),
        (
            [("DEB_VAR", "RESERVED")],
            ValueError,
        ),
        (
            [("DPKG_VAR", "RESERVED")],
            ValueError,
        ),
        (
            [("PACKAGE", "RESERVED")],
            ValueError,
        ),
        (
            [("foo:var", "RESERVED")],
            ValueError,
        ),
        (
            [("env:var", "RESERVED")],
            ValueError,
        ),
        (
            [("SOURCE_DATE_EPOCH", "RESERVED")],
            ValueError,
        ),
        (
            [("!MY_VAR", "INVALID_NAME")],
            ValueError,
        ),
        (
            [("VALUE_DEPENDS_ON_VAR", "{{UNKNOWN_VAR}}")],
            ValueError,
        ),
        (
            [("VALUE_DEPENDS_ON_VAR", "{{DEB_HOST_ARCH}}")],
            ValueError,
        ),
        (
            [("DEFINED_TWICE", "ONCE"), ("DEFINED_TWICE", "TWICE")],
            PluginConflictError,
        ),
    ],
)
def test_invalid_manifest_variables(
    variables: List[Tuple[str, str]],
    exec_type: Type[Exception],
) -> None:
    def _initializer(api: DebputyPluginInitializer):
        with pytest.raises(exec_type):
            for varname, value in variables:
                api.manifest_variable(varname, value)

    initialize_plugin_under_test_preloaded(
        1,
        _initializer,
        plugin_name="test-plugin",
        load_debputy_plugin=False,
    )


def test_valid_manifest_variables() -> None:
    variables = {
        "PLUGIN_VAR": "TEST VALUE",
        "ANOTHER_PLUGIN_VAR": "ANOTHER VALUE",
        "path:SOMEWHERE_DIR": "/usr/share/some-where",
    }

    def _initializer(api: DebputyPluginInitializer):
        for k, v in variables.items():
            api.manifest_variable(k, v)

    plugin = initialize_plugin_under_test_preloaded(
        1,
        _initializer,
        plugin_name="test-plugin",
        load_debputy_plugin=False,
    )

    assert plugin.declared_manifest_variables == variables.keys()


def test_valid_manifest_variables_json() -> None:
    variables = {
        "PLUGIN_VAR": "TEST VALUE",
        "ANOTHER_PLUGIN_VAR": "ANOTHER VALUE",
        "path:SOMEWHERE_DIR": "/usr/share/some-where",
    }
    content = {
        "api-compat-version": 1,
        "manifest-variables": [
            {
                "name": k,
                "value": v,
            }
            for k, v in variables.items()
        ],
    }
    plugin = initialize_plugin_under_test_from_inline_json(
        "test-plugin",
        json.dumps(content),
    )
    assert plugin.declared_manifest_variables == variables.keys()


def test_automatic_discard_rules_example() -> None:
    plugin = initialize_plugin_under_test_preloaded(
        1,
        # Internal API used
        cast("PluginInitializationEntryPoint", adr_inconsistent_example_plugin),
        # API is restricted
        plugin_name="debputy",
        load_debputy_plugin=False,
    )
    issues = plugin.automatic_discard_rules_examples_with_issues()
    assert len(issues) == 1
    issue = issues[0]
    assert issue.name == "adr-example-test"
    assert issue.example_index == 0
    assert set(issue.inconsistent_paths) == {
        "/discard-me/foo",
        "/bar/discard-me",
        "/baz/something",
    }
