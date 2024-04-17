from debputy.plugin.api import (
    DebputyPluginInitializer,
    VirtualPath,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)


def _udeb_metadata_detector(
    _path: VirtualPath,
    ctrl: BinaryCtrlAccessor,
    _context: PackageProcessingContext,
) -> None:
    ctrl.substvars["Test:Udeb-Metadata-Detector"] = "was-run"


def custom_plugin_initializer(api: DebputyPluginInitializer) -> None:
    api.packager_provided_file(
        "my-file",
        "/no-where/this/is/a/test/plugin/{name}.conf",
        post_formatting_rewrite=lambda x: x.replace("+", "_"),
    )
    api.metadata_or_maintscript_detector(
        "udeb-only", _udeb_metadata_detector, package_type="udeb"
    )
