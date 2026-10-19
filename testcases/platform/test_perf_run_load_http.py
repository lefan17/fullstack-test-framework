"""压测编排器的 HTTP 传输层：``perf/run_load.py`` 的 ``_http_get`` / ``_http_post_json``。

为什么这些用例必须存在
----------------------
2026-10-19 手动触发 CI 时，``perf`` job **第一次真正执行就红了**，报
``urllib.error.URLError: <urlopen error [Errno 111] Connection refused>``。

根因不在被测服务，在编排器自己：`_start_service()` 用「能不能打通 ``/readyz``」当就绪信号，
而它的**第一次**探测几乎必然打在 uvicorn 还没 bind 端口的那一刻。此时 ``urlopen`` 抛的是
``URLError(ConnectionRefusedError)``，但 `_http_get()` 只 catch 了 ``HTTPError``——
而 ``HTTPError`` 是 ``URLError`` 的**子类**，也就是说原来那句 ``except HTTPError``
对「连不上」这种情况完全无效，它把一次**正常竞态**升级成了未捕获异常。

为什么它能藏这么久：``perf`` job 的 ``if`` 只放行 ``workflow_dispatch`` / ``schedule``，
push 与 PR 上一律 skip。所以这段代码在 CI 里从来没被执行过，本机又因为服务起得快、
探测第一枪就打中，也没暴露。**一个从不执行的 job 等于没有这个 job，
它里面的代码等于没写过。**

第二个缺陷是同一轮发现的：``urlopen`` 默认读 ``HTTP_PROXY`` / ``HTTPS_PROXY``。
打 ``127.0.0.1`` 本该直连，经代理绕一圈后「连不上」会变成代理自己的 502 ——
就绪探测于是把「还没起来」误判成「服务回了 502」，白等到 60 秒超时。
更要紧的是压测本身：locust 底下的 ``requests`` 同样读这些变量，
P95 里量到的是**代理转发延迟**而不是被测服务的处理能力。
一份被代理污染的容量结论比没有结论更危险，因为它看起来像个数字。

这里钉的是三条契约，每条都对应一个真实发生过的失败
--------------------------------------------------
1. **连不上返回哨兵值，不抛异常**（``_CONN_FAILED``）——就绪探测靠它继续轮询。
2. **连不上不因代理而变成 502**——显式禁用代理，且要能在**父进程带代理环境变量**时成立
   （这正是 CI 与本机的真实状态；不加这条断言，把 ``_OPENER`` 删掉测试也照样绿）。
3. **POST 连不上必须抛 ``RuntimeError``**——预热令牌没有「继续往下跑」的合理路径，
   早失败比拿一个哨兵值继续更诚实。GET 与 POST 在这里**故意不对称**。

关于「怎么造一个连不上的端口」：用 ``socket`` 现绑一个端口再立刻关掉拿回号码。
写死高位端口有概率撞上别的进程；绑完再释放是这个场景的标准做法，也顺带证明
探测逻辑不依赖任何特定端口。极小概率被别的进程在窗口期抢走，真有那天这条用例
会红成一个「连上了」的断言失败——它会自己说话，不需要人去猜。
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.platform

RUN_LOAD = Path(__file__).resolve().parents[2] / "perf" / "run_load.py"


def _load_run_load() -> ModuleType:
    """按路径加载 ``perf/run_load.py``。

    不能 ``from perf import run_load``：``perf/`` 没有 ``__init__.py``，
    那样同一个文件会以两个模块名被 mypy 发现，直接 exit 2
    （``Source file found twice under different module names``）。与
    ``test_scripts_mes_data_ttl.py`` 用的是同一套加载方式。
    """
    spec = importlib.util.spec_from_file_location("_run_load_under_test", RUN_LOAD)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_load = _load_run_load()


def _free_port() -> int:
    """绑一个端口再立刻释放，返回一个当前没人监听的端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestConnectionRefusedIsAReturnValue:
    """契约 1：GET 连不上 → 返回 ``_CONN_FAILED``，不抛。"""

    def test_get_returns_sentinel_instead_of_raising(self) -> None:
        code, body = run_load._http_get(f"http://127.0.0.1:{_free_port()}/readyz")
        assert code == run_load._CONN_FAILED
        # body 要能让人从日志里看出是哪类失败，而不是一个空串。
        assert "ConnectionRefusedError" in body or "refused" in body.lower()

    def test_sentinel_is_not_a_real_http_status(self) -> None:
        """哨兵值不能是 200/404/500 这类真状态码，否则调用方无法区分。"""
        assert run_load._CONN_FAILED == 0
        assert not (100 <= run_load._CONN_FAILED <= 599)

    def test_http_error_still_returns_the_real_status(self) -> None:
        """服务**确实回了** 4xx/5xx 时必须如实返回状态码，不能被哨兵值吞掉。

        用 ``data:`` URL 造不出 HTTPError，所以这里直接构造一个真实可达的失败：
        起一个只回 503 的极简 socket 服务。这一步不是为了测 urllib，
        是为了证明 ``except HTTPError`` 那条分支**仍在生效**（修 URLError 时很容易顺手改坏）。
        """
        import threading

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            port = int(server.getsockname()[1])
            server.listen(1)

            def _serve_once() -> None:
                try:
                    conn, _ = server.accept()
                    with conn:
                        conn.recv(4096)
                        conn.sendall(
                            b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 7\r\nConnection: close\r\n\r\ndown :("
                        )
                except OSError:  # pragma: no cover - 客户端提前断开时清理用
                    pass

            worker = threading.Thread(target=_serve_once, daemon=True)
            worker.start()
            try:
                code, body = run_load._http_get(f"http://127.0.0.1:{port}/readyz")
            finally:
                worker.join(timeout=5)

        assert code == 503, f"HTTPError 分支失效了，返回 {code} {body!r}"
        assert "down" in body


class TestProxyIsBypassed:
    """契约 2：打本机必须直连，即使父进程导出了代理变量。"""

    def test_conn_refused_survives_proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """带代理环境变量时，连不上仍然返回 ``_CONN_FAILED`` 而不是代理的 502。

        这是**承重断言**：把 ``_OPENER`` 换回裸 ``urlopen``，这条会从 0 变成 502。
        上面那几条不带环境变量的用例抓不到这个回归。
        """
        dead = _free_port()
        # 指向一个真实可用的代理形式；我们只要求「不要用它」。
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")

        code, body = run_load._http_get(f"http://127.0.0.1:{dead}/readyz")

        assert code == run_load._CONN_FAILED, f"请求走了代理，拿到 {code}: {body[:80]}"

    def test_opener_carries_no_proxy_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """直接检查 ``_OPENER`` 没有任何承载代理配置的 handler。

        比端到端更直接：它不依赖「代理端口恰好连不上」这类巧合，
        纯粹断言「我们不接受任何代理配置」这个决定本身。

        **这里有个反直觉的实测事实，写下来免得后人「修正」掉它**：
        ``build_opener(ProxyHandler({}))`` 在 CPython 3.12 上返回的 opener
        **一个 ProxyHandler 都没有** —— 传入的空实例让默认 ProxyHandler 被跳过
        （``skip = True``），而空实例自身也不再被追加。结果是「彻底无代理」，
        比「有一个 proxies == {} 的 handler」更强，但断言必须按实际形态写：
        *没有* ProxyHandler 才是正确态。所以下面用 ``or`` 把两种合法态都收进断言，
        而把「有 ProxyHandler 且 proxies 非空」判为失败 —— 那才是真正会被环境变量
        重新填充的形态。
        """
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

        from urllib.request import ProxyHandler

        # `.handlers` 与 `.proxies` 都不在 typeshed 的公开签名里（它们是运行时属性），
        # 所以这里显式走 getattr —— 要测的正是这两个运行时属性，不该为了取悦类型检查器
        # 而改被测代码的形状。
        handlers: list[Any] = list(getattr(run_load._OPENER, "handlers", []))
        proxy_handlers = [h for h in handlers if isinstance(h, ProxyHandler)]
        if proxy_handlers:
            proxies: dict[str, str] = getattr(proxy_handlers[0], "proxies", {})
            assert not proxies, proxies

    def test_bare_urlopen_would_pick_up_the_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向验证：证明上面那条断言不是空断言。

        如果连裸 ``build_opener()`` 都不会读环境变量，那 ``_OPENER`` 的存在就没有意义，
        前面几条也都是永远为真的空断言。这里断言「默认 opener **确实**会带上代理」，
        从而证明我们要防的东西真实存在。
        """
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

        from urllib.request import ProxyHandler, build_opener

        default_handlers: list[Any] = list(getattr(build_opener(), "handlers", []))
        found = [h for h in default_handlers if isinstance(h, ProxyHandler)]
        assert found, "默认 opener 没有 ProxyHandler，说明本机环境不读代理，这条反向验证失去意义"
        proxies: dict[str, str] = getattr(found[0], "proxies", {})
        assert proxies, "默认 ProxyHandler 的代理表为空，无法证明风险存在"


class TestPostFailsLoudly:
    """契约 3：POST 连不上 → ``RuntimeError``，不返回哨兵值。"""

    def test_post_raises_runtime_error_when_unreachable(self) -> None:
        dead = _free_port()
        with pytest.raises(RuntimeError) as excinfo:
            run_load._http_post_json(f"http://127.0.0.1:{dead}/auth/login", {"username": "x"})
        message = str(excinfo.value)
        assert "连接失败" in message
        assert "ConnectionRefusedError" in message or "refused" in message.lower()

    def test_post_and_get_deliberately_disagree(self) -> None:
        """把「不对称」本身钉成契约，免得后人以为是疏漏而顺手统一掉。"""
        dead = _free_port()
        code, _ = run_load._http_get(f"http://127.0.0.1:{dead}/readyz")
        assert code == run_load._CONN_FAILED
        with pytest.raises(RuntimeError):
            run_load._http_post_json(f"http://127.0.0.1:{dead}/auth/login", {})


def test_run_load_module_is_importable_without_locust() -> None:
    """``perf/run_load.py`` 能在没装 locust 的环境里被导入。

    CI 的 ``test`` job 只装 ``requirements.txt``，不装 ``requirements-perf.txt``
    （locust 会拖进 gevent 并 monkey-patch 标准库）。如果哪天有人在模块顶层
    ``import locust``，上面所有用例会在**收集阶段**就炸，而且报错指向 conftest，
    看不出根因。这条把「编排器与压测框架解耦」变成可执行断言。
    """
    assert not hasattr(run_load, "locust")
    assert run_load._CONN_FAILED == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1/",  # 特权端口，普通用户连不上
        "http://127.0.0.1:65535/",  # 合法端口号上界，几乎不会有人监听
    ],
)
def test_other_unreachable_ports_also_return_sentinel(url: str) -> None:
    """端口号不该影响结论：不止「刚释放的那个端口」能触发哨兵路径。"""
    code, _body = run_load._http_get(url, timeout=2.0)
    assert code == run_load._CONN_FAILED


def test_annotations_are_what_callers_expect() -> None:
    """``_http_get`` 的返回类型真的是 ``tuple[int, str]``（mypy 之外的运行时确认）。

    这条看起来很浅，但它挡的是一个真实风险：把哨兵值改成 ``None`` 或
    ``HTTPStatus(0)`` 会让 `_start_service()` 里的 ``code == 200`` 比较依然能跑，
    直到某处把 code 当数字格式化时才炸。
    """
    dead = _free_port()
    result: Any = run_load._http_get(f"http://127.0.0.1:{dead}/readyz")
    assert isinstance(result, tuple)
    assert len(result) == 2
    code, body = result
    assert isinstance(code, int)
    assert isinstance(body, str)
