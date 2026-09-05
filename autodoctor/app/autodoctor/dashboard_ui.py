from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from . import AUTODOCTOR_VERSION
from .redact import redact

_INACTIVE_CASE_STATUSES = {"historical", "resolved", "suppressed_nonfatal"}
_MANUAL_RESOLVE_STATUSES = {"new", "diagnosed", "needs_user_action", "reopened"}
_STATUS_ORDER = {
    "repair_available": 0,
    "needs_user_action": 1,
    "verifying": 2,
    "investigating": 3,
    "reopened": 4,
    "new": 5,
    "diagnosed": 6,
}


def _esc(value: Any, *, quote: bool = False) -> str:
    return html.escape(str(value if value is not None else ""), quote=quote)


def _safe_text(value: Any, limit: int = 800) -> str:
    return _esc(redact(str(value or ""))[:limit])


def _timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%d %b %Y · %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"


def _money(value: Any, places: int = 3) -> str:
    try:
        return f"${float(value):.{places}f}"
    except (TypeError, ValueError):
        return "$0.000"


def _status_label(status: str) -> str:
    labels = {
        "repair_available": "Repair ready",
        "needs_user_action": "Needs attention",
        "verifying": "Verifying",
        "investigating": "Investigating",
        "reopened": "Reopened",
        "new": "New",
        "diagnosed": "Diagnosed",
        "historical": "Historical",
        "resolved": "Resolved",
        "suppressed_nonfatal": "Observed only",
    }
    return labels.get(status, status.replace("_", " ").title() or "Unknown")


def _status_class(status: str) -> str:
    if status in {"repair_available", "needs_user_action"}:
        return "danger"
    if status in {"investigating", "verifying", "reopened", "new"}:
        return "warning"
    if status in {"resolved"}:
        return "success"
    if status in {"historical", "suppressed_nonfatal"}:
        return "muted"
    return "info"


def _metric(label: str, value: Any, *, hint: str = "", state: str = "") -> str:
    state_class = f" metric--{state}" if state else ""
    hint_html = f'<div class="metric__hint">{_esc(hint)}</div>' if hint else ""
    return (
        f'<article class="metric{state_class}">'
        f'<div class="metric__label">{_esc(label)}</div>'
        f'<div class="metric__value">{_esc(value)}</div>'
        f"{hint_html}</article>"
    )


def _case_sort_key(case: dict[str, Any]) -> tuple[int, float]:
    status = str(case.get("status") or "")
    return (_STATUS_ORDER.get(status, 99), -float(case.get("last_seen") or 0))


def _case_card(case: dict[str, Any], approval_nonce: str) -> str:
    status = str(case.get("status") or "unknown")
    label = str(case.get("pattern_label") or case.get("pattern_key") or "Unknown case")
    family = str(case.get("family") or "unknown")
    occurrences = int(case.get("occurrences") or 0)
    fingerprints = int(case.get("fingerprint_count") or 0)
    confidence = float(case.get("confidence") or 0)
    risk = str(case.get("risk") or "")
    summary = str(case.get("summary") or "")
    summary_html = _safe_text(summary or "Awaiting diagnosis.", 700)
    risk_text = f" · {_esc(risk.title())} risk" if risk else ""
    confidence_text = f" · {confidence:.0%} confidence" if confidence > 0 else ""

    controls = ""
    if status in _MANUAL_RESOLVE_STATUSES:
        controls = (
            '<form method="post" action="./api/cases/resolve" class="case__action">'
            f'<input type="hidden" name="approval_nonce" value="{_esc(approval_nonce, quote=True)}">'
            f'<input type="hidden" name="pattern_key" value="{_esc(case.get("pattern_key"), quote=True)}">'
            '<button class="button button--quiet" type="submit" '
            'title="Marks this AutoDoctor case resolved and dismisses its AutoDoctor notification. A recurrence reopens it.">'
            'Mark resolved & dismiss</button></form>'
        )

    return (
        '<article class="case">'
        '<div class="case__header">'
        '<div class="case__title-wrap">'
        f'<span class="badge badge--{_status_class(status)}">{_esc(_status_label(status))}</span>'
        f'<h3 class="case__title">{_safe_text(label, 180)}</h3>'
        '</div>'
        f'<div class="case__time">{_esc(_timestamp(case.get("last_seen")))}</div>'
        '</div>'
        f'<div class="case__meta">{_safe_text(family, 100)} · {occurrences:,} occurrence(s) · '
        f'{fingerprints:,} fingerprint(s){risk_text}{confidence_text}</div>'
        f'<p class="case__summary">{summary_html}</p>'
        f'{controls}'
        '</article>'
    )


def _repair_card(plan: dict[str, Any], executor: Any, approval_nonce: str) -> str:
    plan_id = str(plan.get("plan_id") or "")
    allowed, reason, _target = executor.validate_plan(plan)
    confidence = float(plan.get("confidence") or 0)
    risk = str(plan.get("risk") or "unknown")
    summary = _safe_text(plan.get("summary") or "Repair plan awaiting review.", 800)
    repair_type = _safe_text(plan.get("repair_type") or "manual_review", 100)

    reject = (
        f'<form method="post" action="./api/repair-plans/{_esc(plan_id, quote=True)}/reject">'
        f'<input type="hidden" name="approval_nonce" value="{_esc(approval_nonce, quote=True)}">'
        '<button class="button button--quiet" type="submit">Reject plan</button></form>'
    )
    approve = ""
    if bool(getattr(executor, "enabled", False)) and allowed:
        approve = (
            f'<form method="post" action="./api/repair-plans/{_esc(plan_id, quote=True)}/approve">'
            f'<input type="hidden" name="approval_nonce" value="{_esc(approval_nonce, quote=True)}">'
            '<button class="button button--primary" type="submit">Approve one config-entry reload</button></form>'
        )
    else:
        approve = f'<div class="repair__reason">Not executable: {_safe_text(reason, 240)}</div>'

    return (
        '<article class="repair">'
        '<div class="repair__top">'
        '<span class="badge badge--danger">Approval required</span>'
        f'<span class="repair__meta">{_esc(risk.title())} risk · {confidence:.0%} · {repair_type}</span>'
        '</div>'
        f'<p class="repair__summary">{summary}</p>'
        '<p class="repair__guardrail">Approval can execute only the independently validated fixed repair shown here. '
        'The AI cannot call this endpoint or select another Home Assistant service.</p>'
        f'<div class="repair__actions">{approve}{reject}</div>'
        '</article>'
    )


def _incident_row(item: dict[str, Any]) -> str:
    analysis = None
    raw_analysis = item.get("analysis_json")
    if raw_analysis:
        try:
            analysis = json.loads(str(raw_analysis))
        except json.JSONDecodeError:
            analysis = None
    ai = "Not analysed"
    if isinstance(analysis, dict):
        risk = str(analysis.get("risk") or "unknown")
        summary = _safe_text(analysis.get("summary") or "", 250)
        ai = f'<span class="badge badge--{_status_class(risk)}">{_esc(risk.title())}</span> {summary}'

    return (
        '<tr>'
        f'<td>{_esc(_timestamp(item.get("last_seen")))}</td>'
        f'<td>{int(item.get("occurrences") or 0):,}</td>'
        f'<td><strong>{_safe_text(item.get("pattern_label") or "", 100)}</strong></td>'
        f'<td>{_safe_text(item.get("name") or "", 140)}</td>'
        f'<td class="message">{_safe_text(item.get("message") or "", 340)}</td>'
        f'<td>{ai}</td>'
        '</tr>'
    )


def render_dashboard(
    *,
    health: dict[str, Any],
    incidents: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    executor_health: dict[str, Any],
    executor: Any,
    approval_nonce: str,
) -> str:
    """Render the complete Home Assistant ingress dashboard without external assets."""

    case_health = health.get("case_management") or {}
    statuses = case_health.get("cases_by_status") or {}
    budget = health.get("ai_budget") or {}
    mcp = health.get("mcp") or {}
    triage = case_health.get("backlog_triage") or {}
    lifecycle = case_health.get("notification_lifecycle") or {}
    nonfatal = case_health.get("nonfatal_observation_filter") or {}
    target = case_health.get("private_target_resolution") or {}

    active_cases = [
        case for case in cases if str(case.get("status") or "") not in _INACTIVE_CASE_STATUSES
    ]
    active_cases.sort(key=_case_sort_key)
    proposed = [plan for plan in plans if str(plan.get("status") or "") == "proposed"]

    watcher = str(health.get("status") or "unknown")
    watcher_good = watcher.lower() in {"ok", "healthy", "running", "watching"}
    mcp_connected = bool(mcp.get("connected"))
    executor_enabled = bool(executor_health.get("enabled"))
    spend = _money(budget.get("spent_usd")) if budget.get("enabled") else "Locked"
    budget_stop = _money(budget.get("stop_threshold_usd"), 2) if budget.get("enabled") else "AI budget off"

    metrics = "".join(
        (
            _metric("Watcher", watcher.title(), hint="Live system-log stream", state="good" if watcher_good else "warn"),
            _metric("Active cases", f"{len(active_cases):,}", hint="Cases still needing monitoring or action", state="warn" if active_cases else "good"),
            _metric("Repair approvals", f"{len(proposed):,}", hint="Always requires your approval", state="danger" if proposed else "good"),
            _metric("AI spend", spend, hint=f"Stop threshold {budget_stop}"),
            _metric("MCP diagnostics", "Connected" if mcp_connected else "Offline", hint=str(mcp.get("server_profile") or "read-only"), state="good" if mcp_connected else "warn"),
            _metric("Repair executor", "Approval gated" if executor_enabled else "Off", hint="Automatic repairs are always off", state="good"),
        )
    )

    repair_html = "".join(
        _repair_card(plan, executor, approval_nonce) for plan in proposed
    ) or (
        '<div class="empty"><div class="empty__icon">✓</div>'
        '<div><strong>No repair plans awaiting approval</strong>'
        '<p>AutoDoctor will surface a plan here only after deterministic safety checks.</p></div></div>'
    )

    case_html = "".join(_case_card(case, approval_nonce) for case in active_cases[:40]) or (
        '<div class="empty"><div class="empty__icon">✓</div>'
        '<div><strong>No active cases</strong><p>Nothing currently needs your attention.</p></div></div>'
    )

    incident_rows = "".join(_incident_row(item) for item in incidents[:50]) or (
        '<tr><td colspan="6" class="empty-cell">No incident evidence captured yet.</td></tr>'
    )

    lifecycle_summary = (
        f'{int(statuses.get("resolved", 0)):,} resolved · '
        f'{int(statuses.get("historical", 0)):,} historical · '
        f'{int(statuses.get("suppressed_nonfatal", 0)):,} observed-only'
    )
    nonfatal_count = int(statuses.get("suppressed_nonfatal", 0))
    lifecycle_dismissals = int(lifecycle.get("notification_dismissals", 0))
    dismissal_failures = int(lifecycle.get("notification_dismiss_failures", 0))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="30">
<title>AutoDoctor · Home Assistant</title>
<style>
:root{{--bg:#f5f7fb;--surface:#ffffff;--surface-2:#f8fafc;--text:#172033;--muted:#667085;--border:#e4e7ec;--primary:#5865f2;--primary-soft:#eef0ff;--good:#157f5b;--good-soft:#e9f8f1;--warn:#a35b00;--warn-soft:#fff4dc;--danger:#b42318;--danger-soft:#feeceb;--shadow:0 10px 30px rgba(16,24,40,.06);--radius:18px}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d1117;--surface:#151b23;--surface-2:#111720;--text:#eef2f7;--muted:#9aa6b2;--border:#273241;--primary:#8b93ff;--primary-soft:#212649;--good:#6ed6ad;--good-soft:#132d26;--warn:#ffc36b;--warn-soft:#332713;--danger:#ff948d;--danger-soft:#361b1d;--shadow:0 10px 30px rgba(0,0,0,.2)}}}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
button,input{{font:inherit}}a{{color:inherit}}.shell{{max-width:1440px;margin:0 auto;padding:24px clamp(16px,3vw,38px) 56px}}.topbar{{display:flex;gap:20px;align-items:flex-start;justify-content:space-between;margin-bottom:22px}}.brand{{display:flex;gap:14px;align-items:center}}.brand__mark{{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(145deg,var(--primary),#7c5cff);color:white;font-size:23px;box-shadow:var(--shadow)}}h1{{font-size:clamp(24px,3vw,34px);line-height:1.1;margin:0;letter-spacing:-.03em}}.brand__sub{{color:var(--muted);margin-top:4px}}.topbar__actions{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}}.version{{padding:7px 10px;background:var(--surface);border:1px solid var(--border);border-radius:999px;color:var(--muted);font-weight:600}}.button{{appearance:none;border:0;border-radius:11px;padding:9px 13px;font-weight:700;cursor:pointer;transition:transform .12s ease,background .12s ease;display:inline-flex;align-items:center;justify-content:center;text-decoration:none}}.button:hover{{transform:translateY(-1px)}}.button:focus-visible{{outline:3px solid color-mix(in srgb,var(--primary) 45%,transparent);outline-offset:2px}}.button--primary{{background:var(--primary);color:white}}.button--quiet{{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}}
.metrics{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:18px}}.metric{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;min-width:0;box-shadow:var(--shadow)}}.metric__label{{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}}.metric__value{{font-size:clamp(19px,2vw,26px);font-weight:800;letter-spacing:-.025em;margin:4px 0 1px;overflow-wrap:anywhere}}.metric__hint{{font-size:12px;color:var(--muted)}}.metric--good{{border-top:3px solid var(--good)}}.metric--warn{{border-top:3px solid var(--warn)}}.metric--danger{{border-top:3px solid var(--danger)}}
.safety{{display:grid;grid-template-columns:auto 1fr;gap:12px;background:var(--primary-soft);border:1px solid color-mix(in srgb,var(--primary) 25%,var(--border));border-radius:var(--radius);padding:15px 17px;margin-bottom:24px}}.safety__icon{{font-size:20px}}.safety strong{{display:block;margin-bottom:2px}}.safety p{{margin:0;color:var(--muted)}}
.section{{margin-top:26px}}.section__head{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:11px}}h2{{font-size:19px;margin:0;letter-spacing:-.015em}}.section__hint{{font-size:12px;color:var(--muted);text-align:right}}.stack{{display:grid;gap:10px}}.repair,.case,.panel,.empty{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}}.repair{{padding:17px;border-left:4px solid var(--danger)}}.repair__top,.case__header{{display:flex;align-items:center;justify-content:space-between;gap:12px}}.repair__meta,.case__time,.case__meta,.repair__reason{{color:var(--muted);font-size:12px}}.repair__summary{{font-size:15px;font-weight:650;margin:12px 0 5px}}.repair__guardrail{{color:var(--muted);margin:0;font-size:12px}}.repair__actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:13px}}.repair__actions form{{margin:0}}.case{{padding:16px}}.case__title-wrap{{display:flex;align-items:center;gap:9px;min-width:0}}.case__title{{font-size:15px;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.case__meta{{margin:7px 0 5px}}.case__summary{{margin:0;color:var(--text)}}.case__action{{margin:12px 0 0}}.badge{{display:inline-flex;align-items:center;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:800;white-space:nowrap}}.badge--success{{background:var(--good-soft);color:var(--good)}}.badge--warning{{background:var(--warn-soft);color:var(--warn)}}.badge--danger{{background:var(--danger-soft);color:var(--danger)}}.badge--muted{{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}}.badge--info{{background:var(--primary-soft);color:var(--primary)}}.empty{{padding:20px;display:flex;gap:13px;align-items:center;color:var(--muted)}}.empty__icon{{width:34px;height:34px;border-radius:50%;background:var(--good-soft);color:var(--good);font-size:18px;display:grid;place-items:center;font-weight:900}}.empty strong{{color:var(--text)}}.empty p{{margin:2px 0 0}}
.columns{{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}}.panel{{padding:17px}}.panel h3{{margin:0 0 13px;font-size:14px}}.telemetry{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}}.telemetry__item{{padding:11px;background:var(--surface-2);border-radius:12px}}.telemetry__k{{font-size:11px;color:var(--muted);text-transform:uppercase;font-weight:750;letter-spacing:.04em}}.telemetry__v{{font-size:15px;font-weight:750;margin-top:2px;overflow-wrap:anywhere}}details.panel summary{{cursor:pointer;font-weight:750;list-style:none}}details.panel summary::-webkit-details-marker{{display:none}}details.panel summary::after{{content:"＋";float:right;color:var(--muted)}}details[open].panel summary::after{{content:"−"}}.note{{margin-top:12px;padding:12px;border-radius:12px;background:var(--surface-2);color:var(--muted)}}
.table-wrap{{overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}}table{{width:100%;border-collapse:collapse;min-width:920px}}th,td{{padding:11px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:var(--surface);z-index:1;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}}tbody tr:last-child td{{border-bottom:0}}td{{font-size:12px}}td.message{{max-width:420px;color:var(--muted)}}.empty-cell{{text-align:center;color:var(--muted);padding:28px}}.footer{{margin-top:24px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
@media(max-width:1100px){{.metrics{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}@media(max-width:760px){{.shell{{padding-top:18px}}.topbar{{align-items:stretch;flex-direction:column}}.topbar__actions{{justify-content:flex-start}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.columns{{grid-template-columns:1fr}}.repair__top,.case__header{{align-items:flex-start;flex-direction:column}}.case__title-wrap{{align-items:flex-start;flex-direction:column}}.case__title{{white-space:normal}}.section__head{{align-items:flex-start;flex-direction:column}}.section__hint{{text-align:left}}}}@media(max-width:460px){{.metrics{{grid-template-columns:1fr}}.repair__actions{{align-items:stretch;flex-direction:column}}.repair__actions form,.repair__actions .button{{width:100%}}}}
@media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}*{{transition:none!important}}}}
</style>
</head>
<body>
<main class="shell" id="main-content">
<header class="topbar">
  <div class="brand"><div class="brand__mark" aria-hidden="true">✚</div><div><h1>AutoDoctor</h1><div class="brand__sub">Home Assistant incident health & safe repair control</div></div></div>
  <div class="topbar__actions"><span class="version">v{_esc(AUTODOCTOR_VERSION)}</span><a class="button button--quiet" href="./" aria-label="Refresh AutoDoctor dashboard">Refresh</a></div>
</header>
<section class="metrics" aria-label="System overview">{metrics}</section>
<section class="safety" aria-label="Safety status"><div class="safety__icon" aria-hidden="true">🛡️</div><div><strong>Safety boundaries are active</strong><p>MCP diagnostics remain read-only and fail-closed. Private target identifiers are withheld from the AI. Automatic repairs are off; any supported repair requires a deterministic plan and your individual ingress approval.</p></div></section>

<section class="section" aria-labelledby="repair-heading"><div class="section__head"><div><h2 id="repair-heading">Repair plans awaiting review</h2></div><div class="section__hint">Nothing executes without your approval.</div></div><div class="stack">{repair_html}</div></section>

<section class="section" aria-labelledby="cases-heading"><div class="section__head"><div><h2 id="cases-heading">Active cases</h2></div><div class="section__hint">Quiet diagnosed cases retire to history after 24h. A recurrence reopens them automatically.</div></div><div class="stack">{case_html}</div></section>

<section class="section columns" aria-label="Operations and lifecycle">
  <article class="panel"><h3>Notification lifecycle</h3><div class="telemetry">
    <div class="telemetry__item"><div class="telemetry__k">Inactive cases</div><div class="telemetry__v">{_esc(lifecycle_summary)}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Notices dismissed</div><div class="telemetry__v">{lifecycle_dismissals:,}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Dismiss failures</div><div class="telemetry__v">{dismissal_failures:,}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Observed-only Kasa cases</div><div class="telemetry__v">{nonfatal_count:,}</div></div>
  </div><div class="note">Resolved, historical and proven non-fatal observation cases do not keep AutoDoctor notifications. Observed-only Kasa sub-call noise is still retained as evidence but skips AI, notifications and repairs.</div></article>
  <details class="panel"><summary>Operational telemetry</summary><div class="telemetry" style="margin-top:14px">
    <div class="telemetry__item"><div class="telemetry__k">Backlog triage</div><div class="telemetry__v">{_esc('On' if triage.get('enabled') else 'Off')}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Pending triage</div><div class="telemetry__v">{int(triage.get('pending_cases',0)):,}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">AI analyses</div><div class="telemetry__v">{int(budget.get('analyses_count',0)):,}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Budget remaining</div><div class="telemetry__v">{_esc(_money(budget.get('remaining_to_stop_usd')) if budget.get('enabled') else 'Locked')}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Private bindings</div><div class="telemetry__v">{int(target.get('bindings',0)):,}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Private target last result</div><div class="telemetry__v">{_safe_text(target.get('last_result') or 'none',100)}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">Non-fatal evidence filter</div><div class="telemetry__v">{_esc('On' if nonfatal.get('enabled') else 'Off')}</div></div>
    <div class="telemetry__item"><div class="telemetry__k">AI skipped by filter</div><div class="telemetry__v">{int(nonfatal.get('events_suppressed_since_start',0)):,}</div></div>
  </div></details>
</section>

<section class="section" aria-labelledby="evidence-heading"><div class="section__head"><div><h2 id="evidence-heading">Recent incident evidence</h2></div><div class="section__hint">Private network values and secrets are redacted in this view.</div></div><div class="table-wrap" role="region" aria-label="Recent incident evidence" tabindex="0"><table><thead><tr><th>Last seen</th><th>Count</th><th>Pattern</th><th>Logger</th><th>Message</th><th>AI</th></tr></thead><tbody>{incident_rows}</tbody></table></div></section>
<footer class="footer"><span>Auto-refreshes every 30 seconds · Home Assistant ingress only</span><span>AutoDoctor v{_esc(AUTODOCTOR_VERSION)}</span></footer>
</main>
</body>
</html>"""
