"""Capability strings.

A capability is ``<resource>:<action>`` in lower snake case, for example
``market_data:read`` or ``trade:paper_execute``. There are deliberately no
wildcards: AGENTS.md requires explicit grants only, and a ``trade:*`` grant is
precisely the kind of thing that turns a later-added capability into silent
privilege.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from pydantic_core import core_schema

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler

_CAPABILITY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}:[a-z][a-z0-9_]{0,63}$")


class InvalidCapabilityError(ValueError):
    """Raised for a malformed capability string, including any wildcard."""


class Capability(str):
    """A validated capability string. Immutable, hashable, and still a ``str``."""

    __slots__ = ()

    def __new__(cls, value: object) -> Capability:
        if not isinstance(value, str) or not _CAPABILITY_RE.fullmatch(value):
            raise InvalidCapabilityError(
                f"invalid capability {value!r}: expected '<resource>:<action>' in "
                "lower snake case, with no wildcards"
            )
        return super().__new__(cls, value)

    @property
    def resource(self) -> str:
        return self.partition(":")[0]

    @property
    def action(self) -> str:
        return self.partition(":")[2]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True)
        )


# The capabilities the finance_basic pack understands. Nothing here is special to
# the grant machinery; these are conveniences, not an allow-list.
MARKET_DATA_READ: Final = Capability("market_data:read")
REPORT_WRITE: Final = Capability("report:write")
TRADE_PROPOSE: Final = Capability("trade:propose")
TRADE_PAPER_EXECUTE: Final = Capability("trade:paper_execute")
