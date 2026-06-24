# 62.234.165.125 —— 幻觉维基百科

一个由 AI 动态生成的"幻觉维基百科"，所有页面均由大语言模型实时创造，支持缓存和二次修正。无论是真实存在的概念还是完全虚构的条目，AI 都会以严肃专业的百科语调进行撰写，打造一个亦真亦幻的知识宇宙。

## 功能特性

- **动态页面生成**：访问 `/wiki/页面标题` 动态生成 Wikipedia 风格的 HTML 页面（允许虚构）
- **智能缓存**：首次生成后自动缓存为静态 HTML，后续访问直接返回，缓存 TTL 可配置（默认 10 分钟）
- **异步任务机制**：通过 `/loading?next=...` 提供加载动画页面，后台异步生成内容，轮询获取结果
- **搜索结果页**：`/search?q=关键词` 生成包含虚构条目的搜索结果页，搜索结果摘要自动缓存
- **随机条目**：`/random` 随机跳转到预置词库中的条目
- **插图生成**：可选启用 AI 插图（通过 AIHubMix），在条目中自动插入与内容相关的图片
- **HTML 格式校验**：自动校验 AI 生成的 HTML 标签闭合，确保页面结构正确
- **模板化布局**：首页 `/` 和 `/about` 使用预加载模板，统一视觉风格
- **异步高性能**：基于 FastAPI + httpx 异步架构
- **配置分离**：所有 AI 配置和提示词独立文件管理，便于维护

## 技术栈

| 组件 | 技术 |
|------|------|
| Web 框架 | [FastAPI](https://fastapi.tiangolo.com/) |
| 服务器 | [Uvicorn](https://www.uvicorn.org/) |
| HTTP 客户端 | [httpx](https://www.python-httpx.org/) (异步) |
| 文本生成 | DeepSeek API (OpenAI 兼容) |
| 图片生成 | AIHubMix API (可选) |
| 环境管理 | python-dotenv |
| 图片处理 | Pillow |

## 项目结构

```
wiki/
├── main.py              # 应用入口，FastAPI 路由、缓存管理、任务调度
├── config.py            # 配置项（API Key、模型、缓存 TTL、随机词库）
├── prompts.py           # AI 提示词模板（生成、审核、搜索）
├── html_validator.py    # HTML 标签闭合校验
├── image_generator.py   # AIHubMix 图片生成模块
├── requirements.txt     # Python 依赖
├── Dockerfile           # Docker 镜像构建文件
├── docker-compose.yml   # Docker 一键部署配置
├── .env.example         # 环境变量模板
├── .gitignore           # Git 忽略规则
├── templates/           # HTML 模板
│   ├── layout.html      # 统一页面布局
│   ├── home_content.html # 首页内容
│   ├── about_content.html # 关于页面内容
│   └── loading.html     # 生成加载动画页
├── wiki/                # 页面缓存目录（自动生成，由 .gitignore 忽略）
└── wiki_image/          # 插图缓存目录（自动生成，由 .gitignore 忽略）
```

## 部署方式

项目支持两种部署方式：本地开发部署（适合调试）和 Docker 部署（适合服务器，一键启动，无需手动配置 nginx/systemd）。

### 方式一：本地开发部署

#### 环境要求

- **Python 3.10+**（推荐 3.12+）
- **pip**（Python 包管理器）
- **DeepSeek API Key**（必需，用于文本生成。也可替换为其他 OpenAI 兼容接口，见下文）
- **AIHubMix API Key**（可选，用于插图生成。也可替换为其他图片生成服务，见下文）

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd wiki
```

### 2. 创建虚拟环境

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

复制环境变量模板并填入你的 API Key：

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入必要配置：

```env
# 必填：DeepSeek API 配置
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# 可选：AIHubMix 图片生成配置（不填则跳过插图生成）
# AIHUBMIX_API_KEY=your_api_key_here
# AIHUBMIX_BASE_URL=https://aihubmix.com/v1
# AIHUBMIX_MODELS=gemini-3.1-flash-image-preview

# 可选：页面缓存过期时间（秒），默认 600（10 分钟）
# CACHE_TTL_SECONDS=600
```

### 替换 API 服务商

项目默认使用 **DeepSeek** 进行文本生成、**AIHubMix** 进行图片生成，但你完全可以替换为其他服务商。

#### 替换文本生成 API

文本生成使用的是 OpenAI 兼容的 `/v1/chat/completions` 接口，因此任何兼容 OpenAI 协议的 API 都可以直接替换，只需修改 `.env` 中的三个变量即可，**无需改动任何代码**：

| 服务商 | DEEPSEEK_BASE_URL | DEEPSEEK_MODEL |
|--------|-------------------|----------------|
| DeepSeek（默认） | `https://api.deepseek.com` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` / `gpt-3.5-turbo` |
| 通义千问（阿里云） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Moonshot（月之暗面） | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4` |
| 其他 OpenAI 兼容代理 | 填写对应地址 | 填写对应模型名 |

示例——改用 OpenAI：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.openai.com/v1
DEEPSEEK_MODEL=gpt-4o
```

> **注意**：虽然环境变量名仍以 `DEEPSEEK_` 开头，但实际指向的是你所配置的服务商。如果想彻底重命名变量，需要同步修改 [config.py](config.py) 和 [main.py](main.py) 中的引用。

#### 替换图片生成 API

图片生成逻辑集中在 [image_generator.py](image_generator.py) 中，目前调用的是 AIHubMix 的多模态接口（`/chat/completions` + `modalities: ["text", "image"]`）。替换为其他图片生成服务需要修改该文件，以下是几种常见方案的改造思路：

**方案一：改用 OpenAI DALL·E**

在 `image_generator.py` 中替换 `_try_generate` 函数：

```python
async def _try_generate(client, model, prompt, aspect_ratio):
    headers = {
        "Authorization": f"Bearer {AIHUBMIX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": "1792x1024",
    }
    resp = await client.post(
        f"{AIHUBMIX_BASE_URL}/images/generations",
        json=payload,
        headers=headers,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    image_url = data["data"][0]["url"]
    # 下载图片并返回 bytes
    img_resp = await client.get(image_url)
    return img_resp.content
```

然后在 `.env` 中配置：

```env
AIHUBMIX_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AIHUBMIX_BASE_URL=https://api.openai.com/v1
AIHUBMIX_MODELS=dall-e-3
```

**方案二：改用 Stable Diffusion WebUI / ComfyUI**

```python
async def _try_generate(client, model, prompt, aspect_ratio):
    payload = {
        "prompt": prompt,
        "negative_prompt": "low quality, blurry",
        "steps": 20,
        "width": 1024, "height": 576,
    }
    resp = await client.post(
        f"{AIHUBMIX_BASE_URL}/sdapi/v1/txt2img",
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return base64.b64decode(data["images"][0])
```

**方案三：完全关闭图片生成**

只需不在 `.env` 中填写 `AIHUBMIX_API_KEY`，程序会自动跳过所有插图生成逻辑。

### 5. 启动项目

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问 **http://localhost:8000** 即可看到首页。

### 6. 使用自定义端口

如需指定其他端口，修改 `--port` 参数即可：

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 3000
```

### 方式二：Docker 部署（推荐用于服务器）

Docker 部署可以省去手动配置 Python 环境、nginx 反向代理、systemd 服务的繁琐步骤，一条命令即可启动。

#### 环境要求

- **Docker** 和 **Docker Compose**（[安装指南](https://docs.docker.com/compose/install/)）

#### 1. 克隆项目并配置

```bash
git clone <your-repo-url>
cd wiki
cp .env.example .env
# 编辑 .env 填入 API Key（参考方式一中的配置说明）
```

#### 2. 一键启动

```bash
docker compose up -d
```

首次运行会自动构建镜像，之后启动只需几秒。服务默认运行在 **http://localhost:8000**。

#### 3. 常用命令

```bash
docker compose up -d        # 后台启动
docker compose down         # 停止服务
docker compose restart      # 重启服务
docker compose logs -f      # 查看实时日志
docker compose pull         # 拉取最新基础镜像
docker compose up -d --build  # 重新构建并启动（代码更新后）
```

#### 架构说明

`docker-compose.yml` 帮你自动处理了以下事项，无需再手动折腾：

| 你原本需要手动做的事 | Docker 自动处理 |
|----------------------|----------------|
| 安装 Python + 创建 venv | 镜像内预装 Python 3.12 |
| `pip install -r requirements.txt` | `docker build` 时自动执行 |
| 配置 nginx 反向代理 | 不需要，直接暴露 FastAPI 端口，简单省事 |
| 编写 systemd service 文件 | `restart: unless-stopped` 自动重启 |
| 管理进程守护 | 容器运行时自动守护 |
| 缓存目录持久化 | `volumes` 映射到宿主机，重启不丢失 |

#### 自定义端口

修改 `docker-compose.yml` 中的端口映射即可：

```yaml
ports:
  - "3000:8000"   # 将宿主机 3000 端口映射到容器 8000
```

#### 生产环境建议

如果需要在公网对外提供服务，建议在 Docker 前面加一层反向代理（如 Nginx、Caddy 或 Cloudflare Tunnel），用于处理 HTTPS 和域名。最简单的方案是使用 Caddy：

```bash
# 在 docker-compose.yml 同级目录创建 Caddyfile
# 然后运行: docker run -d -p 80:80 -p 443:443 -v ./Caddyfile:/etc/caddy/Caddyfile caddy
```

Caddyfile 示例：
```
your-domain.com {
    reverse_proxy localhost:8000
}
```

## API 路由一览

| 路径 | 说明 |
|------|------|
| `GET /` | 首页 |
| `GET /about` | 关于页面 |
| `GET /wiki/{title}` | 查看/生成百科条目，支持 `?image=1` 启用插图 |
| `GET /search?q=关键词` | 搜索页面 |
| `GET /random` | 随机跳转到预置词库中的条目，支持 `?image=1` |
| `GET /loading?next=/wiki/xxx` | 带加载动画的异步生成页 |
| `GET /api/jobs/{job_id}` | 查询异步任务状态 |
| `GET /api/jobs/{job_id}/result` | 获取异步任务结果 |
| `GET /health` | 健康检查 |
| `GET /image/{filename}` | 静态插图文件服务 |

## 使用示例

### 直接访问条目
```
http://localhost:8000/wiki/量子力学
http://localhost:8000/wiki/第十三种语言
```

### 带插图的条目
```
http://localhost:8000/wiki/硅基生命?image=1
```

### 搜索
```
http://localhost:8000/search?q=时间旅行
```

### 随机条目
```
http://localhost:8000/random
```

### 带加载动画的异步生成
```
http://localhost:8000/loading?next=/wiki/火星殖民地
```

## 工作原理

1. 用户访问 `/wiki/条目名` 或 `/loading?next=/wiki/条目名`
2. 系统检查缓存目录 `wiki/` 中是否已有该条目的 HTML 文件且未过期
3. 缓存命中 → 直接返回静态 HTML
4. 缓存未命中 → 调用 DeepSeek API 生成百科正文，经 HTML 格式校验后，若启用插图则通过 AIHubMix 生成配图，最终渲染为完整页面并写入缓存
5. 异步模式下（`/loading`），后台执行上述流程，前端轮询 `/api/jobs/{job_id}` 获取进度，完成后自动跳转

## 许可证

MIT