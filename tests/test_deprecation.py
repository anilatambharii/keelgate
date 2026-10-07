"""The deprecation helper: how a public symbol is retired (docs/versioning.md)."""

from __future__ import annotations

import warnings

import pytest

from keelgate._internal.deprecation import (
    KeelgateDeprecationWarning,
    deprecated,
    deprecation_message,
    warn_deprecated,
)


@deprecated(since="0.3.0", removal="0.5.0", replacement="new_name")
def old_name(x: int) -> int:
    """Double a number."""
    return x * 2


def test_a_deprecated_function_still_works_and_warns_with_the_replacement_and_removal() -> None:
    with pytest.warns(KeelgateDeprecationWarning) as caught:
        assert old_name(21) == 42
    message = str(caught[0].message)
    assert "old_name is deprecated since keelgate 0.3.0" in message
    assert "removed in 0.5.0" in message and "Use new_name instead" in message


def test_the_warning_points_at_the_caller_not_at_keelgate() -> None:
    with pytest.warns(KeelgateDeprecationWarning) as caught:
        old_name(1)
    assert caught[0].filename == __file__


def test_the_docstring_says_so() -> None:
    assert old_name.__doc__ is not None
    assert old_name.__doc__.startswith("**Deprecated since 0.3.0**: use new_name.")
    assert "Double a number." in old_name.__doc__
    assert old_name.__name__ == "old_name"  # functools.wraps


def test_it_is_a_deprecation_warning_so_standard_filters_apply() -> None:
    assert issubclass(KeelgateDeprecationWarning, DeprecationWarning)
    with warnings.catch_warnings():
        warnings.simplefilter("error", KeelgateDeprecationWarning)
        with pytest.raises(KeelgateDeprecationWarning):
            old_name(1)


def test_a_deprecated_argument_can_be_flagged_without_wrapping_the_function() -> None:
    def f(legacy: int | None = None) -> None:
        if legacy is not None:
            warn_deprecated("f(legacy=)", since="0.3.0", removal="0.4.0", replacement="f(modern=)")

    with pytest.warns(KeelgateDeprecationWarning, match=r"f\(legacy=\) is deprecated"):
        f(legacy=1)


def test_the_message_format_is_stable() -> None:
    assert deprecation_message("a.b", since="1", removal="2", replacement="c") == (
        "a.b is deprecated since keelgate 1 and will be removed in 2. Use c instead."
    )
