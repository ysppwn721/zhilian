# 纯 API 与三层路由耗时实测

## 测试对象

- 数据：72 条中文语义改写论断、固定事实表。
- 纯 API：使用已完成的真实 DeepSeek 回放记录。
- 三层路由：规则唯一候选、本地 BGE、API 低置信兜底。
- 本地模型：BGE reranker ONNX INT8，CPU Execution Provider。
- 测量脚本：`measure_pure_api_vs_routed_latency.py`。

## 已获得的实测结果

| 项目 | 结果 |
|---|---:|
| 纯 API 第 1 批 | 5.245 秒 |
| 纯 API 第 2 批 | 4.484 秒 |
| **纯 API 总耗时** | **9.729 秒** |
| 三层规则 + 本地 BGE 第 1 次 | 5.73 秒 |
| 三层规则 + 本地 BGE 第 2 次 | 5.67 秒 |
| 三层规则 + 本地 BGE 第 3 次 | 5.66 秒 |
| **三层本地阶段中位数** | **5.67 秒** |

本次路由统计为：15 条规则唯一、43 条本地 BGE 直接通过、14 条需要 API 兜底。

## 当前能得出的结论

纯 API 的网络调用耗时已经有真实记录，总计 9.729 秒。三层路由的规则和本地模型阶段在 CPU 上实测中位数为 5.67 秒，比纯 API 总耗时少 4.06 秒，约少 41.7%。

但是，当前本地进程没有配置 API Key，因此本轮没有重复发送 14 条低置信论断的真实 API 请求。三层路由的完整端到端时间应为：

```text
5.67 秒（规则 + 本地 BGE） + 14 条困难论断的真实 API 请求时间
```

因此当前不能把 5.67 秒直接称为三层路由最终总耗时，也不能仅凭这轮结果宣称三层路由端到端一定更快。已有历史真实 API 记录中，14 条左右的小批次通常约为 2—4 秒，但这只能作为参考，不能替代同机同次测量。

## 完成同机同次端到端测量

在配置 API Key 的环境中运行：

```powershell
$env:DEEPSEEK_API_KEY = "<你的密钥>"
$env:ZHILIAN_LOCAL_RERANKER_PATH = "$PWD\models\bge-reranker-v2-m3-onnx-int8"
$env:ZHILIAN_LOCAL_RERANKER_ENABLED = "1"
$env:ZHILIAN_LOCAL_RERANKER_DEVICE = "cpu"
.venv\Scripts\python.exe 答辩评测\measure_pure_api_vs_routed_latency.py --live-api
```

脚本只输出耗时统计，不会把 API Key 写入 JSON、日志或交付文件。

## 大批量场景的已知差异

280 条长文论断的批量仿真显示：纯 API 需要 7 批，三层路由只需要 2 批；输入 token 减少约 82.8%，输出 token 减少约 84.6%。这个结果已经能证明三层路由减少远程请求量，但端到端延迟仍应使用同一批输入和同一网络环境重新测量。
