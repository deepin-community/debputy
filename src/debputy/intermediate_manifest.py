import dataclasses
import json
import os
import stat
import sys
import tarfile
from enum import Enum


from typing import Optional, List, Dict, Any, Iterable, Union, Self, Mapping, IO

IntermediateManifest = List["TarMember"]


class PathType(Enum):
    FILE = ("file", tarfile.REGTYPE)
    DIRECTORY = ("directory", tarfile.DIRTYPE)
    SYMLINK = ("symlink", tarfile.SYMTYPE)
    # TODO: Add hardlink, FIFO, Char device, BLK device, etc.

    @property
    def manifest_key(self) -> str:
        return self.value[0]

    @property
    def tarinfo_type(self) -> bytes:
        return self.value[1]

    @property
    def can_be_virtual(self) -> bool:
        return self in (PathType.DIRECTORY, PathType.SYMLINK)


KEY2PATH_TYPE = {pt.manifest_key: pt for pt in PathType}


def _dirname(path: str) -> str:
    path = path.rstrip("/")
    if path == ".":
        return path
    return os.path.dirname(path)


def _fs_type_from_st_mode(fs_path: str, st_mode: int) -> PathType:
    if stat.S_ISREG(st_mode):
        path_type = PathType.FILE
    elif stat.S_ISDIR(st_mode):
        path_type = PathType.DIRECTORY
    #        elif stat.S_ISFIFO(st_result):
    #            type = FIFOTYPE
    elif stat.S_ISLNK(st_mode):
        raise ValueError(
            "Symlinks should have been rewritten to use the virtual rule."
            " Otherwise, the link would not be normalized according to Debian Policy."
        )
    #        elif stat.S_ISCHR(st_result):
    #            type = CHRTYPE
    #        elif stat.S_ISBLK(st_result):
    #            type = BLKTYPE
    else:
        raise ValueError(
            f"The path {fs_path} had an unsupported/unknown file type."
            f" Probably a bug in the tool"
        )
    return path_type


@dataclasses.dataclass(slots=True)
class TarMember:
    member_path: str
    path_type: PathType
    fs_path: Optional[str]
    mode: int
    owner: str
    uid: int
    group: str
    gid: int
    mtime: float
    link_target: str = ""
    is_virtual_entry: bool = False
    may_steal_fs_path: bool = False

    def create_tar_info(self, tar_fd: tarfile.TarFile) -> tarfile.TarInfo:
        tar_info: tarfile.TarInfo
        if self.is_virtual_entry:
            assert self.path_type.can_be_virtual
            tar_info = tar_fd.tarinfo(self.member_path)
            tar_info.size = 0
            tar_info.type = self.path_type.tarinfo_type
            tar_info.linkpath = self.link_target
        else:
            try:
                tar_info = tar_fd.gettarinfo(
                    name=self.fs_path, arcname=self.member_path
                )
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"Unable to prepare tar info for {self.member_path}"
                ) from e
            # TODO: Eventually, we should be able to unconditionally rely on link_target.  However,
            # until we got symlinks and hardlinks correctly done in the JSON generator, it will be
            # conditional for now.
            if self.link_target != "":
                tar_info.linkpath = self.link_target
        tar_info.mode = self.mode
        tar_info.uname = self.owner
        tar_info.uid = self.uid
        tar_info.gname = self.group
        tar_info.gid = self.gid
        tar_info.mode = self.mode
        tar_info.mtime = int(self.mtime)

        return tar_info

    @classmethod
    def from_file(
        cls,
        member_path: str,
        fs_path: str,
        mode: Optional[int] = None,
        owner: str = "root",
        uid: int = 0,
        group: str = "root",
        gid: int = 0,
        path_mtime: Optional[Union[float, int]] = None,
        clamp_mtime_to: Optional[int] = None,
        path_type: Optional[PathType] = None,
        may_steal_fs_path: bool = False,
    ) -> "TarMember":
        # Avoid lstat'ing if we can as it makes it easier to do tests of the code
        # (as we do not need an existing physical fs path)
        if path_type is None or path_mtime is None or mode is None:
            st_result = os.lstat(fs_path)
            st_mode = st_result.st_mode
            if mode is None:
                mode = st_mode
            if path_mtime is None:
                path_mtime = st_result.st_mtime
            if path_type is None:
                path_type = _fs_type_from_st_mode(fs_path, st_mode)

        if clamp_mtime_to is not None and path_mtime > clamp_mtime_to:
            path_mtime = clamp_mtime_to

        if may_steal_fs_path:
            assert (
                "debputy/scratch-dir/" in fs_path
            ), f"{fs_path} should not have been stealable"

        return cls(
            member_path=member_path,
            path_type=path_type,
            fs_path=fs_path,
            mode=mode,
            owner=owner,
            uid=uid,
            group=group,
            gid=gid,
            mtime=float(path_mtime),
            is_virtual_entry=False,
            may_steal_fs_path=may_steal_fs_path,
        )

    @classmethod
    def virtual_path(
        cls,
        member_path: str,
        path_type: PathType,
        mtime: float,
        mode: int,
        link_target: str = "",
        owner: str = "root",
        uid: int = 0,
        group: str = "root",
        gid: int = 0,
    ) -> Self:
        if not path_type.can_be_virtual:
            raise ValueError(f"The path type {path_type.name} cannot be virtual")
        if (path_type == PathType.SYMLINK) ^ bool(link_target):
            if not link_target:
                raise ValueError("Symlinks must have a link target")
            # TODO: Dear future programmer. Hardlinks will appear here some day and you will have to fix this
            # code then!
            raise ValueError("Non-symlinks must not have a link target")
        return cls(
            member_path=member_path,
            path_type=path_type,
            fs_path=None,
            link_target=link_target,
            mode=mode,
            owner=owner,
            uid=uid,
            group=group,
            gid=gid,
            mtime=mtime,
            is_virtual_entry=True,
        )

    def clone_and_replace(self, /, **changes: Any) -> "TarMember":
        return dataclasses.replace(self, **changes)

    def to_manifest(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        try:
            d["mode"] = oct(self.mode)
        except (TypeError, ValueError) as e:
            raise TypeError(f"Bad mode in TarMember {self.member_path}") from e
        d["path_type"] = self.path_type.manifest_key
        # "compress" the output by removing redundant fields
        if self.link_target is None or self.link_target == "":
            del d["link_target"]
        if self.is_virtual_entry:
            assert self.fs_path is None
            del d["fs_path"]
        else:
            del d["is_virtual_entry"]
        return d

    @classmethod
    def parse_intermediate_manifest(cls, manifest_path: str) -> IntermediateManifest:
        directories = {"."}
        if manifest_path == "-":
            with sys.stdin as fd:
                data = json.load(fd)
                contents = [TarMember.from_dict(m) for m in data]
        else:
            with open(manifest_path) as fd:
                data = json.load(fd)
                contents = [TarMember.from_dict(m) for m in data]
        if not contents:
            raise ValueError(
                "Empty manifest (note that the root directory should always be present"
            )
        if contents[0].member_path != "./":
            raise ValueError('The first member must always be the root directory "./"')
        for tar_member in contents:
            directory = _dirname(tar_member.member_path)
            if directory not in directories:
                raise ValueError(
                    f'The path "{tar_member.member_path}" came before the directory it is in (or the path'
                    f" is not a directory). Either way leads to a broken deb."
                )
            if tar_member.path_type == PathType.DIRECTORY:
                directories.add(tar_member.member_path.rstrip("/"))
        return contents

    @classmethod
    def from_dict(cls, d: Any) -> "TarMember":
        member_path = d["member_path"]
        raw_mode = d["mode"]
        if not raw_mode.startswith("0o"):
            raise ValueError(f"Bad mode for {member_path}")
        is_virtual_entry = d.get("is_virtual_entry") or False
        path_type = KEY2PATH_TYPE[d["path_type"]]
        fs_path = d.get("fs_path")
        mode = int(raw_mode[2:], 8)
        if is_virtual_entry:
            if not path_type.can_be_virtual:
                raise ValueError(
                    f"Bad file type or is_virtual_entry for {d['member_path']}."
                    " The file type cannot be virtual"
                )
            if fs_path is not None:
                raise ValueError(
                    f'Invalid declaration for "{member_path}".'
                    " The path is listed as a virtual entry but has a file system path"
                )
        elif fs_path is None:
            raise ValueError(
                f'Invalid declaration for "{member_path}".'
                " The path is neither a virtual path nor does it have a file system path!"
            )
        if path_type == PathType.DIRECTORY and not member_path.endswith("/"):
            raise ValueError(
                f'Invalid declaration for "{member_path}".'
                " The path is listed as a directory but does not end with a slash"
            )

        link_target = d.get("link_target")
        if path_type == PathType.SYMLINK:
            if mode != 0o777:
                raise ValueError(
                    f'Invalid declaration for "{member_path}".'
                    f" Symlinks must have mode 0o0777, got {oct(mode)[2:]}."
                )
            if not link_target:
                raise ValueError(
                    f'Invalid declaration for "{member_path}".'
                    " Symlinks must have a link_target"
                )
        elif link_target is not None and link_target != "":
            # TODO: Eventually hardlinks should have them too.  But that is a problem for a future programmer
            raise ValueError(
                f'Invalid declaration for "{member_path}".'
                " Only symlinks can have a link_target"
            )
        else:
            link_target = ""
        may_steal_fs_path = d.get("may_steal_fs_path") or False

        if may_steal_fs_path:
            assert (
                "debputy/scratch-dir/" in fs_path
            ), f"{fs_path} should not have been stealable"
        return cls(
            member_path=member_path,
            path_type=path_type,
            fs_path=fs_path,
            mode=mode,
            owner=d["owner"],
            uid=d["uid"],
            group=d["group"],
            gid=d["gid"],
            mtime=float(d["mtime"]),
            link_target=link_target,
            is_virtual_entry=is_virtual_entry,
            may_steal_fs_path=may_steal_fs_path,
        )


def output_intermediate_manifest(
    manifest_output_file: str,
    members: Iterable[TarMember],
) -> None:
    with open(manifest_output_file, "w") as fd:
        output_intermediate_manifest_to_fd(fd, members)


def output_intermediate_manifest_to_fd(
    fd: IO[str], members: Iterable[TarMember]
) -> None:
    serial_format = [m.to_manifest() for m in members]
    json.dump(serial_format, fd)
