# main.py — WMS 文件上传微服务
import io
import os
import re
import time
import uuid
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from PIL import Image

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from qcloud_cos import CosConfig, CosS3Client

# ================= 0. 日志 =================
# 注意：严禁打印 COS SecretId/SecretKey、JWT secret 等任何密钥
logger = logging.getLogger("wms-file-service")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger.setLevel(logging.INFO)

# ================= 1. 加载环境变量 =================
load_dotenv()
TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID", "")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "")
COS_REGION = os.getenv("COS_REGION", "ap-singapore")
COS_BUCKET = os.getenv("COS_BUCKET", "baozehang-1416231675")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./file_records.db")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 预签名 URL 有效期（秒），缺省 3600；缓存 TTL（备用），缺省 3000
COS_SIGNED_URL_EXPIRE_SECONDS = _env_int("COS_SIGNED_URL_EXPIRE_SECONDS", 3600)
IMAGE_CACHE_TTL_SECONDS = _env_int("IMAGE_CACHE_TTL_SECONDS", 3000)

# CORS 白名单：默认允许主系统 + 文件子域 + 本地开发；不要使用 "*" 搭配 credentials=True
_DEFAULT_CORS_ORIGINS = (
    "https://api.zedabeauty.uk,https://file.zedabeauty.uk,"
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:8808,http://127.0.0.1:8808"
)
CORS_ALLOW_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",") if o.strip()
]

# 初始化 COS 客户端
cos_config = CosConfig(Region=COS_REGION, SecretId=TENCENT_SECRET_ID, SecretKey=TENCENT_SECRET_KEY)
cos_client = CosS3Client(cos_config)

# ================= 2. 数据库配置 =================
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 定义文件记录表
class FileAsset(Base):
    __tablename__ = "file_assets"
    id = Column(String, primary_key=True, index=True)
    biz_type = Column(String, nullable=False, index=True)
    biz_id = Column(String, nullable=False, index=True)
    file_type = Column(String, nullable=True)
    original_name = Column(String, nullable=True)
    stored_name = Column(String, nullable=True)
    cos_key = Column(Text, nullable=False, unique=True)
    bucket = Column(String, nullable=False)
    region = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(String, nullable=False)


# 自动创建表
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ================= 辅助函数 =================
def _resolve_biz(biz_type, business_type, biz_id, business_id):
    """兼容旧字段 business_type/business_id，映射到 biz_type/biz_id。
    缺失时抛 422（清晰错误），绝不让其变成 500。"""
    bt = (biz_type or business_type or "").strip()
    bi = (biz_id or business_id or "").strip()
    if not bt or not bi:
        raise HTTPException(status_code=422, detail="biz_type 和 biz_id 不能为空")
    return bt, bi


def _safe_presign(bucket: str, key: str, original_name: Optional[str] = None,
                  expire: Optional[int] = None) -> Optional[str]:
    """生成 COS 预签名 URL；失败仅记录日志并返回 None，不抛异常。"""
    try:
        params = {}
        if original_name:
            disp = urllib.parse.quote(original_name, safe="")
            params["response-content-disposition"] = f"attachment; filename*=UTF-8''{disp}"
        return cos_client.get_presigned_url(
            Method="GET",
            Bucket=bucket,
            Key=key,
            Expired=expire or COS_SIGNED_URL_EXPIRE_SECONDS,
            Params=params,
        )
    except Exception:
        logger.exception("生成预签名 URL 失败 key=%s", key)
        return None


# ================= 3. FastAPI 路由配置 =================
app = FastAPI(title="内部文件上传服务")

# CORS：显式白名单 + 允许 Authorization / multipart；OPTIONS 预检自动成功
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
)


# 根路径跳转到前端页面
@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


# 挂载静态文件（前端页面）
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# ════════════ 接口A：接收上传文件并推送到 COS ════════════
# 主路由 /api/files/upload，兼容 alias /upload
@app.post("/api/files/upload")
@app.post("/upload")
async def upload_file(
    biz_type: Optional[str] = Form(None),
    biz_id: Optional[str] = Form(None),
    business_type: Optional[str] = Form(None),  # 旧字段兼容
    business_id: Optional[str] = Form(None),    # 旧字段兼容
    file_type: str = Form("attachment"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    biz_type, biz_id = _resolve_biz(biz_type, business_type, biz_id, business_id)

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件为空")

    logger.info(
        "上传开始 biz_type=%s biz_id=%s filename=%s file_type=%s size=%d",
        biz_type, biz_id, file.filename, file_type, len(file_bytes),
    )

    # 1. 组装 COS 路径，优先使用前端已重命名的文件名
    now = datetime.now()
    ext = os.path.splitext(file.filename or "file")[1].lower() or ".bin"
    ts = int(time.time())
    raw_name = (file.filename or "").strip()
    safe_name = re.sub(r'[^\w一-龥._-]', '_', raw_name)
    base_name = safe_name if safe_name else f"{file_type}_{ts}{ext}"

    stem, suffix = os.path.splitext(base_name)
    stored_name = base_name
    counter = 1
    base_dir = f"{biz_type}/{now.year}/{now.month:02d}/{biz_id}"
    while db.query(FileAsset).filter(FileAsset.cos_key == f"{base_dir}/{stored_name}").first():
        stored_name = f"{stem}_{counter}{suffix}"
        counter += 1
    cos_key = f"{base_dir}/{stored_name}"

    # 2. 上传至腾讯云 COS（先 COS 后 DB，COS 失败不写 DB，避免脏记录）
    is_image = (file.content_type or "").startswith("image/")
    image_urls: dict = {}
    safe_biz_id = re.sub(r'[^\w-]', '_', biz_id)
    ts_str = now.strftime("%Y%m%d%H%M%S")  # 每次请求唯一时间戳
    original_filename = base_name           # 保留用户上传的原始文件名

    try:
        if biz_type == "product" and is_image:
            # 产品图片：压缩生成 3 种尺寸，每种写 3 个 COS 路径
            SIZE_SPECS = {
                "original": (1200, 1200),
                "medium":   (600,  600),
                "thumb":    (160,  160),
            }
            with Image.open(io.BytesIO(file_bytes)) as img:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                for size_name, (max_w, max_h) in SIZE_SPECS.items():
                    resized = img.copy()
                    resized.thumbnail((max_w, max_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=85, optimize=True)
                    sized_bytes = buf.getvalue()

                    primary_key = f"{base_dir}/{ts_str}_{size_name}.jpg"
                    latest_key  = f"product/{safe_biz_id}/latest/{size_name}.jpg"
                    archive_key = f"product/{safe_biz_id}/archive/{ts_str}/{size_name}.jpg"

                    for key in (primary_key, latest_key, archive_key):
                        cos_client.put_object(Bucket=COS_BUCKET, Body=sized_bytes, Key=key, ContentType="image/jpeg")

                    image_urls[size_name] = archive_key

            cos_key     = f"{base_dir}/{ts_str}_original.jpg"
            stored_name = os.path.basename(cos_key)
        else:
            # 非产品图片：原有逻辑
            cos_client.put_object(
                Bucket=COS_BUCKET,
                Body=file_bytes,
                Key=cos_key,
                ContentType=file.content_type or "application/octet-stream",
            )
            if biz_type == "product":
                cos_client.put_object(
                    Bucket=COS_BUCKET,
                    Body=file_bytes,
                    Key=f"product/{biz_id}/{stored_name}",
                    ContentType=file.content_type or "application/octet-stream",
                )
    except Exception:
        logger.exception("COS 上传失败 biz_type=%s biz_id=%s filename=%s", biz_type, biz_id, file.filename)
        raise HTTPException(status_code=500, detail="COS 上传失败，请稍后重试")

    logger.info("COS 上传成功 biz_type=%s biz_id=%s cos_key=%s", biz_type, biz_id, cos_key)

    # 3. 记录存入 SQLite 数据库（commit 包 try/except + rollback）
    file_id = str(uuid.uuid4())
    mime_type = "image/jpeg" if image_urls else (file.content_type or "application/octet-stream")
    record = FileAsset(
        id=file_id,
        biz_type=biz_type,
        biz_id=biz_id,
        file_type=file_type,
        original_name=original_filename,
        stored_name=stored_name,
        cos_key=cos_key,
        bucket=COS_BUCKET,
        region=COS_REGION,
        mime_type=mime_type,
        size_bytes=len(file_bytes),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("数据库写入失败 biz_type=%s biz_id=%s cos_key=%s", biz_type, biz_id, cos_key)
        raise HTTPException(status_code=500, detail="数据库写入失败")

    logger.info("DB 写入成功 id=%s biz_type=%s biz_id=%s", file_id, biz_type, biz_id)

    # 4. 稳定返回结构（id 与 file_id 同值；url 失败则 null，不抛异常）
    preview_url = _safe_presign(COS_BUCKET, cos_key, original_filename)
    result: dict = {
        "message": "上传成功",
        "id": file_id,
        "file_id": file_id,
        "original_name": original_filename,
        "url": preview_url,
    }
    if image_urls:
        cos_base = f"https://{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
        result["image_original_url"] = f"{cos_base}/{image_urls['original']}"
        result["image_medium_url"]   = f"{cos_base}/{image_urls['medium']}"
        result["image_thumb_url"]    = f"{cos_base}/{image_urls['thumb']}"
        result["image_latest_thumb"] = f"{cos_base}/product/{safe_biz_id}/latest/thumb.jpg"
    return result


# ════════════ 接口B：查询某单据下的文件列表 ════════════
# 主路由 /api/files，兼容 alias /files；始终返回数组
@app.get("/api/files")
@app.get("/files")
def list_files(
    biz_type: Optional[str] = None,
    biz_id: Optional[str] = None,
    business_type: Optional[str] = None,  # 旧参数兼容
    business_id: Optional[str] = None,    # 旧参数兼容
    db: Session = Depends(get_db),
):
    biz_type, biz_id = _resolve_biz(biz_type, business_type, biz_id, business_id)
    rows = (
        db.query(FileAsset)
        .filter(FileAsset.biz_type == biz_type, FileAsset.biz_id == biz_id)
        .order_by(FileAsset.created_at.desc())
        .all()
    )
    # 永远返回数组，哪怕为空也返回 []
    return [
        {
            "id": r.id,
            "original_name": r.original_name,
            "file_type": r.file_type,
            "mime_type": r.mime_type,
            "size_bytes": r.size_bytes,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ════════════ 接口D：上传产品图片（COS-only，返回 image_*_url）════════════
@app.post("/files/product-image/{sku}")
async def upload_product_image(sku: str, file: UploadFile = File(...)):
    allowed_types = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="仅支持 jpg/jpeg/png/webp 格式")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(file_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件大小不能超过 8MB")

    safe_sku = re.sub(r'[^\w-]', '_', sku)
    ts_str   = datetime.now().strftime("%Y%m%d%H%M%S")

    logger.info("产品图上传开始 sku=%s filename=%s size=%d", sku, file.filename, len(file_bytes))

    SIZE_SPECS = {
        "original": (1200, 1200),
        "medium":   (600,  600),
        "thumb":    (160,  160),
    }

    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            archive_keys: dict = {}
            for size_name, (max_w, max_h) in SIZE_SPECS.items():
                resized = img.copy()
                resized.thumbnail((max_w, max_h), Image.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=85, optimize=True)
                img_bytes = buf.getvalue()

                latest_key  = f"product/{safe_sku}/latest/{size_name}.jpg"
                archive_key = f"product/{safe_sku}/archive/{ts_str}/{size_name}.jpg"

                for key in (latest_key, archive_key):
                    cos_client.put_object(
                        Bucket=COS_BUCKET,
                        Body=img_bytes,
                        Key=key,
                        ContentType="image/jpeg",
                    )

                archive_keys[size_name] = archive_key

    except HTTPException:
        raise
    except Exception:
        logger.exception("产品图处理或上传失败 sku=%s", sku)
        raise HTTPException(status_code=500, detail="图片处理或上传失败，请稍后重试")

    logger.info("产品图上传成功 sku=%s ts=%s", sku, ts_str)

    cos_base = f"https://{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
    return {
        "success": True,
        "sku": sku,
        "image_original_url": f"{cos_base}/{archive_keys['original']}",
        "image_medium_url":   f"{cos_base}/{archive_keys['medium']}",
        "image_thumb_url":    f"{cos_base}/{archive_keys['thumb']}",
        "image_latest_thumb":    f"{cos_base}/product/{safe_sku}/latest/thumb.jpg",
        "image_latest_medium":   f"{cos_base}/product/{safe_sku}/latest/medium.jpg",
        "image_latest_original": f"{cos_base}/product/{safe_sku}/latest/original.jpg",
    }


# ════════════ 接口C：获取安全预览链接 ════════════
# 主路由 /api/files/{file_id}/preview，兼容 alias /preview/{file_id}
@app.get("/api/files/{file_id}/preview")
@app.get("/preview/{file_id}")
def preview_file(file_id: str, db: Session = Depends(get_db)):
    row = db.query(FileAsset).filter(FileAsset.id == file_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")

    url = _safe_presign(row.bucket, row.cos_key, row.original_name or row.stored_name)
    if not url:
        # 签名失败：返回受控错误，不让异常冒泡成 500
        raise HTTPException(status_code=502, detail="预览链接生成失败")

    logger.info("预览 URL 生成成功 id=%s", file_id)
    return {
        "url": url,
        "original_name": row.original_name,
        "mime_type": row.mime_type,
    }
