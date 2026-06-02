"""文件微服务接口测试：字段兼容 / 稳定返回 / 错误处理 / alias。"""
from tests.conftest import req


def _upload(data=None, filename="a.txt", content=b"hello", ctype="text/plain", url="/api/files/upload"):
    return req("POST", url, data=data or {}, files={"file": (filename, content, ctype)})


# ── 查询：无数据返回 [] ──────────────────────────────────────
def test_list_empty_returns_array():
    r = req("GET", "/api/files", params={"biz_type": "invoice", "biz_id": "NOPE"})
    assert r.status_code == 200
    assert r.json() == []


# ── 上传：缺字段返回 422，不是 500 ───────────────────────────
def test_upload_missing_fields_returns_422():
    r = _upload(data={})  # 无 biz_type / biz_id
    assert r.status_code == 422


# ── 上传：新字段正常 + 返回结构稳定（id == file_id）──────────
def test_upload_new_fields_stable_response():
    r = _upload(data={"biz_type": "invoice", "biz_id": "INV-1", "file_type": "receipt"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "上传成功"
    assert body["id"] and body["id"] == body["file_id"]
    assert body["original_name"] == "a.txt"
    assert "url" in body  # 可能是 signed url 或 null，但字段必须存在


# ── 上传 + 查询：兼容旧字段 business_type / business_id ───────
def test_legacy_fields_upload_and_list():
    up = _upload(data={"business_type": "invoice", "business_id": "OLD-1"})
    assert up.status_code == 200

    r = req("GET", "/api/files", params={"business_type": "invoice", "business_id": "OLD-1"})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["original_name"] == "a.txt"
    assert rows[0]["mime_type"] == "text/plain"


# ── 预览：文件不存在返回 404 JSON ────────────────────────────
def test_preview_not_found_404():
    r = req("GET", "/api/files/does-not-exist/preview")
    assert r.status_code == 404
    assert r.json() == {"detail": "文件不存在"}


# ── 预览：返回 url / original_name / mime_type ────────────────
def test_preview_shape():
    up = _upload(data={"biz_type": "invoice", "biz_id": "INV-2"})
    fid = up.json()["id"]
    r = req("GET", f"/api/files/{fid}/preview")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"url", "original_name", "mime_type"}
    assert body["url"].startswith("https://fake.cos.local/")
    assert body["original_name"] == "a.txt"
    assert body["mime_type"] == "text/plain"


# ── 产品图接口必须是 POST，误用 GET 返回 405 ────────────────
def test_product_image_get_returns_405():
    r = req("GET", "/files/product-image/SKU-1")
    assert r.status_code == 405


# ── alias 路由与主路由结构一致 ───────────────────────────────
def test_alias_upload_list_preview():
    # POST /upload
    up = _upload(data={"biz_type": "inbound", "biz_id": "AL-1"}, url="/upload")
    assert up.status_code == 200
    fid = up.json()["id"]

    # GET /files
    lst = req("GET", "/files", params={"biz_type": "inbound", "biz_id": "AL-1"})
    assert lst.status_code == 200 and len(lst.json()) == 1

    # GET /preview/{id}
    pv = req("GET", f"/preview/{fid}")
    assert pv.status_code == 200
    assert set(pv.json().keys()) == {"url", "original_name", "mime_type"}
