# Linux 环境排查手册

> 这份手册不是「Linux 命令大全」，而是**把这三个项目搬到 Linux 上跑起来时，真实踩到的每一个坑**。
> 每一条都带现象、根因、排查命令、修法，以及「本机自测为什么发现不了」。
>
> 起点是一句很常见的要求：岗位 JD 写着「熟悉 Linux/Unix」。
> 光说「我会 cd/ls/grep」没有说服力；能说清「同一份代码在 Windows 与 Linux 上
> 为什么给出不同的数字、以及哪个数字能外推」才有。

---

## 0. 三十秒结论

| 问题 | 答案 |
|---|---|
| 三个项目在 Linux 上能跑吗 | 能。实测同一份代码：`591 passed / 11 xfailed`（平台仓非 UI）、`348 passed / 0 skipped`（性能仓）、`342 passed / 15 xfailed`（Agent 仓） |
| 跑通之后发现什么 | 一条**只在 Linux 上暴露**的真 bug：三个 `*_ctl.py` 把 Windows venv 的可执行文件当解释器（issue #24 / PR #25） |
| 数字一样吗 | 不一样，而且方向都可能反。写路径探针：Windows 上 MySQL 快 2.72×，Linux 上 MySQL 慢 1.9×（0.52×） |
| 哪个能外推 | 只有**形状**（锁粒度决定排队形态、多一跳有成本、分位必须池化重算），绝对值一个都不能 |

---

## 1. 先搞清楚「你在哪个环境里」

这一步不做，后面所有排查都是在猜。四条命令：

```bash
uname -a                  # 内核：Linux ... microsoft-standard-WSL2 说明你其实在 WSL 里
cat /etc/os-release       # 发行版：Debian 13 / Ubuntu 24.04
nproc                     # 核数（容器里看到的是宿主分给你的，不是宿主的全部）
python -V && which python # 解释器：3.12.15 /usr/local/bin/python
```

实测这台机器上的容器：

```
Linux 9db54f30d88f 6.18.33.2-microsoft-standard-WSL2 ... x86_64 GNU/Linux
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
Python 3.12.15
16                      # nproc
```

三个信息都有用：

- `microsoft-standard-WSL2` 说明内核是 WSL2 —— 后面那条 vsock 报错就能对上号；
- Debian 13 (trixie) 说明 `apt-get install nginx` 装到的是 1.26.3，和 Ubuntu 24.04 的 1.24.0 不是一个版本；
- 16 核是**容器看到的**，与宿主是否超卖无关。

---

## 2. 最快拿到一个 Linux 环境

这台机器的 `wsl -l -v` 里只有 `docker-desktop`，**没有 Ubuntu 发行版**。所以不要绕 WSL 发行版，直接用 Docker 容器，把仓库挂进去：

```powershell
docker run -d --name linux-probe `
  -v 'D:\DSH\test-projects:/work' `
  -w /work/perf-baseline-quality-gate `
  python:3.12-slim sleep infinity

docker exec linux-probe sh -lc "python -V && ls /work"
```

为什么用 `sleep infinity` 而不是 `docker run --rm ... bash -c '...'`：装依赖要一两分钟，一次装完反复用，比每次重建便宜得多。

为什么不用 `ubuntu:24.04`：`python:3.12-slim` 自带与 CI 同款的解释器版本，少一层「发行版包里的 python 是 3.12.3 还是 3.12.15」的干扰。

### 2.1 装依赖

```bash
pip install --no-cache-dir -r requirements.txt ruff mypy   # 纯 python 依赖，无需编译
apt-get update && apt-get install -y nginx                 # 只有反向代理探针需要
```

三个仓库实测都是纯 python 依赖（含 PyMySQL、schemathesis、playwright 的 **wheel**），不需要 `build-essential`。Playwright 的浏览器另算，见 §5.4。

---

## 3. 六个坑（按「卡住时间」排序）

### 3.1 Windows 的 venv 被当成解释器 —— 报错里没有一个字提到解释器

**现象**

```bash
$ python -m pytest -m api -q
48 passed, 498 deselected, 84 errors in 53.35s
```

84 条 error 全是这一行：

```
testcases.mes.mock_server_ctl.MockServerError: Mock 服务进程已退出（returncode=1）。
启动日志: <3>WSL (1861 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 1
```

**根因** 三个 `*_ctl.py` 用同一个判据挑子进程解释器：

```python
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"  # ← Windows 的 venv 布局
python = os.getenv("MOCK_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
```

仓库挂进 Linux 后，`.venv/Scripts/python.exe` **确实存在**（`exists()` 为真），但它是 Windows 的 PE 可执行文件：

- WSL2 容器里，exec 落到 WSL 的 interop 层试图转给 Windows，vsock 建不起来 → `UtilBindVsockAnyPort ... socket failed 1`；
- 原生 Linux（非 WSL）上是 `OSError: [Errno 8] Exec format error` —— 这个至少还能猜。

**排查**

```bash
ls -l .venv/Scripts/python.exe     # 文件在吗
file .venv/Scripts/python.exe      # 类型是什么（PE32+ executable (console) x86-64）
python -c "import sys; print(sys.executable)"   # 你以为在用的解释器
```

最关键的一步不是上面三条，而是**把子进程的 stdout/stderr 捞出来看**。这三个 ctl 已经把启动日志写进 `log_path` 并在报错里贴了尾巴，那一行 vsock 就是线索。

**修法** 判据改成「显式环境变量 > **仅 Windows** 用本仓 venv > 当前解释器」：

```python
def launcher_python(default_python: Path, env_var: str, *, platform: str | None = None) -> str:
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        return explicit
    current = sys.platform if platform is None else platform
    if current == "win32" and default_python.exists():
        return str(default_python)
    return sys.executable
```

**为什么本机自测发现不了** 只有在「Windows 上建过 venv + 仓库被挂进 Linux」时才触发。纯 Windows 跑、纯 Linux 上 clone 跑，都不会出现。

**`platform=` 参数是给测试用的**：这样判据能被六条单元测试钉住，而不是靠 monkeypatch 全局 `sys.platform` 去撞一个偶然。

---

### 3.2 容器里连不上宿主的 127.0.0.1

**现象** 宿主上 MySQL 跑在 `127.0.0.1:3307`，容器里 `socket.create_connection(("127.0.0.1", 3307))` 直接 `Connection refused`。

**根因** 容器的 `127.0.0.1` 是容器自己。Docker Desktop 在 Windows 上把宿主暴露成 `host.docker.internal`。

**排查**

```bash
getent hosts host.docker.internal     # → fdc4:f303:9324::254   ← 注意这是 IPv6
python -c "import socket; socket.create_connection(('host.docker.internal', 3307), 3); print('ok')"
```

**坑中坑**：它解析出来是 **IPv6** 地址。老的探测代码如果硬编码 `AF_INET`，或者自己 `gethostbyname()`（只返回 IPv4），就会解析失败——用 `getaddrinfo` / `create_connection` 让它自己挑协议族。

**修法**

```bash
PERF_MYSQL_URL=host.docker.internal:3307 \
PERF_MYSQL_USER=perf PERF_MYSQL_PASSWORD=perfpass PERF_MYSQL_DB=perf \
PERF_REDIS_URL=host.docker.internal:6379 \
python -m pytest tests/ -q
```

修好之后性能仓的 skip 从 20 条降到 0 条：

```
328 passed, 20 skipped      # 裸跑：MySQL 13 条 + nginx 集成 1 条 + 其他
348 passed                  # 带上宿主 MySQL/Redis + 容器内 nginx
```

**不要**为了让 `127.0.0.1` 可用去改 `--network host`：Windows 上不支持，Docker Desktop 会直接报错。

---

### 3.3 `python scripts/x.py` 找不到仓库里的包

**现象**

```
ModuleNotFoundError: No module named 'bench'
File ".../scripts/proxy_probe.py", line 50, in <module>
```

**根因** `python scripts/x.py` 时 `sys.path[0]` 是 `scripts/`，**不是仓库根**。所以 `from bench.engine import ...` 找不到。

**修法**（两处都要，不是二选一）

1. 跑的时候带路径：`PYTHONPATH=. python scripts/proxy_probe.py ...`
2. 脚本顶部自己插一次，别指望调用方：

```python
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

只做第 1 步的话，任何「忘记加 `PYTHONPATH`」的调用都会静默失败——而它往往出现在 CI 里（见 §3.4）。

---

### 3.4 `cmd | tee` 让崩溃的步骤显示成功

**现象** GitHub Actions 上这一步报 success，展开日志却是：

```
ModuleNotFoundError: No module named 'bench'
##[error]Process completed with exit code 0.
```

双重假绿：

1. `python scripts/x.py` 崩了（§3.3）；
2. bash 默认**没有 `pipefail`**，管道的退出码取最后一个命令 —— `tee` 永远返回 0。

**修法三件套**

```yaml
- name: 反向代理探针三口径（只发布数字，不断言比值）
  env:
    PYTHONPATH: .          # ① 让脚本 import 得到仓库包
  run: |
    set -euo pipefail      # ② 管道中任一命令失败就算失败
    python scripts/proxy_probe.py --items 5000 --users 10 --duration 5 | tee /tmp/probe.txt
    grep -q "直连 SUT" /tmp/probe.txt    # ③ 断言关键输出真的出现了
```

第 ③ 条最容易被省掉，但它防的是另一类问题：脚本「成功退出但什么也没测」——那一步的退出码依然是 0。

---

### 3.5 slim 镜像里没有你想用的工具

`python:3.12-slim` 里 `free`、`ps`、`ss`、`curl`、`ip`、`vim` 都没有：

```bash
$ free -m
sh: 1: free: not found
```

需要时按需装，别一次装一大堆：

| 想要 | 装什么 | 或者用 |
|---|---|---|
| 内存 | `apt-get install -y procps` | `cat /proc/meminfo \| head -3` |
| 端口/连接 | `apt-get install -y iproute2` | `cat /proc/net/tcp` |
| CPU 占用 | `apt-get install -y procps` | `cat /proc/loadavg` |
| 下载 | `apt-get install -y curl` | `python -c "import urllib.request; ..."` |

排障真正需要的往往只是 `/proc`。

---

### 3.6 端口占用与孤儿进程：两个平台各有一套

**Windows 上的教训**（本项目实测过）：nginx 默认**前台运行**，用 `subprocess.run(capture_output=True)` 调它会永久挂住（管道等不到 EOF）；而 `nginx -s quit` 依赖 pid 文件，若先删了临时目录再 quit，退出命令静默失败 → 孤儿进程占着端口 → 下一次运行连到「旧配置 + 已死后端」，全 502 且 error.log 是空的。

**修法**：不 `wait`、轮询端口；`stop()` 里判 `poll() is None` 就 `taskkill /PID <pid> /T /F`（`/T` 连子进程）；finally 里**先 stop 再 rmtree**。

**Linux 上**对应的是 `kill -9` 与 `pkill -f`，但同样是「先停服务再删工作目录」的顺序问题：

```bash
ps -ef | grep nginx          # 找残留（slim 镜像要先装 procps）
ss -ltnp | grep 18090        # 看端口被谁占着
kill -9 <pid>                # 最后手段
```

判据在哪都一样：**报错说「连不上」时先看进程与端口，而不是先怀疑配置**。

---

## 4. 数字为什么在 Linux 上不一样（实测对照）

这一节是这份手册最该背下来的部分：它证明「换环境跑一遍」不是礼貌性兼容，而是**会改变结论**。

### 4.1 同一份代码，三个环境，方向相反

写路径探针（同一个写动作，SQLite `BEGIN IMMEDIATE` 整库写锁 vs MySQL `SELECT ... FOR UPDATE` 行锁）：

| 环境 | SQLite | MySQL | 谁快 |
|---|---:|---:|---|
| Windows 11 / Python 3.12.10 | 69.7 RPS（P95 346 ms） | 189.8 RPS（P95 66 ms） | **MySQL 2.72×** |
| GitHub Actions / Ubuntu | 419.9 RPS（P95 88 ms） | 267.3 RPS（P95 47 ms） | **SQLite 1.57×** |
| Docker `python:3.12-slim` / WSL2 内核 | 198.3 RPS（P95 184 ms） | 102.6 RPS（P95 126 ms） | **SQLite 1.94×** |

为什么翻：一次提交的代价主要在**持久化**而不在锁。SQLite 的提交路径在 Windows 上比 Linux 贵一个数量级（69.7 ↔ 419.9 RPS），所以在 Linux 上「整库写锁」几乎不痛，而 MySQL 背着独立进程 + 跨容器网络 + 抢同一块 CPU。

**能外推的只有形状**：锁粒度决定排队形态——整库写锁的 `P99/P50` 是 8.2 倍，行锁是 1.4 倍，这个比例两个环境都成立。

### 4.2 N+1 探针：不只是变慢，而是变成超时

| 环境 | agg | n1 | 错误率 |
|---|---:|---:|---:|
| Windows（20 000 条 / 10 并发 / 8 s） | P95 105.2 ms / 95.5 RPS | P95 9 059 ms / 1.08 RPS | 0.00% |
| 容器（同参数） | P95 223.9 ms / 69.8 RPS | P95 **30 031 ms** / 0.33 RPS | **100.00%**（客户端 30 s 读超时） |

同一个缺陷，一边表现为「慢得离谱」，另一边表现为「全部超时」。所以报告里写「N+1 慢 7.8 倍」时，必须说清是哪个环境、什么口径——**连失败模式都会变**。

### 4.3 反向代理探针：Windows 上那 18.67% 的错误是平台特性

| 环境 | 多一跳代理 | 上游连接不复用 | 错误率 |
|---|---:|---:|---:|
| Windows（20 000 条 / 20 并发） | P95 1.21× | P95 **1.70×** | 0 → **18.67%** |
| 容器（同参数） | P95 1.08× | P95 **0.93×** | 0 → **0.00%** |

机制是 Windows 的临时端口 / `TIME_WAIT`：上游用 `proxy_http_version 1.0` + `Connection: close` 时每次反代都新建一条到上游的 TCP，端口很快耗尽。

所以「上游不复用连接代价 1.70×、错误率 18.67%」是**Windows 结论**；到 Linux 上这条不是 0.93× 就是别的数，但机制（端口回收速度）依然值得查。

### 4.4 分布式压测：比值稳定，时钟精度差一个数量级

| 环境 | 单机 RPS | 3 worker RPS | 比值 | 最大时钟偏移 |
|---|---:|---:|---:|---:|
| Windows | 397.81 | 382.90 | 0.96× | 6.80 ~ 8.62 ms |
| 容器 | 124.18 | 118.97 | 0.96× | **0.20 ~ 0.52 ms** |

「同机多进程只是分摊同一块 CPU，不增加天花板」这个结论两边一致（0.96×）；但**时钟偏移**在容器里小一个数量级，所以「多 worker 样本能不能按 `at` 对齐到同一窗口」的判断，在 Linux 上更可信、在 Windows 上必须显式量偏移。

### 4.5 一句话记法

> **形状可外推，绝对值不可外推；连「怎么坏」都可能变。**

对应到面试：不要只报一个倍数，要说清「这个倍数在什么环境下、什么口径下成立，换环境后我看到方向反了，所以我只敢外推锁粒度这一层」。

---

## 5. 三个仓库的跑通记录

### 5.1 性能与门禁平台（纯 python，最省事）

```bash
cd /work/perf-baseline-quality-gate
pip install --no-cache-dir -r requirements.txt ruff==0.17.1 mypy==2.4.0
python -m ruff check .            # All checks passed!
python -m ruff format --check .   # 60 files already formatted
python -m mypy                    # Success: no issues found in 27 source files
python -m pytest tests/ -q        # 348 passed
```

| 探针 | Linux 实测 |
|---|---|
| N+1 并发 | 比值 112×（5 000 条）/ 134×（20 000 条），n1 撞 30 s 超时 |
| 写路径 | SQLite 198.3 RPS vs MySQL 102.6 RPS（0.52×） |
| 分布式 | 3 worker 0.96×，时钟偏移 0.20~0.52 ms |
| 反向代理 | 1.08× / 0.93×，错误率 0.00% |

### 5.2 全链路自动化（坑最多）

```bash
cd /work/fullstack-test-framework
pip install --no-cache-dir -r requirements.txt
python -m pytest -m platform -q        # 230 passed
python -m pytest -m api -q             # 修复前 48 passed / 84 errors → 修复后 132 passed
python -m pytest -q -m "not ui"        # 591 passed, 11 xfailed
```

`-m api` 那 84 条 error 就是 §3.1 的 venv bug。UI 组（34 条）需要 Playwright 浏览器，容器里没装：

```bash
python -m playwright install --with-deps chromium   # ~150 MB，按需
```

### 5.3 Agent 自动测试

```bash
cd /work/agent-test-hub
pip install --no-cache-dir -r requirements.txt
python -m pytest -q -o addopts= --ignore=tests/test_ui_tool.py
# 342 passed, 15 xfailed
```

非 UI 套件与 Windows 完全一致（342）。UI 组用的 `_find_chrome()` 现在已补 Linux 路径与 `shutil.which`（agent PR #25），容器里装上 chromium 就能真跑：

```bash
apt-get install -y chromium && AGENTTEST_CHROME_PATH=/usr/bin/chromium python -m pytest -m ui -q
```

### 5.4 三个仓库的共同门槛

| 项 | 值 |
|---|---|
| 解释器 | 3.12（三个仓库都是） |
| 编译依赖 | 无（全部 wheel） |
| 外部服务 | MySQL 8.0 + Redis 7（宿主容器）+ nginx（容器内 apt 装） |
| 浏览器 | Playwright chromium / 系统 chromium（仅 UI 组） |

---

## 6. 命令速查

```bash
# 我在哪
uname -a; cat /etc/os-release; nproc; python -V; which python

# 文件是不是 Windows 二进制
file .venv/Scripts/python.exe

# 连宿主
getent hosts host.docker.internal
python -c "import socket; socket.create_connection(('host.docker.internal', 3307), 3); print('ok')"

# 端口与进程（slim 需先装 procps / iproute2）
ss -ltnp | head
ps -ef | grep -E 'nginx|uvicorn'

# 跑脚本前先给路径
PYTHONPATH=. python scripts/xxx.py

# 管道必须开 pipefail
set -euo pipefail

# 容器里起一个小服务然后测它
python -m pytest -m api -q
```

---

## 7. 面试怎么讲（30 秒版）

> 「我把三个仓库都搬到 Linux 容器里跑了一遍。不是为了证明『也能跑』——跑的过程里发现一条只在 Linux 暴露的 bug：三个测试夹具用 `DEFAULT_PYTHON.exists()` 挑子进程解释器，而那个路径是 Windows 的 venv 布局，Linux 上文件存在但 exec 不了，报的是 WSL 的 vsock 错误，跟解释器毫无字面关系。修法是判据从『文件在不在』改成『这个平台的 venv 布局对不对』，并加了六条单元测试把判据钉住，其中一条就是这个 bug 的回归用例。
>
> 另外我量了同一份代码在 Windows 与 Linux 上的差异：写路径探针在 Windows 上 MySQL 快 2.72 倍，在 Linux 上反过来慢将近 2 倍；N+1 探针在 Windows 上是『慢 7.8 倍』，在容器里直接变成 100% 超时。所以我现在只外推『锁粒度决定排队形态』这一层形状，绝对值一律标注环境。」

---

## 附：这份手册的证据链

| 结论 | 证据 |
|---|---|
| 84 条 error 的根因是 venv 误判 | issue #24、PR #25（含修复前后对照） |
| 修复后 Linux 全绿 | `591 passed, 11 xfailed`（容器）、`591 passed, 11 xfailed`（Windows） |
| 写路径方向相反 | `docs/evidence/summary.json` 的 `probes.mysql.profiles_*` |
| N+1 在容器里超时 | `platform_cli.py probe --items 20000 --users 10` 输出 |
| 代理探针 18.67% 是 Windows 特性 | `scripts/proxy_probe.py` 两环境输出 |
| 分布式时钟偏移差一个数量级 | `scripts/distributed_run.py` 的 `[hello] ... 时钟偏移≈` 行 |
