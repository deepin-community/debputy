from typing import Dict, Iterable

from debputy.packager_provided_files import (
    PerPackagePackagerProvidedResult,
    PackagerProvidedFile,
)


def flatten_ppfs(
    all_ppfs: Dict[str, PerPackagePackagerProvidedResult]
) -> Iterable[PackagerProvidedFile]:
    for matched_ppf in all_ppfs.values():
        yield from matched_ppf.auto_installable
        for reserved_ppfs in matched_ppf.reserved_only.values():
            yield from reserved_ppfs
