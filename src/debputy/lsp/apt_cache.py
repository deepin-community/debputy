import asyncio
import dataclasses
import subprocess
import sys
from collections import defaultdict
from typing import Literal, Optional, Sequence, Iterable, Mapping

from debian.deb822 import Deb822
from debian.debian_support import Version

AptCacheState = Literal[
    "not-loaded",
    "loading",
    "loaded",
    "failed",
    "tooling-not-available",
    "empty-cache",
]


@dataclasses.dataclass(slots=True)
class PackageInformation:
    name: str
    architecture: str
    version: Version
    multi_arch: str
    # suites: Sequence[Tuple[str, ...]]
    synopsis: str
    section: str
    provides: Optional[str]
    upstream_homepage: Optional[str]


@dataclasses.dataclass(slots=True, frozen=True)
class PackageLookup:
    name: str
    package: Optional[PackageInformation]
    provided_by: Sequence[PackageInformation]


class AptCache:

    def __init__(self) -> None:
        self._state: AptCacheState = "not-loaded"
        self._load_error: Optional[str] = None
        self._lookups: Mapping[str, PackageLookup] = {}

    @property
    def state(self) -> AptCacheState:
        return self._state

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def lookup(self, name: str) -> Optional[PackageLookup]:
        return self._lookups.get(name)

    async def load(self) -> None:
        if self._state in ("loading", "loaded"):
            raise RuntimeError(f"Already {self._state}")
        self._load_error = None
        self._state = "loading"
        try:
            files_raw = subprocess.check_output(
                [
                    "apt-get",
                    "indextargets",
                    "--format",
                    "$(IDENTIFIER)\x1f$(FILENAME)",
                ]
            ).decode("utf-8")
        except FileNotFoundError:
            self._state = "tooling-not-available"
            self._load_error = "apt-get not available in PATH"
            return
        except subprocess.CalledProcessError as e:
            self._state = "failed"
            self._load_error = f"apt-get exited with {e.returncode}"
            return
        packages = {}
        for raw_file_line in files_raw.split("\n"):
            if not raw_file_line or raw_file_line.isspace():
                continue
            identifier, filename = raw_file_line.split("\x1f")
            if identifier not in ("Packages",):
                continue
            try:
                for package_info in parse_apt_file(filename):
                    # Let other computations happen if needed.
                    await asyncio.sleep(0)
                    existing = packages.get(package_info.name)
                    if existing and package_info.version < existing.version:
                        continue
                    packages[package_info.name] = package_info
            except FileNotFoundError:
                self._state = "tooling-not-available"
                self._load_error = "/usr/lib/apt/apt-helper not available"
                return
            except (AttributeError, RuntimeError, IndexError) as e:
                self._state = "failed"
                self._load_error = str(e)
                return
        provides = defaultdict(list)
        for package_info in packages.values():
            if not package_info.provides:
                continue
            # Some packages (`debhelper`) provides the same package multiple times (`debhelper-compat`).
            # Normalize that into one.
            deps = {
                clause.split("(")[0].strip()
                for clause in package_info.provides.split(",")
            }
            for dep in sorted(deps):
                provides[dep].append(package_info)

        self._lookups = {
            name: PackageLookup(
                name,
                packages.get(name),
                tuple(provides.get(name, [])),
            )
            for name in packages.keys() | provides.keys()
        }
        self._state = "loaded"


def parse_apt_file(filename: str) -> Iterable[PackageInformation]:
    proc = subprocess.Popen(
        ["/usr/lib/apt/apt-helper", "cat-file", filename],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    with proc:
        for stanza in Deb822.iter_paragraphs(proc.stdout):
            pkg_info = stanza_to_package_info(stanza)
            if pkg_info is not None:
                yield pkg_info


def stanza_to_package_info(stanza: Deb822) -> Optional[PackageInformation]:
    try:
        name = stanza["Package"]
        architecture = sys.intern(stanza["Architecture"])
        version = Version(stanza["Version"])
        multi_arch = sys.intern(stanza.get("Multi-Arch", "no"))
        synopsis = stanza["Description"]
        section = sys.intern(stanza["Section"])
        provides = stanza.get("Provides")
        homepage = stanza.get("Homepage")
    except KeyError:
        return None
    if "\n" in synopsis:
        # "Modern" Packages files do not have the full description. But in case we see a (very old one)
        # have consistent behavior with the modern ones.
        synopsis = synopsis.split("\n")[0]

    return PackageInformation(
        name,
        architecture,
        version,
        multi_arch,
        synopsis,
        section,
        provides,
        homepage,
    )
