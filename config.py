import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# OpenAI 兼容预留
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# AIHubMix 图片生成配置（多模型按优先级排序，逗号分隔）
AIHUBMIX_API_KEY = os.getenv("AIHUBMIX_API_KEY", "")
AIHUBMIX_BASE_URL = os.getenv("AIHUBMIX_BASE_URL", "https://aihubmix.com/v1")
AIHUBMIX_MODELS = [m.strip() for m in os.getenv("AIHUBMIX_MODELS", "gemini-3.1-flash-image-preview").split(",") if m.strip()]

# 页面缓存 TTL（秒），默认 10 分钟
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))

# 分享快照 TTL（秒），默认 7 天
SHARE_TTL_SECONDS = int(os.getenv("SHARE_TTL_SECONDS", "604800"))

# 随机条目词库（可自由扩充）
RANDOM_ENTRIES = [
    "量子力学", "香蕉共和国", "1093年香蕉叛乱", "大语言模型幻觉",
    "时间旅行悖论", "大过滤器理论", "硅基生命", "火星殖民地",
    "哀伤之龙", "多元宇宙通信协议", "记忆编辑技术", "反重力引擎",
    "深海智慧文明", "莎士比亚的遗失手稿", "1982年芝加哥时间裂缝",
    "可控核聚变", "完美数字生命", "世界之书", "阿特拉斯档案馆",
    "月面植物园", "第七码头事件", "镜像海洋", "沉默城市",
    "星港历法", "玻璃沙漠", "永夜列车", "格里芬自治区",
    "寒潮观测站", "灰塔编年史", "午夜钟楼", "被遗忘的第九层",
    "柯罗诺斯协议", "海雾共和国", "蜂巢城市", "赤道裂谷带",
    "逆光研究所", "门扉理论", "第十三种语言", "黑曜石图书馆",
    "漂浮教区", "恒星邮局", "回声群岛", "失落航路"
]