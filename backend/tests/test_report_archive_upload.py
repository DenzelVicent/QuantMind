"""report_archive 报告 PDF 上传接口单元测试。

覆盖：合法上传（根/子文件夹）、非 PDF 扩展名拒绝、伪装内容拒绝、
目录穿越清洗、同名自动加时间戳后缀。
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.routers.report_archive import router

_UPLOAD_PATH = "/api/v1/reports/files/upload"
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_REPORT_ARCHIVE_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _upload(client, filename, content=PDF_BYTES, media_type="application/pdf", folder=None):
    data = {"folder": folder} if folder is not None else {}
    return client.post(
        _UPLOAD_PATH,
        files={"file": (filename, content, media_type)},
        data=data,
    )


def test_upload_pdf_to_root(client, tmp_path):
    resp = _upload(client, "贵州茅台600519_2026-08-21_投研分析报告.pdf")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["filename"].endswith(".pdf")
    assert body["folder"] == ""
    assert (tmp_path / body["filename"]).read_bytes() == PDF_BYTES


def test_upload_pdf_to_folder(client, tmp_path):
    resp = _upload(client, "report.pdf", folder="A股市场/贵州茅台")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["folder"] == "A股市场/贵州茅台"
    assert (tmp_path / "A股市场" / "贵州茅台" / body["filename"]).exists()


def test_upload_non_pdf_extension_rejected(client):
    resp = _upload(client, "report.txt", content=PDF_BYTES)
    assert resp.status_code == 400
    assert "PDF" in resp.json()["detail"]


def test_upload_fake_content_rejected(client):
    resp = _upload(client, "report.pdf", content=b"not a pdf at all", media_type="application/pdf")
    assert resp.status_code == 400
    assert "不是 PDF" in resp.json()["detail"]


def test_upload_strips_directory_from_filename(client, tmp_path):
    """文件名带目录前缀时只取 basename，防穿越。"""
    resp = _upload(client, "../evil.pdf", media_type="application/pdf")
    assert resp.status_code == 200
    saved = resp.json()["data"]["filename"]
    assert saved == "evil.pdf"
    assert (tmp_path / "evil.pdf").exists()
    # 未逃逸到上级目录
    assert not (tmp_path.parent / "evil.pdf").exists()


def test_upload_duplicate_filename_suffixes(client, tmp_path):
    name = "600519_2026-08-21_投研分析报告.pdf"
    r1 = _upload(client, name)
    r2 = _upload(client, name)
    assert r1.status_code == 200
    assert r2.status_code == 200
    f1 = r1.json()["data"]["filename"]
    f2 = r2.json()["data"]["filename"]
    assert f1 != f2
    assert (tmp_path / f1).exists()
    assert (tmp_path / f2).exists()


def test_upload_invalid_folder_rejected(client):
    resp = _upload(client, "report.pdf", folder="../潜水")
    assert resp.status_code == 400
    assert "文件夹" in resp.json()["detail"]
