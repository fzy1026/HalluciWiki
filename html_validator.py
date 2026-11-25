import re

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

TAG_PATTERN = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*/?>')
COMMENT_PATTERN = re.compile(r'<!--.*?-->', re.DOTALL)


def validate_html(html: str) -> list[str]:
    """使用栈式括号匹配算法校验 HTML 标签闭合。
    返回错误信息列表，空列表表示格式正确。
    """
    errors: list[str] = []
    cleaned = COMMENT_PATTERN.sub("", html)
    stack: list[str] = []

    for match in TAG_PATTERN.finditer(cleaned):
        tag_str = match.group(0)
        tag_name = match.group(1).lower()

        if tag_str.startswith("</"):
            if not stack:
                errors.append(f"多余的闭合标签 </{tag_name}>，没有对应的开始标签")
                continue
            expected = stack.pop()
            if expected != tag_name:
                errors.append(f"标签不匹配：期望 </{expected}>，但遇到 </{tag_name}>")
        elif tag_str.endswith("/>") or tag_name in VOID_ELEMENTS:
            continue
        else:
            stack.append(tag_name)

    for tag in reversed(stack):
        errors.append(f"未闭合的标签 <{tag}>")

    return errors