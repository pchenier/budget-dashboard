"""
Who may use the dashboard, by subscription (Stripe).

fiscit.com runs the checkout, the customer portal and the Stripe webhook, and writes
the subscription onto the users row; this side only reads it to decide access.

Off until BILLING_START (a date, YYYY-MM-DD) is set: before that nobody is blocked.
Everyone gets 14 days free from sign-up, and accounts that existed before billing
started get their 14 days from BILLING_START. FISCIT_COMPED_EMAILS (comma-separated)
lists accounts that never pay (the owner, testers).
"""

import os
from datetime import datetime, timedelta, timezone

import requests

TRIAL_DAYS = 14
# past_due: Stripe is still retrying the card, so access continues meanwhile
PAYING = {'active', 'trialing', 'past_due'}
PRICES = {'monthly': 10, 'yearly': 96}  # CAD, as on fiscit.com's pricing section


def _start():
    raw = os.getenv('BILLING_START', '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d') if raw else None
    except ValueError:
        return None


def enabled():
    start = _start()
    return bool(start and datetime.now() >= start)


def _naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt and dt.tzinfo else dt


def comped(email):
    allowed = {e.strip().lower() for e in os.getenv('FISCIT_COMPED_EMAILS', '').split(',') if e.strip()}
    return (email or '').lower() in allowed


def access(user):
    """What the person can do and what to show them about their plan."""
    status = (user.subscription_status or '').lower()
    period_end = _naive(user.subscription_period_end)
    info = {'plan': user.subscription_plan or '', 'status': status,
            'periodEnd': period_end.date().isoformat() if period_end else '',
            'cancelAtPeriodEnd': bool(user.subscription_cancel_at_period_end),
            'prices': PRICES, 'trialDays': TRIAL_DAYS, 'billingEnabled': enabled(),
            # BILLING_START is set (maybe in the future): the trial countdown is real
            'trialCounting': bool(_start())}
    if comped(user.email):
        return {**info, 'state': 'comped', 'ok': True}
    if status in PAYING:
        return {**info, 'state': 'past_due' if status == 'past_due' else 'subscribed', 'ok': True}
    start = _start()
    created = _naive(user.created_at) or datetime.utcnow()
    trial_end = max(created, start or created) + timedelta(days=TRIAL_DAYS)
    left = (trial_end - datetime.utcnow()).total_seconds() / 86400
    info.update(trialEnds=trial_end.date().isoformat(), daysLeft=max(0, int(left + 0.999)))
    if not enabled() or left > 0:
        return {**info, 'state': 'trial', 'ok': True}
    return {**info, 'state': 'expired', 'ok': False}


def cancel_subscription(subscription_id):
    """Cancel now (account deletion), so a deleted account is never charged again."""
    key = os.getenv('STRIPE_SECRET_KEY', '')
    if not (key and subscription_id):
        return False
    try:
        r = requests.delete(f'https://api.stripe.com/v1/subscriptions/{subscription_id}', auth=(key, ''), timeout=15)
        return r.status_code in (200, 404)  # 404: already gone
    except requests.RequestException:
        return False
