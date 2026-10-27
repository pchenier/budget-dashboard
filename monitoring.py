"""
Error pages and log shipping to Better Stack.

Logs: set BETTERSTACK_SOURCE_TOKEN and BETTERSTACK_INGEST_HOST (both shown on the
source's page in Better Stack → Telemetry) and every WARNING-or-worse log line is
sent there in the background. Without them nothing is sent. Log messages in this
app never contain credentials or balances; keep it that way.
"""

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

import requests


class BetterStackHandler(logging.Handler):
    """Queues records and posts them in batches from a daemon thread, so a slow or
    unreachable Better Stack never slows down a request."""

    def __init__(self, token, host, level=logging.WARNING):
        super().__init__(level)
        self.url = host if host.startswith('http') else f'https://{host}'
        self.headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        self.q = queue.Queue(maxsize=5000)
        threading.Thread(target=self._run, daemon=True, name='betterstack-logs').start()

    def emit(self, record):
        try:
            self.q.put_nowait({
                'dt': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                'level': record.levelname.lower(),
                'logger': record.name,
                'message': self.format(record),
                'service': 'app.fiscit.com',
            })
        except queue.Full:
            pass  # drop rather than block or grow without bound

    def _run(self):
        while True:
            batch = [self.q.get()]
            time.sleep(1)  # let a burst of related lines go out together
            while len(batch) < 100:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            try:
                requests.post(self.url, headers=self.headers, data=json.dumps(batch), timeout=5)
            except Exception:
                pass  # never log from here: that would feed back into this handler


def init_logging(app):
    token = os.getenv('BETTERSTACK_SOURCE_TOKEN', '').strip()
    host = os.getenv('BETTERSTACK_INGEST_HOST', '').strip()
    if not (token and host):
        return False
    handler = BetterStackHandler(token, host)
    handler.setFormatter(logging.Formatter('%(message)s'))
    app.logger.addHandler(handler)
    logging.getLogger('gunicorn.error').addHandler(handler)
    return True


ERROR_PAGES = {
    404: ('Page not found', "This page doesn't exist or has moved."),
    405: ('Not allowed', "That action isn't available here."),
    429: ('Slow down a little', 'Too many requests in a short time. Wait a minute, then try again.'),
    500: ('Something went wrong', "We hit an unexpected error and we've been notified. Try again in a moment."),
    502: ('Service unavailable', "One of our providers didn't answer. Try again in a moment."),
    503: ('Back in a moment', 'Fiscit is briefly unavailable. Try again in a minute.'),
}

ERROR_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} — Fiscit</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#080808;color:#f4f4f5;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
background-image:radial-gradient(ellipse at 50% -10%,rgba(184,245,102,.10),transparent 60%)}}
.card{{max-width:440px;width:100%;text-align:center}}
.code{{font-size:72px;font-weight:700;letter-spacing:-.04em;line-height:1;color:#b8f566;margin:28px 0 12px}}
h1{{font-size:22px;font-weight:600;margin-bottom:10px}}
p{{color:#a1a1aa;font-size:15px;line-height:1.6;margin-bottom:28px}}
.row{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
a{{display:inline-block;padding:11px 20px;border-radius:10px;font-weight:600;font-size:14px;text-decoration:none}}
.primary{{background:#b8f566;color:#080808}}
.ghost{{border:1px solid #27272a;color:#f4f4f5}}
.ghost:hover{{border-color:#3f3f46}}
.ref{{margin-top:28px;font-size:12px;color:#52525b}}
</style></head><body>
<div class="card">
  <svg viewBox="0 0 32 32" width="40" height="40" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#0A0F1A"/>
  <rect x="7" y="6" width="5" height="20" rx="2" fill="#F0F4F8"/><rect x="7" y="6" width="16" height="5" rx="2" fill="#F0F4F8"/>
  <rect x="7" y="14" width="12" height="4" rx="2" fill="#F0F4F8"/><circle cx="26" cy="8.5" r="3.5" fill="#b8f566"/></svg>
  <div class="code">{code}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <div class="row">
    <a class="primary" href="/">Back to dashboard</a>
    <a class="ghost" href="mailto:support@fiscit.com">Contact support</a>
  </div>
  {ref}
</div></body></html>"""


def error_page(code, ref=''):
    title, message = ERROR_PAGES.get(code, ERROR_PAGES[500])
    ref_html = f'<div class="ref">Reference: {ref}</div>' if ref else ''
    return ERROR_HTML.format(code=code, title=title, message=message, ref=ref_html)
