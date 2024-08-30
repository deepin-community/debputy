"""Wrapper module around `lsprotocol.types`

This wrapper module is here to facility making `lsprotocol` optional
for -backports. When available, it is a (mostly) transparent wrapper
for the real module. When missing, it returns a placeholder object
for anything for the purpose of making things simpler.
"""

from typing import TYPE_CHECKING, Any, Iterator, List

if TYPE_CHECKING:
    from lsprotocol import types

    # To defeat "unused" detections that might attempt to
    # optimize out the import
    assert types is not None
    __all__ = dir(types)

else:
    try:
        from lsprotocol import types

        __all__ = dir(types)

    except ImportError:

        stub_attr = {
            "__name__": __name__,
            "__file__": __file__,
            "__doc__": __doc__,
        }
        bad_attr = frozenset(
            [
                "pytestmark",
                "pytest_plugins",
            ]
        )

        class StubModule:
            @staticmethod
            def __getattr__(item: Any) -> Any:
                if item in stub_attr:
                    return stub_attr[item]
                if item in bad_attr:
                    raise AttributeError(item)
                return types

            def __call__(self, *args, **kwargs) -> Any:
                return self

            def __iter__(self) -> Iterator[Any]:
                return iter(())

        types = StubModule()
        __all__ = []


def __dir__() -> List[str]:
    return dir(types)


def __getattr__(name: str) -> Any:
    return getattr(types, name)
