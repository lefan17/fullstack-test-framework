"""路径常量集中处。

单独成文件，是因为 ``__init__`` 被 ``-p qaplat._dump`` 这类 pytest 插件入口占用时
（插件在 pytest 启动早期被 import），需要一条不触发任何副作用、不 import sqlite3 /
subprocess 的轻量路径来源。``paths`` 只依赖标准库。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

#: 仓库根目录（``qaplat/`` 的上一级）。
ROOT = Path(__file__).resolve().parent.parent

#: 平台层产物目录：数据库、原始 JSON、HTML 报表都落这里（已进 .gitignore）。
CACHE_DIR = ROOT / "qaplat" / ".cache"

#: CLI 默认数据库路径，对应契约里的 ``qaplat/.cache/qaplat.db``。
DEFAULT_DB = CACHE_DIR / "qaplat.db"

#: 默认契约基准文件（OpenAPI 3.1.0，13 paths / 29 operations）。
DEFAULT_SPEC_PATH = ROOT / "docs" / "openapi.json"

#: 契约漂移检查脚本。
EXPORT_SCRIPT = ROOT / "scripts" / "export_openapi.py"


def confcutdir_args(args: Sequence[str]) -> list[str]:
    """给「被采集目标在仓库之外」的 pytest 调用补一条 ``--confcutdir``。

    治的是这样一个缺陷：``pytest`` 收集一个参数路径时，会把它的**每一级祖先目录**
    都放进枚举列表（``_pytest/main.py`` 里那句 ``for path in argpath.parents``），
    只有 ``--confcutdir`` 能截断这条链。目标若落在系统临时目录下，而同一个临时根
    又被并行进程反复建删目录，枚举到一半那个目录就消失了，
    ``os.path.samestat(p1.lstat(), p2.lstat())`` 抛 ``FileNotFoundError``，
    收集期直接崩、退出码 2、一条 nodeid 都拿不到——对 ``qaplat registry`` 就是
    「采集通道坏了」，对 ``qaplat gate`` 就是「这一层跑了 0 条」，两者都会伪装成
    「没有用例」。

    只在目标确实位于 ``ROOT`` 之外时才补：仓库内目标本来就以 ``ROOT`` 为根，
    祖先链只到 ``ROOT`` 为止，多写一条只会让 nodeid 的解析规则跟着变，是纯粹的
    风险。调用方自己已经传了 ``--confcutdir`` 时一律让位。

    已知边界（都是既有局限，不是这个函数引入的）：

    * 只看**第一个**真实存在的位置参数就返回。所以``[仓库内路径, --, 仓外目标]``
      这种混排里仓外目标得不到保护——它的祖先链照样全枚举。实际触发面很窄：
      ``qaplat gate``/``run`` 透传的目标恒为单个，只有 ``registry build`` 可能传混合
      列表，而且还得仓外目标的祖先里正好有一个会炸的 ``conftest.py`` 才看得见。
      要收口就得改成取所有仓外目标的公共祖先，那是行为变更，得配新用例。
    * ``exists()`` 与 ``resolve()`` 按**当前进程**的 cwd 判断，但 ``collect_cases``
      拉起子进程时 cwd 是 ``ROOT``。所以从别的目录传相对路径进来，两边看到的
      不是同一个文件。这条从子进程固定 cwd 那天起就存在。
    """
    if any(str(item).startswith("--confcutdir") for item in args):
        return []
    for item in args:
        text = str(item)
        if text.startswith("-"):
            continue
        path = Path(text)
        if not path.exists():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            return [f"--confcutdir={resolved.parent}"]
        return []
    return []


def _splice_index(args: Sequence[str]) -> int:
    """返回 ``--`` 在参数列表里的位置；没有 ``--`` 时就是列表末尾。"""
    for index, item in enumerate(args):
        if str(item) == "--":
            return index
    return len(args)


def with_confcutdir(args: Sequence[str]) -> list[str]:
    """返回补好 ``--confcutdir`` 的完整参数列表。

    这个函数存在的唯一理由是**位置**：``--`` 之后的一切都被 pytest 当成位置参数
    （要收集的路径），把 ``--confcutdir=...`` 追加在末尾，只要调用方用了 ``--``
    透传（``qaplat run --layer smoke -- <仓外目标>``，见 ``FROZEN_CLI.md``），
    这个选项就会变成一个不存在的路径，直接 ``file or directory not found``、
    退出码 4、零收集。所以选项必须插在 ``--`` **之前**。

    决策本身仍由 :func:`confcutdir_args` 做，这里只负责落位。

    已知边界：``--`` 只按字面匹配，不认「上一个 token 是必须带值的选项」。所以
    ``['-k', '--', <目标>]`` 会把选项插进 ``-k`` 与它的值之间，把 ``-k`` 的值换成
    confcutdir 串。触发条件是用户真把 ``-k`` 的值写成 ``--``，代价远小于为它做
    完整的选项表解析。
    """
    items = [str(item) for item in args]
    extra = confcutdir_args(items)
    if not extra:
        return items
    at = _splice_index(items)
    return [*items[:at], *extra, *items[at:]]
