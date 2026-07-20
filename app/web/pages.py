"""Dependency-free HTML shells; all data is loaded from versioned local APIs."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.security.browser import set_csrf_cookie
from app.security.local_access import require_local_access

router = APIRouter(include_in_schema=False)

_PAGES = {
    "/": ("投资概览", "overview"),
    "/portfolio": ("持仓录入", "portfolio"),
    "/evidence": ("证据列表", "evidence"),
    "/reports": ("历史报告", "reports"),
    "/sources": ("数据源", "sources"),
    "/jobs": ("任务运行", "jobs"),
    "/settings": ("设置", "settings"),
}


def _content(page: str) -> str:
    if page == "overview":
        return """
<section class="hero"><p class="eyebrow">个人半导体 ETF 训练台</p>
<h1>把杂乱消息，变成可复核的决策证据</h1>
<p>只生成建议，不登录支付宝，不执行申购或赎回。</p></section>
<section class="grid metrics" id="overview-metrics" aria-live="polite"></section>
<section class="panel"><div class="panel-head"><div><p class="eyebrow">最新结论</p>
<h2 id="latest-label">尚无报告</h2></div><a class="button ghost" href="/reports">查看历史</a></div>
<p id="latest-summary">先运行抓取与分析。</p>
<div id="latest-warning" class="warning hidden"></div></section>
<section class="panel action-panel"><div><p class="eyebrow">本地闭环</p><h2>生成一次新分析</h2>
<p>组合总额只用于本地计算仓位比例，不发送给 AI。</p></div>
<label>组合参考总额（元）
<input id="portfolio-value" inputmode="decimal" placeholder="例如 3000"></label>
<button class="button" id="run-analysis">运行分析</button>
<p id="analysis-status" class="status"></p></section>
"""
    if page == "portfolio":
        return """
<section class="page-title"><p class="eyebrow">本地记录</p><h1>持仓录入</h1>
<p>把支付宝中的最新数字手动填到这里；系统不读取账户。</p></section>
<section id="portfolio-list" class="stack" aria-live="polite"></section>
"""
    if page == "evidence":
        return """
<section class="page-title"><p class="eyebrow">可追溯</p><h1>证据列表</h1>
<p>每条声明都保留方向、评分和原始来源。</p></section>
<section class="panel table-wrap"><table><thead><tr>
<th>方向</th><th>声明</th><th>分数</th><th>来源</th></tr></thead>
<tbody id="evidence-list"></tbody></table></section>
"""
    if page == "reports":
        return """
<section class="page-title"><p class="eyebrow">不可变快照</p><h1>历史报告</h1>
<p>旧报告保留当时的规则、证据和模板版本。</p></section>
<section id="report-list" class="stack"></section>
"""
    if page == "sources":
        return """
<section class="page-title"><p class="eyebrow">采集健康</p><h1>数据源</h1>
<p>来源失败会清楚显示，不会被隐藏在最终结论后面。</p></section>
<section class="panel"><button class="button" id="run-crawl">立即检索最近 3 小时</button>
<p id="crawl-status" class="status"></p></section><section id="source-list" class="grid"></section>
"""
    if page == "jobs":
        return """
<section class="page-title"><p class="eyebrow">运行记录</p><h1>任务</h1></section>
<section class="panel table-wrap"><table><thead><tr>
<th>类型</th><th>状态</th><th>计划时间</th><th>完成时间</th></tr></thead>
<tbody id="job-list"></tbody></table></section>
"""
    return """
<section class="page-title"><p class="eyebrow">当前运行环境</p><h1>设置</h1>
<p>密钥不在页面显示或编辑，只告知是否已配置。</p></section>
<section id="settings-list" class="grid"></section>
"""


def _page(request: Request, title: str, page: str) -> HTMLResponse:
    require_local_access(request)
    token = escape(request.app.state.csrf_token, quote=True)
    nav = "".join(
        f'<a href="{path}" class="{"active" if item_page == page else ""}">{escape(name)}</a>'
        for path, (name, item_page) in _PAGES.items()
    )
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="investment-csrf" content="{token}"><title>{escape(title)} · ETF 投资分析</title>
<link rel="stylesheet" href="/static/app.css"></head><body data-page="{escape(page)}">
<header class="topbar"><a class="brand" href="/"><span>ETF</span>证据工作台</a>
<nav>{nav}</nav></header>
<main class="container">{_content(page)}</main><footer>仅供个人决策参考 · 系统不执行交易</footer>
<script src="/static/app.js" defer></script></body></html>"""
    response = HTMLResponse(html)
    set_csrf_cookie(response, request)
    return response


def _endpoint(title: str, page_name: str):
    def page_endpoint(request: Request) -> HTMLResponse:
        return _page(request, title, page_name)

    return page_endpoint


for path, (title, page_name) in _PAGES.items():
    router.add_api_route(path, _endpoint(title, page_name), methods=["GET"])


@router.get("/reports/{report_id}")
def report_detail(report_id: str, request: Request) -> HTMLResponse:
    require_local_access(request)
    safe_id = escape(report_id, quote=True)
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>报告详情</title>
<link rel="stylesheet" href="/static/app.css"></head><body><header class="topbar">
<a class="brand" href="/reports"><span>ETF</span>返回报告列表</a></header>
<main class="report-frame"><iframe title="投资分析报告"
sandbox="allow-popups allow-popups-to-escape-sandbox"
src="/api/v1/reports/{safe_id}/html"></iframe></main></body></html>"""
    response = HTMLResponse(html)
    set_csrf_cookie(response, request)
    return response
