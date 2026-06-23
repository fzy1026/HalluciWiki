import os
import random
import asyncio
import time
from uuid import uuid4
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlparse, parse_qs

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from dotenv import load_dotenv

from config import CACHE_TTL_SECONDS, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, RANDOM_ENTRIES
from prompts import GENERATE_PROMPT, REVIEW_PROMPT, SEARCH_PROMPT

load_dotenv()

CACHE_DIR = Path("wiki")
CACHE_DIR.mkdir(exist_ok=True)
JOB_TTL_SECONDS = 1800

JOB_STORE: dict[str, dict] = {}

def sanitize_filename(title: str) -> str:
    keepchars = (' ', '.', '_', '-')
    sanitized = "".join(c if c.isalnum() or c in keepchars else "_" for c in title).strip()
    return (sanitized or "untitled") + ".html"

def is_cache_fresh(cache_path: Path) -> bool:
    if not cache_path.exists():
        return False

    age_seconds = time.time() - cache_path.stat().st_mtime
    return age_seconds <= CACHE_TTL_SECONDS

def cleanup_expired_cache() -> int:
    deleted_count = 0
    now = time.time()

    for cache_file in CACHE_DIR.glob("*.html"):
        try:
            age_seconds = now - cache_file.stat().st_mtime
            if age_seconds > CACHE_TTL_SECONDS:
                cache_file.unlink()
                deleted_count += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue

    return deleted_count

def create_job(target_url: str) -> str:
    job_id = uuid4().hex
    JOB_STORE[job_id] = {
        "job_id": job_id,
        "target_url": target_url,
        "state": "queued",
        "progress": 0,
        "stage": "排队中",
        "message": "任务已创建，正在等待开始处理。",
        "result_html": None,
        "error": None,
        "updated_at": time.time(),
    }
    return job_id

def update_job(job_id: str, **updates) -> None:
    job = JOB_STORE.get(job_id)
    if not job:
        return

    job.update(updates)
    job["updated_at"] = time.time()

def get_job(job_id: str) -> dict | None:
    return JOB_STORE.get(job_id)

def cleanup_expired_jobs() -> int:
    now = time.time()
    deleted_count = 0

    for job_id, job in list(JOB_STORE.items()):
        if job.get("state") not in {"done", "error"}:
            continue
        if now - float(job.get("updated_at", now)) > JOB_TTL_SECONDS:
            JOB_STORE.pop(job_id, None)
            deleted_count += 1

    return deleted_count

def describe_target(target_url: str) -> str:
    parsed = urlparse(target_url)
    if parsed.path.startswith("/wiki/"):
        title = unquote(parsed.path[len("/wiki/"):]) or "首页"
        return f"条目：{title}"
    if parsed.path == "/search":
        query = parse_qs(parsed.query).get("q", [""])[0]
        return f"搜索：{query or '未命名关键词'}"
    if parsed.path == "/random":
        return "随机条目"
    return target_url or "页面"

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
    cleanup_expired_cache()
    cleanup_expired_jobs()

    stop_event = asyncio.Event()

    async def cache_cleanup_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                cleanup_expired_cache()
                cleanup_expired_jobs()

    cleanup_task = asyncio.create_task(cache_cleanup_loop())
    yield
    stop_event.set()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
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

async def generate_and_review(title: str, job_id: str | None = None) -> str:
    """构建正文片段 → 整理 → 返回片段"""
    if job_id:
        update_job(job_id, state="running", progress=25, stage="构建条目", message=f"正在整理 {title} 的正文结构。")

    prompt_gen = GENERATE_PROMPT.format(title=title)
    raw_fragment = await call_deepseek(prompt_gen)

    if job_id:
        update_job(job_id, progress=55, stage="正文完成", message="正文已整理完成，正在进入修订阶段。")

    prompt_review = REVIEW_PROMPT.format(content=raw_fragment)
    reviewed_fragment = await call_deepseek(prompt_review)

    if job_id:
        update_job(job_id, progress=80, stage="整理条目", message="正在调整 HTML 结构和百科风格语气。")

    # 简单清理可能被 AI 意外加上的 ```html 等标记
    if reviewed_fragment.startswith("```"):
        reviewed_fragment = reviewed_fragment.strip("` \n")
        if reviewed_fragment.startswith("html\n"):
            reviewed_fragment = reviewed_fragment[5:]

    if job_id:
        update_job(job_id, progress=90, stage="即将完成", message="条目已整理完成，正在收尾。")

    return reviewed_fragment

def render_full_page(content_fragment: str, page_title: str = "HalluciWiki", next_url: str = "/") -> str:
    """将内容片段插入统一布局"""
    layout = templates["layout"]
    return (
        layout.replace("{{ title }}", page_title)
        .replace("{{ next_url }}", next_url)
        .replace("{{ content }}", content_fragment)
    )

def render_loading_page(next_url: str, job_id: str, target_label: str, result_url: str) -> str:
    loading = templates["loading"]
    return (
        loading.replace("{{ next_url }}", next_url)
        .replace("{{ job_id }}", job_id)
        .replace("{{ target_label }}", target_label)
        .replace("{{ result_url }}", result_url)
    )

async def get_wiki_page(title: str, job_id: str | None = None) -> str:
    """获取完整 HTML 页面（优先缓存）"""
    safe_name = sanitize_filename(title)
    cache_path = CACHE_DIR / safe_name

    if is_cache_fresh(cache_path):
        if job_id:
            update_job(job_id, progress=100, stage="命中缓存", message=f"{title} 已从本地缓存直接返回。")
        return cache_path.read_text(encoding="utf-8")

    if cache_path.exists():
        try:
            cache_path.unlink()
        except OSError:
            pass

    fragment = await generate_and_review(title, job_id=job_id)
    full_html = render_full_page(fragment, page_title=f"{title} - HalluciWiki")

    cache_path.write_text(full_html, encoding="utf-8")
    if job_id:
        update_job(job_id, progress=100, stage="完成", message=f"{title} 已准备好。")
    return full_html

async def get_search_page(query: str, job_id: str | None = None) -> str:
    if job_id:
        update_job(job_id, state="running", progress=30, stage="整理搜索结果", message=f"正在构建搜索词 {query} 的结果页。")

    prompt = SEARCH_PROMPT.format(query=query)
    fragment = await call_deepseek(prompt)

    if job_id:
        update_job(job_id, progress=85, stage="整理版式", message="正在整理搜索结果的 HTML 结构。")

    html = render_full_page(fragment, page_title=f"搜索结果：{query}")

    if job_id:
        update_job(job_id, progress=100, stage="完成", message="搜索结果已准备好。")

    return html

async def generate_page_for_target(target_url: str, job_id: str) -> None:
    parsed = urlparse(target_url)

    try:
        update_job(job_id, state="running", progress=10, stage="解析请求", message=f"正在处理 {describe_target(target_url)}。")

        if parsed.path.startswith("/wiki/"):
            title = unquote(parsed.path[len("/wiki/"):]) or "首页"
            html = await get_wiki_page(title, job_id=job_id)
            update_job(job_id, result_html=html, state="done")
            return

        if parsed.path == "/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if not query:
                raise HTTPException(status_code=400, detail="搜索关键词不能为空")
            html = await get_search_page(query, job_id=job_id)
            update_job(job_id, result_html=html, state="done")
            return

        if parsed.path == "/random":
            entry = random.choice(RANDOM_ENTRIES)
            update_job(job_id, progress=40, stage="选择随机条目", message=f"已选中条目：{entry}。正在整理页面。")
            html = await get_wiki_page(entry, job_id=job_id)
            update_job(job_id, result_html=html, state="done", message=f"随机条目 {entry} 已准备好。")
            return

        raise HTTPException(status_code=404, detail=f"不支持的目标：{target_url}")
    except Exception as exc:
        update_job(job_id, state="error", progress=100, stage="处理失败", message=str(exc), error=str(exc))

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
    job_id = create_job(next_url)
    target_label = describe_target(next_url)
    result_url = f"/api/jobs/{job_id}/result"
    asyncio.create_task(generate_page_for_target(next_url, job_id))
    return HTMLResponse(render_loading_page(next_url, job_id, target_label, result_url))

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
    try:
        html = await get_search_page(q)
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

@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    return JSONResponse({
        "job_id": job["job_id"],
        "target_url": job["target_url"],
        "state": job["state"],
        "progress": job["progress"],
        "stage": job["stage"],
        "message": job["message"],
        "error": job["error"],
        "result_url": f"/api/jobs/{job_id}/result",
    })

@app.get("/api/jobs/{job_id}/result", response_class=HTMLResponse)
async def job_result(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("state") != "done" or not job.get("result_html"):
        raise HTTPException(status_code=425, detail="任务尚未完成")

    return HTMLResponse(content=job["result_html"])