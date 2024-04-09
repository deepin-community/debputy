from typing import Tuple, Mapping

from debian.deb822 import Deb822
from debian.debian_support import DpkgArchTable

from debputy.architecture_support import (
    faked_arch_table,
    DpkgArchitectureBuildProcessValuesTable,
)
from debputy.packages import BinaryPackage

_DPKG_ARCHITECTURE_TABLE_NATIVE_AMD64 = None
_DPKG_ARCH_QUERY_TABLE = None


def faked_binary_package(
    package, architecture="any", section="misc", is_main_package=False, **fields
) -> BinaryPackage:
    _arch_data_tables_loaded()

    dpkg_arch_table, dpkg_arch_query = _arch_data_tables_loaded()
    return BinaryPackage(
        Deb822(
            {
                "Package": package,
                "Architecture": architecture,
                "Section": section,
                **fields,
            }
        ),
        dpkg_arch_table,
        dpkg_arch_query,
        is_main_package=is_main_package,
    )


def binary_package_table(*args: BinaryPackage) -> Mapping[str, BinaryPackage]:
    packages = list(args)
    if not any(p.is_main_package for p in args):
        p = args[0]
        np = faked_binary_package(
            p.name,
            architecture=p.declared_architecture,
            section=p.archive_section,
            is_main_package=True,
            **{
                k: v
                for k, v in p.fields.items()
                if k.lower() not in ("package", "architecture", "section")
            },
        )
        packages[0] = np
    return {p.name: p for p in packages}


def _arch_data_tables_loaded() -> (
    Tuple[DpkgArchitectureBuildProcessValuesTable, DpkgArchTable]
):
    global _DPKG_ARCHITECTURE_TABLE_NATIVE_AMD64
    global _DPKG_ARCH_QUERY_TABLE
    if _DPKG_ARCHITECTURE_TABLE_NATIVE_AMD64 is None:
        _DPKG_ARCHITECTURE_TABLE_NATIVE_AMD64 = faked_arch_table("amd64")
    if _DPKG_ARCH_QUERY_TABLE is None:
        # TODO: Make a faked table instead, so we do not have data dependencies in the test.
        _DPKG_ARCH_QUERY_TABLE = DpkgArchTable.load_arch_table()
    return _DPKG_ARCHITECTURE_TABLE_NATIVE_AMD64, _DPKG_ARCH_QUERY_TABLE
