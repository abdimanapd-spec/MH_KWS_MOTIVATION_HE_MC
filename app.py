# -*- coding: utf-8 -*-
"""MH Motivational Programme 2026 — трекер команды KWS.
Flask + SQLAlchemy. Ежедневный журнал отгрузок → расчёт по волнам и месяцам.
Деплой: Railway (Postgres через DATABASE_URL) или локально (SQLite)."""
import os, json, functools, secrets
from datetime import datetime
from collections import defaultdict
from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, abort, jsonify, Response)
from models import (db, User, Outlet, Entry, MonthClose, Setting,
                    OutletAccess, TeamAccess)
import program as P

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- база ----------
def db_uri():
    """Куда писать данные.

    На Railway диск контейнера обнуляется при КАЖДОМ деплое, поэтому SQLite там
    равносилен потере всех аккаунтов и отгрузок. Строку подключения к Postgres
    Railway отдаёт в DATABASE_URL (приватная сеть, бесплатно) либо в
    DATABASE_PUBLIC_URL (публичный прокси). Берём первую доступную.
    Если на Railway нет ни одной — падаем с понятной ошибкой, а НЕ уходим
    молча в SQLite: лучше заметная авария, чем тихо стёртые данные.
    """
    uri = (os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_PUBLIC_URL') or '').strip()
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    if uri:
        return uri
    on_railway = bool(os.environ.get('RAILWAY_ENVIRONMENT_ID') or os.environ.get('RAILWAY_SERVICE_ID'))
    if on_railway and not os.environ.get('ALLOW_EPHEMERAL_DB'):
        raise RuntimeError(
            'Нет подключения к Postgres: не задана ни DATABASE_URL, ни DATABASE_PUBLIC_URL. '
            'Приложение остановлено, чтобы не писать данные на временный диск Railway — '
            'он обнуляется при каждом деплое. Откройте сервис в Railway → Variables → '
            'New Variable → DATABASE_URL со значением ${{Postgres.DATABASE_URL}} и передеплойте. '
            '(Осознанно запустить без базы: переменная ALLOW_EPHEMERAL_DB=1.)')
    return 'sqlite:///' + os.path.join(HERE, 'tracker.db')

def db_label(uri):
    if uri.startswith('postgresql'):
        kind = 'Postgres'
        if not os.environ.get('DATABASE_URL') and os.environ.get('DATABASE_PUBLIC_URL'):
            kind += ' (публичный адрес)'
        return kind, True
    return 'SQLite (временный диск)', False

def secret_key():
    """Постоянный ключ сессий. Из переменной окружения, иначе — из базы.
    Раньше ключ генерировался заново при каждом старте, из-за чего всех
    выкидывало из аккаунтов после любого деплоя."""
    env = (os.environ.get('SECRET_KEY') or '').strip()
    if env:
        return env
    s = db.session.get(Setting, 'secret_key')
    if not s:
        s = Setting(key='secret_key', value=secrets.token_hex(32))
        db.session.add(s)
        db.session.commit()
    return s.value

def create_app():
    app = Flask(__name__)
    uri = db_uri()
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}
    app.config['DB_LABEL'], app.config['DB_PERSISTENT'] = db_label(uri)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        app.secret_key = secret_key()
        seed()
        sync_outlets()
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
        with open(os.path.join(HERE, 'SEED_CREDENTIALS.json'), 'w', encoding='utf-8') as f:
            json.dump(creds, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def sync_outlets():
    """Подтягивает справочные поля точек из program_data.json на каждом старте.

    seed() отрабатывает только на пустой базе, поэтому без этого правки в
    program_data.json (переименование, город, канал, новая точка) не доезжают
    до боевой базы. Привязку к менеджеру (manager_id) НЕ трогаем — её ведёт
    админ руками; отгрузки привязаны к id точки и не затрагиваются.
    """
    existing = {o.id: o for o in Outlet.query.all()}
    by_team = {}
    for u in User.query.filter_by(role='manager').all():
        by_team.setdefault(u.team, u)
    touched = 0
    for pl in P.PLANS:
        o = existing.get(pl['id'])
        if o is None:
            m = by_team.get(pl['team'])
            db.session.add(Outlet(id=pl['id'], name=pl['name'], brand=pl['brand'],
                                  city=pl['city'], channel=pl['channel'], team=pl['team'],
                                  manager_id=m.id if m else None))
            touched += 1
            continue
        for f in ('name', 'brand', 'city', 'channel', 'team'):
            if getattr(o, f) != pl[f]:
                setattr(o, f, pl[f]); touched += 1
    if touched:
        db.session.commit()
    return touched

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

# ---------- доступ к точкам ----------
# Любую точку могут вести несколько человек. Один из них — «ответственный»
# (Outlet.manager_id): по нему точка попадает в командный зачёт. Остальные
# получают доступ либо поимённо (OutletAccess), либо оптом — как супервайзер
# над целой командой (TeamAccess). Права у всех одинаковые.
def is_chain(oid):
    return bool(P.PLAN_BY_ID[oid].get('is_chain'))

def active_q():
    """Точки, у которых есть план в program_data.json.

    Выведенную из программы точку убираем из плана, но строку в базе и её
    отгрузки НЕ удаляем — история и резервная копия остаются целыми. Такая
    точка просто перестаёт показываться и считаться. Возвращает Query, к
    которому можно доклеить свои фильтры."""
    return Outlet.query.filter(Outlet.id.in_(list(P.PLAN_BY_ID)))

def shared_ids(uid):
    return {a.outlet_id for a in OutletAccess.query.filter_by(user_id=uid).all()}

def sup_teams(uid):
    """Команды, над которыми человек супервайзер."""
    return {t.team for t in TeamAccess.query.filter_by(user_id=uid).all()}

def supervisors_of(team):
    if not team:
        return []
    return [t.user for t in TeamAccess.query.filter_by(team=team).all() if t.user]

def my_outlets(u):
    """Точки человека: свои + открытые поимённо + все точки подшефных команд."""
    rows = active_q().filter_by(manager_id=u.id).all()
    have = {o.id for o in rows}
    extra = shared_ids(u.id) - have
    if extra:
        rows += active_q().filter(Outlet.id.in_(extra)).all()
        have |= extra
    teams = sup_teams(u.id)
    if teams:
        rows += [o for o in active_q().filter(Outlet.team.in_(teams)).all()
                 if o.id not in have]
    return rows

def can_access(u, o):
    if u.role == 'admin' or o.manager_id == u.id:
        return True
    if OutletAccess.query.filter_by(outlet_id=o.id, user_id=u.id).first():
        return True
    return bool(o.team) and TeamAccess.query.filter_by(
        user_id=u.id, team=o.team).first() is not None

def outlet_team(o):
    """Кто ведёт точку: ответственный первым, затем остальные."""
    team = [o.manager] if o.manager else []
    seen = {o.manager_id}
    for a in OutletAccess.query.filter_by(outlet_id=o.id).all():
        if a.user and a.user_id not in seen:
            team.append(a.user); seen.add(a.user_id)
    for s in supervisors_of(o.team):
        if s.id not in seen:
            team.append(s); seen.add(s.id)
    return team

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
        # потолок волны в разбивке: сколько может получить сама точка и сколько команда
        c['max110'] = P.wave_potential(plan, w, 1.1)
        c['max100'] = P.wave_potential(plan, w, 1.0)
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
            # логин без учёта регистра: при создании он сохраняется строчными,
            # а телефоны любят автоматически ставить заглавную букву
            u = User.query.filter_by(username=request.form['username'].strip().lower()).first()
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
        rows = [outlet_summary(o) for o in my_outlets(u)]
        # у сетей точку могут вести несколько человек — подписываем, кто ещё
        co = {}
        for r in rows:
            others = [m.name for m in outlet_team(r['o']) if m.id != u.id]
            if others:
                co[r['o'].id] = ', '.join(others)
        return render_template('manager.html', sections=group_sections(rows),
                               waves=P.WAVE_PERIODS, all_rows=rows, co=co,
                               cur_wave=current_wave())

    @app.route('/outlet/<int:oid>')
    @login_required()
    def outlet(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if o.id not in P.PLAN_BY_ID:
            abort(404)          # точка выведена из программы
        if not can_access(u, o):
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
        # кто вносил день (важно для сетей, где точку ведут несколько менеджеров)
        who = defaultdict(set)
        names = {x.id: (x.name or x.username) for x in User.query.all()}
        for e in Entry.query.filter_by(outlet_id=o.id).all():
            if e.updated_by:
                who[e.date].add(names.get(e.updated_by, '—'))
        jdays = []
        for d in sorted(journal, reverse=True):
            units = journal[d]
            b = P.eq_bottles(o.brand, units)
            jdays.append(dict(date=d, month=d[:7], bottles=round(b, 1),
                              closed=d[:7] in closed,
                              by=', '.join(sorted(who.get(d, ()))),
                              line=', '.join(f'{k}×{int(v)}' for k, v in units.items() if v)))
        return render_template('outlet.html', o=o, plan=plan, skus=skus, summ=summ,
                               closed=closed, jdays=jdays, edit_date=edit_date,
                               edit_vals=edit_vals, can_edit=(u.role != 'admin'),
                               team=outlet_team(o),
                               wavep=P.WAVE_PERIODS, month_wave=P.MONTH_WAVE)

    @app.route('/outlet/<int:oid>/save', methods=['POST'])
    @login_required()
    def save_entry(oid):
        o = Outlet.query.get_or_404(oid)
        u = current_user()
        if o.id not in P.PLAN_BY_ID:
            abort(404)          # точка выведена из программы
        if not can_access(u, o):
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
        if o.id not in P.PLAN_BY_ID:
            abort(404)          # точка выведена из программы
        if not can_access(u, o):
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
        rows = [outlet_summary(o) for o in active_q().all()]
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
        outlets = active_q().all()
        cnt, own_ids, by_team = {}, {}, {}
        for o in outlets:
            cnt[o.manager_id] = cnt.get(o.manager_id, 0) + 1
            own_ids.setdefault(o.manager_id, set()).add(o.id)
            by_team.setdefault(o.team, set()).add(o.id)
        acc = {}
        for a in OutletAccess.query.all():
            acc.setdefault(a.user_id, set()).add(a.outlet_id)
        sups = {}
        for t in TeamAccess.query.all():
            sups.setdefault(t.user_id, []).append(t.team)
            acc.setdefault(t.user_id, set()).update(by_team.get(t.team, ()))
        for tl in sups.values():
            tl.sort()
        shared = {uid: len(s - own_ids.get(uid, set())) for uid, s in acc.items()}
        free = active_q().filter(Outlet.manager_id.is_(None)).count()
        return render_template('users.html', users=us, cnt=cnt, shared=shared,
                               sups=sups, free=free,
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
        passed = 0
        for o in active_q().filter_by(manager_id=u.id).all():
            # если точку вёл ещё кто-то — ответственным становится он,
            # чтобы точка не осталась без владельца
            other = OutletAccess.query.filter(OutletAccess.outlet_id == o.id,
                                              OutletAccess.user_id != u.id).first()
            if other:
                o.manager_id = other.user_id
                db.session.delete(other); passed += 1
            else:
                o.manager_id = None
        for a in OutletAccess.query.filter_by(user_id=u.id).all():
            db.session.delete(a)
        for t in TeamAccess.query.filter_by(user_id=u.id).all():
            db.session.delete(t)
        db.session.delete(u); db.session.commit()
        flash(f'Пользователь «{u.name}» удалён, его точки освобождены'
              + (f' (из них {passed} перешли к со-менеджеру)' if passed else ''))
        return redirect(url_for('users'))

    @app.route('/admin/users/<int:uid>/outlets', methods=['GET', 'POST'])
    @login_required('admin')
    def user_outlets(uid):
        u = User.query.get_or_404(uid)
        if request.method == 'POST':
            keep = {int(x) for x in request.form.getlist('oid')}
            lead = {int(x) for x in request.form.getlist('lead')}
            # форма присылает скрытый маркер — значит колонка «ответственный»
            # действительно показывалась и снятой отметке можно верить
            has_lead = bool(request.form.get('leadform'))

            def grant(oid, user_id):
                if not OutletAccess.query.filter_by(outlet_id=oid, user_id=user_id).first():
                    db.session.add(OutletAccess(outlet_id=oid, user_id=user_id))

            def revoke(oid, user_id):
                a = OutletAccess.query.filter_by(outlet_id=oid, user_id=user_id).first()
                if a:
                    db.session.delete(a)

            def heir(oid, not_user):
                """Со-менеджер, которому можно передать ответственность."""
                return OutletAccess.query.filter(
                    OutletAccess.outlet_id == oid,
                    OutletAccess.user_id != not_user).first()

            own = co = 0
            for o in active_q().all():
                if o.id in keep:
                    take = (o.manager_id in (None, u.id) or o.id in lead)
                    if take and o.manager_id == u.id and has_lead \
                            and o.id not in lead and heir(o.id, u.id):
                        take = False          # сняли отметку — уступаем ответственность
                    if take:
                        prev = o.manager_id
                        if prev and prev != u.id:
                            grant(o.id, prev)  # прежний ответственный остаётся в деле
                        o.manager_id = u.id
                        revoke(o.id, u.id)
                        own += 1
                    else:
                        if o.manager_id == u.id:
                            other = heir(o.id, u.id)
                            o.manager_id = other.user_id
                            db.session.delete(other)
                        grant(o.id, u.id)      # точка уже за кем-то — просто добавляем
                        co += 1
                else:
                    revoke(o.id, u.id)
                    if o.manager_id == u.id:
                        other = heir(o.id, u.id)
                        if other:
                            o.manager_id = other.user_id
                            db.session.delete(other)
                        else:
                            o.manager_id = None

            # супервайзер над целыми командами
            want = set(request.form.getlist('team'))
            had = sup_teams(u.id)
            for t in TeamAccess.query.filter_by(user_id=u.id).all():
                if t.team not in want:
                    db.session.delete(t)
            for t in want - had:
                db.session.add(TeamAccess(user_id=u.id, team=t))

            db.session.commit()
            flash(f'Доступ для «{u.name}» сохранён: точек под ответственность — {own}'
                  + (f', общих — {co}' if co else '')
                  + (f'; супервайзер над: {", ".join(sorted(want))}' if want else ''))
            return redirect(url_for('users'))
        mine = shared_ids(u.id)
        sup = sup_teams(u.id)
        rows = [dict(o=o, plan=P.PLAN_BY_ID[o.id], section=P.section_of(P.PLAN_BY_ID[o.id]),
                     checked=(o.manager_id == u.id or o.id in mine),
                     lead=(o.manager_id == u.id),
                     chain=bool(P.PLAN_BY_ID[o.id].get('is_chain')),
                     via_team=(o.team in sup and o.manager_id != u.id
                               and o.id not in mine),
                     team=outlet_team(o))
                for o in active_q().all()]
        return render_template('user_outlets.html', u=u,
                               sections=group_sections(rows),
                               all_teams=list(TEAM_MANAGER.keys()), sup=sup)

    # ----- резервная копия (только админ) -----
    @app.route('/admin/backup.json')
    @login_required('admin')
    def backup():
        """Выгрузка всего, что нельзя пересоздать автоматически: аккаунты,
        привязка точек к менеджерам, отгрузки, закрытые месяцы."""
        uname = {u.id: u.username for u in User.query.all()}
        data = {
            'version': 1,
            'saved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
            'users': [dict(username=u.username, name=u.name, role=u.role,
                           team=u.team, pw_hash=u.pw_hash)
                      for u in User.query.order_by(User.id).all()],
            'outlets': [dict(id=o.id, manager=uname.get(o.manager_id))
                        for o in Outlet.query.order_by(Outlet.id).all()],
            'shared': [dict(outlet=a.outlet_id, manager=uname.get(a.user_id))
                       for a in OutletAccess.query.order_by(OutletAccess.id).all()],
            'supervisors': [dict(manager=uname.get(t.user_id), team=t.team)
                            for t in TeamAccess.query.order_by(TeamAccess.id).all()],
            'entries': [dict(outlet=e.outlet_id, date=e.date, sku=e.sku, units=e.units)
                        for e in Entry.query.order_by(Entry.id).all()],
            'closed': sorted(m.month for m in MonthClose.query.all()),
        }
        fn = 'mh-tracker-' + datetime.utcnow().strftime('%Y-%m-%d-%H%M') + '.json'
        return Response(json.dumps(data, ensure_ascii=False, indent=1),
                        mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename=' + fn})

    @app.route('/admin/restore', methods=['POST'])
    @login_required('admin')
    def restore():
        """Восстановление из файла копии. Только добавляет — ничего не удаляет
        и не перезаписывает существующее."""
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Файл не выбран'); return redirect(url_for('users'))
        try:
            data = json.loads(f.read().decode('utf-8'))
        except Exception:
            flash('Не удалось прочитать файл — нужен .json из кнопки «Скачать копию»')
            return redirect(url_for('users'))
        me = current_user().id
        by_name = {u.username: u for u in User.query.all()}
        new_u = 0
        for row in data.get('users', []):
            un = (row.get('username') or '').strip().lower()
            if not un or un in by_name:
                continue
            u = User(username=un, name=row.get('name') or un,
                     role=row.get('role') or 'manager', team=row.get('team'))
            u.pw_hash = row.get('pw_hash') or None
            if not u.pw_hash:
                u.set_password(un + '-26')
            db.session.add(u); by_name[un] = u; new_u += 1
        db.session.flush()
        linked = 0
        for row in data.get('outlets', []):
            oid = row.get('id')
            o = db.session.get(Outlet, oid) if oid else None
            m = by_name.get(row.get('manager') or '')
            if o and m and o.manager_id != m.id:
                o.manager_id = m.id; linked += 1
        have = {(a.outlet_id, a.user_id) for a in OutletAccess.query.all()}
        for row in data.get('shared', []):
            o = db.session.get(Outlet, row.get('outlet')) if row.get('outlet') else None
            m = by_name.get(row.get('manager') or '')
            if not (o and m) or o.manager_id == m.id or (o.id, m.id) in have:
                continue
            db.session.add(OutletAccess(outlet_id=o.id, user_id=m.id))
            have.add((o.id, m.id)); linked += 1
        have_t = {(t.user_id, t.team) for t in TeamAccess.query.all()}
        for row in data.get('supervisors', []):
            m = by_name.get(row.get('manager') or '')
            t = row.get('team')
            if not (m and t) or (m.id, t) in have_t:
                continue
            db.session.add(TeamAccess(user_id=m.id, team=t))
            have_t.add((m.id, t)); linked += 1
        seen = {(e.outlet_id, e.date, e.sku) for e in Entry.query.all()}
        new_e = 0
        for row in data.get('entries', []):
            k = (row.get('outlet'), row.get('date'), row.get('sku'))
            if not all(k) or k in seen:
                continue
            db.session.add(Entry(outlet_id=k[0], date=k[1], sku=k[2],
                                 units=float(row.get('units') or 0), updated_by=me))
            seen.add(k); new_e += 1
        for m in data.get('closed', []):
            if m in P.MONTHS and not db.session.get(MonthClose, m):
                db.session.add(MonthClose(month=m, closed_by=me))
        db.session.commit()
        flash(f'Из копии восстановлено: аккаунтов +{new_u}, привязок точек {linked}, '
              f'отгрузок +{new_e}. Ничего не удалено.')
        return redirect(url_for('users'))

    # ----- инструкция (для всех) -----
    @app.route('/help')
    @login_required()
    def help_page():
        u = current_user()
        # мои цифры — чтобы менеджер видел инструкцию «про себя»
        my = None
        if u.role == 'manager':
            rows = [outlet_summary(o) for o in my_outlets(u)]
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
        rows = [outlet_summary(o) for o in active_q().all()]
        month = request.args.get('month') or last_active_month(rows)
        return render_template('leaderboard.html', lead=leaderboards(rows, month),
                               month=month)

    @app.route('/healthz')
    def healthz():
        return jsonify(ok=True, plans=len(P.PLANS),
                       db=app.config['DB_LABEL'],
                       persistent=app.config['DB_PERSISTENT'],
                       users=User.query.count(),
                       entries=Entry.query.count())

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
