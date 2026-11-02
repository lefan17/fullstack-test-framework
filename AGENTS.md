# AGENTS.md · fullstack-test-framework

## 这是什么

一套 **接口 / UI / 契约 / 业务全链路自动化测试框架**，外加一层自研的平台层（`qaplat/`）：
不是"写了一堆用例"，而是把**用例 → 门禁 → 度量 → 报表**做成闭环。

- 被测服务**也在这个仓里**：`mock_server/`（接口）、`login_app/`（登录服务，含 6 个已知缺陷）、
  `mes_web/`（MES 业务页）。这不是偷懒 —— 只有拿到被测服务的源码，
  「缺陷 → 用例 → 契约」三者的对应关系才能被完整核验。
- 平台层 `qaplat/`：按层跑用例、落库、算覆盖率矩阵、判门禁、出报表（含禅道导入）。
- 用例分层（**marker 有重叠，别做加法**）：
  `api` / `ui` / `mes` / `login` / `contract` / `perf` / `platform`，横切标签
  `smoke` / `regression` / `flaky` / `quarantine` / `covers`。全量 **636 条**。
- 兄弟仓：`agent-test-hub`（LangGraph Agent 自动测试）、`perf-baseline-quality-gate`（性能基线门禁）、
  `api-regression-selector`（从本仓 fork，做变更→测试选择）。

## 怎么跑

一律用本仓 venv 的绝对路径调 `-m`（不必 activate）：

```bash
VENV=/d/DSH/test-projects/fullstack-test-framework/.venv/Scripts/python.exe
cd /d/DSH/test-projects/fullstack-test-framework

$VENV -m pytest -m "smoke and not ui" -q          # 冒烟，提交前最小集（实测 30 条 / 27s）
$VENV -m pytest -m api -n 4 --dist loadfile       # 接口层 132 条；CI 挂覆盖率门的就是这一步
$VENV -m pytest -m "not ui" -q                    # 全量非 UI
$VENV -m pytest -m ui                             # UI 34 条，要 chromium
$VENV -m mypy --cache-dir /tmp/mypy-cache         # 120 个源文件
$VENV -m ruff check . && $VENV -m ruff format --check .

make gate                                          # 一条命令的门禁入口 = python -m qaplat.cli gate --layer pr
                                                   # 退出码 0 达标 / 1 未达标 / 2 用法错
```

流量回放是**独立子项目**，自带 `pytest.ini`：`cd traffic-replay && $VENV -m pytest`（173 条）。

## 环境硬约束（本仓特有，必读）

### 1. `$env:TMP` 必须指向一个**Python 进程真正能写**的目录

这不是洁癖，它同时影响正确性与可复现性：

- **不可写时** pytest 的 basetemp 会**回落到仓根**（生成 `pytest-of-lefan/`），
  而 `--cov=.` 会把这些生成的 `.py` 算进分母 → **覆盖率数字跟着漂**
- **跨盘时**（TMP 在 C: 而仓库在 D:）pytest 对 rootdir 之外的路径算不出相对路径
  （`bestrelpath` 的 `commonpath` 抛 `ValueError`），nodeid 前缀变空串，
  `registry` 靠前缀读源码那一步跟着失效。已由 `qaplat/_dump.py::_qualified_nodeid`
  在收集期用 `item.path` 补回前缀，被改过的 nodeid 记在 dump 的 `renamed` 字段里

**实测坑（2026-11-02）**：用 `mkdir` 从 bash 建出来的目录（如
`D:\DSH\test-projects\fts_tmp`）虽然 `os.access(W_OK)` 返回 True，
**Python 实际写入仍然 `PermissionError [Errno 13]`**。
后果是 `testcases/platform/test_registry_coverage.py` 里 3 条 confcutdir 相关用例假红：

```
FAILED test_confcutdir_only_added_for_targets_outside_the_repo
FAILED test_collect_cases_survives_a_churning_temp_root
FAILED test_confcutdir_option_is_spliced_before_the_double_dash
```

换成环境自带的 `D:\study\AppCache\Temp\dsh-g8ujd2` 后 → **34 passed**。
判定方法：`TMP=... python -c "import tempfile; print(tempfile.gettempdir())"`，
若回显的是**仓库根**而不是你给的目录，说明那个目录不可用。

### 2. mypy 的缓存目录要可写

本机沙箱下 `.mypy_cache` 可能不可写，表现为：

```
error: INTERNAL ERROR -- Please try using mypy master on GitHub
version: 2.4.0
note: please use --show-traceback to print a traceback when reporting a bug
```

**这不是类型错误。** 加 `--cache-dir` 指向可写目录 → `Success: no issues found in 120 source files`。

### 3. 两组用例钉的是"当前行为"，不是"正确行为"

`-m login` 的 29 条与 `-m mes` 的 64 条里，有一部分断言刻意钉住被测服务的**已知缺陷**
（登录服务 6 个缺陷见 `login_app/app.py` 顶部注释与 `docs/login-contract.md` 的
"契约 vs 实现"差异表）。

**修好一个缺陷会让用例变红**，然后由人决定是改用例还是改实现。
看到红先读那份差异表，**不要直接改断言让它变绿**。

### 4. `conftest.py` 的 `sys.path` 注入不要"顺手清理"

`testcases/mes/` 与 `testcases/login/` 的用例用同级 import
（`from concurrency import ...`），因为那些模块是「和用例放一起的测试辅助件」，
不是可安装的包。改成包结构会让 import 路径变长、`pytest_plugins` 语义变化，
收益只是"看起来更规范"。后面那个 `# noqa: E402` 也是必需的，别删。

### 5. CI 的 `-n` 必须配 `--dist loadfile`

`pytest-xdist` 默认按**用例**分发。本仓**不能**用它：实测
`-m mes -n 4 --dist load` → `64 passed, 1 error`、退出码 1（连跑 8 次里 7 次复现，
1 次侥幸全绿 —— **别把"这次没红"当成没问题**）。

### 6. `mock_server/*` 的覆盖率靠 launcher 才采得到

`--cov-context=test` 的 context 在子进程 spawn 时被 `COV_CORE_CONTEXT` 固定。
改了启动方式等于把这部分覆盖率打回 0%。相关坑见下方 `api-regression-selector` 的记录。

### 7. 覆盖率门禁有**两道**，别只盯仓库级

- **仓库级**：`--cov-fail-under=58`（同一命令实测 59.48%，余量约 1.5pp；分母已排除 `testcases/`）
- **单文件级**：`scripts/check_coverage.py`，`[tool.coverage.perfile] fail_under=55`、
  `min_statements=20`，CI 里 `if: always()` 且**无** `continue-on-error`

为什么需要第二道：`qaplat/cli.py` 253 语句在 `-m api` 口径下只有 13.8%，
而仓库级门对个别模块的整体失守**完全不知情**。

## 硬约束

- **冻结文件不许改**：`FROZEN_MESWEB.md`、`qaplat/FROZEN_CLI.md`、
  `traffic-replay/FROZEN_INTERFACES.md`、
  `traffic-replay/replay/model.py`（"本文件由调度者冻结"，只许末尾追加带默认值字段）。
- **`mock_server/**` 的业务逻辑属任务边界明令禁止改动。**
- **不加依赖**：`python-multipart`（用 `Form(...)` 会在建路由阶段抛
  `RuntimeError: Form data requires "python-multipart"`）、`itsdangerous`、`jinja2`
  （本机有 3.1.6 但不在 requirements.txt，CI 上没有）。`locust` 只进
  `requirements-perf.txt`（拖进 gevent 会 monkey-patch 标准库）。
- **依赖只认 `requirements.txt`**，改完必须 `pip-compile` 重生成 `requirements.lock`，
  否则 `check_deps_sync` 用例红。`pytest` 钉 9.1.1；`pytest-cov` 必须 6.x
  （7.0 起移除 `pytest_cov/embed.py` 与 `pytest-cov.pth`，`COV_CORE_*` 子进程覆盖率通道消失）。
- **`traffic-replay` 必须留在根 `ruff.toml` 的 `extend-exclude`**：
  `replay/model.py:18` 用 PEP 695 `type JsonValue = ...` 要 py312，
  根配置是 `target-version="py310"`，放出来立刻 `invalid-syntax`。
- **新增/改名 marker 必须同时改 `docs/质量门禁策略.md`**，否则
  `scripts/check_gate_policy.py` 判红（它逐条校验策略文档 / `test.yml` / `pytest.ini` 三者一致）。

## 已知坑

- **旧解释器路径已死**：`D:\DSH\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`
  不存在（仓库改名 + 迁到 `D:\DSH\test-projects\` 后消失），回落系统 python 会
  `ModuleNotFoundError`。上游 `6f59f42` 专门修过 6 处同类死路径。
- **端口不固定**：`mock_server`、`mes_web` 都用 `free_port()` 选空闲端口
  （`testcases/mes/mock_server_ctl.py:65`），别写死 8000。
- **`-m ui` / `-m smoke` 报 `Executable doesn't exist ... chrome-headless-shell.exe`**：
  不是浏览器没装，是 Playwright 要**可写 TEMP** 而沙箱默认 TEMP 不可写 →
  把 `TMP`/`TEMP` 指到可写目录并在非沙箱进程跑。
- **并行跑 pytest 偶发收集期崩**：`FileNotFoundError [WinError 2]`、退出码 2、零 nodeid →
  pytest 9 把每一级祖先目录塞进枚举列表，共用 `$env:TMP` 根被并发进程反复建删，
  `os.path.samestat` 抛错 → 手敲 pytest 时自己给独立 `--basetemp`，
  且 `--confcutdir` 必须插在 `--` **之前**（提交 `b1df5bd` 的回归）。
- **`Path.read_text()/write_text()` 会写出 CRLF**：本仓是 LF 仓库，`.gitattributes` +
  pre-commit `mixed-line-ending --fix=lf` 会判红 → 用 `read_bytes/write_bytes` 或
  `newline=""`；改文件优先用编辑工具。
- **`clean` 用 `coverage erase` 不用 `rm .coverage*`**：后者会把 `.coveragerc` 一起匹配删掉
  （真发生过，见 `beaf73d`），且 `rm` 在 Windows cmd 里不存在。
- **`PYTHONPATH=work\pylibs` 是旧口径，别照抄**：那份 pylibs 里是 schemathesis 3.39.16，
  按路径优先级会顶掉 venv 的 4.31.1。

## 这个仓的「智能体协作」事实（面试会被问，也是本仓最特殊的一点）

本仓 200 条提交里，**100 条来自 AI 编码代理会话**（`dispatcher` 76 + `integ` 17 + `agent` 7），
人工 `Lefan Ma` 84 条，其余 `lefan17` 8 + `dependabot[bot]` 8。
集中产出于 2026-10-14 ~ 10-18 五天（19 / 22 / 25 / 28 / 6 条）。

```bash
git log --format='%an' | sort | uniq -c | sort -rn     # 复现作者分布
```

**它们不是外部贡献者，是这个仓库的开发过程本身。** 身份没统一是工程疏漏
（后续提交已改用同一身份），但这件事之所以值得留在 README 里，是因为它恰好能回答
「AI 写的代码要过什么闸才敢上线」：答案不在这段历史里，在 `qaplat/` 的四层门禁、
`docs/质量门禁策略.md`（被 `check_gate_policy.py` 机器校验）、以及
本仓 + 三个兄弟仓各自的 `AGENTS.md`。

## 提交纪律

- Conventional Commits，`scripts/check_commit_msg.py` 校验（CI `commit-convention` job）。
- 一次提交只做一件事。改缺陷时把编号写进提交信息。
- 提交前：`$VENV -m pytest -m "smoke and not ui" -q` + `ruff check .` + `mypy --cache-dir`，
  三条都绿再提交；动到用例层时再补跑对应的 `-m <layer>`。
