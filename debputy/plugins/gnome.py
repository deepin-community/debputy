import re
from typing import Any

from debputy.plugin.api import (
    DebputyPluginInitializer,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.util import _error

GNOME_VERSION1_RE = re.compile(r"^(\d+:)?(\d+)\.(\d+)\.[\d.]+.*$")
GNOME_VERSION2_RE = re.compile(
    r"^(\d+:)?(\d+)(?:\.[\d.]+|~(alpha|beta|rc)[\d.]*|[+~])?.*$"
)


def initialize(api: DebputyPluginInitializer) -> None:
    api.metadata_or_maintscript_detector(
        "gnome-versions",
        gnome_versions,
        # Probably not necessary, but this is the most faithful conversion
        package_type=["deb", "udeb"],
    )
    # Looking for "clean_la_files"? The `debputy` plugin provides a replacement
    # feature.


def gnome_versions(
    _unused: Any,
    ctrl: BinaryCtrlAccessor,
    context: PackageProcessingContext,
) -> None:
    # Conversion note: In debhelper, $dh{VERSION} is actually the "source" version
    # (though sometimes it has a binNMU version too).  In `debputy`, we have access
    # to the "true" binary version (dpkg-gencontrol -v<VERSION>). In 99% of all cases,
    # the difference is irrelevant as people rarely use dpkg-gencontrol -v<VERSION>.
    version = context.binary_package_version
    m = GNOME_VERSION1_RE.match(version)
    epoch = ""
    gnome_version = ""
    gnome_next_version = ""
    if m:
        major_version = int(m.group(2))
        if major_version < 40:
            epoch = m.group(1)
            minor_version = int(m.group(3))
            gnome_version = f"{major_version}.{minor_version}"
            if major_version == 3 and minor_version == 38:
                prefix = ""
            else:
                prefix = f"{major_version}."
            gnome_next_version = f"{prefix}{minor_version + 2}"
    if gnome_version == "":
        m = GNOME_VERSION2_RE.match(version)
        if not m:
            _error(
                f"Unable to determine the GNOME major version from {version} for package"
                f" {context.binary_package.name}. If this is not a GNOME package or it does"
                f" not follow the GNOME version standard, please disable the GNOME plugin"
                f" (debputy-plugin-gnome)."
            )
        epoch = m.group(1)
        version = int(m.group(2))
        gnome_version = f"{version}~"
        gnome_next_version = f"{version + 1}~"
    if epoch is None:
        epoch = ""
    ctrl.substvars["gnome:Version"] = f"{epoch}{gnome_version}"
    ctrl.substvars["gnome:UpstreamVersion"] = f"{gnome_version}"
    ctrl.substvars["gnome:NextVersion"] = f"{epoch}{gnome_next_version}"
    ctrl.substvars["gnome:NextUpstreamVersion"] = f"{gnome_next_version}"
