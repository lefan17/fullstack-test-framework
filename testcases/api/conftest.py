"""性能门禁用例的 fixture：起一个本地 mock_server，并换一个管理员令牌。

为什么这里自己起服务、而不是复用 `apis/` 指向的公网 base_url：
性能基线一旦采自公网，测的就是"本机到 jsonplaceholder 的链路"，那条链路不可控也不可复现，
换台机器、换个时段基线全部作废。所以基线必须打在本地 mock_server 上——
同一份代码、同一份种子数据、同一个 SQLite 文件，只有在这样的前提下
"P95 变差了"才指向被测代码。

为什么复用 `testcases/mes/mock_server_ctl.MockServer` 而不另写一套启动逻辑：
那份实现已经在 mes / login / token 生命周期等场景跑熟，含健康的数据库路径校验
（`_wait_healthy` 会核对子进程连的库与期望一致）和优雅停机。
性能门禁最怕"服务其实没起起来、请求打到了别的端口"，这个校验正好挡住。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests

from testcases.mes.mock_server_ctl import MockServer

# 基线里记录的端点全部需要鉴权（/healthz 除外），用一个会话复用它，
# 避免把"每次请求都重新登录"的额外开销混进延迟样本。
ADMIN_ROLE = "ADMIN"


@pytest.fixture(scope="session")
def perf_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MockServer]:
    """每个 pytest 进程起一个独立的 mock_server；端口由系统分配，避免并发互撞。"""
    workdir = tmp_path_factory.mktemp("perf")
    server = MockServer(db_path=workdir / "perf.db", log_path=workdir / "perf-server.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def perf_base_url(perf_server: MockServer) -> str:
    return perf_server.base_url


@pytest.fixture(scope="session")
def perf_token(perf_base_url: str) -> str:
    """换令牌。登录接口本身不在基线里——它的耗时含密码哈希，与查询性能无关。

    这里也走 ``trust_env = False`` 的会话，理由与 ``perf_session`` 相同：
    登录虽然只发生一次、不进分位，但它是整条采样链路的**前置条件**——
    代理配置严一点（拒绝 CONNECT 到 127.0.0.1）就会让整组用例在 setup 阶段
    红掉，报错指向登录而不是代理，排查方向从一开始就是错的。
    """
    from common.secrets import role_password, role_username

    username = role_username(ADMIN_ROLE)
    with requests.Session() as client:
        client.trust_env = False
        response = client.post(
            f"{perf_base_url}/auth/login",
            json={"username": username, "password": role_password(ADMIN_ROLE)},
            timeout=15,
        )
    assert response.status_code == 200, f"性能基线的服务登不进去：{response.status_code} {response.text[:200]}"
    return str(response.json()["access_token"])


@pytest.fixture(scope="session")
def perf_session(perf_token: str) -> Iterator[requests.Session]:
    """带连接池的会话。基线里所有端点共用它，测的才是稳态复用连接下的延迟。

    **必须显式 ``trust_env = False``。** 默认的 ``requests.Session`` 会读
    ``HTTP_PROXY`` / ``HTTPS_PROXY``，把发往 ``127.0.0.1`` 的采样请求交给代理。
    实测（2026-10-19，本机导出 ``HTTP_PROXY=http://127.0.0.1:7897``）：

        未禁用：P50 15.4ms / P95 30.1ms → 6 个端点全部超出预算，用例红
        trust_env=False：P50 2.8ms  → 2 passed in 4.20s

    也就是说这份「性能基线」在没有这行时量的是**代理的转发延迟**。它不一定每次
    都红——代理空闲时 30ms 可能刚好还在预算内——于是一个受环境摆布的用例会变成
    偶发红，而偶发红通常被当成"机器慢"，不该被修的根因就此长期潜伏。

    采样测的东西必须只由被测服务决定：环境里有代理是开发机的常态，
    让"本机服务"绕代理一圈既不必要也不正确。
    """
    session = requests.Session()
    # 不吃任何 *_PROXY 环境变量：采样目标恒为 127.0.0.1，直连是唯一正确路径。
    session.trust_env = False
    session.headers.update({"Authorization": f"Bearer {perf_token}"})
    yield session
    session.close()
