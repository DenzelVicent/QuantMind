"""报告归档（Report Archive）REST API — 分析报告文件管理。

从原 `routers/trading_agents.py` 拆分而来（TradingAgents 多 Agent 投研管线已下线，
但报告档案库被「技能中心」前端与多个 QuantBot 技能复用，故独立成模块）。

职责：
- 列出报告目录（市场文件夹 → 股票名子文件夹 → 文件，含文件名解析出的元数据）
- PDF 内联预览 / 上传 / 移动 / 删除 / 新建与删除文件夹

目录约定：
- 根目录由 `QM_REPORT_ARCHIVE_DIR` 指定，兼容历史 `TRADING_AGENTS_RESULTS_DIR`，
  默认 `/data/reports/trading_agents`（沿用既有路径，避免已同步的 QuantBot 技能失效）。
- 结构：`{根}/{市场中文名}/{股票名}/{股票名}{代码}_{trade_date}_{报告类型}.{md,pdf}`
  市场中文名见 `backend/scripts/md_to_pdf_report.py` 等报告生成方（A股市场 / 美股市场 /
  港股市场 / 区块链市场 / 期货市场）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/reports", tags=["ReportArchive"])

# 报告归档根目录（默认沿用历史路径，见模块 docstring）
_DEFAULT_RESULTS_DIR = Path(
    os.getenv("QM_REPORT_ARCHIVE_DIR", "").strip() or "/data/reports/trading_agents"
)
# 历史遗留目录（旧版落在容器内 /app/db，宿主机 ./db）
_LEGACY_RESULTS_DIR = Path("/app/db/trading_agents_results")

# 报告文件扩展名
_REPORT_SUFFIXES = (".pdf", ".md")

# 评级关键词（从文件名解析，评级必须以下划线分隔）
_SIGNAL_KEYWORDS = ("buy", "overweight", "hold", "underweight", "sell")


# ============================================================================
# 路径与元数据工具
# ============================================================================


def _safe_filename(name: str) -> bool:
    """拒绝路径穿越/特殊字符，只允许安全文件名。"""
    if not name or name in (".", ".."):
        return False
    return not any(ch in name for ch in ("/", "\\", "\x00"))


def _safe_folder_path(path: str) -> bool:
    """校验文件夹路径：允许「市场/股票名」两级路径，逐段校验，拒绝穿越。"""
    if not path or path in (".", ".."):
        return False
    if path.startswith("/") or path.endswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(_safe_filename(part) for part in path.split("/"))


def _sanitize_name(raw: str) -> str:
    """清洗股票名/文件名非法字符（Windows/路径分隔符等）。"""
    cleaned = raw.replace("/", "").replace("\\", "").replace(":", "").replace("*", "")
    cleaned = cleaned.replace("?", "").replace('"', "").replace("<", "").replace(">", "")
    return cleaned.replace("|", "").strip() or "未命名"


def _resolve_results_dir() -> Path:
    """解析报告目录（宿主机/容器均可）。

    优先级：`QM_REPORT_ARCHIVE_DIR` → 历史 `TRADING_AGENTS_RESULTS_DIR` → 默认目录
    → 旧版 `/app/db/trading_agents_results`（保证历史报告仍可见）。
    """
    for key in ("QM_REPORT_ARCHIVE_DIR", "TRADING_AGENTS_RESULTS_DIR"):
        env_val = os.getenv(key, "").strip()
        if env_val and Path(env_val).is_dir():
            return Path(env_val)
    if _DEFAULT_RESULTS_DIR.is_dir():
        return _DEFAULT_RESULTS_DIR
    if _LEGACY_RESULTS_DIR.is_dir():
        return _LEGACY_RESULTS_DIR
    return _DEFAULT_RESULTS_DIR


def _iter_report_files(root: Path):
    """递归产出报告目录下所有 .md/.pdf 文件（根 + 任意层级子文件夹）。"""
    if not root.is_dir():
        return
    for entry in root.rglob("*"):
        if entry.is_file() and entry.suffix.lower() in _REPORT_SUFFIXES:
            yield entry


def _parse_report_meta(filename: str) -> dict:
    """从文件名解析元数据：{ticker, name(股票名), date, signal}。

    新格式: {股票名}{代码}_{date}_{报告类型}.pdf
    例: 贵州茅台600519_2026-08-15_投研分析报告.pdf → ticker=600519, name=贵州茅台
    旧格式: {ticker}_{date}_{报告类型}.pdf
    例: 002594_2026-08-14_投研分析报告.pdf → ticker=002594, name=""
    """
    stem = filename.rsplit(".", 1)[0]
    parts = stem.split("_")
    meta = {
        "filename": filename,
        "ticker": "",
        "date": "",
        "time": "",
        "name": "",
        "signal": None,
    }
    if len(parts) >= 2:
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})$", parts[1])
        if date_match:
            # 新格式：{股票名}{代码}_{date}_...
            head = parts[0]
            ticker_match = re.search(r"(\d{4,6})$", head)
            if ticker_match:
                meta["ticker"] = ticker_match.group(1)
                meta["name"] = head[: ticker_match.start()]
            else:
                meta["ticker"] = head
            meta["date"] = parts[1]
        else:
            # 旧格式：{ticker}_{date}_...
            meta["ticker"] = parts[0]
            meta["date"] = parts[1]
    # 尝试从文件名解析评级（Buy/Overweight/Hold/Underweight/Sell）
    for kw in _SIGNAL_KEYWORDS:
        if f"_{kw}" in stem.lower():
            meta["signal"] = kw.capitalize()
            break
    return meta


def _file_meta(f: Path) -> dict:
    meta = _parse_report_meta(f.name)
    meta.update({"size": f.stat().st_size, "modified": f.stat().st_mtime})
    return meta


# ============================================================================
# 列表 / 预览
# ============================================================================


@router.get("/files/list")
async def list_report_files():
    """列出报告目录下所有文件，按「市场文件夹 → 股票名文件夹 → 文件」分组（含元数据）。"""
    root = _resolve_results_dir()
    if not root.exists():
        return {"code": 200, "data": {"root": str(root), "folders": [], "files": []}}

    folders: list[dict] = []
    files: list[dict] = []

    # 根目录文件（未分类，历史遗留）
    for f in sorted(
        (p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _REPORT_SUFFIXES),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        files.append(_file_meta(f))

    # 子文件夹：市场文件夹（可含股票名子文件夹）
    for market_dir in sorted(root.iterdir()):
        if not market_dir.is_dir():
            continue
        # 股票名子文件夹（{市场名}/{股票名}/{文件}）
        nested_folders: list[dict] = []
        direct_files: list[dict] = []
        for entry in sorted(market_dir.iterdir()):
            if entry.is_dir():
                sub_files = [
                    _file_meta(f)
                    for f in sorted(entry.iterdir())
                    if f.is_file() and f.suffix.lower() in _REPORT_SUFFIXES
                ]
                nested_folders.append({"name": entry.name, "files": sub_files})
            elif entry.is_file() and entry.suffix.lower() in _REPORT_SUFFIXES:
                direct_files.append(_file_meta(entry))
        # 兼容旧结构：{市场名} 直接放文件（无股票名子文件夹）
        if direct_files:
            direct_files.sort(key=lambda m: m["modified"], reverse=True)
        if nested_folders:
            nested_folders.sort(key=lambda f: f["name"])
        folders.append({
            "name": market_dir.name,
            "files": direct_files,
            "subfolders": nested_folders,
        })

    return {"code": 200, "data": {"root": str(root), "folders": folders, "files": files}}


@router.get("/files/pdf/{filename}")
async def get_report_pdf(filename: str):
    """返回 PDF 文件（供前端 iframe 内联预览，不触发下载）。

    支持任意层级：根目录、市场文件夹、股票名子文件夹。同名文件取修改时间最新的。
    """
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="非法文件名")
    root = _resolve_results_dir()
    candidates = [p for p in _iter_report_files(root) if p.name == filename]
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        p = candidates[0]
        if p.suffix.lower() == ".pdf":
            from fastapi.responses import FileResponse

            # 不传 filename 参数：保持 Content-Disposition 为空 → 浏览器 iframe 内联预览
            return FileResponse(p, media_type="application/pdf")
    raise HTTPException(status_code=404, detail=f"PDF 不存在: {filename}")


# ============================================================================
# 上传
# ============================================================================


@router.post("/files/upload")
async def upload_report_file(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """前端上传 PDF 报告到报告目录（可选指定「市场/股票名」目标文件夹）。

    仅接受 .pdf；基名为路径穿越时拒绝；同名冲突追加时间戳后缀避免覆盖。
    """
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="非法文件名")

    root = _resolve_results_dir()
    target_dir = root
    if folder:
        if not _safe_folder_path(folder):
            raise HTTPException(status_code=400, detail="非法文件夹路径")
        target_dir = root / Path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    dest = target_dir / filename
    if dest.exists():
        dest = target_dir / f"{dest.stem}_{int(time.time())}{dest.suffix}"

    # 分块写入，上限 50MB；文件头必须是 PDF magic 字节，防扩展名伪装
    max_bytes = 50 * 1024 * 1024
    written = 0
    try:
        with dest.open("wb") as fh:
            head = await file.read(5)
            if head.lower() != b"%pdf-":
                raise HTTPException(status_code=400, detail="文件内容不是 PDF")
            fh.write(head)
            written += len(head)
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="文件过大（上限 50MB）")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    return {
        "code": 200,
        "data": {"filename": dest.name, "folder": folder or "", "size": written},
    }


# ============================================================================
# 移动 / 删除 / 文件夹
# ============================================================================


class FileDeleteRequest(BaseModel):
    files: list[str] = Field(default_factory=list, description="待删除文件名列表")


class FileMoveRequest(BaseModel):
    files: list[str] = Field(default_factory=list, description="待移动文件名列表")
    target_folder: str = Field(..., description="目标文件夹（支持「市场/股票名」两级路径）")


class FolderRequest(BaseModel):
    folder: str = Field(..., description="文件夹（支持「市场/股票名」两级路径）")


@router.post("/files/move")
async def move_report_files(req: FileMoveRequest):
    """批量移动报告文件到目标文件夹（target_folder 支持两级路径）。"""
    if not _safe_folder_path(req.target_folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / Path(req.target_folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    errors: list[str] = []
    # 收集所有源文件（任意层级，同名取最新）
    source_map: dict[str, Path] = {}
    for p in _iter_report_files(root):
        if p.name not in source_map or p.stat().st_mtime > source_map[p.name].stat().st_mtime:
            source_map[p.name] = p
    for name in req.files:
        if not _safe_filename(name):
            errors.append(f"非法文件名: {name}")
            continue
        src = source_map.get(name)
        if src is None:
            errors.append(f"文件不存在: {name}")
            continue
        try:
            dest = target_dir / name
            # 已在目标文件夹则跳过
            if src.resolve() == dest.resolve():
                continue
            src.replace(dest)
            moved.append(name)
        except Exception as exc:  # noqa: BLE001 — 单个文件失败不影响批量
            errors.append(f"{name}: {exc}")
    return {"code": 200, "data": {"moved": moved, "errors": errors}}


@router.post("/files/delete")
async def delete_report_files(req: FileDeleteRequest):
    """批量删除报告文件（支持子文件夹）。"""
    root = _resolve_results_dir()
    deleted: list[str] = []
    errors: list[str] = []
    for name in req.files:
        if not _safe_filename(name):
            errors.append(f"非法文件名: {name}")
            continue
        found = False
        for p in _iter_report_files(root):
            if p.name == name:
                try:
                    p.unlink()
                    deleted.append(name)
                    found = True
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {exc}")
                break
        if not found:
            errors.append(f"文件不存在: {name}")
    return {"code": 200, "data": {"deleted": deleted, "errors": errors}}


@router.post("/files/delete-folder")
async def delete_report_folder(req: FolderRequest):
    """删除报告文件夹（含其中所有文件与子文件夹）。"""
    if not _safe_folder_path(req.folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    target = root / Path(req.folder)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"文件夹不存在: {req.folder}")
    shutil.rmtree(target, ignore_errors=True)
    return {"code": 200, "data": {"deleted": req.folder}}


@router.post("/files/create-folder")
async def create_report_folder(req: FolderRequest):
    """新建报告文件夹（支持两级路径，如「A股市场/贵州茅台」）。"""
    if not _safe_folder_path(req.folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / Path(req.folder)
    target.mkdir(parents=True, exist_ok=True)
    return {"code": 200, "data": {"created": req.folder}}
