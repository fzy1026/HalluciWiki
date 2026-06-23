# HalluciWiki —— 幻觉维基百科

一个由 AI 动态生成的“幻觉维基百科”，所有页面均由大语言模型实时创造，支持缓存和二次修正。

## 功能

- 访问 `/wiki/页面标题` 动态生成 Wikipedia 风格的 HTML 页面（允许虚构）
- 首次生成后自动缓存为静态 HTML，后续访问直接返回
- 页面生成后会通过审核提示词进行二次修正，提升输出质量
- 首页 `/` 和 `/about` 使用预加载模板
- `/search?q=关键词` 生成包含虚构条目的搜索结果页
- 异步高性能（FastAPI + httpx）
- 所有 AI 配置和提示词独立文件管理

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd halluciwiki
```
