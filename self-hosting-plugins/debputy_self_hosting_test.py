from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
)


def test_plugin():
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    plugin.run_metadata_detector("debputy-self-hosting", fs)
