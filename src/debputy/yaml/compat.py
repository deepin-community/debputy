__all__ = [
    "YAML",
    "YAMLError",
    "MarkedYAMLError",
    "Node",
    "LineCol",
    "CommentedBase",
    "CommentedMap",
    "CommentedSeq",
]

try:
    from ruyaml import YAML, Node
    from ruyaml.comments import LineCol, CommentedBase, CommentedMap, CommentedSeq
    from ruyaml.error import YAMLError, MarkedYAMLError
except (ImportError, ModuleNotFoundError):
    from ruamel.yaml import YAML, Node  # type: ignore
    from ruamel.yaml.comments import LineCol, CommentedBase, CommentedMap, CommentedSeq  # type: ignore
    from ruamel.yaml.error import YAMLError, MarkedYAMLError  # type: ignore
