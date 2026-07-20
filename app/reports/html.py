"""Stable plain-HTML renderer that treats every external string as untrusted."""

from __future__ import annotations

from hashlib import sha256
from html import escape
from urllib.parse import urlsplit, urlunsplit

from app.domain.analysis import Report
from app.domain.contracts import RenderedReport, ReportRenderRequest
from app.domain.enums import ReportFormat

HTML_TEMPLATE_VERSION = "report-html-1.0.0"
_MEDIA_TYPE = "text/html; charset=utf-8"
_LABELS = {
    "INSUFFICIENT_DATA": "信息不足",
    "OBSERVE": "继续观察",
    "HOLD": "持有不动",
    "SMALL_ADD": "小额加仓参考",
    "PAUSE_ADDING": "暂停加仓",
    "REBALANCE": "再平衡参考",
    "REDUCE": "保守减仓参考",
}


def safe_external_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("report source URL has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port not in {80, 443})
    ):
        raise ValueError("report source URL must be a credential-free HTTP(S) URL")
    return urlunsplit(parsed)


def _list(items: tuple[str, ...], *, empty: str = "暂无") -> str:
    if not items:
        return f"<p>{escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


class HTMLReportRenderer:
    renderer_name = "plain-html-report-renderer"
    template_version = HTML_TEMPLATE_VERSION

    async def render(self, request: ReportRenderRequest) -> RenderedReport:
        if request.output_format is not ReportFormat.HTML:
            raise ValueError("the first report renderer supports HTML only")
        report = request.report
        if report.template_version != self.template_version:
            raise ValueError("report template version does not match the renderer")
        content = self._render_html(report).encode("utf-8")
        return RenderedReport(
            report_id=report.report_id,
            output_format=ReportFormat.HTML,
            media_type=_MEDIA_TYPE,
            content=content,
            content_sha256=sha256(content).hexdigest(),
        )

    def _render_html(self, report: Report) -> str:
        decision = report.decision
        analysis = report.analysis
        amount = ""
        if decision.reference_amount is not None:
            amount = (
                "<p><strong>非强制金额参考：</strong>"
                f"{escape(str(decision.reference_amount.minimum.amount))}–"
                f"{escape(str(decision.reference_amount.maximum.amount))} "
                f"{escape(decision.reference_amount.minimum.currency)}</p>"
            )
        if decision.reference_reduce_fraction is not None:
            amount = (
                "<p><strong>非强制减仓上限：</strong>"
                f"{escape(str(decision.reference_reduce_fraction * 100))}%</p>"
            )
        stale = (
            '<p role="alert"><strong>⚠ 数据已过期，仅用于回顾，不应据此操作。</strong></p>'
            if report.data_is_stale
            else "<p>数据在时效范围内。</p>"
        )
        chains = []
        for chain in analysis.causal_chains:
            steps = "".join(
                "<li>"
                f"{escape(step.evidence_id)}：{escape(step.relation)} "
                f"(置信度 {escape(str(step.confidence))})"
                "</li>"
                for step in chain.steps
            )
            chains.append(
                "<li>"
                f"{escape(chain.conclusion)} (置信度 {escape(str(chain.confidence))})"
                f"<ol>{steps}</ol></li>"
            )
        source_items = []
        for source in report.sources:
            url = escape(safe_external_url(str(source.url)), quote=True)
            source_items.append(
                "<li>"
                f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">'
                f"{escape(source.title)}</a> "
                f"[来源 {escape(source.source_id)} / 健康 {escape(source.health_status)} / "
                f"证据 {escape(source.evidence_id)}]"
                "</li>"
            )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src 'none'; style-src 'none'; script-src 'none';
                 frame-src 'none'; base-uri 'none'; form-action 'none'">
  <meta name="referrer" content="no-referrer">
  <title>ETF 投资分析报告</title>
</head>
<body>
<main>
  <h1>ETF 投资分析报告</h1>
  {stale}
  <p><strong>资产：</strong>{escape(report.asset_id)}</p>
  <p><strong>数据截止：</strong>{escape(report.data_as_of.isoformat())}</p>
  <p><strong>报告生成：</strong>{escape(report.generated_at.isoformat())}</p>
  <h2>当前建议</h2>
  <p><strong>{escape(_LABELS[decision.label.value])}</strong>
     / 建议强度 {escape(str(decision.strength))}</p>
  {amount}
  {_list(decision.reasons)}
  <h2>综合分析</h2>
  <p><strong>立场：</strong>{escape(analysis.stance)}
     / 置信度 {escape(str(analysis.confidence))}</p>
  <p>{escape(analysis.summary)}</p>
  <h3>支持上涨的证据</h3>
  {_list(analysis.bullish_factors)}
  <h3>支持下跌的证据</h3>
  {_list(analysis.bearish_factors)}
  <h3>因果链</h3>
  {("<ol>" + "".join(chains) + "</ol>") if chains else "<p>暂无已验证因果链。</p>"}
  <h3>未确认信息</h3>
  {_list(analysis.uncertainties)}
  <h3>建议失效条件</h3>
  {_list(analysis.invalidation_triggers)}
  <h2>原始来源</h2>
  {("<ul>" + "".join(source_items) + "</ul>") if source_items else "<p>暂无可用来源链接。</p>"}
  <h2>版本与限制</h2>
  <ul>
    <li>规则：{escape(report.rule_version)}</li>
    <li>Prompt：{escape(report.prompt_version)}</li>
    <li>模型：{escape(analysis.model_version)}</li>
    <li>模板：{escape(report.template_version)}</li>
  </ul>
  <p><strong>{escape(report.disclaimer)}</strong></p>
  <p>不承诺收益；系统不执行申购、赎回或任何账户操作。</p>
</main>
</body>
</html>"""
