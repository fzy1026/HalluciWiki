import base64
import logging
import httpx
from config import AIHUBMIX_API_KEY, AIHUBMIX_BASE_URL, AIHUBMIX_MODEL

logger = logging.getLogger("image_generator")


async def generate_image(
    client: httpx.AsyncClient,
    prompt: str,
    aspect_ratio: str = "16:9",
) -> bytes | None:
    if not AIHUBMIX_API_KEY:
        logger.warning("AIHUBMIX_API_KEY 未配置，跳过插图生成")
        return None

    short_prompt = prompt[:80] + ("..." if len(prompt) > 80 else "")
    logger.info("开始生成插图 | 描述: %s | 比例: %s", short_prompt, aspect_ratio)

    headers = {
        "Authorization": f"Bearer {AIHUBMIX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AIHUBMIX_MODEL,
        "messages": [
            {"role": "system", "content": f"aspect_ratio={aspect_ratio}"},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        "modalities": ["text", "image"],
    }

    try:
        resp = await client.post(
            f"{AIHUBMIX_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()

        parts = data["choices"][0]["message"].get("multi_mod_content", [])
        for part in parts:
            if "inline_data" in part:
                image_bytes = base64.b64decode(part["inline_data"]["data"])
                logger.info("插图生成成功 | 大小: %d bytes", len(image_bytes))
                return image_bytes
        logger.warning("API 响应中未找到图片数据")
        return None
    except Exception as e:
        logger.error("插图生成失败 | 错误: %s", e)
        return None