"""采集插件 ``qaplat/_dump.py`` 的分类判据。

为什么这些用例必须存在：``_dump.py`` 是 **error 预算唯一的数据来源**。门禁判的是
``run.errors > spec.max_errors``，而 ``run.errors`` 完全由这个插件把 pytest 的
setup/call/teardown 三个阶段压成一条结论得来。把 ``setup`` 失败错记成 ``failed``，
error 预算就永远不触发——上一轮刚修掉的「没跑成当通过」会原样回来，且**门禁一声不响**。

补这些用例之前的实测：把 ``qaplat/_dump.py:82`` 的 ``"error"`` 改成 ``"failed"``、
把 ``:90`` 的 teardown 分支整条删掉、把 ``:89`` 的 ``wasxfail`` 分支压成 ``skipped``，
这些变异下 154 条平台测试**全部通过、零失败**。覆盖率显示 84% 是误导性的——
没被覆盖的恰恰就是这几个分类分支（``_dump.py:82, 84, 87, 89, 91, 93`` 与
``gate.py:165-179`` 的兜底解析）。所以这里不测「跑过没有」，只测「分类对不对」。

关于 ``gate.py:169`` 那个归一化，有个**反直觉的事实要记下来**：``_SUMMARY_COUNT_RE``
的交替顺序是 ``error|errors``，正则的最左优先让 ``6 errors`` 里的 ``error`` 先被吃掉，
于是 ``name`` 本来就是 ``"error"``——归一化里带 ``"errors"`` 的那个分支是**死代码**。
把那个分支删掉是**等价变异**（实测行为一字不变），不能拿它当「测试能抓到」的证据。
真正承重的是输出 key 必须叫 ``error``（与 ``_counts_from_cases`` 对齐），
所以下面的变异实验改的是那个 key，不是那个分支。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qaplat import _dump, gate

pytestmark = pytest.mark.platform


#: 替身 nodeid。真钩子会同时写入「当前正在跑的这条用例」，所以判定只认这一个 key。
_FAKE_NODEID = "testcases/x.py::test_a"


class _Report:
    """够用的 pytest report 替身：只带 ``_dump.py`` 真正读的那几个属性。"""

    def __init__(
        self,
        *,
        when: str,
        nodeid: str = _FAKE_NODEID,
        failed: bool = False,
        skipped: bool = False,
        duration: float = 0.0,
        longreprtext: str = "",
        wasxfail: str | None = None,
    ) -> None:
        self.nodeid = nodeid
        self.when = when
        self.failed = failed
        self.skipped = skipped
        self.duration = duration
        self.longreprtext = longreprtext
        # ``xfailed`` 的判据是「有没有这个属性」，所以只有需要时才 setattr。
        if wasxfail is not None:
            self.wasxfail = wasxfail


@pytest.fixture(autouse=True)
def _clean_state(request: pytest.FixtureRequest) -> Any:
    """每个用例前后都把插件的全局状态还原，并且**不吞掉本用例自己的失败**。

    这里有三件事必须同时做到，少一件就会造出一条假绿通道：

    1. **必须还原，不能只清空。** ``qaplat gate`` 用 ``-p qaplat._dump`` 真实加载这个
       插件，本文件的每一条用例都会经过真钩子。只清空不还原时，``_finish()`` 把
       ``_STATE["dump"]`` 指向 tmp_path 之后就永久丢了外层那次门禁的落盘路径——实测
       ``pr.json`` 根本不生成，门禁退化成汇总行兜底。
    2. **还原时要留下本用例那条非通过记录。** 本 fixture 的 finalizer 在 teardown 的
       logreport **之前**运行，还原会连「当前这条用例刚刚被判失败」的那条记录一起清掉；
       随后真钩子处理 teardown 报告时用 ``setdefault`` 重新建一条默认 ``passed`` 的
       entry（``_dump.py`` 的 teardown 分支只在「已是 passed 且 teardown 也失败」时改
       ``error``，teardown 正常就什么都不做），于是 call 阶段的 ``failed`` 被永久覆盖。
       实测探针：某条用例被强制判失败后 pytest 侧 ``1 failed``、退出码 1，而落盘 dump 里
       它记成 ``passed``、门禁 ``EXIT=0``「门禁达标」。**守卫用例自己坏了却给绿灯，
       比原本那条「全红」更糟。**
    3. **只留非通过的**，通过的记录照旧丢弃。留着通过的记录会让 ``test_sessionfinish_*``
       里断言「落盘 cases 只有 1 条」这类用例数错。
    """
    nodeid = request.node.nodeid
    saved = {key: (value.copy() if isinstance(value, dict) else value) for key, value in _dump._STATE.items()}
    _dump._STATE.update({"dump": None, "records": {}, "markers": {}, "files": {}, "covers": {}, "renamed": {}})
    yield
    # 走到这里时真钩子已经处理完 setup 与 call 两个报告，records 里就是本用例的真实结论。
    live = _dump._STATE["records"].get(nodeid)
    own = live if isinstance(live, dict) and live.get("outcome") != "passed" else None
    _dump._STATE.clear()
    _dump._STATE.update(saved)
    if own is not None:
        _dump._STATE["records"][nodeid] = own


def _outcome_of(*reports: _Report) -> str:
    """把三个阶段交给真钩子，取回替身 nodeid 的结论。

    断言**不数** ``len(records)``：在 ``-p qaplat._dump`` 下，真钩子会把当前这条
    测试自己也写进 ``_STATE``，条数恒为 2——按条数断言会让本文件在被门禁加载时全红
    （上一轮就是这么红的），而它要证明的事跟「有几条记录」无关。
    """
    for report in reports:
        _dump.pytest_runtest_logreport(report)
    records = _dump._STATE["records"]
    assert _FAKE_NODEID in records, "替身 nodeid 没有产生记录"
    return str(records[_FAKE_NODEID]["outcome"])


# ------------------------------------------------------------------ 四条分类分支


def test_setup_failure_is_error_not_failed() -> None:
    """setup 炸了 = 没跑成，不是被测失败。

    这条错记成 ``failed`` 的后果最严重：error 预算（``max_errors``）永远数不到它，
    而 ``decisive = passed + failed`` 反而把这批算进通过率分母，两个方向一起把假绿做实。
    """
    assert _outcome_of(_Report(when="setup", failed=True), _Report(when="teardown")) == "error"


def test_call_failure_is_failed() -> None:
    """正常跑完但断言失败 → ``failed``，与 setup 失败严格区分。"""
    assert _outcome_of(_Report(when="setup"), _Report(when="call", failed=True), _Report(when="teardown")) == "failed"


def test_teardown_failure_is_error_when_nothing_failed_before() -> None:
    """用例本体通过、收尾炸了 → ``error``。不记的话它会以 ``passed`` 收场。"""
    assert _outcome_of(_Report(when="setup"), _Report(when="call"), _Report(when="teardown", failed=True)) == "error"


def test_teardown_failure_does_not_overwrite_an_earlier_failure() -> None:
    """前面已经 failed，收尾再炸也只记 ``failed``。

    若这里被改成无条件覆盖，``failed`` 会被 teardown 的噪声冲成 ``error``，
    于是「被测真的坏了」从通过率里消失、只剩一个「环境问题」——归因被悄悄调包。
    """
    assert (
        _outcome_of(_Report(when="setup"), _Report(when="call", failed=True), _Report(when="teardown", failed=True))
        == "failed"
    )


def test_setup_skip_is_skipped() -> None:
    assert _outcome_of(_Report(when="setup", skipped=True), _Report(when="teardown")) == "skipped"


def test_call_skip_without_wasxfail_is_skipped() -> None:
    """运行期 ``pytest.skip()`` 与 ``xfail`` 都报告为 skipped，必须靠 ``wasxfail`` 分开。"""
    assert _outcome_of(_Report(when="setup"), _Report(when="call", skipped=True), _Report(when="teardown")) == "skipped"


def test_call_skip_with_wasxfail_is_xfailed() -> None:
    """``xfail`` 被独立计成 ``xfailed``：它不是 ``skipped``，也绝不进 ``passed``。"""
    report = _Report(when="call", skipped=True, wasxfail="已知缺陷 DEF-01")
    assert _outcome_of(_Report(when="setup"), report, _Report(when="teardown")) == "xfailed"


def test_all_three_phases_clean_is_passed() -> None:
    assert _outcome_of(_Report(when="setup"), _Report(when="call"), _Report(when="teardown")) == "passed"


# ------------------------------------------------------------------ 附带信息


def test_durations_of_all_phases_are_summed() -> None:
    """三个阶段的耗时累加到同一条记录上；只取 call 会低估 setup 很重的用例。"""
    _outcome_of(
        _Report(when="setup", duration=0.25),
        _Report(when="call", duration=1.5),
        _Report(when="teardown", duration=0.75),
    )
    assert _dump._STATE["records"][_FAKE_NODEID]["duration_seconds"] == pytest.approx(2.5)


def test_message_keeps_first_failure_and_is_capped() -> None:
    """``message`` 是 ``qaplat.classify`` 唯一的证据来源，必须留下且只留第一次。

    截断到 2000 字符是刻意的：一个巨大的 traceback 会让 dump 文件膨胀到没法用眼睛看，
    而分类真正需要的是**第一段**报错。
    """
    long_text = "E   AssertionError: 实际状态码: 502\n" + "x" * 5000
    _outcome_of(
        _Report(when="setup", failed=True, longreprtext=long_text),
        _Report(when="teardown", failed=True, longreprtext="E   later noise"),
    )
    message = str(_dump._STATE["records"][_FAKE_NODEID]["message"])
    assert message.startswith("E   AssertionError")
    assert len(message) == 2000, "必须截断到 2000 字符：不截断会让 dump 大到没法看"


def test_passing_case_keeps_empty_message() -> None:
    _outcome_of(_Report(when="setup"), _Report(when="call"), _Report(when="teardown"))
    assert _dump._STATE["records"][_FAKE_NODEID]["message"] == ""


# ------------------------------------------------------------------ 落盘与跳过


def _finish(tmp_path: Path, *, worker: bool = False, set_dump: bool = True) -> Path | None:
    target = tmp_path / "out" / "dump.json"
    _dump._STATE["dump"] = str(target) if set_dump else None

    class _Config:
        pass

    config = _Config()
    if worker:
        config.workerinput = {}  # type: ignore[attr-defined]

    class _Session:
        pass

    session = _Session()
    session.config = config  # type: ignore[attr-defined]
    _dump.pytest_sessionfinish(session, 0)
    return target if target.exists() else None


def test_sessionfinish_writes_cases_markers_files_covers(tmp_path: Path) -> None:
    """四样东西缺一不可：只有 cases 的话 registry 读不到 marker 与源码路径。"""
    _dump._STATE["markers"] = {"testcases/x.py::test_a": ["platform"]}
    _dump._STATE["files"] = {"testcases/x.py::test_a": "testcases/x.py"}
    _dump._STATE["covers"] = {"testcases/x.py::test_a": ["healthz_api_v1_healthz_get"]}
    _outcome_of(_Report(when="setup"), _Report(when="call"))
    written = _finish(tmp_path)
    assert written is not None

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["exitstatus"] == 0
    assert payload["cases"][0]["outcome"] == "passed"
    assert payload["markers"]["testcases/x.py::test_a"] == ["platform"]
    assert payload["files"]["testcases/x.py::test_a"] == "testcases/x.py"
    assert payload["covers"]["testcases/x.py::test_a"] == ["healthz_api_v1_healthz_get"]


def test_sessionfinish_does_nothing_without_dump_option(tmp_path: Path) -> None:
    """没传 ``--qaplat-dump`` 就不写文件：普通 ``pytest`` 运行不该被这个插件留下垃圾。"""
    assert _finish(tmp_path, set_dump=False) is None


def test_sessionfinish_in_worker_writes_nothing(tmp_path: Path) -> None:
    """``-n`` 下的 worker 不落盘，避免和 controller 抢同一个路径。"""
    assert _finish(tmp_path, worker=True) is None


# ------------------------------------------------------------------ 跨盘 nodeid 补前缀


class _Item:
    """够用的 collection item 替身：只带 ``nodeid`` 与 ``path``。"""

    def __init__(self, nodeid: str, path: object) -> None:
        self.nodeid = nodeid
        self.path = path
        self._markers: list = []

    def iter_markers(self, name: str | None = None) -> list:
        return [m for m in self._markers if name is None or m.name == name]


class _Marker:
    def __init__(self, name: str, args: tuple = ()) -> None:
        self.name = name
        self.args = args


def test_qualified_nodeid_leaves_normal_nodeid_alone() -> None:
    """正常相对路径必须原样返回——这是绝大多数收集的情形，改它就是改全部报表。"""
    item = _Item("testcases/mes/test_a.py::test_x", Path("testcases/mes/test_a.py"))
    assert _dump._qualified_nodeid(item) == "testcases/mes/test_a.py::test_x"


def test_qualified_nodeid_repairs_a_cross_drive_empty_prefix() -> None:
    """跨盘时 pytest 算出 ``::test_x``，前缀由 ``item.path`` 补回绝对路径。"""
    item = _Item("::test_probe_a", Path(r"C:\Users\lefan\AppData\Local\Temp\p\probe.py"))
    assert _dump._qualified_nodeid(item) == r"C:\Users\lefan\AppData\Local\Temp\p\probe.py::test_probe_a"


def test_qualified_nodeid_without_path_stays_broken() -> None:
    """没有 ``path`` 时不要硬造：返回原值，让下游按空来源处理，而不是编一个假路径。"""
    assert _dump._qualified_nodeid(_Item("::test_probe_a", None)) == "::test_probe_a"


def test_collection_bookkeeping_is_keyed_by_the_repaired_nodeid() -> None:
    """补完前缀后，markers/files/covers 三张表必须挂在**同一个新 key** 上。

    这是修复的关键：只改 nodeid 不改 key，``registry`` 拿到的还是 ``::name``，
    读源码那一步照样落空——表面上「修了」，报表上一分未动。
    """
    path = Path(r"C:\Users\lefan\AppData\Local\Temp\p\probe.py")
    item = _Item("::test_probe_a", path)
    item._markers = [_Marker("covers", ("healthz_api_v1_healthz_get",))]
    _dump.pytest_collection_modifyitems(None, None, [item])

    expected = rf"{path}::test_probe_a"
    assert set(_dump._STATE["markers"]) == {expected}
    assert _dump._STATE["files"][expected] == str(path)
    assert _dump._STATE["covers"][expected] == ["healthz_api_v1_healthz_get"]
    # 留痕：跨盘补前缀不是静默改数据，dump 里能查到它被改过。
    assert _dump._STATE["renamed"] == {"::test_probe_a": expected}


# ------------------------------------------------------------------ 兜底解析（dump 缺位时）


def test_parse_summary_line_normalises_errors_and_drops_warnings() -> None:
    """dump 缺位时的兜底：输出 key 必须是 ``error``，与 ``_counts_from_cases`` 对齐。

    兜底只在采集插件坏掉那天生效，平时看不出来；一旦它的 key 与正常路径不一致
    （例如输出 ``errors``），改 ``max_errors`` 的 error 预算就永远数到 0——
    一个专门在该兜住的那天失效的假绿通道。
    """
    counts = gate.parse_summary_line("30 passed, 2 failed, 6 errors, 3 warnings, 4 skipped in 24.29s")
    assert counts["error"] == 6
    assert "errors" not in counts
    assert "warnings" not in counts, "warnings 不是结论，不该混进计数"
    assert counts["passed"] == 30
    assert counts["failed"] == 2
    assert counts["skipped"] == 4


def test_parse_summary_seconds_reads_the_tail() -> None:
    assert gate.parse_summary_seconds("30 passed in 24.29s") == pytest.approx(24.29)
    assert gate.parse_summary_seconds("no summary here") is None


# ------------------------------------------------------------------ 端到端：真子进程


def test_real_run_reports_setup_and_teardown_failures_as_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真起一次 pytest，确认「假 report 单测」描述的语义与 pytest 实际报告一致。

    前面的单测手搓 report 对象，验证的是本文件对契约的理解；这条验证的是那个理解
    没有跑偏——pytest 在 setup 失败与 teardown 失败时确实以 ``when``/``failed`` 报出来。
    """
    target = tmp_path / "probe_dump_e2e.py"
    target.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def setup_boom():\n"
        "    raise RuntimeError('setup 炸了')\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def teardown_boom():\n"
        "    yield\n"
        "    raise RuntimeError('teardown 炸了')\n"
        "\n"
        "\n"
        "def test_ok():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_setup_error(setup_boom):\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_teardown_error(teardown_boom):\n"
        "    assert True\n",
        encoding="utf-8",
    )

    # dump 落点由 ``RAW_DUMP_DIR`` 与层名共同决定（``<layer>.json``），
    # ``run_layer`` 没有开放 override 参数；monkeypatch 前者既能把产物写进 tmp_path，
    # 又保持 ``run_layer`` 本体一个字不改。
    monkeypatch.setattr(gate, "RAW_DUMP_DIR", tmp_path / "raw")
    dump_path = tmp_path / "raw" / "smoke.json"
    spec = gate.LayerSpec(
        layer=gate.Layer.SMOKE,
        marker_expr="",
        timeout_seconds=120,
        min_pass_rate=1.0,
        max_duration_seconds=120,
        max_errors=0,
    )
    run = gate.run_layer(
        spec,
        [str(target), "-o", "addopts=", "-q", "-p", "no:cacheprovider"],
    )
    assert dump_path.exists(), "子进程没写出 dump，下游读到的计数会退化成兜底解析"
    by_name = {nodeid.split("::")[-1]: outcome for nodeid, outcome, _duration in run.cases}
    assert by_name["test_ok"] == "passed"
    assert by_name["test_setup_error"] == "error", "setup 失败必须是 error，不能落成 failed"
    assert by_name["test_teardown_error"] == "error", "teardown 失败必须是 error，不能落成 passed"

    # 门禁真的会被这两条 error 拦住（max_errors=0 的 smoke 层）
    assert run.counts["error"] == 2
    assert run.exit_code != 0
