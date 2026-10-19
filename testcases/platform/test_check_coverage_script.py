"""`scripts/check_coverage.py` 的判定规则：门槛语义、排除匹配、配置错误的失败方式。

为什么这个脚本自己也要有测试：它是**门禁的门禁**。一个"配置写错了就当通过"
的检查比没有检查更坏——它会让人以为有保护。所以下面每一条用例都自己造
coverage JSON 与 pyproject，把结论钉在**造出来的输入**上，而不是拿真实产物
跑一遍看是不是绿的（那条断言在"判定逻辑被删空、永远返回 0"时同样为绿）。

脚本不是包里的模块（`scripts/` 没有 `__init__.py`），所以用 `importlib`
按路径加载。

关于 excluded 目录为什么长这样：本仓库的 per-file 门槛挂在 `-m api` 那条
带 `--cov-fail-under` 的命令上（覆盖率产物只由它生成），所以 `qaplat/`、
`ui/`、`mes_web/` 这些"别的测试层才覆盖"的目录必须排除，否则门禁会逼人
写跨层的假用例。理由写在 `pyproject.toml` 那一节里。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from qaplat.paths import ROOT

pytestmark = pytest.mark.platform

SCRIPT = ROOT / "scripts" / "check_coverage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_check_coverage_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker() -> Any:
    return _load_module()


# --------------------------------------------------------------------------- #
# 造假数据
# --------------------------------------------------------------------------- #


def _file_entry(pct: float, stmts: int, missing: list[int] | None = None) -> dict[str, Any]:
    """造一条 coverage JSON 的文件记录。

    注意这里刻意把 ``missing_lines`` 放在**文件对象顶层**（行号列表）而不是
    ``summary`` 里——``summary["missing_lines"]`` 是**数量**（int）。这两个
    同名不同义，脚本第一版就是因为混用而报
    ``TypeError: 'int' object is not iterable``。
    """
    miss = missing or []
    return {
        "summary": {
            "num_statements": stmts,
            "percent_covered": pct,
            "missing_lines": len(miss),
        },
        "missing_lines": miss,
    }


def _report(**files: dict[str, Any]) -> dict[str, Any]:
    return {"meta": {"format": 3}, "files": files, "totals": {}}


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_pyproject(path: Path, fail_under: float, min_statements: int, exclude: list[str]) -> Path:
    lines = [
        "[tool.coverage.perfile]",
        f"fail_under = {fail_under}",
        f"min_statements = {min_statements}",
        "exclude = [",
        *(f'  "{e}",' for e in exclude),
        "]",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 路径归一化：Windows 反斜杠必须能与配置里的正斜杠匹配
# --------------------------------------------------------------------------- #


class TestNormalization:
    def test_backslash_becomes_forward_slash(self, checker: Any) -> None:
        assert checker._norm("qaplat\\registry.py") == "qaplat/registry.py"

    def test_leading_dot_slash_is_stripped(self, checker: Any) -> None:
        assert checker._norm("./apis/base.py") == "apis/base.py"

    def test_plain_relative_path_unchanged(self, checker: Any) -> None:
        assert checker._norm("common/config.py") == "common/config.py"


# --------------------------------------------------------------------------- #
# 排除匹配：前缀与精确两种形式，且不能误伤
# --------------------------------------------------------------------------- #


class TestExclusion:
    def test_exact_file_excluded(self, checker: Any) -> None:
        assert checker.is_excluded("conftest.py", ["conftest.py"])

    def test_sibling_not_excluded_by_exact(self, checker: Any) -> None:
        assert not checker.is_excluded("apis/base.py", ["conftest.py"])

    def test_trailing_slash_is_prefix(self, checker: Any) -> None:
        assert checker.is_excluded("testcases/api/test_x.py", ["testcases/"])

    def test_prefix_does_not_match_substring(self, checker: Any) -> None:
        # 目录排除必须是**开头**前缀，不能是任意位置包含：
        # 否则 `testcases/` 会把 `other/testcases/a.py` 也排掉。
        assert not checker.is_excluded("other/testcases/a.py", ["testcases/"])

    def test_nested_dir_under_prefix_excluded(self, checker: Any) -> None:
        assert checker.is_excluded("qaplat/store/deep/x.py", ["qaplat/"])

    def test_empty_excludes_excludes_nothing(self, checker: Any) -> None:
        assert not checker.is_excluded("anything.py", [])


# --------------------------------------------------------------------------- #
# 缺失行号压缩
# --------------------------------------------------------------------------- #


class TestSummarizeLines:
    def test_consecutive_run_becomes_range(self, checker: Any) -> None:
        assert checker._summarize_lines([10, 11, 12]) == "10-12"

    def test_gaps_become_separate_spans(self, checker: Any) -> None:
        assert checker._summarize_lines([10, 11, 20, 30]) == "10-11, 20, 30"

    def test_empty_is_dash(self, checker: Any) -> None:
        assert checker._summarize_lines([]) == "—"

    def test_long_list_truncated_with_count(self, checker: Any) -> None:
        text = checker._summarize_lines([1, 3, 5, 7, 9, 11, 13])
        assert "…共 7 段" in text


# --------------------------------------------------------------------------- #
# 判定本身：门槛是「不低于」，小文件与排除项都不参与
# --------------------------------------------------------------------------- #


class TestCollect:
    def test_below_threshold_reported_with_lines(self, checker: Any) -> None:
        data = _report(**{"qaplat\\gate.py": _file_entry(41.6, 161, [10, 11, 12])})
        failures, considered = checker.collect(data, 55.0, 20, [])
        assert considered == 1
        assert failures[0][0] == "qaplat/gate.py"
        assert failures[0][1] == pytest.approx(41.6)
        assert failures[0][3] == "10-12"

    def test_exactly_at_threshold_passes(self, checker: Any) -> None:
        # 「不低于」不是「高于」：正好等于门槛算通过。
        data = _report(**{"apis/base.py": _file_entry(55.0, 147)})
        failures, considered = checker.collect(data, 55.0, 20, [])
        assert considered == 1
        assert failures == []

    def test_small_file_skipped(self, checker: Any) -> None:
        data = _report(**{"common/tiny.py": _file_entry(0.0, 8)})
        failures, considered = checker.collect(data, 55.0, 20, [])
        assert considered == 0
        assert failures == []

    def test_excluded_file_skipped_even_at_zero(self, checker: Any) -> None:
        data = _report(**{"mes_web\\app.py": _file_entry(0.0, 147)})
        failures, considered = checker.collect(data, 55.0, 20, ["mes_web/"])
        assert considered == 0
        assert failures == []

    def test_windows_backslash_matches_forward_slash_exclude(self, checker: Any) -> None:
        # coverage 在 Windows 上写反斜杠，配置里写正斜杠 —— 不归一化就会
        # 「看起来排除了、实际没排除」，0% 的文件照样把门禁点红。
        data = _report(**{"qaplat\\cli.py": _file_entry(0.0, 253)})
        failures, considered = checker.collect(data, 55.0, 20, ["qaplat/"])
        assert considered == 0
        assert failures == []

    def test_failures_sorted_worst_first(self, checker: Any) -> None:
        data = _report(
            **{
                "a.py": _file_entry(40.0, 100),
                "b.py": _file_entry(10.0, 100),
                "c.py": _file_entry(30.0, 100),
            }
        )
        failures, _ = checker.collect(data, 55.0, 20, [])
        assert [row[1] for row in failures] == [10.0, 30.0, 40.0]

    def test_missing_lines_absent_does_not_crash(self, checker: Any) -> None:
        # 顶层 missing_lines 缺失时摘要退化成「—」，不能抛异常。
        data = {"files": {"x.py": {"summary": {"num_statements": 50, "percent_covered": 1.0}}}}
        failures, _ = checker.collect(data, 55.0, 20, [])
        assert failures[0][3] == "—"


# --------------------------------------------------------------------------- #
# 配置错误：全部退出 1，不能静默通过
# --------------------------------------------------------------------------- #


class TestConfigErrors:
    def test_missing_table_raises(self, checker: Any, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.coverage.report]\nfail_under = 54\n", encoding="utf-8")
        with pytest.raises(checker.ConfigError, match="perfile"):
            checker.load_perfile_config(pyproject)

    def test_missing_fail_under_raises(self, checker: Any, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.coverage.perfile]\nmin_statements = 20\n", encoding="utf-8")
        with pytest.raises(checker.ConfigError, match="fail_under"):
            checker.load_perfile_config(pyproject)

    def test_missing_min_statements_raises(self, checker: Any, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.coverage.perfile]\nfail_under = 55\n", encoding="utf-8")
        with pytest.raises(checker.ConfigError, match="min_statements"):
            checker.load_perfile_config(pyproject)

    def test_non_list_exclude_raises(self, checker: Any, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.coverage.perfile]\nfail_under = 55\nmin_statements = 20\nexclude = "qaplat/"\n',
            encoding="utf-8",
        )
        with pytest.raises(checker.ConfigError, match="exclude"):
            checker.load_perfile_config(pyproject)

    def test_valid_config_read_back(self, checker: Any, tmp_path: Path) -> None:
        pyproject = _write_pyproject(tmp_path / "pyproject.toml", 55, 20, ["qaplat/"])
        fail_under, min_statements, excludes = checker.load_perfile_config(pyproject)
        assert fail_under == pytest.approx(55.0)
        assert min_statements == 20
        assert excludes == ["qaplat/"]

    def test_missing_report_file_raises(self, checker: Any, tmp_path: Path) -> None:
        with pytest.raises(checker.ConfigError, match="找不到"):
            checker.load_report(tmp_path / "nope.json")

    def test_report_without_files_key_raises(self, checker: Any, tmp_path: Path) -> None:
        path = _write_json(tmp_path / "r.json", {"totals": {}})
        with pytest.raises(checker.ConfigError, match="files"):
            checker.load_report(path)


# --------------------------------------------------------------------------- #
# exclude 路径必须真实存在：写错的排除等于没写，但它看起来写了
# --------------------------------------------------------------------------- #


class TestVerifyExcludes:
    def test_real_dir_passes(self, checker: Any) -> None:
        assert checker.verify_excludes(["qaplat/"]) == []

    def test_absent_dir_reported(self, checker: Any) -> None:
        assert checker.verify_excludes(["no_such_dir/"]) == ["no_such_dir/"]

    def test_absent_file_reported(self, checker: Any) -> None:
        assert checker.verify_excludes(["qaplat/not_a_real_file.py"]) == ["qaplat/not_a_real_file.py"]

    def test_mixed_returns_only_missing(self, checker: Any) -> None:
        result = checker.verify_excludes(["qaplat/", "nope/", "apis"])
        assert result == ["nope/", "apis"]


# --------------------------------------------------------------------------- #
# 端到端：走 main()，验退出码与是否能指名文件
# --------------------------------------------------------------------------- #


class TestEndToEnd:
    def test_pass_returns_zero(self, checker: Any, tmp_path: Path, capsys: Any) -> None:
        report = _write_json(tmp_path / "r.json", _report(**{"apis/base.py": _file_entry(85.0, 147)}))
        pyproject = _write_pyproject(tmp_path / "p.toml", 55, 20, [])
        code = checker.main(["--report", str(report), "--pyproject", str(pyproject)])
        assert code == 0
        assert "通过" in capsys.readouterr().out

    def test_failure_returns_one_and_names_file(self, checker: Any, tmp_path: Path, capsys: Any) -> None:
        report = _write_json(tmp_path / "r.json", _report(**{"mock_server\\quantity.py": _file_entry(54.0, 82, [47])}))
        pyproject = _write_pyproject(tmp_path / "p.toml", 55, 20, [])
        code = checker.main(["--report", str(report), "--pyproject", str(pyproject)])
        out = capsys.readouterr().out
        assert code == 1
        assert "mock_server/quantity.py" in out

    def test_bad_config_returns_one(self, checker: Any, tmp_path: Path, capsys: Any) -> None:
        report = _write_json(tmp_path / "r.json", _report(**{}))
        empty = tmp_path / "p.toml"
        empty.write_text("", encoding="utf-8")
        code = checker.main(["--report", str(report), "--pyproject", str(empty)])
        assert code == 1
        assert "配置错误" in capsys.readouterr().out

    def test_bad_exclude_returns_one(self, checker: Any, tmp_path: Path, capsys: Any) -> None:
        report = _write_json(tmp_path / "r.json", _report(**{}))
        pyproject = _write_pyproject(tmp_path / "p.toml", 55, 20, ["no_such_dir/"])
        code = checker.main(["--report", str(report), "--pyproject", str(pyproject)])
        assert code == 1
        assert "no_such_dir/" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 真实配置的事实快照
# --------------------------------------------------------------------------- #


class TestRealConfig:
    """当前仓库的配置必须自洽：排除路径真实存在，且判定能跑通。

    不是重复测逻辑，而是钉住「配置漂了」这件事——删掉一个目录却忘了删
    对应的 exclude 行，必须在这里红，而不是等到某次 CI 才红。
    """

    def test_excludes_all_exist(self, checker: Any) -> None:
        _, _, excludes = checker.load_perfile_config(ROOT / "pyproject.toml")
        assert checker.verify_excludes(excludes) == []

    def test_threshold_is_a_ratchet_not_a_goal(self, checker: Any) -> None:
        # 门槛必须明显低于当前最差入选文件，否则一有抖动就红、最后一定被
        # `continue-on-error` 绕过 —— 本仓的 `ruff format` 就这么走过一次。
        fail_under, _, _ = checker.load_perfile_config(ROOT / "pyproject.toml")
        assert fail_under <= 55
