# wms-file-service

> WMS 仓储管理系统 — 独立文件上传微服务

基于 **FastAPI + 腾讯云 COS + SQLite** 的轻量文件服务，负责文件上传、存储和预览链接生成。  
与 WMS 主系统（Flask）部署在同一台服务器上，通过不同端口协作。

## 项目结构

```
wms-file-service/
├── main.py              # 核心服务代码（上传 / 查询 / 预览）
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
├── .gitignore           # Git 忽略规则
├── Dockerfile           # Docker 镜像构建
├── docker-compose.yml   # Docker Compose 配置
├── static/
│   └── index.html       # 前端管理页面
└── docs/
    └── FILE-SERVICE-API.md  # 接口文档（PRD）
```

## 快速启动

```bash
# 1. 克隆
git clone https://github.com/ZehangBAO/wms-file-service.git
cd wms-file-service

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入腾讯云 SecretId / SecretKey

# 3. 安装依赖
pip install -r requirements.txt

# 4. Docker 启动（推荐，端口 8808）
docker compose up -d --build

# 或直接运行
uvicorn main:app --host 0.0.0.0 --port 8808
```

启动后：
- 访问 `http://服务器IP:8808` → 文件管理前端页面
- 访问 `http://服务器IP:8808/docs` → Swagger 交互式 API 文档

## API 概览

| 方法   | 路径                         | 说明           |
| ------ | ---------------------------- | -------------- |
| POST   | `/api/files/upload`          | 上传文件到 COS |
| GET    | `/api/files`                 | 查询文件列表   |
| GET    | `/api/files/{file_id}/preview` | 获取预览链接 |

详细接口说明见 [`docs/FILE-SERVICE-API.md`](docs/FILE-SERVICE-API.md)。

## 与主系统协作

| 服务             | 端口 | 框架    | 职责         |
| ---------------- | ---- | ------- | ------------ |
| WMS 主系统        | 8000 | Flask   | 业务逻辑     |
| wms-file-service | 8808 | FastAPI | 文件上传/预览（Docker） |

前端页面直接调用 `:8808` 的文件接口（已启用 CORS），无需主系统做任何改动。