import dataclasses

from debputy.manifest_parser.base_types import FileSystemMode


@dataclasses.dataclass(slots=True)
class DebputyCapability:
    capabilities: str
    capability_mode: FileSystemMode
    definition_source: str
