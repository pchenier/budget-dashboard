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
    password_hash = db.Column(db.String(255), nullable=False)
    name          = db.Column(db.String(100), default='')
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    plaid_connections = db.relationship('PlaidConnection', backref='user', lazy=True, cascade='all, delete-orphan')
    wise_connections  = db.relationship('WiseConnection', backref='user', lazy=True, cascade='all, delete-orphan')
    crypto_wallets    = db.relationship('CryptoWallet', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        import bcrypt
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    def check_password(self, pw):
        import bcrypt
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