# -*- coding: utf-8 -*-
"""MH Motivational Programme 2026 — трекер команды KWS.
Flask + SQLAlchemy. Деплой: Railway (Postgres через DATABASE_URL) или локально (SQLite)."""
import os, json, functools, secrets
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, abort, jsonify)
from models import db, User, Outlet, Entry, MonthClose
import program as P

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))
    # Railway отдаёт DATABASE_URL для Postgres; локально — SQLite-файл.
    uri = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(os.path.dirname(__file__), 'tracker.db'))
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed()
    register_routes(app)
    return app

# ---------- seed ----------
# команда -> (логин, отображаемое имя, пароль по умолчанию)
TEAM_MANAGER = {
    'Алматы OFF': ('almaty_off', 'Алматы офф-трейд', 'almaty-off-26'),
    'Алматы ON':  ('almaty_on',  'Алматы он-трейд',  'almaty-on-26'),
    'Астана OFF': ('astana_off', 'Астана офф-трейд', 'astana-off-26'),
    'Астана ON':  ('astana_on',  'Астана он-трейд',  'astana-on-26'),
    'Регионы':    ('regiony',    'Регионы',          'regiony-26'),
}

def seed():
    if User.query.first():
        return
    creds = {}
    # админ
    admin = User(username='admin', name='Дидар (KWS)', role='admin')
    apw = os.environ.get('ADMIN_PASSWORD', 'kws-admin-2026')
    admin.set_password(apw); db.session.add(admin); db.session.flush()
    creds['admin'] = apw
    # менеджеры по командам (пароли по умолчанию; можно сменить через код/БД)
    mgr = {}
    for team, (uname, disp, pw) in TEAM_MANAGER.items():
        u = User(username=uname, name=disp, role='manager', team=team)
        u.set_password(pw); db.session.add(u); db.session.flush()
        mgr[team] = u; creds[uname] = pw
    # точки
    for pl in P.PLANS:
        m = mgr.get(pl['team'])
        db.session.add(Outlet(id=pl['id'], name=pl['name'], brand=pl['brand'],
                              city=pl['city'], channel=pl['channel'], team=pl['team'],
                              manager_id=m.id if m else None))
    db.session.commit()
    # сохраняем сгенерированные пароли в файл (для выдачи менеджерам)
    try:
        with open(os.path.join(os.path.dirname(__file__), 'SEED_CREDENTIALS.json'), 'w', encoding='utf-8') as f:
            json.dump(creds, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

# ---------- auth helpers ----------
def current_user():
    uid = session.get('uid')
    return User.query.get(uid) if uid else None

def login_required(role=None):
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            u = current_user()
            if not u:
                return redirect(url_for('login'))
            if role and u.role != role:
                abort(403)
            return fn(*a, **k)
        return wrap
    return deco

# ---------- routes ----------
def register_routes(app):

    @app.context_processor
    def inject():
        return dict(user=current_user(), MONTH_RU=P.MONTH_RU, fmt=lambda n: f'{int(n):,}'.replace(',', ' '))

    @app.route('/')
    def index():
        u = current_user()
        if not u:
            return render_template('landing.html', prog=P.META, totals=P.program_totals())
        return redirect(url_for('admin' if u.role == 'admin' else 'dashboard'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            u = User.query.filter_by(username=request.form['username'].strip()).first()
            if u and u.check_password(request.form['password']):
                session['uid'] = u.id
                return redirect(url_for('index'))
            flash('Неверный логин или пароль')
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    # ----- менеджер -----
    @app.route('/dashboard')
    @login_required('manager')
    def dashboard():
        u = current_user()
        outlets = Outlet.query.filter_by(manager_id=u.id).all()
        rows = [outlet_summary(o) for o in outlets]
        return render_template('manager.html', rows=rows, waves=P.WAVE_PERIODS)

    @app.route('/outlet/<int:oid>', methods=['GET'])
    @login_required()
    def outlet(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if u.role != 'admin' and o.manager_id != u.id:
            abort(403)
        plan = P.PLAN_BY_ID[o.id]
        skus = P.sku_list(o.brand)
        entries = {(e.month, e.sku): e.units for e in Entry.query.filter_by(outlet_id=o.id)}
        closed = {m.month for m in MonthClose.query.all()}
        waves = []
        for w in (1, 2, 3):
            months = P.WAVE_MONTHS[w]
            units = {}
            for m in months:
                for s in skus:
                    units[s['name']] = units.get(s['name'], 0) + (entries.get((m, s['name']), 0) or 0)
            waves.append(dict(w=w, period=P.WAVE_PERIODS[f'w{w}'], months=months,
                              calc=P.compute_wave(plan, w, units)))
        return render_template('outlet.html', o=o, plan=plan, skus=skus, months=P.MONTHS,
                               entries=entries, closed=closed, waves=waves,
                               can_edit=(u.role != 'admin'))

    @app.route('/outlet/<int:oid>/save', methods=['POST'])
    @login_required()
    def save_entry(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if u.role != 'admin' and o.manager_id != u.id:
            abort(403)
        closed = {m.month for m in MonthClose.query.all()}
        month = request.form['month']
        if month in closed and u.role != 'admin':
            flash('Месяц закрыт — правки недоступны'); return redirect(url_for('outlet', oid=oid))
        for s in P.sku_list(o.brand):
            raw = request.form.get('sku_' + s['name'], '').strip().replace(' ', '')
            val = float(raw) if raw else 0.0
            e = Entry.query.filter_by(outlet_id=o.id, month=month, sku=s['name']).first()
            if not e:
                e = Entry(outlet_id=o.id, month=month, sku=s['name'])
                db.session.add(e)
            e.units = val; e.updated_by = u.id; e.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Сохранено: {P.MONTH_RU[month]}')
        return redirect(url_for('outlet', oid=oid))

    # ----- админ -----
    @app.route('/admin')
    @login_required('admin')
    def admin():
        outlets = Outlet.query.all()
        rows = [outlet_summary(o) for o in outlets]
        closed = sorted(m.month for m in MonthClose.query.all())
        # агрегаты по волнам и командам
        wave_tot = {1: 0, 2: 0, 3: 0}
        team_tot = {}
        for r in rows:
            for w in (1, 2, 3):
                wave_tot[w] += r['waves'][w]['total']
            team_tot.setdefault(r['o'].team, {'prize': 0, 'plan': 0, 'n': 0})
            team_tot[r['o'].team]['prize'] += r['earned']
            team_tot[r['o'].team]['n'] += 1
        return render_template('admin.html', rows=rows, closed=closed, months=P.MONTHS,
                               wave_tot=wave_tot, team_tot=team_tot,
                               totals=P.program_totals(), lead=leaderboards(rows))

    @app.route('/admin/close', methods=['POST'])
    @login_required('admin')
    def close_month():
        month = request.form['month']
        if month in P.MONTHS and not MonthClose.query.get(month):
            db.session.add(MonthClose(month=month, closed_by=current_user().id))
            db.session.commit()
            flash(f'Месяц {P.MONTH_RU[month]} закрыт')
        return redirect(url_for('admin'))

    @app.route('/admin/reopen', methods=['POST'])
    @login_required('admin')
    def reopen_month():
        month = request.form['month']
        mc = MonthClose.query.get(month)
        if mc:
            db.session.delete(mc); db.session.commit()
            flash(f'Месяц {P.MONTH_RU[month]} открыт заново')
        return redirect(url_for('admin'))

    @app.route('/leaderboard')
    @login_required()
    def leaderboard():
        rows = [outlet_summary(o) for o in Outlet.query.all()]
        return render_template('leaderboard.html', lead=leaderboards(rows))

    @app.route('/healthz')
    def healthz():
        return jsonify(ok=True, plans=len(P.PLANS))

# ---------- summaries ----------
def outlet_summary(o):
    plan = P.PLAN_BY_ID[o.id]
    skus = P.sku_list(o.brand)
    entries = {(e.month, e.sku): e.units for e in Entry.query.filter_by(outlet_id=o.id)}
    filled_months = {m for (m, s), v in entries.items() if v}
    waves = {}
    earned = 0
    total_bottles = 0
    for w in (1, 2, 3):
        units = {}
        for m in P.WAVE_MONTHS[w]:
            for s in skus:
                units[s['name']] = units.get(s['name'], 0) + (entries.get((m, s['name']), 0) or 0)
        c = P.compute_wave(plan, w, units)
        waves[w] = c
        earned += c['total']
        total_bottles += c['bottles']
    ann_target = plan['target'] or 1
    return dict(o=o, plan=plan, waves=waves, earned=earned,
                filled=len(filled_months), bottles=round(total_bottles, 1),
                ann_ach=total_bottles / ann_target)

def leaderboards(rows):
    # самые растущие ТТ — по годовому % выполнения (только с фактом)
    growing = sorted([r for r in rows if r['bottles'] > 0], key=lambda r: -r['ann_ach'])[:10]
    # менеджеры по заполнению — доля месяцев с фактом
    by_mgr = {}
    for r in rows:
        t = r['o'].team
        by_mgr.setdefault(t, {'team': t, 'filled': 0, 'cells': 0, 'earned': 0, 'n': 0})
        by_mgr[t]['filled'] += r['filled']
        by_mgr[t]['cells'] += 6
        by_mgr[t]['earned'] += r['earned']
        by_mgr[t]['n'] += 1
    mgr = sorted(by_mgr.values(), key=lambda m: -(m['filled'] / m['cells'] if m['cells'] else 0))
    return dict(growing=growing, managers=mgr)

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
