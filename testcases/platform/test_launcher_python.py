"""`common/launch.py::launcher_python` 的判定规则：这条规则是被一个真 bug 换来的。

## 为什么需要这个函数

三个 `*_ctl.py` 原本都写：

```python
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
python = os.getenv("MOCK_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
```

`.venv/Scripts/python.exe` 是 **Windows 的 venv 布局**。把仓库挂进 Linux 容器
（或 WSL 里共享盘）后，`exists()` 为真，但那个文件是 PE 可执行文件，
exec 它必失败。实测两种表现：

- WSL2 容器：`<3>WSL (1861 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 1`
- 原生 Linux：`OSError: [Errno 8] Exec format error`

2026-10-25 在 `python:3.12-slim` 容器里跑 `pytest -m api` 时，这 84 条 error
全是它造成的：报错只有 vsock 那一行，跟「测试代码有问题」看起来毫无关系。

所以下面每一条都**显式传 `platform=`**，不去 monkeypatch 全局的 `sys.platform`——
判据本身要能被直接钉住，而不是靠改环境去撞。
"""

from __future__ import annotations

import sys

import pytest

from common.launch import launcher_python

pytestmark = pytest.mark.platform


def test_env_override_wins_over_everything(tmp_path, monkeypatch):
    """显式环境变量优先级最高：这是人在救火时唯一的逃生门。"""
    fake_venv = tmp_path / "python.exe"
    fake_venv.write_text("", encoding="utf-8")
    monkeypatch.setenv("MOCK_PYTHON", "/opt/custom/python3")

    assert launcher_python(fake_venv, "MOCK_PYTHON", platform="win32") == "/opt/custom/python3"


def test_env_override_is_stripped(tmp_path, monkeypatch):
    """环境变量里带空白（复制粘贴换行是最常见的）不该被当成路径的一部分。"""
    monkeypatch.setenv("LOGIN_PYTHON", "  /opt/python3 \n")

    assert launcher_python(tmp_path / "nope.exe", "LOGIN_PYTHON", platform="linux") == "/opt/python3"


def test_windows_uses_the_repo_venv(tmp_path, monkeypatch):
    """Windows 上仍然是原来那套：优先用本仓 venv，保证子进程装的就是这份依赖。"""
    monkeypatch.delenv("MOCK_PYTHON", raising=False)
    fake_venv = tmp_path / "python.exe"
    fake_venv.write_text("", encoding="utf-8")

    assert launcher_python(fake_venv, "MOCK_PYTHON", platform="win32") == str(fake_venv)


def test_linux_ignores_the_windows_venv_even_when_it_exists(tmp_path, monkeypatch):
    """回归用例：文件存在不等于能执行。

    这正是那个 bug 的判据所在——修复前这里是 `str(fake_venv)`，
    于是 Linux 上会去 exec 一个 PE 文件。
    """
    monkeypatch.delenv("MOCK_PYTHON", raising=False)
    fake_venv = tmp_path / "python.exe"
    fake_venv.write_text("", encoding="utf-8")

    assert launcher_python(fake_venv, "MOCK_PYTHON", platform="linux") == sys.executable
    assert launcher_python(fake_venv, "MOCK_PYTHON", platform="darwin") == sys.executable


def test_windows_falls_back_when_the_venv_is_missing(tmp_path, monkeypatch):
    """Windows 上没装 venv 时退回当前解释器（原实现的 else 分支，不能退化）。"""
    monkeypatch.delenv("MOCK_PYTHON", raising=False)

    assert launcher_python(tmp_path / "absent.exe", "MOCK_PYTHON", platform="win32") == sys.executable


def test_default_platform_is_the_current_one(tmp_path, monkeypatch):
    """不传 `platform` 时用 `sys.platform`：生产路径与测试路径必须是同一条判据。"""
    monkeypatch.delenv("MOCK_PYTHON", raising=False)
    fake_venv = tmp_path / "python.exe"
    fake_venv.write_text("", encoding="utf-8")

    expected = str(fake_venv) if sys.platform == "win32" else sys.executable
    assert launcher_python(fake_venv, "MOCK_PYTHON") == expected
