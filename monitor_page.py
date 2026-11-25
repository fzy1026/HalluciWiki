"""监控页面的 HTML 渲染（/admin）。纯服务端渲染，无前端依赖，不做任何自动检测。"""

from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

TZ = timezone(timedelta(hours=8))

BADGE = {
    "ok": ("#d5fdf9", "#0b6b56"),
    "cooling": ("#fef6e7", "#8a5300"),
    "dead": ("#fee7e6", "#b32424"),
    "unconfigured": ("#eaecf0", "#54595d"),
    "disabled": ("#eaecf0", "#72777d"),
    "unknown": ("#eaf3ff", "#3366cc"),
}


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _ts(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value, TZ).strftime("%m-%d %H:%M:%S")


def _badge(state: str, text: str) -> str:
    bg, fg = BADGE.get(state, BADGE["unknown"])
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:11px;'
            f'background:{bg};color:{fg};font-size:12px;font-weight:700;white-space:nowrap;">'
            f'{_e(text)}</span>')


def _quota(quota: dict) -> str:
    if not quota:
        return '<span style="color:#a2a9b1;">—</span>'
    labels = {
        "x-ratelimit-remaining-requests": "请求",
        "x-ratelimit-remaining-tokens": "token",
        "x-ratelimit-limit-requests": "上限",
        "retry-after": "等待",
    }
    parts = []
    for key, label in labels.items():
        if key in quota:
            suffix = "s" if key == "retry-after" else ""
            parts.append(f"{label} {_e(quota[key])}{suffix}")
    return "<br>".join(parts) if parts else _e(quota)


def _latency(ms: int | None) -> str:
    if ms is None:
        return "—"
    color = "#14866d" if ms < 1500 else ("#8a5300" if ms < 5000 else "#b32424")
    return f'<span style="color:{color};">{ms} ms</span>'


def render(data: dict) -> str:
    ov = data["overview"]
    rows = data["rows"]
    rate = "—"
    if ov["calls"]:
        rate = f'{ov["success"] * 100 / ov["calls"]:.0f}%'

    chips = [
        ("配置模型", ov["total"], "#202122"),
        ("可用", ov["usable"], "#0b6b56"),
        ("冷却中", ov["cooling"], "#8a5300"),
        ("不可用", ov["dead"], "#b32424"),
        ("未配置密钥", ov["unconfigured"], "#54595d"),
        ("累计调用", ov["calls"], "#202122"),
        ("成功率", rate, "#3366cc"),
    ]
    chip_html = "".join(
        f'<span style="display:inline-block;padding:6px 12px;margin:0 8px 8px 0;border-radius:6px;'
        f'background:#f8f9fa;border:1px solid #eaecf0;font-size:13px;color:#54595d;">'
        f'{_e(label)} <strong style="color:{color};font-size:15px;">{_e(value)}</strong></span>'
        for label, value, color in chips
    )

    body_rows = []
    for r in rows:
        note = ""
        if r["consecutive"]:
            note = f'<div style="color:#8a5300;font-size:11px;margin-top:2px;">连续失败 {r["consecutive"]} 次</div>'
        if r["cooldown_left"]:
            note += f'<div style="color:#8a5300;font-size:11px;margin-top:2px;">{r["cooldown_left"]} 秒后重试</div>'
        if r["chars"]:
            note += f'<div style="color:#72777d;font-size:11px;margin-top:2px;">最近输出 {r["chars"]} 字</div>'

        echo = ""
        if r["echo_model"] and r["echo_model"] != r["model"]:
            echo = f'<div style="color:#72777d;font-size:11px;">实际返回 {_e(r["echo_model"])}</div>'

        inspect_url = "/admin/probe?key=" + quote(r["key_id"], safe="")
        body_rows.append(f"""
    <tr style="border-top:1px solid #eaecf0;vertical-align:top;">
      <td style="padding:10px 8px;text-align:center;font-weight:700;color:#54595d;">{r["priority"]}</td>
      <td style="padding:10px 8px;white-space:nowrap;">{_badge(r["state"], r["state_text"])}{note}</td>
      <td style="padding:10px 8px;font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#54595d;white-space:nowrap;">{_e(r["kind"])}</td>
      <td style="padding:10px 8px;">
        <div style="font-weight:700;color:#202122;">{_e(r["name"])}</div>
        <div style="font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#3366cc;">{_e(r["model"])}</div>
        {echo}
      </td>
      <td style="padding:10px 8px;font-size:11px;color:#54595d;white-space:nowrap;">{_e(r["base_url"])}</td>
      <td style="padding:10px 8px;font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#72777d;white-space:nowrap;">{_e(r["key"])}</td>
      <td style="padding:10px 8px;font-size:12px;white-space:nowrap;">{_latency(r["latency_ms"])}</td>
      <td style="padding:10px 8px;font-size:12px;color:#54595d;white-space:nowrap;">{_e(r["last_http"] or "—")}</td>
      <td style="padding:10px 8px;font-size:12px;color:#54595d;white-space:nowrap;">{_quota(r["quota"])}</td>
      <td style="padding:10px 8px;font-size:12px;white-space:nowrap;">
        <span style="color:#0b6b56;">{r["success"]}</span> / <span style="color:{"#b32424" if r["failure"] else "#72777d"};">{r["failure"]}</span>
      </td>
      <td style="padding:10px 8px;font-size:12px;color:#54595d;white-space:nowrap;">{_ts(r["last_used"] or r["last_checked"])}</td>
      <td style="padding:10px 8px;white-space:nowrap;">
        <form method="post" action="{inspect_url}" style="margin:0;">
          <button type="submit" style="padding:4px 10px;border-radius:4px;border:1px solid #3366cc;
                  background:#fff;color:#3366cc;font-size:12px;cursor:pointer;">检测</button>
        </form>
      </td>
      <td style="padding:10px 8px;font-size:11px;color:#b32424;word-break:break-word;max-width:240px;">{_e(r["last_error"]) or "—"}</td>
    </tr>""")

    warn = ""
    if data.get("load_error"):
        warn = (f'<div style="background:#fee7e6;border:1px solid #f5c2c0;border-radius:6px;'
                f'padding:10px 14px;margin:0 0 16px;color:#b32424;font-size:13px;">'
                f'<strong>models.json 解析失败</strong>，当前沿用上一份可用配置：<br>'
                f'{_e(data["load_error"])}</div>')

    if not rows:
        warn += ('<div style="background:#f8f9fa;border:1px solid #eaecf0;border-radius:6px;'
                 'padding:14px;margin:0 0 16px;color:#54595d;font-size:13px;">'
                 'models.json 里还没有任何模型。编辑后保存即生效，无需重启。</div>')

    return f"""
<style>
  .mp-btn {{ display:inline-block; padding:7px 15px; border-radius:5px; border:1px solid #3366cc;
            background:#3366cc; color:#fff; font-size:13px; font-weight:700; cursor:pointer;
            text-decoration:none; }}
  .mp-btn.ghost {{ background:#fff; color:#3366cc; }}
  .mp-btn:hover {{ opacity:.88; }}
  .mp-table {{ width:100%; min-width:1560px; border-collapse:collapse; font-size:13px; background:#fff;
               border:1px solid #c8ccd1; border-radius:6px; overflow:hidden; }}
  .mp-table th {{ background:#f8f9fa; color:#54595d; font-size:12px; font-weight:700;
                  text-align:left; padding:9px 8px; white-space:nowrap; border-bottom:1px solid #c8ccd1; }}
</style>

<div class="article-meta">
  <span class="article-badge">运维</span>
  <span>模型轮询状况 · 手动检测</span>
</div>
<h1>模型状态监控</h1>

<p style="color:#54595d;font-size:14px;">
下表<b>完全由 models.json 生成</b>：写了几个就有几行，<b>序号即优先级</b>——条目生成时从 1 号开始试，
失败或额度用尽就自动切到下一个。本页<b>不会自动检测</b>，状态只在你点「检测」或真实生成时更新。
数据自容器启动起累计，渲染于 {_ts(data["generated_at"])}（UTC+8）。
</p>

<div style="margin:0 0 14px;">{chip_html}</div>

{warn}

<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 14px;">
  <form method="post" action="/admin/probe" style="margin:0;">
    <button class="mp-btn" type="submit">检测全部</button>
  </form>
  <a class="mp-btn ghost" href="/admin">刷新本页</a>
  <span style="color:#a2a9b1;font-size:12px;">每个模型只发 4 个 token 的 ping，几乎不消耗额度</span>
</div>

<div style="overflow-x:auto;">
<table class="mp-table">
  <thead>
    <tr>
      <th>优先级</th><th>状态</th><th>种类</th><th>名称 / 型号</th><th>端点</th><th>Key</th>
      <th>延迟</th><th>HTTP</th><th>额度</th><th>成功/失败</th><th>最近使用</th><th>检测</th><th>最近错误</th>
    </tr>
  </thead>
  <tbody>{''.join(body_rows) if body_rows else '<tr><td colspan="13" style="padding:18px;text-align:center;color:#a2a9b1;">暂无数据</td></tr>'}</tbody>
</table>
</div>

<h2 style="margin-top:28px;">怎么加 / 换模型</h2>
<ol style="color:#54595d;font-size:13px;line-height:1.9;">
  <li>编辑服务器上的 <code style="background:#f8f9fa;padding:1px 5px;border-radius:3px;">/opt/hub/apps/halluciwiki/models.json</code>（挂载在容器内 <code style="background:#f8f9fa;padding:1px 5px;border-radius:3px;">/app/models.json</code>）。</li>
  <li><b>种类</b>由 <code>id</code> 表示，<b>型号</b>由 <code>model</code>/<code>models</code> 表示，<b>密钥</b>直接写 <code>api_key</code>；<b>优先级</b>就是数组里的先后顺序，想调优先级就挪数组位置。</li>
  <li>保存即生效（热加载），刷新本页就能看到新行，然后点「检测」确认它真的能用。</li>
  <li>同一家多个型号：把 <code>"model": "A"</code> 换成 <code>"models": ["A", "B"]</code>，每个型号各占一个优先级。</li>
  <li><code>enabled: false</code> 的模型不参与轮询，但仍会显示在表里。</li>
</ol>
"""
