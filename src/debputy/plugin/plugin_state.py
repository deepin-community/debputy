import contextvars
import functools
import inspect
from contextvars import ContextVar
from typing import Optional, Callable, ParamSpec, TypeVar, NoReturn, Union

from debputy.exceptions import (
    UnhandledOrUnexpectedErrorFromPluginError,
    DebputyRuntimeError,
)
from debputy.util import _debug_log, _is_debug_log_enabled

_current_debputy_plugin_cxt_var: ContextVar[Optional[str]] = ContextVar(
    "current_debputy_plugin",
    default=None,
)

P = ParamSpec("P")
R = TypeVar("R")


def current_debputy_plugin_if_present() -> Optional[str]:
    return _current_debputy_plugin_cxt_var.get()


def current_debputy_plugin_required() -> str:
    v = current_debputy_plugin_if_present()
    if v is None:
        raise AssertionError(
            "current_debputy_plugin_required() was called, but no plugin was set."
        )
    return v


def wrap_plugin_code(
    plugin_name: str,
    func: Callable[P, R],
    *,
    non_debputy_exception_handling: Union[bool, Callable[[Exception], NoReturn]] = True,
) -> Callable[P, R]:
    if isinstance(non_debputy_exception_handling, bool):

        runner = run_in_context_of_plugin
        if non_debputy_exception_handling:
            runner = run_in_context_of_plugin_wrap_errors

        def _wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
            return runner(plugin_name, func, *args, **kwargs)

        functools.update_wrapper(_wrapper, func)
        return _wrapper

    def _wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            return run_in_context_of_plugin(plugin_name, func, *args, **kwargs)
        except DebputyRuntimeError:
            raise
        except Exception as e:
            non_debputy_exception_handling(e)

    functools.update_wrapper(_wrapper, func)
    return _wrapper


def run_in_context_of_plugin(
    plugin: str,
    func: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    context = contextvars.copy_context()
    if _is_debug_log_enabled():
        call_stack = inspect.stack()
        caller: str = "[N/A]"
        for frame in call_stack:
            if frame.filename != __file__:
                try:
                    fname = frame.frame.f_code.co_qualname
                except AttributeError:
                    fname = None
                if fname is None:
                    fname = frame.function
                caller = f"{frame.filename}:{frame.lineno} ({fname})"
                break
        # Do not keep the reference longer than necessary
        del call_stack
        _debug_log(
            f"Switching plugin context to {plugin} at {caller} (from context: {current_debputy_plugin_if_present()})"
        )
    # Wish we could just do a regular set without wrapping it in `context.run`
    context.run(_current_debputy_plugin_cxt_var.set, plugin)
    return context.run(func, *args, **kwargs)


def run_in_context_of_plugin_wrap_errors(
    plugin: str,
    func: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    try:
        return run_in_context_of_plugin(plugin, func, *args, **kwargs)
    except DebputyRuntimeError:
        raise
    except Exception as e:
        if plugin != "debputy":
            raise UnhandledOrUnexpectedErrorFromPluginError(
                f"{func.__qualname__} from the plugin {plugin} raised exception that was not expected here."
            ) from e
        else:
            raise AssertionError(
                "Bug in the `debputy` plugin: Unhandled exception."
            ) from e
