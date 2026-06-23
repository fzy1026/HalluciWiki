import os
import random
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from dotenv import load_dotenv

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, RANDOM_ENTRIES
from prompts import GENERATE_PROMPT, REVIEW_PROMPT, SEARCH_PROMPT

load_dotenv()

CACHE_DIR = Path("wiki")
CACHE_DIR.mkdir(exist_ok=True)

def sanitize_filename(title: str) -> str:
    keepchars = (' ', '.', '_', '-')
    sanitized = "".join(c if c.isalnum() or c in keepchars else "_" for c in title).strip()
    return (sanitized or "untitled") + ".html"

# ---------- 模板预加载 ----------
templates = {}  # 存储布局、首页内容、关于内容、加载页

def load_templates():
    global templates
    files = {
        "layout": "templates/layout.html",
        "home_content": "templates/home_content.html",
        "about_content": "templates/about_content.html",
        "loading": "templates/loading.html"
    }
    for name, path in files.items():
        with open(path, "r", encoding="utf-8") as f:
            templates[name] = f.read()

# ---------- HTTP 客户端 ----------
async_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global async_client
    async_client = httpx.AsyncClient(timeout=60.0)
    load_templates()
    CACHE_DIR.mkdir(exist_ok=True)
    yield
    await async_client.aclose()

app = FastAPI(lifespan=lifespan)

# ---------- 工具函数 ----------
async def call_deepseek(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    try:
        resp = await async_client.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 调用失败: {str(e)}")

async def generate_and_review(title: str) -> str:
    """生成正文片段 → 审核 → 返回片段"""
    prompt_gen = GENERATE_PROMPT.format(title=title)
    raw_fragment = await call_deepseek(prompt_gen)

    prompt_review = REVIEW_PROMPT.format(content=raw_fragment)
    reviewed_fragment = await call_deepseek(prompt_review)

    # 简单清理可能被 AI 意外加上的 ```html 等标记
    if reviewed_fragment.startswith("```"):
        reviewed_fragment = reviewed_fragment.strip("` \n")
        if reviewed_fragment.startswith("html\n"):
            reviewed_fragment = reviewed_fragment[5:]

    return reviewed_fragment

def render_full_page(content_fragment: str, page_title: str = "HalluciWiki", next_url: str = "/") -> str:
    """将内容片段插入统一布局"""
    layout = templates["layout"]
    return (
        layout.replace("{{ title }}", page_title)
        .replace("{{ next_url }}", next_url)
        .replace("{{ content }}", content_fragment)
    )

def render_loading_page(next_url: str) -> str:
    loading = templates["loading"]
    return loading.replace("{{ next_url }}", next_url)

async def get_wiki_page(title: str) -> str:
    """获取完整 HTML 页面（优先缓存）"""
    safe_name = sanitize_filename(title)
    cache_path = CACHE_DIR / safe_name

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    fragment = await generate_and_review(title)
    full_html = render_full_page(fragment, page_title=f"{title} - HalluciWiki")

    cache_path.write_text(full_html, encoding="utf-8")
    return full_html

# ---------- 路由 ----------
@app.get("/", response_class=HTMLResponse)
async def home():
    content = templates["home_content"]
    return HTMLResponse(render_full_page(content, page_title="HalluciWiki - 首页"))

@app.get("/about", response_class=HTMLResponse)
async def about():
    content = templates["about_content"]
    return HTMLResponse(render_full_page(content, page_title="关于 HalluciWiki"))

@app.get("/loading", response_class=HTMLResponse)
async def loading(next: str = Query("/")):
    next_url = next if next.startswith("/") else f"/{next.lstrip('/')}"
    return HTMLResponse(render_loading_page(next_url))

@app.get("/wiki/{title:path}", response_class=HTMLResponse)
async def wiki_page(title: str):
    title = unquote(title)
    if not title:
        title = "首页"
    try:
        html = await get_wiki_page(title)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search", response_class=HTMLResponse)
async def search(q: str = Query(..., min_length=1)):
    prompt = SEARCH_PROMPT.format(query=q)
    try:
        fragment = await call_deepseek(prompt)
        html = render_full_page(fragment, page_title=f"搜索结果：{q}")
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")

@app.get("/random")
async def random_page():
    entry = random.choice(RANDOM_ENTRIES)
    return RedirectResponse(f"/wiki/{entry}")

@app.get("/health")
async def health():
    return {"status": "ok"}