# WMS 文件服务 — 接口文档 (PRD)

> 本文档描述 `wms-file-service` 微服务对外暴露的所有 HTTP 接口。  
> 主系统前端（HTML）通过这些接口完成文件的上传、查询和预览。  
> 服务默认运行在 **8808** 端口（Docker），与主系统（8000 端口）部署在同一台服务器上。

---

## 一、架构概览

```
┌─────────────┐        ┌───────────────────┐        ┌──────────────┐
│  前端 HTML   │──:8000──│  主系统后端 (Flask) │        │  腾讯云 COS   │
│  页面        │        │  业务逻辑          │        │  对象存储     │
└──────┬──────┘        └───────────────────┘        └──────▲───────┘
       │                                                    │
       │  :8808                                             │
       ▼                                                    │
┌───────────────────┐                                       │
│  wms-file-service │───── 上传/预览 ──────────────────────►│
│  (本项目)          │                                       │
│  FastAPI + SQLite  │                                       │
└───────────────────┘
```

- **前端** 直接调用 `:8808` 端口的文件服务接口（已开启 CORS）
- **文件服务** 负责接收文件 → 推送腾讯云 COS → 记录元数据到 SQLite
- **主系统** 不需要任何改动

---

## 二、基础信息

| 项目       | 值                              |
| ---------- | ------------------------------- |
| Base URL   | `http://<服务器IP>:8808`        |
| 协议       | HTTP（生产环境建议加 Nginx 反代 HTTPS） |
| 数据格式   | JSON                            |
| 认证方式   | 暂无（内部网络部署）             |

---

## 三、接口列表

### 接口 A — 上传文件

**`POST /api/files/upload`**

将一个文件上传到腾讯云 COS，并在本地 SQLite 中记录元数据。

#### 请求

| 类型 | Content-Type |
| ---- | ------------ |
| 表单 | `multipart/form-data` |

| 字段名    | 类型     | 必填 | 说明                                                 |
| --------- | -------- | ---- | ---------------------------------------------------- |
| `biz_type` | string  | ✅   | 业务类型，如 `inbound`（入库）、`outbound`（出库）    |
| `biz_id`   | string  | ✅   | 业务单号，如 `IN-1001`                                |
| `file_type`| string  | ❌   | 文件用途标签，默认 `attachment`。也可传 `photo`、`invoice` 等 |
| `file`     | File    | ✅   | 要上传的文件（二进制）。**前端应在上传前将文件重命名为业务文件名**（如 `INV-20260423113057_admin1_1.jpg`） |

#### COS 存储路径规则

优先使用前端已设定的文件名（经过安全清洗），降级时使用时间戳命名：

```
{biz_type}/{年}/{月}/{biz_id}/{cleaned_filename}
```

| 场景 | stored_name 生成规则 |
| ---- | -------------------- |
| 前端已提供文件名 | 取 `file.filename`，用 `re.sub(r'[^\w\u4e00-\u9fa5._-]', '_', name)` 清洗非法字符 |
| 文件名为空 | 降级为 `{file_type}_{时间戳}{扩展名}` |

示例：
- 前端命名 → `invoice/2026/04/INV-20260423113057/INV-20260423113057_admin1_1.jpg`
- 降级命名 → `invoice/2026/04/IN-1001/attachment_1713330000.jpg`

#### Product SKU 自动备份

当 `biz_type == "product"` 时，文件服务会在正常上传之外，**额外备份一份**到 SKU 根目录：

| 路径类型 | COS Key | 数据库记录 |
| -------- | ------- | ---------- |
| 正常路径 | `product/{年}/{月}/{sku}/{filename}` | ✅ 记录 |
| 备份路径 | `product/{sku}/{filename}` | ❌ 不记录 |

示例（SKU = `SKU-001`，文件名 = `photo.jpg`）：
- 正常路径：`product/2026/04/SKU-001/photo.jpg` ← 数据库记录此条
- 备份路径：`product/SKU-001/photo.jpg` ← COS 多一份，作为产品图库永久存档

> 备份路径按 SKU 组织（无年月层级），方便作为产品图库直接按 SKU 文件夹浏览。前端无需任何改动。

#### 成功响应 `200`

```json
{
  "message": "上传成功",
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

| 字段      | 说明                           |
| --------- | ------------------------------ |
| `message` | 固定为 `"上传成功"`            |
| `id`      | 文件记录的唯一 ID（UUID v4）    |

#### 错误响应

| 状态码 | 场景           | 示例                                      |
| ------ | -------------- | ----------------------------------------- |
| `400`  | 上传了空文件   | `{"detail": "文件为空"}`                  |
| `422`  | 缺少必填字段   | FastAPI 自动返回校验错误                   |
| `500`  | COS 上传失败   | `{"detail": "上传腾讯云失败: ..."}`       |

#### 前端调用示例

```javascript
const formData = new FormData();
formData.append("biz_type", "inbound");
formData.append("biz_id", "IN-1001");
formData.append("file_type", "photo");
formData.append("file", fileInput.files[0]);

const res = await fetch("http://服务器IP:8808/api/files/upload", {
  method: "POST",
  body: formData,
});
const data = await res.json();
console.log("文件ID:", data.id);
```

---

### 接口 B — 查询文件列表

**`GET /api/files`**

查询某个业务单据下关联的所有文件。

#### 请求参数（Query String）

| 参数名    | 类型   | 必填 | 说明                        |
| --------- | ------ | ---- | --------------------------- |
| `biz_type` | string | ✅  | 业务类型，如 `inbound`      |
| `biz_id`   | string | ✅  | 业务单号，如 `IN-1001`      |

#### 成功响应 `200`

```json
[
  {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "original_name": "发票扫描件.pdf",
    "created_at": "2026-04-18T08:30:00+00:00"
  },
  {
    "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "original_name": "货物照片.jpg",
    "created_at": "2026-04-18T08:25:00+00:00"
  }
]
```

| 字段            | 说明               |
| --------------- | ------------------ |
| `id`            | 文件记录 UUID       |
| `original_name` | 上传时的原始文件名  |
| `created_at`    | 上传时间（UTC ISO） |

> 返回结果按 `created_at` 倒序排列（最新的在前面）

#### 前端调用示例

```javascript
const res = await fetch(
  "http://服务器IP:8808/api/files?biz_type=inbound&biz_id=IN-1001"
);
const files = await res.json();
files.forEach((f) => {
  console.log(f.original_name, f.created_at);
});
```

---

### 接口 C — 获取预览/下载链接

**`GET /api/files/{file_id}/preview`**

根据文件 ID 生成一个**有效期 1 小时**的腾讯云 COS 预签名 URL，可直接在浏览器中打开预览或下载。

#### 路径参数

| 参数名    | 类型   | 说明           |
| --------- | ------ | -------------- |
| `file_id` | string | 文件记录的 UUID |

#### 成功响应 `200`

```json
{
  "url": "https://baozehang-1416231675.cos.ap-singapore.myqcloud.com/invoice/2026/04/INV-20260423113057/INV-20260423113057_admin1_1.jpg?sign=...&response-content-disposition=attachment%3B%20filename%2A%3DUTF-8%27%27INV-20260423113057_admin1_1.jpg"
}
```

| 字段  | 说明                                     |
| ----- | ---------------------------------------- |
| `url` | 带签名的 COS 临时访问链接（1 小时有效），附带 `response-content-disposition` 参数，浏览器下载时文件名自动显示为原始文件名 |

> **下载文件名说明**：预签名 URL 携带 `response-content-disposition: attachment; filename*=UTF-8''<RFC5987编码文件名>`，支持中文及特殊字符，浏览器点击直接显示正确文件名（无需手动重命名）。

#### 错误响应

| 状态码 | 场景         | 示例                          |
| ------ | ------------ | ----------------------------- |
| `404`  | 文件不存在   | `{"detail": "文件不存在"}`    |

#### 前端调用示例

```javascript
const res = await fetch(
  `http://服务器IP:8808/api/files/${fileId}/preview`
);
const data = await res.json();
window.open(data.url, "_blank"); // 新标签页预览
```

---

## 四、数据库模型 — `file_assets` 表

本服务使用 SQLite，数据库文件自动生成在运行目录下（`file_records.db`）。

| 字段名         | 类型    | 约束              | 说明                     |
| -------------- | ------- | ----------------- | ------------------------ |
| `id`           | String  | PK, Index         | UUID v4                  |
| `biz_type`     | String  | NOT NULL, Index   | 业务类型                 |
| `biz_id`       | String  | NOT NULL, Index   | 业务单号                 |
| `file_type`    | String  | Nullable          | 文件用途标签              |
| `original_name`| String  | Nullable          | 原始文件名               |
| `stored_name`  | String  | Nullable          | COS 上存储的文件名        |
| `cos_key`      | Text    | NOT NULL, Unique  | COS 对象完整路径          |
| `bucket`       | String  | NOT NULL          | COS Bucket 名            |
| `region`       | String  | NOT NULL          | COS 区域                 |
| `mime_type`    | String  | Nullable          | MIME 类型                 |
| `size_bytes`   | Integer | Nullable          | 文件大小（字节）          |
| `created_at`   | String  | NOT NULL          | 创建时间（UTC ISO 8601） |

---

## 五、与主系统协作说明

### 5.1 部署拓扑

两个服务部署在**同一台服务器**上，占用不同端口：

| 服务             | 端口  | 框架    | 职责           |
| ---------------- | ----- | ------- | -------------- |
| WMS 主系统        | 8000  | Flask   | 业务逻辑       |
| wms-file-service | 8808  | FastAPI | 文件上传/预览（Docker） |

### 5.2 前端调用流程

```
用户点击"上传附件"
      │
      ▼
前端 JS 用 FormData 发 POST 到 :8808/api/files/upload
      │
      ▼
文件服务返回 { id: "xxx" }
      │
      ▼
前端可选择把 file_id 存到主系统的业务表里（调主系统接口）
      │
      ▼
用户点击"查看附件"
      │
      ▼
前端 GET :8808/api/files?biz_type=inbound&biz_id=IN-1001
      │
      ▼
前端拿到文件列表后，点击某一个 → GET :8808/api/files/{id}/preview
      │
      ▼
拿到预签名 URL → 新窗口打开预览
```

### 5.3 Nginx 反代配置建议（可选）

如果你希望前端只访问一个域名/端口，可以用 Nginx 按路径分流：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 文件服务相关请求 → 转发到 8808
    location /api/files {
        proxy_pass http://127.0.0.1:8808;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 50m;
    }

    # 其他请求 → 转发到主系统 8000
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

这样前端所有请求都走 `:80`，Nginx 自动按路径分发到不同服务。

---

## 六、快速启动

```bash
# 1. 克隆项目
git clone https://github.com/ZehangBAO/wms-file-service.git
cd wms-file-service

# 2. 创建并配置环境变量
cp .env.example .env
# 编辑 .env，填入真实的腾讯云密钥

# 3. 安装依赖
pip install -r requirements.txt

# 4. Docker 启动（推荐）
docker compose up -d --build

# 或直接运行
# uvicorn main:app --host 0.0.0.0 --port 8808
```

启动后自动生成 `file_records.db`，服务即可接收请求。

启动后：
- 访问 `http://服务器IP:8808` → 文件管理前端页面（上传 / 查询 / 预览）
- 访问 `http://服务器IP:8808/docs` → Swagger 自动生成的交互式 API 文档

---

## 七、前端页面

服务内置了一个轻量管理页面 `static/index.html`，访问根路径自动跳转。

功能：
- **上传文件**：选择业务类型、填写单号、拖拽/选择文件后一键上传
- **查询列表**：按业务类型 + 单号查询关联的所有文件
- **在线预览**：点击预览按钮，新窗口打开 COS 预签名链接

前端页面通过 `window.location.origin` 自动获取 API 地址，无需手动配置。
