# -*- coding: utf-8 -*-
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(120))
    role = db.Column(db.String(16), default='manager')   # admin | manager
    team = db.Column(db.String(40))                       # для менеджера: его команда/канал
    pw_hash = db.Column(db.String(256))

    def set_password(self, pw):
        self.pw_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.pw_hash, pw)

class Outlet(db.Model):
    id = db.Column(db.Integer, primary_key=True)          # = plan id из program_data
    name = db.Column(db.String(200))
    brand = db.Column(db.String(4))
    city = db.Column(db.String(120))
    channel = db.Column(db.String(4))
    team = db.Column(db.String(40))
    manager_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    manager = db.relationship('User', backref='outlets')

class OutletAccess(db.Model):
    """Дополнительный доступ к точке: сеть может вести несколько менеджеров.

    Ответственный (Outlet.manager_id) остаётся один — по нему считается команда
    в рейтинге; остальные получают такие же права на ввод отгрузок.
    """
    __tablename__ = 'outlet_access'
    id = db.Column(db.Integer, primary_key=True)
    outlet_id = db.Column(db.Integer, db.ForeignKey('outlet.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    outlet = db.relationship('Outlet', backref='shared')
    user = db.relationship('User', backref='shared_outlets')
    __table_args__ = (db.UniqueConstraint('outlet_id', 'user_id', name='uix_access'),)

class Entry(db.Model):
    """Факт отгрузки: строка на (ТТ, дата, SKU). Ежедневный журнал."""
    id = db.Column(db.Integer, primary_key=True)
    outlet_id = db.Column(db.Integer, db.ForeignKey('outlet.id'), index=True)
    date = db.Column(db.String(10), index=True)           # '2026-07-15'
    sku = db.Column(db.String(60))
    units = db.Column(db.Float, default=0)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('outlet_id', 'date', 'sku', name='uix_entry'),)

    @property
    def month(self):
        return self.date[:7]

class MonthClose(db.Model):
    """Закрытый (заблокированный) месяц — менеджеры не могут править."""
    month = db.Column(db.String(7), primary_key=True)     # '2026-07'
    closed_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_by = db.Column(db.Integer)

class Setting(db.Model):
    """Внутренние настройки приложения (например, постоянный SECRET_KEY,
    чтобы сессии не слетали при каждом перезапуске)."""
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Text)
