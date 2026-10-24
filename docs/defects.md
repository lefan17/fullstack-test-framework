# 缺陷台账

本仓库的「被测服务」里保留了一批**真实的、已知的**缺陷。它们不是漏测，而是**故意留着**
做两件事：证明测试能发现缺陷、证明缺陷被修好时测试会立刻知道。

缺陷分两类，处理方式**刻意不同**，这是本文档最值得看的部分。

---

## 一、契约缺陷 `DEF-01` ~ `DEF-06`（被测服务 `mock_server/`）

来源：用 `schemathesis` 拿 OpenAPI spec 当输入去发请求 + 手写用例与自动导出 spec 逐条对照。

| 编号 | 缺陷 | 当前处理 | 钉住 / 验证它的用例 |
|---|---|---|---|
| DEF-01 | FastAPI 给 422 自动生成的 `HTTPValidationError` 是 `{detail: 数组}`，与 spec 自己声明的 schema 不兼容 | `xfail(strict=True)` 钉住 | `testcases/contract/test_contract_export.py::test_live_422_body_conforms_to_declared_schema` |
| DEF-02a | 鉴权在 spec 里没有语义：写成 `authorization: str \| None = Header(...)`，OAS 里没有 securityScheme | `xfail(strict=True)` 钉住 | `testcases/contract/test_contract_export.py::test_spec_declares_bearer_auth_scheme` |
| DEF-02b | 400/401/403/404/409 由全局 exception_handler 产出，FastAPI 生成器看不到，纯净 spec 只声明 200/201/422 | `xfail(strict=True)` 钉住（**×6**） | `testcases/contract/test_contract_export.py::test_reachable_error_status_is_declared_in_spec` |
| DEF-03 | `POST /api/v1/auth/login` 的 description 宣称同时接受 JSON 与 `x-www-form-urlencoded`，实际表单拿不到 200；空表单还会因未装 `python-multipart` 变 500 | `xfail(strict=True)` 钉住 | `testcases/contract/test_contract_export.py::test_declared_form_login_returns_token` |
| DEF-04 | 手写契约与自动导出 spec 对登录接口的声明不一致 | `xfail(strict=True)` 钉住（**×2**） | `testcases/contract/test_handwritten_parity.py::test_auto_export_documents_declared_login_status` |
| DEF-05 | `{"qty": "INFINITY"}` 这类畸形输入让服务回 500 而不是 422 | **已修**，转正常用例 | `testcases/contract/test_schemathesis_properties.py::test_non_finite_decimal_literal_is_rejected_as_422` |
| DEF-06 | `page=461168601842738792` 让 `(page-1)*page_size` 溢出 int64，绑进 SQLite 抛 `OverflowError` → 500 | **已修**，转正常用例 | `testcases/contract/test_schemathesis_properties.py::test_out_of_range_pagination_is_rejected_not_500` |

**`-m contract` 的 xfailed 恒为 11** = DEF-01 ×1 + DEF-02a ×1 + DEF-02b ×6 + DEF-03 ×1 + DEF-04 ×2。
这个数字是刻意钉死的：它一变，就说明某条缺陷被修好了（XPASS → strict 模式下红）或被扩大了。

---

## 二、登录服务缺陷 `FIND-01` ~ `FIND-06`（被测服务 `testcases/login/login_server.py`）

这些是**故意写进被测服务**的缺陷，用来演示「用例设计方法能不能真的发现问题」。
它们的用例**是绿的**——断言的是「当前行为确实有这个问题」，不是「期望它被修好」。

| 编号 | 级别 | 缺陷 |
|---|---|---|
| FIND-01 | 中 | 无用户存在性校验：任意用户名 + 正确密码都能登录 |
| FIND-02 | 低 | 契约偏差（宽松）：额外接受表单提交，契约只定义了 JSON |
| FIND-03 | 低 | `username` trim 而 `password` 不 trim（处理不对称） |
| FIND-04 | 低 | 无长度限制：1000 字符的用户名/密码均可提交 |
| FIND-05 | 中 | 用户名原样回显：前端未转义则存在 XSS 风险 |
| FIND-06 | 中 | token 为固定值 `demo-token-123`：无签发时间、无过期 |

设计与契约细节见 [`docs/login-test-cases.md`](login-test-cases.md)、
[`docs/login-contract.md`](login-contract.md)。

---

## 三、为什么用 `xfail` 钉住，而不是跳过或删掉

这是本仓库在缺陷处理上唯一一个"有观点的选择"：

- **写成期望式断言**（`assert 应该返回 422`）：会常年红着。团队很快学会"这几条是已知失败，
  忽略即可"，然后**真的失败也没人看了**。
- **跳过或删掉**：缺陷消失于视野，修好时也没人知道。
- **钉住当前行为**（`xfail(strict=True)` + 断言"契约应该声明的东西"）：用例是绿的，
  一旦有人修好 → XPASS → strict 模式下立刻红 → **缺陷单会自己找上门**。

代价是「缺陷清单」必须独立存在（就是本文档），否则用例本身读不出缺陷的严重级和业务影响。

---

## 四、对应到缺陷管理系统的样子

如果把这些录进禅道 / Jira，字段映射是：

| 这里 | 缺陷管理系统里 |
|---|---|
| `DEF-0x` / `FIND-0x` | 缺陷单号 |
| 表格第二列 | 缺陷标题 + 复现步骤 |
| `钉住用例` 列 | **回归用例**（修好后把 xfail 去掉，这条用例就成为回归门禁） |
| `xfail(strict=True)` | 状态「已确认 / 挂起」，且带自动回归探测 |
| `-m contract` 恒为 11 xfailed | 缺陷基线的**哨兵值**，进 CI |

真正的项目里缺的那一环是「谁修、什么时候修」——个人仓库没有另一方，所以这里只做到
「缺陷被量化、被钉住、被哨兵化」，不假装有完整的需求-开发-测试闭环。

---

## 五、怎么复核

```bash
# 契约缺陷：应恰好 11 xfailed
pytest -m contract -q

# 登录缺陷：6 条 FIND 标记分散在 test_login_contract.py 里
grep -n "FIND-0" testcases/login/test_login_contract.py
```
