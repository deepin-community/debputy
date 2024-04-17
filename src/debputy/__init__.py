import pathlib

from .version import IS_RELEASE_BUILD, __version__

# Replaced during install; must be a single line
# fmt: off
DEBPUTY_ROOT_DIR = pathlib.Path(__file__).parent.parent.parent
DEBPUTY_PLUGIN_ROOT_DIR = pathlib.Path(__file__).parent.parent.parent
# fmt: on

if IS_RELEASE_BUILD:
    DEBPUTY_DOC_ROOT_DIR = (
        f"https://salsa.debian.org/debian/debputy/-/blob/debian/{__version__}"
    )
else:
    DEBPUTY_DOC_ROOT_DIR = "https://salsa.debian.org/debian/debputy/-/blob/main"
