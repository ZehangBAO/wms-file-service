# main.py — WMS 文件上传微服务
import os
import time
import uuid
from datetime import datetime, timezone
from dotenv import load_dotenv

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

    # 1. 组装 COS 路径 (例如: inbound/2026/04/IN-1001/attachment_1713330000.jpg)
    now = datetime.now()
    ext = os.path.splitext(file.filename)[1].lower() or ".bin"
    ts = int(time.time())
    stored_name = f"{file_type}_{ts}{ext}"
    cos_key = f"{biz_type}/{now.year}/{now.month:02d}/{biz_id}/{stored_name}"

    # 2. 上传至腾讯云 COS
    try:
        cos_client.put_object(
            Bucket=COS_BUCKET,
            Body=file_bytes,
            Key=cos_key,
            ContentType=file.content_type,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传腾讯云失败: {str(e)}")

    # 3. 记录存入 SQLite 数据库
    record = FileAsset(
        id=str(uuid.uuid4()),
        biz_type=biz_type,
        biz_id=biz_id,
        file_type=file_type,
        original_name=file.filename,
        stored_name=stored_name,
        cos_key=cos_key,
        bucket=COS_BUCKET,
        region=COS_REGION,
        mime_type=file.content_type,
        size_bytes=len(file_bytes),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(record)
    db.commit()

    return {"message": "上传成功", "id": record.id}


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


# 接口C：获取安全预览链接
@app.get("/api/files/{file_id}/preview")
def preview_file(file_id: str, db: Session = Depends(get_db)):
    row = db.query(FileAsset).filter(FileAsset.id == file_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 签发 1 小时有效的私有读取链接
    url = cos_client.get_presigned_url(
        Method="GET", Bucket=row.bucket, Key=row.cos_key, Expired=3600
    )
    return {"url": url}
