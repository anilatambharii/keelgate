"""How a public symbol is retired: warn, name the replacement, and say when it goes.

The deprecation policy (``docs/versioning.md``) promises that a deprecated symbol keeps working
for at least one minor release and that the warning names its replacement. This decorator is the
one way to do that, so every deprecation looks the same and can be found by grep.

    @deprecated(since="0.3.0", removal="0.5.0", replacement="new_name")
    def old_name(...): ...
"""

from __future__ import annotations

import functools
import warnings
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

P = ParamSpec("P")
R = TypeVar("R")


class KeelgateDeprecationWarning(DeprecationWarning):
    """Raised (as a warning) when a deprecated Keelgate symbol is used."""


def deprecation_message(name: str, *, since: str, removal: str, replacement: str) -> str:
    return (
        f"{name} is deprecated since keelgate {since} and will be removed in {removal}. "
        f"Use {replacement} instead."
    )


def deprecated(
    *, since: str, removal: str, replacement: str
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark a function or method deprecated. It still works; calling it warns."""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        message = deprecation_message(
            fn.__qualname__, since=since, removal=removal, replacement=replacement
        )

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            warnings.warn(message, KeelgateDeprecationWarning, stacklevel=2)
            return fn(*args, **kwargs)

        wrapper.__doc__ = f"**Deprecated since {since}**: use {replacement}.\n\n" + (
            fn.__doc__ or ""
        )
        return wrapper

    return decorate


def warn_deprecated(name: str, *, since: str, removal: str, replacement: str) -> None:
    """Warn about a deprecated argument or code path that is not a whole function."""
    warnings.warn(
        deprecation_message(name, since=since, removal=removal, replacement=replacement),
        KeelgateDeprecationWarning,
        stacklevel=3,
    )
