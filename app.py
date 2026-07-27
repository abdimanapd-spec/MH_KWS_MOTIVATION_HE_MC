# -*- coding: utf-8 -*-
"""MH Motivational Programme 2026 — трекер команды KWS.
Flask + SQLAlchemy. Ежедневный журнал отгрузок → расчёт по волнам и месяцам.
Деплой: Railway (Postgres через DATABASE_URL) или локально (SQLite)."""
import os, json, functools, secrets
from datetime import datetime
from collections import defaultdict
from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, abort, jsonify)
from models import db, User, Outlet, Entry, MonthClose
import program as P

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))
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
    admin = User(username='admin', name='Дидар (KWS)', role='admin')
    apw = os.environ.get('ADMIN_PASSWORD', 'kws-admin-2026')
    admin.set_password(apw); db.session.add(admin); db.session.flush()
    creds['admin'] = apw
    mgr = {}
    for team, (uname, disp, pw) in TEAM_MANAGER.items():
        u = User(username=uname, name=disp, role='manager', team=team)
        u.set_password(pw); db.session.add(u); db.session.flush()
        mgr[team] = u; creds[uname] = pw
    for pl in P.PLANS:
        m = mgr.get(pl['team'])
        db.session.add(Outlet(id=pl['id'], name=pl['name'], brand=pl['brand'],
                              city=pl['city'], channel=pl['channel'], team=pl['team'],
                              manager_id=m.id if m else None))
    db.session.commit()
    try:
        with open(os.path.join(os.path.dirname(__file__), 'SEED_CREDENTIALS.json'), 'w', encoding='utf-8') as f:
            json.dump(creds, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

# ---------- auth ----------
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

def closed_months():
    return {m.month for m in MonthClose.query.all()}

# ---------- агрегация из ежедневного журнала ----------
def entries_map(oid):
    """{(date, sku): units} по всем отгрузкам точки."""
    return {(e.date, e.sku): e.units for e in Entry.query.filter_by(outlet_id=oid)}

def sum_units(emap, skus, dates):
    u = {}
    for s in skus:
        tot = 0
        for d in dates:
            tot += emap.get((d, s['name']), 0) or 0
        u[s['name']] = tot
    return u

def dates_in_month(emap, month):
    return sorted({d for (d, s) in emap if d.startswith(month)})

def outlet_summary(o, emap=None):
    plan = P.PLAN_BY_ID[o.id]
    skus = P.sku_list(o.brand)
    if emap is None:
        emap = entries_map(o.id)
    waves = {}
    earned = total_bottles = 0
    for w in (1, 2, 3):
        dates = [d for m in P.WAVE_MONTHS[w] for d in dates_in_month(emap, m)]
        c = P.compute_wave(plan, w, sum_units(emap, skus, dates))
        c['gap'] = P.wave_gap(plan, w, c)
        waves[w] = c; earned += c['total']; total_bottles += c['bottles']
    months = {}
    for m in P.MONTHS:
        dts = dates_in_month(emap, m)
        months[m] = P.compute_month(plan, m, sum_units(emap, skus, dts))
        months[m]['days'] = len(dts)
    ann_target = plan['target'] or 1
    pot110 = P.plan_potential(plan, 1.1)
    pot100 = P.plan_potential(plan, 1.0)
    return dict(o=o, plan=plan, waves=waves, months=months, earned=earned,
                bottles=round(total_bottles, 1), ann_ach=total_bottles / ann_target,
                days=sum(m['days'] for m in months.values()),
                section=P.section_of(plan),
                # деньги точки: максимум за программу и сколько ещё не добрано
                pot90=P.plan_potential(plan, 0.9), pot100=pot100, pot110=pot110,
                left=max(pot110 - earned, 0),
                pot_ach=(earned / pot110 if pot110 else 0.0))


def current_wave():
    """Волна, которая идёт сейчас (до старта — 1, после финиша — 3)."""
    m = datetime.now().strftime('%Y-%m')
    if m <= P.MONTHS[0]:
        return 1
    if m >= P.MONTHS[-1]:
        return 3
    return P.MONTH_WAVE[m]

def group_sections(summaries):
    by = defaultdict(list)
    for s in summaries:
        by[s['section']].append(s)
    out = []
    for sec in P.SECTION_ORDER:
        if by.get(sec):
            out.append((sec, sorted(by[sec], key=lambda r: -r['plan']['target'])))
    return out

# ---------- routes ----------
def register_routes(app):

    @app.context_processor
    def inject():
        return dict(user=current_user(), MONTH_RU=P.MONTH_RU, MONTHS=P.MONTHS,
                    fmt=lambda n: f'{int(round(n)):,}'.replace(',', ' '))

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
        rows = [outlet_summary(o) for o in Outlet.query.filter_by(manager_id=u.id).all()]
        return render_template('manager.html', sections=group_sections(rows),
                               waves=P.WAVE_PERIODS, all_rows=rows,
                               cur_wave=current_wave())

    @app.route('/outlet/<int:oid>')
    @login_required()
    def outlet(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if u.role != 'admin' and o.manager_id != u.id:
            abort(403)
        plan = P.PLAN_BY_ID[o.id]
        skus = P.sku_list(o.brand)
        emap = entries_map(o.id)
        closed = closed_months()
        summ = outlet_summary(o, emap)
        # редактируемая дата (?date=) — префилл значений этого дня
        edit_date = request.args.get('date', '')
        edit_vals = {}
        if edit_date:
            for s in skus:
                edit_vals[s['name']] = emap.get((edit_date, s['name']), '')
        # журнал по дням
        journal = defaultdict(lambda: defaultdict(float))
        for (d, sk), v in emap.items():
            if v:
                journal[d][sk] = v
        jdays = []
        for d in sorted(journal, reverse=True):
            units = journal[d]
            b = P.eq_bottles(o.brand, units)
            jdays.append(dict(date=d, month=d[:7], bottles=round(b, 1),
                              closed=d[:7] in closed,
                              line=', '.join(f'{k}×{int(v)}' for k, v in units.items() if v)))
        return render_template('outlet.html', o=o, plan=plan, skus=skus, summ=summ,
                               closed=closed, jdays=jdays, edit_date=edit_date,
                               edit_vals=edit_vals, can_edit=(u.role != 'admin'),
                               wavep=P.WAVE_PERIODS, month_wave=P.MONTH_WAVE)

    @app.route('/outlet/<int:oid>/save', methods=['POST'])
    @login_required()
    def save_entry(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if u.role != 'admin' and o.manager_id != u.id:
            abort(403)
        date = request.form.get('date', '').strip()
        if not (date and date[:7] in P.MONTHS and date[:4] == '2026'):
            flash('Укажите корректную дату отгрузки (июль–декабрь 2026)')
            return redirect(url_for('outlet', oid=oid))
        if date[:7] in closed_months() and u.role != 'admin':
            flash('Месяц закрыт — правки недоступны')
            return redirect(url_for('outlet', oid=oid))
        for s in P.sku_list(o.brand):
            raw = request.form.get('sku_' + s['name'], '').strip().replace(' ', '')
            val = float(raw) if raw else 0.0
            e = Entry.query.filter_by(outlet_id=o.id, date=date, sku=s['name']).first()
            if val:
                if not e:
                    e = Entry(outlet_id=o.id, date=date, sku=s['name']); db.session.add(e)
                e.units = val; e.updated_by = u.id; e.updated_at = datetime.utcnow()
            elif e:
                db.session.delete(e)
        db.session.commit()
        flash(f'Отгрузка за {date} сохранена')
        return redirect(url_for('outlet', oid=oid))

    @app.route('/outlet/<int:oid>/delete', methods=['POST'])
    @login_required()
    def delete_day(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if u.role != 'admin' and o.manager_id != u.id:
            abort(403)
        date = request.form.get('date', '')
        if date[:7] in closed_months() and u.role != 'admin':
            flash('Месяц закрыт'); return redirect(url_for('outlet', oid=oid))
        for e in Entry.query.filter_by(outlet_id=o.id, date=date).all():
            db.session.delete(e)
        db.session.commit()
        flash(f'Отгрузка за {date} удалена')
        return redirect(url_for('outlet', oid=oid))

    # ----- админ -----
    @app.route('/admin')
    @login_required('admin')
    def admin():
        rows = [outlet_summary(o) for o in Outlet.query.all()]
        closed = sorted(closed_months())
        month = request.args.get('month') or last_active_month(rows)
        wave_tot = {1: 0, 2: 0, 3: 0}
        team_tot = {}
        for r in rows:
            for w in (1, 2, 3):
                wave_tot[w] += r['waves'][w]['total']
            t = team_tot.setdefault(r['o'].team, {'prize': 0, 'n': 0})
            t['prize'] += r['earned']; t['n'] += 1
        return render_template('admin.html', sections=group_sections(rows), all_rows=rows,
                               closed=closed, wave_tot=wave_tot, team_tot=team_tot,
                               totals=P.program_totals(), lead=leaderboards(rows, month),
                               month=month)

    @app.route('/admin/close', methods=['POST'])
    @login_required('admin')
    def close_month():
        m = request.form['month']
        if m in P.MONTHS and not MonthClose.query.get(m):
            db.session.add(MonthClose(month=m, closed_by=current_user().id)); db.session.commit()
            flash(f'Месяц {P.MONTH_RU[m]} закрыт')
        return redirect(url_for('admin'))

    @app.route('/admin/reopen', methods=['POST'])
    @login_required('admin')
    def reopen_month():
        m = request.form['month']
        mc = MonthClose.query.get(m)
        if mc:
            db.session.delete(mc); db.session.commit()
            flash(f'Месяц {P.MONTH_RU[m]} открыт заново')
        return redirect(url_for('admin'))

    # ----- пользователи (только админ) -----
    @app.route('/admin/users')
    @login_required('admin')
    def users():
        us = User.query.order_by(User.role.desc(), User.name).all()
        cnt = {}
        for o in Outlet.query.all():
            cnt[o.manager_id] = cnt.get(o.manager_id, 0) + 1
        free = Outlet.query.filter(Outlet.manager_id.is_(None)).count()
        return render_template('users.html', users=us, cnt=cnt, free=free,
                               teams=list(TEAM_MANAGER.keys()),
                               new_pw=session.pop('new_pw', None))

    @app.route('/admin/users/add', methods=['POST'])
    @login_required('admin')
    def user_add():
        uname = request.form.get('username', '').strip().lower()
        name = request.form.get('name', '').strip()
        team = request.form.get('team', '').strip()
        pw = request.form.get('password', '').strip() or (uname + '-26')
        if not uname or not name:
            flash('Укажите логин и имя'); return redirect(url_for('users'))
        if User.query.filter_by(username=uname).first():
            flash(f'Логин «{uname}» уже занят'); return redirect(url_for('users'))
        u = User(username=uname, name=name, role='manager', team=team or None)
        u.set_password(pw); db.session.add(u); db.session.commit()
        session['new_pw'] = {'username': uname, 'password': pw}
        flash(f'Менеджер {name} создан. Логин: {uname} · пароль: {pw}')
        return redirect(url_for('user_outlets', uid=u.id))

    @app.route('/admin/users/<int:uid>/password', methods=['POST'])
    @login_required('admin')
    def user_password(uid):
        u = User.query.get_or_404(uid)
        pw = request.form.get('password', '').strip()
        if len(pw) < 4:
            flash('Пароль слишком короткий (мин. 4 символа)'); return redirect(url_for('users'))
        u.set_password(pw); db.session.commit()
        session['new_pw'] = {'username': u.username, 'password': pw}
        flash(f'Пароль для «{u.name}» обновлён: {pw}')
        return redirect(url_for('users'))

    @app.route('/admin/users/<int:uid>/delete', methods=['POST'])
    @login_required('admin')
    def user_delete(uid):
        u = User.query.get_or_404(uid)
        if u.role == 'admin':
            flash('Админа удалить нельзя'); return redirect(url_for('users'))
        for o in Outlet.query.filter_by(manager_id=u.id).all():
            o.manager_id = None
        db.session.delete(u); db.session.commit()
        flash(f'Пользователь «{u.name}» удалён, его точки освобождены')
        return redirect(url_for('users'))

    @app.route('/admin/users/<int:uid>/outlets', methods=['GET', 'POST'])
    @login_required('admin')
    def user_outlets(uid):
        u = User.query.get_or_404(uid)
        if request.method == 'POST':
            keep = {int(x) for x in request.form.getlist('oid')}
            for o in Outlet.query.all():
                if o.id in keep:
                    o.manager_id = u.id
                elif o.manager_id == u.id:
                    o.manager_id = None
            db.session.commit()
            flash(f'Точки для «{u.name}» сохранены ({len(keep)})')
            return redirect(url_for('users'))
        rows = [dict(o=o, plan=P.PLAN_BY_ID[o.id], section=P.section_of(P.PLAN_BY_ID[o.id]))
                for o in Outlet.query.all()]
        return render_template('user_outlets.html', u=u,
                               sections=group_sections(rows))

    # ----- инструкция (для всех) -----
    @app.route('/help')
    @login_required()
    def help_page():
        u = current_user()
        # мои цифры — чтобы менеджер видел инструкцию «про себя»
        my = None
        if u.role == 'manager':
            rows = [outlet_summary(o) for o in Outlet.query.filter_by(manager_id=u.id).all()]
            my = dict(n=len(rows),
                      pot110=sum(r['pot110'] for r in rows),
                      earned=sum(r['earned'] for r in rows),
                      chains=sum(1 for r in rows if r['plan'].get('is_chain')),
                      rebased=sum(1 for r in rows if r['plan'].get('rebased')))
        # пример расчёта — считаем теми же функциями, что и выплаты, чтобы не разошлось
        eu = {'VS 0.7': 400, 'VSOP 0.7': 50}
        ex = dict(target=430, vs=400, vsop=50,
                  price_vs=P.hf(P.net_price('HY', 'VS 0.7', 'OFF')),
                  price_vsop=P.hf(P.net_price('HY', 'VSOP 0.7', 'OFF')),
                  bottles=round(P.eq_bottles('HY', eu), 1),
                  turn=P.hf(P.turnover_kzt('HY', 'OFF', eu)))
        ex['ach'] = ex['bottles'] / ex['target']
        ex['rate'] = P.rate_for('HY', 1, 'p100')
        ex['prize'] = P.hf(ex['rate'] * ex['turn'])
        ex['team'] = P.hf(P.TEAM_RATE * ex['bottles'] * P.RSP['HY'] * P.FX)
        ex['total'] = ex['prize'] + ex['team']
        counts = dict(n=len(P.PLANS),
                      hy=sum(1 for p in P.PLANS if p['brand'] == 'HY'),
                      mc=sum(1 for p in P.PLANS if p['brand'] == 'MC'),
                      chains=sum(1 for p in P.PLANS if p.get('is_chain')),
                      rebased=sum(1 for p in P.PLANS if p.get('rebased')))
        dc = sorted({p['name'].replace(' (chain)', '') for p in P.PLANS if p.get('dc')})
        return render_template('help.html', my=my, ex=ex, counts=counts, dc=dc,
                               meta=P.META, totals=P.program_totals(),
                               wavep=P.WAVE_PERIODS, shares=P.WAVES,
                               r1=P.RATE_W1, rs=P.RATE_STD, ku=P.KU_FLAT,
                               rsp_kzt={b: P.hf(v * P.FX) for b, v in P.RSP.items()},
                               pay={'w1': 'начало сентября 2026', 'w2': 'ноябрь 2026',
                                    'w3': 'январь 2027'},
                               cur_wave=current_wave())

    @app.route('/leaderboard')
    @login_required()
    def leaderboard():
        rows = [outlet_summary(o) for o in Outlet.query.all()]
        month = request.args.get('month') or last_active_month(rows)
        return render_template('leaderboard.html', lead=leaderboards(rows, month),
                               month=month)

    @app.route('/healthz')
    def healthz():
        return jsonify(ok=True, plans=len(P.PLANS))

# ---------- рейтинг ----------
def last_active_month(rows):
    active = [m for m in P.MONTHS if any(r['months'][m]['days'] for r in rows)]
    return active[-1] if active else P.MONTHS[0]

def leaderboards(rows, month):
    # растущие точки — по % выполнения ВЫБРАННОГО месяца (с фактом)
    growing = sorted([r for r in rows if r['months'][month]['days']],
                     key=lambda r: -r['months'][month]['ach'])[:10]
    # команды по заполнению за месяц (сколько точек внесли отгрузки)
    by = {}
    for r in rows:
        t = r['o'].team
        d = by.setdefault(t, {'team': t, 'filled': 0, 'n': 0, 'earned': 0})
        d['n'] += 1
        if r['months'][month]['days']:
            d['filled'] += 1
        d['earned'] += r['earned']
    managers = sorted(by.values(), key=lambda m: -(m['filled'] / m['n'] if m['n'] else 0))
    return dict(growing=growing, managers=managers, month=month)

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
