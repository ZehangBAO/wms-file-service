# main.py — WMS 文件上传微服务
import io
import os
import re
import time
import uuid
import urllib.parse
from datetime import datetime, timezone
from dotenv import load_dotenv
from PIL import Image

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from qcloud_cos import CosConfig, CosS3Client

# ================= 1. 加载环境变量 =================
load_dotenv()
TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID", "")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "")
COS_REGION = os.getenv("COS_REGION", "ap-singapore")
COS_BUCKET = os.getenv("COS_BUCKET", "baozehang-1416231675")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./file_records.db")

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


# ================= 3. FastAPI 路由配置 =================
app = FastAPI(title="内部文件上传服务")

# 必须开启 CORS，允许你主系统前端跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境可以改成你的主系统域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 根路径跳转到前端页面
@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


# 挂载静态文件（前端页面）
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# 接口A：接收上传文件并推送到 COS
@app.post("/api/files/upload")
async def upload_file(
    biz_type: str = Form(...),
    biz_id: str = Form(...),
    file_type: str = Form("attachment"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件为空")

    # 1. 组装 COS 路径，优先使用前端已重命名的文件名
    now = datetime.now()
    ext = os.path.splitext(file.filename or "file")[1].lower() or ".bin"
    ts = int(time.time())
    raw_name = (file.filename or "").strip()
    safe_name = re.sub(r'[^\w\u4e00-\u9fa5._-]', '_', raw_name)
    base_name = safe_name if safe_name else f"{file_type}_{ts}{ext}"

    stem, suffix = os.path.splitext(base_name)
    stored_name = base_name
    counter = 1
    base_dir = f"{biz_type}/{now.year}/{now.month:02d}/{biz_id}"
    while db.query(FileAsset).filter(FileAsset.cos_key == f"{base_dir}/{stored_name}").first():
        stored_name = f"{stem}_{counter}{suffix}"
        counter += 1
    cos_key = f"{base_dir}/{stored_name}"

    # 2. 上传至腾讯云 COS
    is_image = (file.content_type or "").startswith("image/")
    image_urls: dict = {}
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
            safe_biz_id = re.sub(r'[^\w-]', '_', biz_id)
            with Image.open(io.BytesIO(file_bytes)) as img:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                for size_name, (max_w, max_h) in SIZE_SPECS.items():
                    resized = img.copy()
                    resized.thumbnail((max_w, max_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=85, optimize=True)
                    sized_bytes = buf.getvalue()

                    # 主路径：含日期层级 + 时间戳，每次唯一，DB 记录 original 这条
                    primary_key = f"{base_dir}/{ts_str}_{size_name}.jpg"
                    # latest：覆盖写，始终是最新上传的“主图”，无需知道日期就能找到
                    latest_key  = f"product/{safe_biz_id}/latest/{size_name}.jpg"
                    # archive：每次上传独立子目录，永不覆盖，保留历史
                    archive_key = f"product/{safe_biz_id}/archive/{ts_str}/{size_name}.jpg"

                    for key in (primary_key, latest_key, archive_key):
                        cos_client.put_object(Bucket=COS_BUCKET, Body=sized_bytes, Key=key, ContentType="image/jpeg")

                    # 返回 archive 路径（唯一不变，适合 WMS 主系统存储）
                    image_urls[size_name] = archive_key

            # DB 记录指向 original 主路径（含日期，唯一）
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传腾讯云失败: {str(e)}")

    # 3. 记录存入 SQLite 数据库
    record = FileAsset(
        id=str(uuid.uuid4()),
        biz_type=biz_type,
        biz_id=biz_id,
        file_type=file_type,
        original_name=original_filename,   # 用户上传的原始文件名
        stored_name=stored_name,           # COS 中实际存储的文件名
        cos_key=cos_key,
        bucket=COS_BUCKET,
        region=COS_REGION,
        mime_type="image/jpeg" if image_urls else (file.content_type or "application/octet-stream"),
        size_bytes=len(file_bytes),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(record)
    db.commit()

    result: dict = {"message": "上传成功", "id": record.id}
    if image_urls:
        cos_base = f"https://{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
        # 返回 archive URL（唯一不变）供 WMS 写入产品表；同时返回 latest URL 供页面展示
        result["image_original_url"] = f"{cos_base}/{image_urls['original']}"
        result["image_medium_url"]   = f"{cos_base}/{image_urls['medium']}"
        result["image_thumb_url"]    = f"{cos_base}/{image_urls['thumb']}"
        result["image_latest_thumb"] = f"{cos_base}/product/{safe_biz_id}/latest/thumb.jpg"
    return result


# 接口B：查询某单据下的文件列表
@app.get("/api/files")
def list_files(biz_type: str, biz_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(FileAsset)
        .filter(FileAsset.biz_type == biz_type, FileAsset.biz_id == biz_id)
        .order_by(FileAsset.created_at.desc())
        .all()
    )

    return [
        {"id": r.id, "original_name": r.original_name, "created_at": r.created_at}
        for r in rows
    ]


# 接口D：上传产品图片（自动压缩生成 3 种尺寸，写入 latest/ + archive/{ts}/ 两个层级）
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

                # latest：覆盖写，始终是最新的“主图”——无需知道任何日期就能找到
                latest_key  = f"product/{safe_sku}/latest/{size_name}.jpg"
                # archive：每次上传独立子目录，永不覆盖，保留历史
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片处理或上传失败: {str(e)}")

    cos_base = f"https://{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
    return {
        "success": True,
        "sku": sku,
        # archive URL：唯一不变，适合 WMS 主系统存储到 products 表
        "image_original_url": f"{cos_base}/{archive_keys['original']}",
        "image_medium_url":   f"{cos_base}/{archive_keys['medium']}",
        "image_thumb_url":    f"{cos_base}/{archive_keys['thumb']}",
        # latest URL：始终指向最新上传的主图，无需更新 WMS DB 就可直接展示
        "image_latest_thumb":    f"{cos_base}/product/{safe_sku}/latest/thumb.jpg",
        "image_latest_medium":   f"{cos_base}/product/{safe_sku}/latest/medium.jpg",
        "image_latest_original": f"{cos_base}/product/{safe_sku}/latest/original.jpg",
    }


# 接口C：获取安全预览链接
@app.get("/api/files/{file_id}/preview")
def preview_file(file_id: str, db: Session = Depends(get_db)):
    row = db.query(FileAsset).filter(FileAsset.id == file_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")

    disp_name = urllib.parse.quote(row.original_name or row.stored_name or "file", safe="")
    url = cos_client.get_presigned_url(
        Method="GET", Bucket=row.bucket, Key=row.cos_key, Expired=3600,
        Params={"response-content-disposition": f"attachment; filename*=UTF-8''{disp_name}"},
    )
    return {"url": url}
