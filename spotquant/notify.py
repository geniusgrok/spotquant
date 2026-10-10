"""Email alerts and an optional webhook. No third-party packages.

The notifier reads only ``SPOTQUANT_SMTP_HOST``, ``SPOTQUANT_SMTP_PORT``,
``SPOTQUANT_SMTP_USER``, ``SPOTQUANT_SMTP_PASSWORD``, ``SPOTQUANT_SMTP_FROM``
and ``SPOTQUANT_SMTP_TO`` (comma-separated). On the VPS those live in
``/etc/spotquant/notify.env``. Port 465 uses ``SMTP_SSL`` (implicit TLS).
Port 587 uses STARTTLS. Port 25 is refused. A webhook URL is optional.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from .types import Blocked

ALERT_WINDOW_SECONDS = 6 * 3600
HEARTBEAT_PREFIX = '[spotquant][心跳]'
ALERT_PREFIX = '[spotquant][告警]'


def smtp_settings(env=None) -> dict | None:
    """Read SMTP settings. Missing host means email is not configured."""
    env = os.environ if env is None else env
    host = (env.get('SPOTQUANT_SMTP_HOST') or '').strip()
    if not host:
        return None
    raw_port = (env.get('SPOTQUANT_SMTP_PORT') or '').strip()
    user = (env.get('SPOTQUANT_SMTP_USER') or '').strip()
    password = env.get('SPOTQUANT_SMTP_PASSWORD') or ''
    sender = (env.get('SPOTQUANT_SMTP_FROM') or user).strip()
    recipients = _recipients(env.get('SPOTQUANT_SMTP_TO') or '')
    if not raw_port or not raw_port.isdigit():
        raise Blocked('SPOTQUANT_SMTP_PORT must be 465 (SMTPS) or 587 (STARTTLS)')
    port = int(raw_port)
    if port == 25:
        raise Blocked('outbound SMTP port 25 is blocked on this network; use 465 or 587')
    if port not in (465, 587):
        raise Blocked('SPOTQUANT_SMTP_PORT must be 465 (SMTPS) or 587 (STARTTLS)')
    if not sender or not recipients:
        raise Blocked('SPOTQUANT_SMTP_FROM and SPOTQUANT_SMTP_TO are required')
    if not user or not password:
        raise Blocked('SPOTQUANT_SMTP_USER and SPOTQUANT_SMTP_PASSWORD are required')
    return {
        'host': host,
        'port': port,
        'user': user,
        'password': password,
        'from': sender,
        'to': recipients,
    }


def _recipients(raw: str) -> list[str]:
    people = []
    for part in raw.replace(';', ',').split(','):
        item = part.strip()
        if item and item not in people:
            people.append(item)
    return people


def webhook_url(env=None) -> str | None:
    env = os.environ if env is None else env
    url = (env.get('SPOTQUANT_WEBHOOK_URL') or '').strip()
    return url or None


def send_email(settings: dict, subject: str, body: str, *, smtp_ssl=None, smtp_plain=None,
               timeout=30) -> None:
    """Send one UTF-8 message. 465 wraps TLS immediately; 587 uses STARTTLS."""
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = settings['from']
    message['To'] = ', '.join(settings['to'])
    message.set_content(body, charset='utf-8')
    context = ssl.create_default_context()
    if settings['port'] == 465:
        factory = smtp_ssl or smtplib.SMTP_SSL
        client = factory(settings['host'], settings['port'], timeout=timeout, context=context)
    else:
        factory = smtp_plain or smtplib.SMTP
        client = factory(settings['host'], settings['port'], timeout=timeout)
    try:
        if settings['port'] == 587:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(settings['user'], settings['password'])
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            pass


def post_webhook(url: str, payload: dict, *, urlopen=None, timeout=10) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'},
                                     method='POST')
    opener = urlopen or urllib.request.urlopen
    with opener(request, timeout=timeout) as response:
        if getattr(response, 'status', 200) >= 400:
            raise Blocked(f'webhook returned HTTP {response.status}')


def deliver(subject: str, body: str, *, env=None, smtp_ssl=None, smtp_plain=None, urlopen=None) -> list[str]:
    """Send email when configured, then the optional webhook. Returns channel names."""
    sent = []
    errors = []
    settings = smtp_settings(env)
    hook = webhook_url(env)
    if settings is None and not hook:
        raise Blocked('email is not configured; set SPOTQUANT_SMTP_HOST, PORT, USER, PASSWORD, FROM and TO')
    if settings is not None:
        try:
            send_email(settings, subject, body, smtp_ssl=smtp_ssl, smtp_plain=smtp_plain)
            sent.append('email')
        except Blocked:
            raise
        except Exception as exc:
            errors.append(f'email failed: {exc}')
    if hook:
        try:
            post_webhook(hook, {'subject': subject, 'text': body}, urlopen=urlopen)
            sent.append('webhook')
        except Exception as exc:
            errors.append(f'webhook failed: {exc}')
    if errors and not sent:
        raise Blocked('; '.join(errors))
    if errors:
        raise Blocked('; '.join(errors))
    return sent


def collect_alerts(report: dict, *, exit_code: int = 0) -> list[dict]:
    """Alerts for one finished run. Heartbeats and missed runs are separate."""
    report = report or {}
    risk = report.get('risk_state') or {}
    alerts = []
    if report.get('status') == 'unknown':
        alerts.append(_alert('status-unknown', '状态未知', report.get('reason') or '会话状态未知'))
    if report.get('manual_takeover') or risk.get('manual_takeover'):
        alerts.append(_alert('manual-takeover', '人工接管', '需要人工核对持仓、止损或未知订单'))
    failure = report.get('protection_failure') or (report.get('execution_evidence') or {}).get('protection_failure')
    if failure:
        text = failure.get('reason') if isinstance(failure, dict) else str(failure)
        msg = failure.get('exchange_msg') if isinstance(failure, dict) else None
        detail = text or '保护单未能挂上'
        if msg:
            detail = f'{detail}\n交易所消息：{msg}'
        alerts.append(_alert('protection-failure', '保护失败', detail))
    if report.get('report_persistence_failed'):
        alerts.append(_alert('report-persistence-failed', '报告未能写入', 'latest.json 没有写成功'))
    if exit_code:
        alerts.append(_alert('nonzero-exit', '进程退出码非零', f'退出码 {exit_code}'))
    if _stop_unplaceable(report):
        alerts.append(_alert('stop-unplaceable', '止损无法挂出', '本轮止损被判定为无法挂出'))
    if report.get('buy_halt'):
        alerts.append(_alert('buy-halt', '回撤停买', str(report['buy_halt'])))
    return alerts


def _alert(key: str, title: str, detail: str) -> dict:
    return {'key': key, 'title': title, 'detail': detail, 'kind': 'alert'}


def _stop_unplaceable(report: dict) -> bool:
    preview = report.get('model_preview') or {}
    for row in preview.get('protections') or []:
        if row.get('placeable') is False or row.get('unplaceable_reason'):
            return True
    for row in (preview.get('sleeves') or {}).values():
        protection = (row or {}).get('protection') or {}
        if protection.get('placeable') is False or protection.get('unplaceable_reason'):
            return True
    return False


def heartbeat_message(report: dict) -> dict:
    """One daily summary. Not an alarm."""
    mark = report.get('equity_mark') or {}
    risk = report.get('risk_state') or {}
    clamp = (report.get('execution_evidence') or {}).get('stop_clamp') or {}
    protections = (report.get('model_preview') or {}).get('protections') or []
    stop = clamp.get('placed') or (protections[0].get('stopPrice') if protections else None)
    clamped = clamp.get('clamped')
    lines = [
        f"环境：{report.get('environment')}",
        f"状态：{report.get('status')}",
        f"权益（USDT 计价）：{mark.get('last') or mark.get('equity') or '未知'}",
        f"峰值：{mark.get('peak') or '未知'}",
        f"回撤：{mark.get('drawdown') or '未知'}",
        f"BTC：{risk.get('btc') if risk.get('btc') is not None else '未知'}",
        f"止损价：{stop or '无'}",
        f"是否钳制：{clamped if clamped is not None else '未知'}",
        f"价格带开关：{report.get('stop_price_percent_band')}",
    ]
    if report.get('buy_halt'):
        lines.append(f"停买：{report['buy_halt']}")
    return {
        'key': 'heartbeat',
        'kind': 'heartbeat',
        'title': '日结',
        'detail': '\n'.join(str(line) for line in lines),
    }


def missed_run_alert(heartbeat: dict | None, now: float, max_age_seconds: float) -> dict | None:
    if max_age_seconds <= 0:
        raise Blocked('heartbeat max age must be positive')
    if not heartbeat or heartbeat.get('at') is None:
        return _alert('missed-run', '错过会话', '还没有心跳记录')
    age = now - float(heartbeat['at'])
    if age > max_age_seconds:
        return _alert('missed-run', '错过会话', f'心跳已过期 {int(age)} 秒')
    return None


def subject_for(item: dict) -> str:
    prefix = HEARTBEAT_PREFIX if item.get('kind') == 'heartbeat' else ALERT_PREFIX
    return f"{prefix} {item.get('title') or '通知'}"


def body_for(item: dict, *, environment: str | None = None) -> str:
    lines = [item.get('detail') or '']
    if environment:
        lines.append(f'环境：{environment}')
    return '\n'.join(lines).strip() + '\n'


def _dedupe_key(item: dict) -> str:
    if item.get('kind') == 'heartbeat':
        return f"heartbeat:{item.get('day') or ''}"
    return item['key']


def _load_dedupe(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return saved if isinstance(saved, dict) else {}


def unsent(path: Path, items: list[dict], now: float, window_seconds: float) -> list[dict]:
    """Alerts not sent inside the window. Does not record them."""
    if window_seconds <= 0:
        raise Blocked('alert dedupe window must be positive')
    saved = _load_dedupe(path)
    fresh = []
    for item in items:
        key = _dedupe_key(item)
        previous = saved.get(key)
        if item.get('kind') == 'heartbeat':
            if previous is not None:
                continue
        elif previous is not None and now - float(previous) < window_seconds:
            continue
        fresh.append(item)
    return fresh


def remember_sent(path: Path, items: list[dict], now: float) -> None:
    """Record a successful delivery so the same alert is not repeated immediately."""
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = _load_dedupe(path)
    for item in items:
        saved[_dedupe_key(item)] = now
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(saved, sort_keys=True), encoding='utf-8')
    os.replace(temporary, path)
