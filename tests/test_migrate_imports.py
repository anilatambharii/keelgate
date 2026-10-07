"""The 0.1 -> 0.2 import migration tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from keelgate import cli
from keelgate._internal.migrate import legacy_modules, migrate_paths, migrate_source


def test_the_legacy_map_is_derived_from_the_installed_package() -> None:
    legacy = legacy_modules()
    assert legacy["keelgate.llm.types"] == "keelgate.llm"
    assert legacy["keelgate.tools.gateway"] == "keelgate.tools"
    assert legacy["keelgate.adapters.governed"] == "keelgate.adapters"
    assert legacy["keelgate.adapters.mcp.server"] == "keelgate.adapters.mcp"
    assert len(legacy) >= 60
    # modules that stayed public are not "legacy"
    assert "keelgate.policy.cedar" not in legacy and "keelgate.cli" not in legacy


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("from keelgate.llm.types import LLMRequest\n", "from keelgate.llm import LLMRequest\n"),
        (
            "from keelgate.llm.types import LLMRequest as R, Message\n",
            "from keelgate.llm import LLMRequest as R, Message\n",
        ),
        (
            "from keelgate.tools.gateway import (\n    ToolGateway,\n    CallContext,\n)\n",
            "from keelgate.tools import (\n    ToolGateway,\n    CallContext,\n)\n",
        ),
        (
            "from keelgate.adapters.governed import GovernedToolset\n",
            "from keelgate.adapters import GovernedToolset\n",
        ),
        ("from keelgate.policy.engine import deny\n", "from keelgate.policy import deny\n"),
    ],
)
def test_deep_imports_of_public_names_move_to_the_package_root(old: str, new: str) -> None:
    result = migrate_source(old)
    assert result.new_text == new and result.changed == 1 and result.manual == []


def test_public_imports_are_left_alone_and_the_tool_is_idempotent() -> None:
    text = "from keelgate.llm import LLMRequest\nfrom keelgate.policy.cedar import CedarEngine\nimport os\n"
    assert migrate_source(text).changed == 0
    once = migrate_source("from keelgate.llm.types import LLMRequest\n")
    assert migrate_source(once.new_text).changed == 0


def test_a_name_that_is_not_public_is_reported_never_guessed() -> None:
    result = migrate_source("from keelgate.tools.gateway import ToolGateway, _State\n", "f.py")
    assert result.changed == 0 and result.new_text.startswith("from keelgate.tools.gateway")
    assert (
        len(result.manual) == 1
        and "_State" in result.manual[0]
        and "not public API" in result.manual[0]
    )


def test_a_plain_import_of_a_legacy_module_needs_a_human() -> None:
    result = migrate_source("import keelgate.llm.types\n", "g.py")
    assert result.changed == 0 and "needs a manual change" in result.manual[0]


def test_unparseable_files_are_skipped_not_crashed_on() -> None:
    result = migrate_source("def broken(:\n", "bad.py")
    assert result.changed == 0 and "not valid Python" in result.manual[0]


def test_paths_are_migrated_in_place_only_with_write(tmp_path: Path) -> None:
    file = tmp_path / "pkg" / "mod.py"
    file.parent.mkdir()
    original = "from keelgate.llm.types import LLMRequest\n"
    file.write_text(original, encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text(original, encoding="utf-8")

    _, report = migrate_paths([tmp_path], write=False)
    assert file.read_text(encoding="utf-8") == original and "would rewrite 1 import(s)" in report
    assert "-from keelgate.llm.types import LLMRequest" in report

    _, report = migrate_paths([tmp_path], write=True)
    assert file.read_text(encoding="utf-8") == "from keelgate.llm import LLMRequest\n"
    assert (tmp_path / ".venv" / "skip.py").read_text(encoding="utf-8") == original  # untouched


def test_the_cli_reports_changes_manual_work_and_missing_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("from keelgate.llm.types import LLMRequest\n", encoding="utf-8")
    assert cli.main(["migrate-imports", str(ok)]) == 0
    assert "would rewrite 1" in capsys.readouterr().out
    assert cli.main(["migrate-imports", str(ok), "--write"]) == 0
    assert "from keelgate.llm import LLMRequest" in ok.read_text(encoding="utf-8")

    bad = tmp_path / "bad.py"
    bad.write_text("from keelgate.tools.gateway import _State\n", encoding="utf-8")
    assert cli.main(["migrate-imports", str(bad)]) == 1  # manual work remains
    assert "MANUAL" in capsys.readouterr().out
    assert cli.main(["migrate-imports", str(tmp_path / "nope")]) == 2
