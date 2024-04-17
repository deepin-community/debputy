import io
import os
import struct
from typing import List, Optional, Callable, Tuple, Iterable

from debputy.filesystem_scan import FSPath
from debputy.plugin.api import VirtualPath

ELF_HEADER_SIZE32 = 136
ELF_HEADER_SIZE64 = 232
ELF_MAGIC = b"\x7fELF"
ELF_VERSION = 0x00000001
ELF_ENDIAN_LE = 0x01
ELF_ENDIAN_BE = 0x02
ELF_TYPE_EXECUTABLE = 0x0002
ELF_TYPE_SHARED_OBJECT = 0x0003

ELF_LINKING_TYPE_ANY = None
ELF_LINKING_TYPE_DYNAMIC = True
ELF_LINKING_TYPE_STATIC = False

ELF_EI_ELFCLASS32 = 1
ELF_EI_ELFCLASS64 = 2

ELF_PT_DYNAMIC = 2

ELF_EI_NIDENT = 0x10

# ELF header format:
# typedef struct {
#     unsigned char e_ident[EI_NIDENT];  # <-- 16 / 0x10 bytes
#     uint16_t      e_type;
#     uint16_t      e_machine;
#     uint32_t      e_version;
#     ElfN_Addr     e_entry;
#     ElfN_Off      e_phoff;
#     ElfN_Off      e_shoff;
#     uint32_t      e_flags;
#     uint16_t      e_ehsize;
#     uint16_t      e_phentsize;
#     uint16_t      e_phnum;
#     uint16_t      e_shentsize;
#     uint16_t      e_shnum;
#     uint16_t      e_shstrndx;
# } ElfN_Ehdr;


class IncompleteFileError(RuntimeError):
    pass


def is_so_or_exec_elf_file(
    path: VirtualPath,
    *,
    assert_linking_type: Optional[bool] = ELF_LINKING_TYPE_ANY,
) -> bool:
    is_elf, linking_type = _read_elf_file(
        path,
        determine_linking_type=assert_linking_type is not None,
    )
    return is_elf and (
        assert_linking_type is ELF_LINKING_TYPE_ANY
        or assert_linking_type == linking_type
    )


def _read_elf_file(
    path: VirtualPath,
    *,
    determine_linking_type: bool = False,
) -> Tuple[bool, Optional[bool]]:
    buffer_size = 4096
    fd_buffer = bytearray(buffer_size)
    linking_type = None
    fd: io.BufferedReader
    with path.open(byte_io=True, buffering=io.DEFAULT_BUFFER_SIZE) as fd:
        len_elf_header_raw = fd.readinto(fd_buffer)
        if (
            not fd_buffer
            or len_elf_header_raw < ELF_HEADER_SIZE32
            or not fd_buffer.startswith(ELF_MAGIC)
        ):
            return False, None

        elf_ei_class = fd_buffer[4]
        endian_raw = fd_buffer[5]
        if endian_raw == ELF_ENDIAN_LE:
            endian = "<"
        elif endian_raw == ELF_ENDIAN_BE:
            endian = ">"
        else:
            return False, None

        if elf_ei_class == ELF_EI_ELFCLASS64:
            offset_size = "Q"
            # We know it needs to be a 64bit ELF, then the header must be
            # large enough for that.
            if len_elf_header_raw < ELF_HEADER_SIZE64:
                return False, None
        elif elf_ei_class == ELF_EI_ELFCLASS32:
            offset_size = "L"
        else:
            return False, None

        elf_type, _elf_machine, elf_version = struct.unpack_from(
            f"{endian}HHL", fd_buffer, offset=ELF_EI_NIDENT
        )
        if elf_version != ELF_VERSION:
            return False, None
        if elf_type not in (ELF_TYPE_EXECUTABLE, ELF_TYPE_SHARED_OBJECT):
            return False, None

        if determine_linking_type:
            linking_type = _determine_elf_linking_type(
                fd, fd_buffer, endian, offset_size
            )
            if linking_type is None:
                return False, None

    return True, linking_type


def _determine_elf_linking_type(fd, fd_buffer, endian, offset_size) -> Optional[bool]:
    # To check the linking, we look for a DYNAMICALLY program header
    # In other words, we assume static linking by default.

    linking_type = ELF_LINKING_TYPE_STATIC
    # To do that, we need to read a bit more of the ELF header to
    # locate the Program header table.
    #
    # Reading - in order at offset 0x18:
    #  * e_entry (ignored)
    #  * e_phoff
    #  * e_shoff (ignored)
    #  * e_flags (ignored)
    #  * e_ehsize (ignored)
    #  * e_phentsize
    #  * e_phnum
    _, e_phoff, _, _, _, e_phentsize, e_phnum = struct.unpack_from(
        f"{endian}{offset_size}{offset_size}{offset_size}LHHH",
        fd_buffer,
        offset=ELF_EI_NIDENT + 8,
    )

    # man 5 elf suggests that Program headers can be absent.  If so,
    # e_phnum will be zero - but we assume the same for e_phentsize.
    if e_phnum == 0:
        return linking_type

    # Program headers must be at least 4 bytes for this code to do
    # anything sanely.  In practise, it must be larger than that
    # as well.  Accordingly, at best this is a corrupted ELF file.
    if e_phentsize < 4:
        return None

    fd.seek(e_phoff, os.SEEK_SET)
    unpack_format = f"{endian}L"
    try:
        for program_header_raw in _read_bytes_iteratively(fd, e_phentsize, e_phnum):
            p_type = struct.unpack_from(unpack_format, program_header_raw)[0]
            if p_type == ELF_PT_DYNAMIC:
                linking_type = ELF_LINKING_TYPE_DYNAMIC
                break
    except IncompleteFileError:
        return None

    return linking_type


def _read_bytes_iteratively(
    fd: io.BufferedReader,
    object_size: int,
    object_count: int,
) -> Iterable[bytes]:
    total_size = object_size * object_count
    bytes_remaining = total_size
    # FIXME: improve this to read larger chunks and yield them one-by-one
    byte_buffer = bytearray(object_size)

    while bytes_remaining > 0:
        n = fd.readinto(byte_buffer)
        if n != object_size:
            break
        bytes_remaining -= n
        yield byte_buffer

    if bytes_remaining:
        raise IncompleteFileError()


def find_all_elf_files(
    fs_root: VirtualPath,
    *,
    walk_filter: Optional[Callable[[VirtualPath, List[VirtualPath]], bool]] = None,
    with_linking_type: Optional[bool] = ELF_LINKING_TYPE_ANY,
) -> List[VirtualPath]:
    matches: List[VirtualPath] = []
    # FIXME: Implementation detail that fs_root is always `FSPath` and has `.walk()`
    assert isinstance(fs_root, FSPath)
    for path, children in fs_root.walk():
        if walk_filter is not None and not walk_filter(path, children):
            continue
        if not path.is_file or path.size < ELF_HEADER_SIZE32:
            continue
        if not is_so_or_exec_elf_file(path, assert_linking_type=with_linking_type):
            continue
        matches.append(path)
    return matches
