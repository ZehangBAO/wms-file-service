"""Pytest 配置：用临时 SQLite + 假 COS 客户端，避免真连腾讯云。"""
import os
import sys
import asyncio
import tempfile
import pathlib

# 1) 让 `import main` 找得到微服务根目录
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 2) 在 import main 之前，把 DB 指向临时文件 + 提供假凭据
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ.setdefault("TENCENT_SECRET_ID", "test-id")
os.environ.setdefault("TENCENT_SECRET_KEY", "test-key")
os.environ.setdefault("COS_BUCKET", "test-bucket")
os.environ.setdefault("COS_REGION", "ap-singapore")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import main as fs_main  # noqa: E402


class FakeCos:
    """记录 put_object，返回可预测的预签名 URL；不发任何网络请求。"""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket=None, Body=None, Key=None, ContentType=None, **kw):
        self.objects[Key] = Body
        return {"ETag": "fake-etag"}

    def get_presigned_url(self, Method=None, Bucket=None, Key=None, Expired=None, Params=None, **kw):
        return f"https://fake.cos.local/{Key}?sign=1"


@pytest.fixture(autouse=True)
def fake_cos(monkeypatch):
    fake = FakeCos()
    monkeypatch.setattr(fs_main, "cos_client", fake)
    # 每个用例重建表，互相隔离
    fs_main.Base.metadata.drop_all(bind=fs_main.engine)
    fs_main.Base.metadata.create_all(bind=fs_main.engine)
    yield fake


def req(method: str, url: str, **kwargs):
    """同步包装 ASGI 请求。"""
    async def _do():
        transport = ASGITransport(app=fs_main.app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.request(method, url, **kwargs)

    return asyncio.run(_do())
