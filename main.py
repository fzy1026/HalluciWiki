import os
import re
import random
import asyncio
import time
import hashlib
import logging
from uuid import uuid4
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlparse, parse_qs, quote
from html import escape

import httpx
from fastapi import FastAPI, Query, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from config import CACHE_TTL_SECONDS, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, RANDOM_ENTRIES, AIHUBMIX_API_KEY, SHARE_TTL_SECONDS
from prompts import GENERATE_PROMPT, SEARCH_PROMPT, SUMMARY_PROMPT
from html_validator import validate_html
from image_generator import generate_image
import providers
import monitor_page
import caches

# 正文最少字数：低于这个值视为模型输出不合格，自动换下一个模型
MIN_ARTICLE_CHARS = int(os.getenv("MIN_ARTICLE_CHARS", "1200"))
MIN_SEARCH_CHARS = int(os.getenv("MIN_SEARCH_CHARS", "200"))

# B 函数（摘要）并发上限与单页最多补几条
SUMMARY_CONCURRENCY = int(os.getenv("SUMMARY_CONCURRENCY", "4"))
SUMMARY_MAX_RESULTS = int(os.getenv("SUMMARY_MAX_RESULTS", "10"))
_summary_semaphore = asyncio.Semaphore(SUMMARY_CONCURRENCY)

load_dotenv()

logger = logging.getLogger("halluciwiki")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

class SuppressJobPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/jobs/" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(SuppressJobPollingFilter())

CACHE_DIR = Path("wiki")
CACHE_DIR.mkdir(exist_ok=True)
IMAGE_CACHE_DIR = Path("wiki_image")
IMAGE_CACHE_DIR.mkdir(exist_ok=True)
SHARES_DIR = Path("shares")
SHARES_DIR.mkdir(exist_ok=True)
JOB_TTL_SECONDS = 1800
JOB_STORE: dict[str, dict] = {}
SHARE_STORE: dict[str, dict] = {}

# ---- A（搜索）本地装配用到的清洗规则 ----
BULLET_PATTERN = re.compile(r"^[\s\-–—*•·>#]+|^\d+\s*[.、)）]\s*")
TITLE_JUNK_PATTERN = re.compile(r"[《》“”\"'‘’（）()\[\]【】<>|/\\·:：;；,，.。!！?？]")
SUBCHAPTER_PATTERN = re.compile(
    r"(概述|简介|历史|历史发展|结构|分类|开放时间|研究进展|研究|进展|评价|影响|"
    r"应用领域|理论基础|基本原理|发展史|大事记|争议|现状|前景|介绍|详情|相关内容)$"
)
MIN_SEARCH_TITLES = int(os.getenv("MIN_SEARCH_TITLES", "4"))

def parse_titles(raw: str, query: str) -> list[str]:
    """把模型吐的「一行一个条目名」洗成干净的标题列表。

    小模型常见毛病：加序号/项目符号、带书名号、把关键词的子章节当条目、
    连着写几个只差两三个字的近义词。这里统一收口，交给本地渲染。
    """
    out: list[str] = []
    for line in (raw or "").splitlines():
        t = BULLET_PATTERN.sub("", line.strip())
        t = t.split("：")[0].strip() if "：" in t else t
        t = TITLE_JUNK_PATTERN.sub("", t).strip()
        t = re.sub(r"\s+", "", t)
        if not (2 <= len(t) <= 14):
            continue
        if t != query and SUBCHAPTER_PATTERN.search(t):
            continue
        if t in out:
            continue
        if any(t in o or o in t for o in out):
            continue
        out.append(t)
    if query and query not in out:
        out.insert(0, query)
    return out


def make_title_validator(query: str):
    """A 的质量闸门：标题太少、或全是关键词的子章节，就判不合格切下一个模型。"""

    def check(content: str) -> str | None:
        titles = parse_titles(content, query)
        if len(titles) < MIN_SEARCH_TITLES:
            return f"标题不足（{len(titles)} 个）"
        return None

    return check


def get_search_summary(title: str) -> str | None:
    """B 层缓存查询：拿这个页面名对应的摘要。"""
    return caches.summary_get(title)

def cleanup_expired_search_summaries() -> int:
    caches.flush()
    return 0

def image_filename(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16] + ".png"

def cleanup_expired_images() -> int:
    deleted = 0
    now = time.time()
    for image_file in IMAGE_CACHE_DIR.glob("*.png"):
        try:
            if now - image_file.stat().st_mtime > CACHE_TTL_SECONDS:
                image_file.unlink()
                deleted += 1
        except (FileNotFoundError, OSError):
            continue
    if deleted:
        logger.info("图片缓存清理完成 | 删除: %d 个过期文件", deleted)
    return deleted

def cleanup_expired_shares() -> int:
    deleted = 0
    now = time.time()
    for share_file in SHARES_DIR.glob("*.html"):
        try:
            if now - share_file.stat().st_mtime > SHARE_TTL_SECONDS:
                share_id = share_file.stem
                share_file.unlink()
                SHARE_STORE.pop(share_id, None)
                deleted += 1
        except (FileNotFoundError, OSError):
            continue
    if deleted:
        logger.info("分享快照清理完成 | 删除: %d 个过期快照", deleted)
    return deleted

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
    IMAGE_CACHE_DIR.mkdir(exist_ok=True)
    SHARES_DIR.mkdir(exist_ok=True)
    caches.init()
    cleanup_expired_cache()
    cleanup_expired_jobs()
    cleanup_expired_search_summaries()
    cleanup_expired_images()
    cleanup_expired_shares()

    stop_event = asyncio.Event()

    async def cache_cleanup_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                cleanup_expired_cache()
                cleanup_expired_jobs()
                cleanup_expired_search_summaries()
                cleanup_expired_images()
                cleanup_expired_shares()
                caches.cleanup()
                caches.flush()

    cleanup_task = asyncio.create_task(cache_cleanup_loop())
    yield
    stop_event.set()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    caches.flush(force=True)
    await async_client.aclose()

app = FastAPI(lifespan=lifespan)

# ---------- 工具函数 ----------
IMAGE_PLACEHOLDER = re.compile(r'\[IMAGE:\s*(.*?)\]')

async def process_image_placeholders(html: str) -> str:
    matches = list(IMAGE_PLACEHOLDER.finditer(html))
    if not matches:
        return html

    logger.info("检测到 %d 个插图占位符，开始处理", len(matches))

    # 第一阶段：检查缓存，收集需要调用 API 的任务
    pending: list[tuple[re.Match, str, str, Path]] = []
    for match in matches:
        description = match.group(1).strip()
        fname = image_filename(description)
        filepath = IMAGE_CACHE_DIR / fname

        if filepath.exists():
            if not is_cache_fresh(filepath):
                logger.info("插图缓存已过期 | 文件: %s", fname)
                try:
                    filepath.unlink()
                except OSError:
                    pass
            else:
                logger.info("插图命中缓存 | 文件: %s", fname)

        if not filepath.exists():
            pending.append((match, description, fname, filepath))

    # 第二阶段：并行生成所有未命中的图片
    if pending:
        logger.info("开始并行生成 %d 张插图", len(pending))
        results = await asyncio.gather(
            *(generate_image(async_client, desc) for _, desc, _, _ in pending),
            return_exceptions=True,
        )
        for (_, desc, fname, filepath), image_bytes in zip(pending, results):
            if isinstance(image_bytes, Exception):
                logger.warning("插图生成失败 | 描述: %s | 错误: %s", desc[:80], image_bytes)
            elif image_bytes is None:
                logger.warning("插图生成失败，将显示占位提示 | 描述: %s", desc[:80])
            else:
                try:
                    filepath.write_bytes(image_bytes)
                    logger.info("插图已写入磁盘 | 文件: %s | 大小: %d bytes", fname, len(image_bytes))
                except OSError as e:
                    logger.error("插图写入失败 | 文件: %s | 错误: %s", fname, e)

    # 第三阶段：替换 HTML 占位符（从后往前避免索引错乱）
    result = html
    for match in reversed(matches):
        description = match.group(1).strip()
        fname = image_filename(description)
        filepath = IMAGE_CACHE_DIR / fname

        if filepath.exists():
            img_tag = (
                f'<figure style="margin:1.5rem 0;text-align:center;">'
                f'<img src="/image/{fname}" alt="{description}" '
                f'style="max-width:100%;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                f'<figcaption style="color:#54595d;font-size:0.9rem;margin-top:0.5rem;">{description}</figcaption>'
                f'</figure>'
            )
        else:
            img_tag = f'<p style="color:#a2a9b1;font-style:italic;">[插图：{description} — 生成失败]</p>'
        result = result[:match.start()] + img_tag + result[match.end():]

    return result

async def call_llm(prompt: str, *, purpose: str = "wiki", min_chars: int = 0, validator=None) -> str:
    """按 routing.json 的分工，用该任务配置的模型顺序轮询；一个失败自动换下一个。"""
    task = "page" if purpose == "wiki" else purpose
    try:
        content, model = await providers.chat(
            async_client, prompt, task=task, min_chars=min_chars, validator=validator
        )
    except providers.AllModelsFailed as exc:
        logger.error("所有模型均不可用 | %s", exc)
        raise HTTPException(status_code=503, detail=f"所有模型均不可用：{exc}") from exc
    logger.info("生成完成 | %s/%s | %d 字", model.id, model.model, len(content))
    return content


def clean_summary(text: str, title: str = "") -> str:
    """把模型吐的摘要洗成一行纯文本。小模型常见毛病：带「摘要：」前缀、重复标题、写多段。"""
    t = re.sub(r"<[^>]+>", "", text or "")
    t = t.replace("```", "").strip()
    t = re.sub(r"^(摘要|简述|简介|条目)\s*[:：]\s*", "", t)
    if title:
        for sep in ("：", ":", "——", "—", "，", " "):
            if t.startswith(title + sep):
                t = t[len(title) + len(sep):]
                break
    t = t.strip().strip("\"'“”‘’「」《》 ")
    t = re.sub(r"^[\s：:，,、\-—–.。]+", "", t)
    t = re.sub(r"\s+", " ", t)
    first = re.match(r"^(.{8,70}?[。！？])", t)
    if first:
        t = first.group(1)
    return t[:120].strip()


async def generate_summary(title: str, context: str = "") -> str:
    """B 函数：条目名 → 一句话摘要。先查 B 层缓存，未命中才调模型。"""
    cached = caches.summary_get(title)
    if cached:
        return cached

    context_block = f"上下文：{context}\n" if context else ""
    prompt = SUMMARY_PROMPT.format(title=title, context_block=context_block)
    try:
        async with _summary_semaphore:
            raw = await call_llm(prompt, purpose="summary", min_chars=6)
    except Exception as exc:  # noqa: BLE001
        logger.warning("摘要生成失败 | %s | %s", title, exc)
        return ""

    text = clean_summary(raw, title)
    if text:
        caches.summary_set(title, text)
        caches.flush()
    return text


async def generate_and_review(title: str, enable_images: bool = False, job_id: str | None = None, link_summary: str = "") -> str:
    """构建正文片段 → HTML 格式校验 → 返回片段"""
    if job_id:
        update_job(job_id, state="running", progress=25, stage="构建条目", message=f"正在整理 {title} 的正文结构。")

    search_summary = get_search_summary(title)
    if link_summary:
        summary_text = link_summary
    elif search_summary:
        summary_text = search_summary
    else:
        # 既没有外部传入、也不是从搜索结果点进来的（比如 /random 或直接输 URL），
        # 就让 B 函数补一个摘要，保证虚构宇宙的口径一致
        generated = await generate_summary(title)
        summary_text = generated if generated else "（无特别约束，自由发挥）"

    image_instruction = (
        "- 在适当位置插入插图占位符，格式为 [IMAGE: 图片描述]。例如 [IMAGE: 一幅展示量子纠缠原理的示意图，包含两个纠缠粒子之间的连接线]。每个条目建议插入 1-2 个插图占位符，描述应具体、可视化，便于生成准确的图片。占位符应放在相关段落之后，独立成行。"
        if enable_images else ""
    )
    prompt_gen = GENERATE_PROMPT.format(title=title, summary=summary_text, image_instruction=image_instruction)
    raw_fragment = await call_llm(prompt_gen, purpose="wiki", min_chars=MIN_ARTICLE_CHARS)

    if job_id:
        update_job(job_id, progress=60, stage="正文完成", message="正文已生成，正在进行 HTML 格式校验。")

    # 简单清理可能被 AI 意外加上的 ```html 等标记
    if raw_fragment.startswith("```"):
        raw_fragment = raw_fragment.strip("` \n")
        if raw_fragment.startswith("html\n"):
            raw_fragment = raw_fragment[5:]

    # HTML 标签闭合校验
    errors = validate_html(raw_fragment)
    if errors:
        error_summary = "; ".join(errors[:3])
        if job_id:
            update_job(job_id, progress=80, stage="格式警告", message=f"HTML 校验发现问题：{error_summary}")
    elif job_id:
        update_job(job_id, progress=80, stage="格式校验通过", message="HTML 格式校验通过。")

    # 处理插图占位符 [IMAGE: ...]（仅在启用插图时）
    if enable_images and AIHUBMIX_API_KEY and IMAGE_PLACEHOLDER.search(raw_fragment):
        if job_id:
            update_job(job_id, progress=85, stage="调取插图", message="正在从档案库中调取插图资料。")
        raw_fragment = await process_image_placeholders(raw_fragment)

    if job_id:
        update_job(job_id, progress=95, stage="即将完成", message="条目已整理完成，正在收尾。")

    return raw_fragment

def render_full_page(content_fragment: str, page_title: str = "HalluciWiki", next_url: str = "/", share_target: str = "") -> str:
    """将内容片段插入统一布局"""
    layout = templates["layout"]
    share_data = f' data-share-target="{share_target}"' if share_target else ""
    return (
        layout.replace("{{ title }}", page_title)
        .replace("{{ next_url }}", next_url)
        .replace("{{ share_data }}", share_data)
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

async def get_wiki_page(title: str, enable_images: bool = False, job_id: str | None = None, link_summary: str = "") -> str:
    """获取完整 HTML 页面（优先缓存）"""
    base_name = sanitize_filename(title)
    cache_name = base_name if not enable_images else base_name.replace(".html", "_image.html")
    cache_path = CACHE_DIR / cache_name

    if is_cache_fresh(cache_path):
        if job_id:
            update_job(job_id, progress=100, stage="命中缓存", message=f"{title} 已从本地缓存直接返回。")
        return cache_path.read_text(encoding="utf-8")

    # C 层单飞：同一个页面只允许一个请求去生成，其余在这里等，等完直接读缓存
    async with caches.page_lock(cache_name):
        if is_cache_fresh(cache_path):
            if job_id:
                update_job(job_id, progress=100, stage="命中缓存", message=f"{title} 已从本地缓存直接返回。")
            return cache_path.read_text(encoding="utf-8")

        if cache_path.exists():
            try:
                cache_path.unlink()
            except OSError:
                pass

        fragment = await generate_and_review(title, enable_images=enable_images, job_id=job_id, link_summary=link_summary)
        full_html = render_full_page(fragment, page_title=f"{title} - HalluciWiki", share_target=f"/wiki/{quote(title)}")

        cache_path.write_text(full_html, encoding="utf-8")

    if job_id:
        update_job(job_id, progress=100, stage="完成", message=f"{title} 已准备好。")
    return full_html

async def get_search_page(query: str, job_id: str | None = None) -> str:
    # A 层缓存：同一个搜索词（归一化后）只生成一次
    cached = caches.search_get(query)
    if cached is not None:
        if job_id:
            update_job(job_id, progress=100, stage="命中缓存", message=f"搜索词 {query} 已从缓存直接返回。")
        logger.info("搜索页命中缓存 | %s", query)
        return cached

    if job_id:
        update_job(job_id, state="running", progress=25, stage="检索条目", message=f"正在为 {query} 检索相关条目。")

    # A：模型只输出「一行一个条目名」，HTML 全部由本地脚本装配
    prompt = SEARCH_PROMPT.format(query=query)
    raw = await call_llm(prompt, purpose="search", min_chars=8, validator=make_title_validator(query))

    titles = parse_titles(raw, query)[:SUMMARY_MAX_RESULTS]
    logger.info("搜索条目解析 | %s | %d 条 | %s", query, len(titles), " / ".join(titles[:8]))

    if job_id:
        update_job(job_id, progress=60, stage="补摘要", message=f"已找到 {len(titles)} 个条目，正在补写摘要。")

    # B：并发补摘要（命中缓存直接用，没命中才调模型）
    fetched = await asyncio.gather(*(generate_summary(t, query) for t in titles), return_exceptions=True)
    summary_map = {t: s for t, s in zip(titles, fetched) if isinstance(s, str) and s}

    rows = []
    for i, title in enumerate(titles):
        snippet = summary_map.get(title) or "该条目暂无摘要。"
        cls = ' class="search-main"' if i == 0 else ""
        rows.append(
            f"<li{cls}>"
            f'<a href="/wiki/{quote(title)}" data-summary="{escape(snippet)}">{escape(title)}</a>'
            f'<span class="result-snippet">{escape(snippet)}</span>'
            f'<span class="result-url">wiki.chamdev.site › wiki › {escape(title)}</span>'
            "</li>"
        )

    body = (
        '<div class="search-results">'
        '<div class="article-meta"><span class="article-badge">搜索</span>'
        f"<span>{escape(query)} · {len(titles)} 条结果</span></div>"
        f'<ul class="search-result-list">{"".join(rows)}</ul>'
        "</div>"
    )
    html = render_full_page(body, page_title=f"搜索结果：{query}")
    caches.search_set(query, html)

    if job_id:
        update_job(job_id, progress=100, stage="完成", message="搜索结果已准备好。")

    return html


async def generate_page_for_target(target_url: str, job_id: str) -> None:
    parsed = urlparse(target_url)
    query_params = parse_qs(parsed.query)
    enable_images = query_params.get("image", ["0"])[0] == "1"
    link_summary = query_params.get("summary", [""])[0]

    try:
        update_job(job_id, state="running", progress=10, stage="解析请求", message=f"正在处理 {describe_target(target_url)}。")

        if parsed.path.startswith("/wiki/"):
            title = unquote(parsed.path[len("/wiki/"):]) or "首页"
            html = await get_wiki_page(title, enable_images=enable_images, job_id=job_id, link_summary=link_summary)
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
            html = await get_wiki_page(entry, enable_images=enable_images, job_id=job_id)
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
async def wiki_page(title: str, image: bool = Query(False), summary: str = Query("")):
    title = unquote(title)
    if not title:
        title = "首页"
    try:
        html = await get_wiki_page(title, enable_images=image, link_summary=summary)
        return HTMLResponse(content=html)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search", response_class=HTMLResponse)
async def search(q: str = Query(..., min_length=1)):
    try:
        html = await get_search_page(q)
        return HTMLResponse(content=html)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")

@app.get("/random")
async def random_page(image: bool = Query(False)):
    entry = random.choice(RANDOM_ENTRIES)
    suffix = "?image=1" if image else ""
    return RedirectResponse(f"/wiki/{entry}{suffix}")

@app.get("/health")
async def health():
    return {"status": "ok", "cache": caches.stats()}

# ---------- 监控页：/admin* 由 Caddy 的 basic_auth 保护 ----------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    data = providers.snapshot()
    return HTMLResponse(
        render_full_page(monitor_page.render(data), page_title="模型状态 - HalluciWiki")
    )

@app.get("/admin/state")
async def admin_state():
    """给脚本 / 监控用的 JSON 版本。"""
    return JSONResponse(providers.snapshot())

@app.post("/admin/probe")
async def admin_probe(key: str | None = None):
    """手动检测：不带 key 检测全部，带 key（kind:model）只检测那一个。"""
    if key:
        await providers.probe_one(key)
        logger.info("手动检测单个模型 | %s", key)
    else:
        await providers.probe_all()
        logger.info("手动检测全部模型")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/routing")
async def admin_routing(task: str = Form(...), op: str = Form(...), key: str = Form("")):
    """模型分工的即时调整：up / down / remove / add / clear。"""
    if task not in providers.TASKS:
        raise HTTPException(status_code=400, detail="未知任务")

    current = {t: list(providers.routing_for(t) or []) for t in providers.TASKS}
    seq = current[task]

    if op == "add" and key and key not in seq:
        seq.append(key)
    elif op == "remove" and key in seq:
        seq.remove(key)
    elif op == "up" and key in seq:
        i = seq.index(key)
        if i > 0:
            seq[i - 1], seq[i] = seq[i], seq[i - 1]
    elif op == "down" and key in seq:
        i = seq.index(key)
        if i < len(seq) - 1:
            seq[i + 1], seq[i] = seq[i], seq[i + 1]
    elif op == "clear":
        seq = []

    current[task] = seq
    try:
        providers.save_routing(current)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 routing.json 失败：{exc}") from exc

    logger.info("模型分工调整 | %s | %s %s -> %s", task, op, key or "-", seq)
    return RedirectResponse("/admin", status_code=303)

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

SHARE_BANNER_HTML = '''<div style="background:linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);border:1px solid #2a3a5c;border-radius:8px;padding:0.9rem 1.1rem;margin-bottom:1.25rem;color:#c8d6e5;font-size:0.9rem;line-height:1.6;display:flex;align-items:flex-start;gap:0.75rem;">
<div style="flex-shrink:0;font-size:1.3rem;">📡</div>
<div>
<div style="font-weight:700;color:#e8eef5;margin-bottom:0.15rem;">多元宇宙量子中继快照</div>
<div>此页面内容经由跨维度量子信道传输并固定于本时间锚点。由于多元宇宙量子连接固有的退相干效应，该快照将随时间推移逐渐衰减，<strong>自生成之日起 7 天后自动消散</strong>。在此期间，内容不受主体宇宙信息流变动的影响，保持观测时刻的精确状态。</div>
</div>
</div>'''

@app.post("/api/share")
async def create_share(request: Request):
    body = await request.json()
    target_url = body.get("target_url", "")
    if not target_url:
        raise HTTPException(status_code=400, detail="缺少 target_url")

    parsed = urlparse(target_url)

    if parsed.path.startswith("/wiki/"):
        title = unquote(parsed.path[len("/wiki/"):]) or "首页"
        base_name = sanitize_filename(title)
        cache_path = CACHE_DIR / base_name
        if cache_path.exists():
            html = cache_path.read_text(encoding="utf-8")
        else:
            try:
                html = await get_wiki_page(title)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"生成页面失败: {str(e)}")
    elif parsed.path == "/search":
        query = parse_qs(parsed.query).get("q", [""])[0]
        if not query:
            raise HTTPException(status_code=400, detail="搜索关键词不能为空")
        try:
            html = await get_search_page(query)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"生成搜索结果失败: {str(e)}")
    elif parsed.path == "/":
        html = render_full_page(templates["home_content"], page_title="HalluciWiki - 首页")
    elif parsed.path == "/about":
        html = render_full_page(templates["about_content"], page_title="关于 HalluciWiki")
    else:
        raise HTTPException(status_code=400, detail="不支持的分享目标")

    share_id = hashlib.sha256(target_url.encode()).hexdigest()[:8]
    share_path = SHARES_DIR / f"{share_id}.html"

    html_with_banner = html.replace(
        '<article class="wiki-article">',
        '<article class="wiki-article">' + SHARE_BANNER_HTML
    )
    html_with_banner = re.sub(
        r'\s+data-share-target="[^"]*"',
        '',
        html_with_banner
    )

    share_path.write_text(html_with_banner, encoding="utf-8")
    SHARE_STORE[share_id] = {
        "share_id": share_id,
        "target_url": target_url,
        "created_at": time.time(),
    }
    logger.info("分享快照已创建 | ID: %s | 目标: %s", share_id, target_url)

    return JSONResponse({
        "share_id": share_id,
        "share_url": f"/share/{share_id}",
    })

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def view_share(share_id: str):
    share_path = SHARES_DIR / f"{share_id}.html"
    if not share_path.exists():
        raise HTTPException(status_code=404, detail="此分享链接不存在，快照可能已经消散")

    age = time.time() - share_path.stat().st_mtime
    if age > SHARE_TTL_SECONDS:
        try:
            share_path.unlink()
        except OSError:
            pass
        SHARE_STORE.pop(share_id, None)
        raise HTTPException(status_code=404, detail="此分享快照已超过 7 天，量子态已退相干消散")

    return HTMLResponse(content=share_path.read_text(encoding="utf-8"))

app.mount("/image", StaticFiles(directory="wiki_image"), name="image")