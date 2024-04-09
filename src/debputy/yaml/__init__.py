from .compat import YAML, YAMLError, MarkedYAMLError

MANIFEST_YAML = YAML()

__all__ = [
    "MANIFEST_YAML",
    "YAMLError",
    "MarkedYAMLError",
]
