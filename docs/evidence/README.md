# evidence/ · 可复核的证据

本目录存放**机器可读**的证据快照，目的是让「README 里的数字」都能回答三个问题：
这是哪次运行产生的、用什么命令产生的、别人能不能自己跑出来。

## summary.json

| 字段 | 含义 |
| --- | --- |
`source` | 这份数据从哪个产物来的 |
`produced_at` | 产物自己的生成时间（不是本文件写入时间） |
`reproduce` | 复核命令 |
`notes` | 哪些数字能外推、哪些不能 |

## 怎么复核

```bash
pip install -r requirements.txt
pytest --collect-only -q          # 用例数应为 636
python -m qaplat.cli registry build
```

生成的 `reports/case-index.json` 应与本目录 `summary.json` 的
`nodeids_collected` / `nodeids_with_case_id` 一致。

## 在线报表为什么单独看

`qaplat/.cache/qaplat.db` 是 SQLite 度量库，体积大且每次运行都会变，不适合入库。
CI 里它通过 `upload-artifact` / `download-artifact` 在 test job 与 pages job 之间传递，
最终渲染成 https://lefan17.github.io/fullstack-test-framework/ 。
那份报表才是「累计轮次 / 用例级记录」的权威来源。
