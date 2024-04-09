import re

from debputy.plugin.api import VirtualPath

_VCS_PATHS = {
    ".arch-inventory",
    ".arch-ids",
    ".be",
    ".bzrbackup",
    ".bzrignore",
    ".bzrtags",
    ".cvsignore",
    ".hg",
    ".hgignore",
    ".hgtags",
    ".hgsigs",
    ".git",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitreview",
    ".mailmap",
    ".mtn-ignore",
    ".svn",
    "{arch}",
    "CVS",
    "RCS",
    "_MTN",
    "_darcs",
}

_BACKUP_FILES_RE = re.compile(
    "|".join(
        [
            # Common backup files
            r".*~",
            r".*[.](?:bak|orig|rej)",
            # Editor backup/swap files
            r"[.]#.*",
            r"[.].*[.]sw.",
            # Other known stuff
            r"[.]shelf",
            r",,.*",  # "baz-style junk" (according to dpkg (Dpkg::Source::Package)
            r"DEADJOE",  # Joe's one line of immortality that just gets cargo cult'ed around ... just in case.
        ]
    )
)

_DOXYGEN_DIR_TEST_FILES = ["doxygen.css", "doxygen.svg", "index.html"]


def _debputy_discard_pyc_files(path: "VirtualPath") -> bool:
    if path.name == "__pycache__" and path.is_dir:
        return True
    return path.name.endswith((".pyc", ".pyo")) and path.is_file


def _debputy_prune_la_files(path: "VirtualPath") -> bool:
    return (
        path.name.endswith(".la")
        and path.is_file
        and path.absolute.startswith("/usr/lib")
    )


def _debputy_prune_backup_files(path: VirtualPath) -> bool:
    return bool(_BACKUP_FILES_RE.match(path.name))


def _debputy_prune_vcs_paths(path: VirtualPath) -> bool:
    return path.name in _VCS_PATHS


def _debputy_prune_info_dir_file(path: VirtualPath) -> bool:
    return path.absolute == "/usr/share/info/dir"


def _debputy_prune_binary_debian_dir(path: VirtualPath) -> bool:
    return path.absolute == "/DEBIAN"


def _debputy_prune_doxygen_cruft(path: VirtualPath) -> bool:
    if not path.name.endswith((".md5", ".map")) or not path.is_file:
        return False
    parent_dir = path.parent_dir
    while parent_dir:
        is_doxygen_dir = True
        for name in _DOXYGEN_DIR_TEST_FILES:
            test_file = parent_dir.get(name)
            if test_file is None or not test_file.is_file:
                is_doxygen_dir = False
                break

        if is_doxygen_dir:
            return True
        parent_dir = parent_dir.parent_dir
    return False
