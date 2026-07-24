# -*- coding: utf-8 -*-
"""Программа и расчёт выплат — зеркало модели build_excel_v18 / build_excel_kws_ru_v1.
Единственный источник правды для трекера. Данные — program_data.json (генерируется из
selection_v5.json). Все суммы в тенге."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, 'program_data.json'), encoding='utf-8') as f:
    DATA = json.load(f)

META = DATA['meta']
PLANS = DATA['plans']
SKUS = DATA['skus']                       # {'HY':[{name,vol,price_kzt}], 'MC':[...]}
FX = META['fx']                           # 560
RSP = META['rsp']                         # {'HY':91,'MC':83.4}
TEAM_RATE = META['team_rate']             # 0.015
BOTT = META['bott']                       # {'HY':0.7,'MC':0.75}
WAVES = META['waves']                     # {'HY':[.215,.25,.535],'MC':[...]}
RATE_W1 = META['rate_w1']                 # {'p90':.03,'p100':.05,'p110':.07}
RATE_STD = META['rate_std']               # {'p90':.02,'p100':.04,'p110':.06}
WAVE_PERIODS = META['wave_periods']       # {'w1':'Июль-Август',...}

# Коммерческие условия (КУ): нетто-цена = прайс × (1-КУ).
# 20% — как в листе «Расчёт выплат» файла для команды (решение Didar 24.07), чтобы цифры
# в трекере и в Excel-файле совпадали. Для per-channel (OFF 15% / ON 20%) поменяйте KU_FLAT
# на KU = {'OFF':0.15,'ON':0.20} и вызовы .get(channel).
KU_FLAT = 0.20
KU = {'OFF': KU_FLAT, 'ON': KU_FLAT}

# Месяц -> волна (1/2/3)
MONTHS = ['2026-07', '2026-08', '2026-09', '2026-10', '2026-11', '2026-12']
MONTH_RU = {'2026-07': 'Июль', '2026-08': 'Август', '2026-09': 'Сентябрь',
            '2026-10': 'Октябрь', '2026-11': 'Ноябрь', '2026-12': 'Декабрь'}
MONTH_WAVE = {'2026-07': 1, '2026-08': 1, '2026-09': 2, '2026-10': 2, '2026-11': 3, '2026-12': 3}
WAVE_MONTHS = {1: ['2026-07', '2026-08'], 2: ['2026-09', '2026-10'], 3: ['2026-11', '2026-12']}

def hf(x):
    return int(x + 0.5)

PLAN_BY_ID = {p['id']: p for p in PLANS}

def sku_list(brand):
    return SKUS[brand]

def net_price(brand, sku_name, channel):
    for s in SKUS[brand]:
        if s['name'] == sku_name:
            return s['price_kzt'] * (1 - KU.get(channel, 0.15))
    return 0.0

def rate_for(brand, wave, tier):
    """Ставка приза: волна 1 — повышенная (3/5/7), волны 2-3 — стандартная (2/4/6)."""
    table = RATE_W1 if wave == 1 else RATE_STD
    return table[tier]

def eq_bottles(brand, units_by_sku):
    """Штуки по SKU -> эквивалент бутылок (0,7 HY / 0,75 MC)."""
    tot = 0.0
    for s in SKUS[brand]:
        u = units_by_sku.get(s['name'], 0) or 0
        tot += u * s['vol']
    return tot / BOTT[brand]

def turnover_kzt(brand, channel, units_by_sku):
    tot = 0.0
    for s in SKUS[brand]:
        u = units_by_sku.get(s['name'], 0) or 0
        tot += u * s['price_kzt'] * (1 - KU.get(channel, 0.15))
    return tot

def wave_target(plan, wave):
    return plan[f'w{wave}']

def compute_wave(plan, wave, units_by_sku):
    """Расчёт выплаты за волну по фактическим SKU. Возвращает dict с bottles/ach/prize/team/total."""
    brand, channel = plan['brand'], plan['channel']
    tgt = wave_target(plan, wave)
    bottles = eq_bottles(brand, units_by_sku)
    turn = turnover_kzt(brand, channel, units_by_sku)
    ach = bottles / tgt if tgt else 0.0
    prize = team = 0
    if ach >= 0.9:
        tier = 'p110' if ach >= 1.1 else 'p100' if ach >= 1.0 else 'p90'
        r = rate_for(brand, wave, tier)
        prize = hf(r * turn * min(ach, 1.1) / ach) if ach else 0
        team = hf(TEAM_RATE * min(bottles, 1.1 * tgt) * RSP[brand] * FX)
    # Потолки: заморожены на уровне базового таргета (значения из модели, доля волны)
    share = {1: WAVES[brand][0], 2: WAVES[brand][1], 3: WAVES[brand][2]}[wave]
    cap_prize = hf(plan['prize110_kzt'] * share)
    cap_team = hf(plan['team110_kzt'] * share)
    prize = min(prize, cap_prize)
    team = min(team, cap_team)
    return dict(bottles=round(bottles, 1), target=tgt, ach=ach, turnover=hf(turn),
                prize=prize, team=team, total=prize + team, tier=(
                    '110%+' if ach >= 1.1 else '100-109%' if ach >= 1.0 else '90-99%' if ach >= 0.9 else '<90%'))

def month_wave(month):
    return MONTH_WAVE[month]

def program_totals():
    """Максимальные (при 110%) призы по программе — для дашборда."""
    t = dict(prize110=0, team110=0, n=len(PLANS))
    for p in PLANS:
        t['prize110'] += p['prize110_kzt']
        t['team110'] += p['team110_kzt']
    t['max_total'] = t['prize110'] + t['team110']
    return t
