"""选择「起被测服务」用的解释器。

## 为什么要单独一个函数

三个 `*_ctl.py`（`testcases/mes/mock_server_ctl.py`、`testcases/login/login_server_ctl.py`、
`testcases/ui/mes_web_ctl.py`）都写了同一种判据：

```python
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
python = os.getenv("MOCK_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
```

`.venv/Scripts/python.exe` 是 **Windows 的 venv 布局**。在 Linux/macOS 上，
如果这个目录被挂载或复制进来（例如 Docker 里 `-v` 挂整个仓库、WSL 里用共享盘），
`DEFAULT_PYTHON.exists()` 会是 True，但那个文件是 Windows 的 PE 可执行文件 ——
exec 它会失败。实测两种表现：

- **WSL2 里的容器**：一行极难懂的报错
  `<3>WSL (1861 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 1`
  （WSL 的 interop 层试图把这次 exec 转交给 Windows，vsock 建不起来）；
- **原生 Linux**：`OSError: [Errno 8] Exec format error`。

所以在 Linux 上跑这套用例时，`testcases/mes`、`testcases/login`、`testcases/ui`
的 server fixture 会全部 error —— 而 error 信息里只有 vsock 那一行，
和「测试代码有问题」看起来毫无关系。

判据不能只看「文件在不在」，还要看「这个平台的 venv 布局对不对」：
**Windows 才用 `Scripts/python.exe`**，其它平台直接用当前解释器。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def launcher_python(
    default_python: Path,
    env_var: str,
    *,
    platform: str | None = None,
) -> str:
    """返回起被测服务应该用的解释器路径。

    优先级：显式环境变量 > 本仓 Windows venv（**仅 Windows**）> 当前解释器。

    `platform` 参数只为可测试性存在：默认取 `sys.platform`，
    测试里直接传 `"linux"` / `"win32"` 就不用 monkeypatch 全局状态。
    """
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        return explicit
    current = sys.platform if platform is None else platform
    if current == "win32" and default_python.exists():
        return str(default_python)
    return sys.executable
