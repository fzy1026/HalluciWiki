import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# OpenAI 兼容预留
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# 随机条目词库（可自由扩充）
RANDOM_ENTRIES = [
    "量子力学", "香蕉共和国", "1093年香蕉叛乱", "大语言模型幻觉",
    "时间旅行悖论", "大过滤器理论", "硅基生命", "火星殖民地",
    "哀伤之龙", "多元宇宙通信协议", "记忆编辑技术", "反重力引擎",
    "深海智慧文明", "莎士比亚的遗失手稿", "1982年芝加哥时间裂缝",
    "可控核聚变", "完美数字生命", "世界之书"
]