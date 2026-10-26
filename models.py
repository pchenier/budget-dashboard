#!/usr/bin/env python3
"""
Fiscit — Database models (SQLAlchemy)
Multi-user: each user has their own Plaid/Wise/Crypto connections.
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)   # nullable for Google-only users
    google_id     = db.Column(db.String(255), unique=True, nullable=True, index=True)
    name          = db.Column(db.String(100), default='')
    onboarded     = db.Column(db.Boolean, default=False, nullable=False)
    tour_seen     = db.Column(db.Boolean, default=False, nullable=False)  # guided tour offered once
    email_confirmed = db.Column(db.Boolean, default=True, nullable=False)  # auto-confirmed for API users
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    plaid_connections = db.relationship('PlaidConnection', backref='user', lazy=True, cascade='all, delete-orphan')
    wise_connections  = db.relationship('WiseConnection', backref='user', lazy=True, cascade='all, delete-orphan')
    crypto_wallets    = db.relationship('CryptoWallet', backref='user', lazy=True, cascade='all, delete-orphan')
    investment_accounts = db.relationship('InvestmentAccount', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        import bcrypt
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    def check_password(self, pw):
        import bcrypt
        if not self.password_hash:
            return False  # Google-only users have no password
        try:
            return bcrypt.checkpw(pw.encode(), self.password_hash.encode())
        except Exception:
            return False



class PlaidConnection(db.Model):
    __tablename__ = 'plaid_connections'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    access_token = db.Column(db.String(500), nullable=False)
    item_id      = db.Column(db.String(200), default='')
    institution  = db.Column(db.String(200), default='')
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class WiseConnection(db.Model):
    __tablename__ = 'wise_connections'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    api_token   = db.Column(db.String(500), nullable=False)
    profile_id  = db.Column(db.String(50), default='')
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class CryptoWallet(db.Model):
    __tablename__ = 'crypto_wallets'
    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    chain    = db.Column(db.String(50), nullable=False)
    address  = db.Column(db.String(200), nullable=False)
    label    = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class InvestmentAccount(db.Model):
    __tablename__ = 'investment_accounts'
    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    platform = db.Column(db.String(100), nullable=False)
    label    = db.Column(db.String(100), default='')
    balance  = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class UsedSsoToken(db.Model):
    """jti of SSO handoff tokens already redeemed (makes /sso links single-use)."""
    __tablename__ = 'used_sso_tokens'
    jti     = db.Column(db.String(64), primary_key=True)
    used_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class InvestmentSnapshot(db.Model):
    """Every balance a manual investment account has had, for the value-over-time chart."""
    __tablename__ = 'investment_snapshots'
    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer, db.ForeignKey('investment_accounts.id'), nullable=False, index=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    balance     = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class InstitutionLogo(db.Model):
    """Cached bank/platform logos, keyed by normalized institution name. data is
    NULL when none was found (retried later)."""
    __tablename__ = 'institution_logos'
    key        = db.Column(db.String(120), primary_key=True)
    name       = db.Column(db.String(200), default='')
    mime       = db.Column(db.String(40), default='')
    data       = db.Column(db.LargeBinary, nullable=True)
    color      = db.Column(db.String(16), default='')
    fetched_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
