import base64
import logging
import httpx
from config import AIHUBMIX_API_KEY, AIHUBMIX_BASE_URL, AIHUBMIX_MODELS

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
    logger.info("开始生成插图 | 描述: %s | 可用模型: %s", short_prompt, ", ".join(AIHUBMIX_MODELS))

    last_error = None
    for model in AIHUBMIX_MODELS:
        logger.info("尝试模型: %s", model)
        try:
            image_bytes = await _try_generate(client, model, prompt, aspect_ratio)
            if image_bytes:
                logger.info("插图生成成功 | 模型: %s | 大小: %d bytes", model, len(image_bytes))
                return image_bytes
            else:
                logger.warning("模型 %s 返回成功但无图片数据，切换下一个", model)
        except Exception as e:
            last_error = e
            logger.warning("模型 %s 调用失败: %s，切换下一个", model, e)

    logger.error("所有模型均已尝试失败 | 最后错误: %s", last_error)
    return None


async def _try_generate(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    aspect_ratio: str,
) -> bytes | None:
    headers = {
        "Authorization": f"Bearer {AIHUBMIX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": f"aspect_ratio={aspect_ratio}"},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        "modalities": ["text", "image"],
    }

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
            return base64.b64decode(part["inline_data"]["data"])
    return None