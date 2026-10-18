# quantmindoss — QuantMind OpenAPI SDK（v1）

Python SDK，用 AK/SK 访问 QuantMind 只读接口：系统新闻情绪、模型推理结果、模拟盘持仓。

> 包名说明：PyPI 上的 `quantmind` 已被他人占用（无关的数学小库），
> 本包以 `quantmindoss` 发布：`pip install quantmindoss`，`from quantmindoss import QuantMindClient`。

## 安装

```bash
pip install quantmindoss
```

## AK/SK 获取

1. 打开前端模拟交易设置页 → 接入凭证 / API 密钥选项卡
2. `permissions` 填 `["news.read", "inference.read", "paper.read"]`（缺省全开）
3. `Access Key` 直接展示；`Secret Key`（`sk_` 开头）点重新生成，仅显示一次，请妥善保存

## 快速开始

```python
from quantmindoss import QuantMindClient

qm = QuantMindClient(
    "http://<服务器IP>:8000",
    access_key="qm_live_xxx",
    secret_key="sk_xxx",
)

# 1. 新闻情绪
qm.news.get_sentiment(tickers="600036.SH", sentiment="bearish", since="2026-09-01")
qm.news.get_stats(tickers="600036.SH")
for art in qm.news.iter_sentiment(tickers="600036.SH", page_size=100):
    print(art["title"], art.get("sentiment_label"))

# 2. 模型推理结果
qm.inference.get_latest()                    # 当前生效批次
qm.inference.get_run("20261010_xxxx")        # 批次明细 signals[]
qm.inference.get_latest_signals()            # 一步拿当前信号列表
qm.inference.get_stock_history("600036.SH", days=180)

# 3. 模拟盘持仓
qm.paper.get_positions(market="CN")          # CN/HK/US/FUTURES/CRYPTO
```

## 方法表

| 资源 | 方法 | 说明 |
|---|---|---|
| news | `get_sentiment(tickers, sentiment, strong_only, keyword, since, until, sort, page, page_size)` | 新闻情绪分页 |
| news | `get_stats(tickers, sentiment, since, until)` | 标签频次统计 |
| news | `iter_sentiment(page_size, **同上)` | 自动翻页迭代 |
| inference | `get_latest(model_id)` | 当前生效批次 |
| inference | `list_models()` | 用户训练模型列表（含 model_id） |
| inference | `list_batches(model_id, status, page, page_size)` | 批量推理历史 |
| inference | `get_run(run_id)` | 批次明细 + items 信号 |
| inference | `get_latest_signals(model_id)` | 当前信号列表（两次请求合并） |
| inference | `get_stock_history(symbol, days, model_id, end_date)` | 个股历史分数 |
| paper | `get_positions(market)` | 模拟盘账户 + 持仓 |

## 行为说明

- 鉴权：首次调用自动 `POST /api/v1/open/auth/token` 兑换 12 小时令牌并缓存；
  遇到 401 自动刷新一次后重试。
- 重试：GET 请求对 429/5xx 最多退避重试 3 次（`max_retries` 可调）。
- 异常：`AuthError(401)` / `PermissionError(403)` / `NotFoundError(404)` /
  `RateLimitError(429)` / `ServerError(5xx)`，基类 `QuantMindError`。
- 股票代码用后缀式（`600036.SH`）；推理/持仓数据按该 AK 所属用户隔离。
- 建议内网使用；公网暴露请前置 HTTPS 反向代理。
