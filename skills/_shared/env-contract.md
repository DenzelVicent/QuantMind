# 运行环境契约（所有自研技能共享，最高优先级）

本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：

1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
   ```bash
   docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
   ```
   脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/stock_reports/{市场或类别}/{股票名}/`（`/data` 是 QwenPaw 与 quantmind 容器共享的可读写挂载，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
4. **MD → PDF 转换（按优先级降级）**：
   ① QwenPaw 本地直接跑 `python3 /app/backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（扩展镜像已内置 reportlab + 中文字体，研报级排版，首选）；
   ② QwenPaw 环境缺依赖时，`docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（扩展镜像已含 docker CLI）；
   ③ 以上都不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
   ④ 全部失败则只交付 MD，并明确告知用户 PDF 未能生成及原因。
5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

> 维护说明：此文件是唯一事实源。各 `SKILL.md` 不得再粘贴全文，只保留一行引用（见 `SKILL_TEMPLATE.md`）。改契约只改这里，随 `quantbot_init.sh` 同步。
>
> 投递方式：技能池的 `upload-zip` 只认「含 `SKILL.md` 的目录」，`_shared/` 不会随 zip 进池，因此 `quantbot_init.sh` 的 `install_shared()` 会把本目录单独投递到 `WORKING_DIR/workspaces/<agent>/skills/_shared/`（与各技能同级，`../_shared/env-contract.md` 才解析得到）。若发现技能里的引用断链，先确认该目录是否就位。
