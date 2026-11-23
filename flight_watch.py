"""Manage locally saved flight pairs, their target prices, and quote history."""

import json
import re
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path

import flight_details as details
import monitor

TEMPLATE = Path(__file__).with_name("watch_page.html")
BAGGAGE_LABELS = {
    "1 free carry-on": "1 件免费随身行李",
    "1st checked bag free": "首件托运行李免费",
    "no checked bags": "无托运行李额",
}


def offer_baggage(offer):
    return " · ".join(escape(BAGGAGE_LABELS.get(note.lower(), note)) for note in offer["baggage"])


def input_value(params, key):
    return params.get(key, [""])[0]


def selection(db, params, trips):
    dates = input_value(params, "outbound"), input_value(params, "returning")
    trip = next((trip for trip in trips if trip.dates == dates), None)
    airport = input_value(params, "airport")
    choice = input_value(params, "choice")
    if not trip or not re.fullmatch(r"[A-Z]{3}", airport) or not re.fullmatch(r"[0-9a-f]{24}", choice):
        return None
    fare = db.execute("""
        SELECT city FROM fares WHERE outbound = ? AND return_date = ? AND airport = ?
        ORDER BY day DESC, cents LIMIT 1
    """, (*trip.dates, airport)).fetchone()
    if not fare:
        return None
    for row in monitor.saved_options(db, trip, airport):
        for option in json.loads(row["options"]):
            if details.watch_id(option) == choice:
                return trip, airport, fare["city"], option
    return None


def target_price(params):
    text = input_value(params, "target").strip()
    if not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,2})?", text):
        raise monitor.ApiError("请输入大于 0 的目标价，最多保留两位小数。")
    cents = int(Decimal(text) * 100)
    if not cents:
        raise monitor.ApiError("请输入大于 0 的目标价。")
    return cents


def field(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value))}">'


def amount_field(target):
    return (
        '<label>单人往返目标价（元）'
        f'<input name="target" type="number" min="0.01" max="999999.99" step="0.01" required value="{target / 100:.2f}"></label>'
    )


def pair_markup(pair):
    return (
        f'<dl><div><dt>去</dt><dd>{details.short_leg(pair["out"])} · {escape(pair["out"]["number"])}</dd></div>'
        f'<div><dt>回</dt><dd>{details.short_leg(pair["back"])} · {escape(pair["back"]["number"])}</dd></div></dl>'
    )


def quote_now(db, watch, holidays):
    try:
        key = monitor.api_key(monitor.DATA / "serpapi.dpapi")
        if not key:
            return "请先配置 SerpApi 密钥。"
        error, attempted = monitor.check_watch(db, key, holidays, watch)
        if error:
            return str(error)
        return "已核对这套往返的报价。" if attempted else "过去一小时已查询，显示这次采集的记录。"
    except monitor.ApiError as error:
        return str(error)


def mutate_watch(db, params, holidays, today):
    trips = monitor.planned_trips(holidays, today)
    dates = {trip.dates for trip in trips}
    action = input_value(params, "action")
    if action == "save":
        selected = selection(db, params, trips)
        if not selected:
            raise monitor.ApiError("这套航班目前无法添加关注。")
        trip, airport, city, option = selected
        watch_id = details.watch_id(option)
        with db:
            db.execute("""
                INSERT INTO flight_watches VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET target_cents = excluded.target_cents, enabled = 1
            """, (watch_id, *trip.dates, airport, city, json.dumps(option, ensure_ascii=False),
                  target_price(params), today.isoformat()))
        watch = db.execute("SELECT * FROM flight_watches WHERE id = ?", (watch_id,)).fetchone()
        return "已关注。 " + quote_now(db, watch, holidays)

    watch_id = input_value(params, "id")
    watch = db.execute("SELECT * FROM flight_watches WHERE id = ?", (watch_id,)).fetchone()
    if watch is None:
        raise monitor.ApiError("找不到这套关注航班。")
    if action == "target":
        with db:
            db.execute("UPDATE flight_watches SET target_cents = ? WHERE id = ?", (target_price(params), watch_id))
        return "目标价已更新。"
    in_scope = (watch["outbound"], watch["return_date"]) in dates
    if action in ("pause", "resume"):
        if action == "resume" and not in_scope:
            raise monitor.ApiError("这组日期当前未监控，暂时无法恢复关注。")
        with db:
            db.execute("UPDATE flight_watches SET enabled = ? WHERE id = ?", (int(action == "resume"), watch_id))
        return "已暂停关注。" if action == "pause" else "已恢复关注。"
    if action == "quote" and watch["enabled"] and in_scope:
        return quote_now(db, watch, holidays)
    raise monitor.ApiError("这套航班当前无法核价。")


def preview_markup(db, params, trips, token):
    if not input_value(params, "choice"):
        return ""
    chosen = selection(db, params, trips)
    if not chosen:
        return '<p class="status">这套航班目前无法添加关注。</p>'
    trip, airport, city, option = chosen
    watch_id = details.watch_id(option)
    existing = db.execute("SELECT id FROM flight_watches WHERE id = ?", (watch_id,)).fetchone()
    if existing:
        return f'<p>这套航班已经保存。<a href="#watch-{watch_id}">查看报价与目标价</a></p>'
    values = {
        "token": token, "action": "save", "outbound": trip.dates[0], "returning": trip.dates[1],
        "airport": airport, "choice": watch_id,
    }
    hidden = "".join(field(name, value) for name, value in values.items())
    return (
        f'<section><h2>关注上海往返{escape(city)}</h2><p>{trip.dates[0]} → {trip.dates[1]}</p>'
        f'<p>上次组合报价 {monitor.money(option["cents"])} / 人</p>{pair_markup(option)}'
        f'<form method="post" action="/watch">{hidden}{amount_field(option["cents"])}'
        '<button class="primary" type="submit">保存并核价</button></form></section>'
    )


def watch_card(db, watch, dates, today, token):
    trip = monitor.Trip(date.fromisoformat(watch["outbound"]), date.fromisoformat(watch["return_date"]))
    active = bool(watch["enabled"]) and trip.dates in dates
    observations = db.execute("""
        SELECT day, checked_at, cents, offer, error FROM watch_observations
        WHERE watch_id = ? ORDER BY day DESC
    """, (watch["id"],)).fetchall()
    recent = observations[0] if observations else None
    priced = next((row for row in observations if row["cents"] is not None), None)
    day = today.isoformat()
    reached = active and priced and priced["day"] == day and priced["cents"] <= watch["target_cents"]
    quote = "待首次核价"
    offer = None
    if priced:
        offer = json.loads(priced["offer"])
        stamp = priced["checked_at"][:16].replace("T", " ")
        if priced["day"] == day:
            quote_label = "最新报价"
        else:
            quote_label = "上次有效报价"
        quote = f'{quote_label} <strong>{monitor.money(priced["cents"])}</strong> / 人 · {stamp}'
    if not watch["enabled"]:
        status = "已暂停关注"
    elif trip.outbound < today:
        status = "出发日期已过"
    elif not active:
        status = "行程暂未监控"
    elif reached:
        status = "达到目标价"
    elif priced and priced["day"] == day:
        difference = priced["cents"] - watch["target_cents"]
        status = f"高于目标 {monitor.money(difference)}"
    else:
        status = "等待当天有效报价"

    source = ""
    if offer:
        seller = escape(offer["seller"]) or "来源卖家未标明"
        bag = offer_baggage(offer) or "行李额度待复核"
        source = f'<p class="muted">{seller} · {bag}</p>'
    last_error = ""
    if recent and recent["error"]:
        stamp = recent["checked_at"][:16].replace("T", " ")
        last_error = f'<p class="status">{stamp} · {escape(recent["error"])}</p>'
    rows = "".join(
        f'<li><time>{row["day"]}</time> <strong>{monitor.money(row["cents"]) if row["cents"] else "无有效报价"}</strong></li>'
        for row in observations
    )
    history = f'<details><summary>核价记录 · {len(observations)} 天</summary><ul>{rows}</ul></details>' if rows else ""
    controls = field("id", watch["id"]) + field("token", token)
    toggle = ("pause", "暂停关注") if watch["enabled"] else ("resume", "恢复关注")
    toggle_button = ""
    if watch["enabled"] or trip.dates in dates:
        toggle_button = f'<button type="submit" name="action" value="{toggle[0]}">{toggle[1]}</button>'
    recheck = '<button type="submit" name="action" value="quote">立即核价</button>' if active else ""
    return (
        f'<article id="watch-{watch["id"]}"><div class="heading"><h2>{escape(watch["city"])}</h2>'
        f'<span class="badge{" reached" if reached else ""}">{status}</span></div>'
        f'<p class="muted">{trip.dates[0]} → {trip.dates[1]} · 1 位成人经济舱</p>'
        f'{pair_markup(offer or json.loads(watch["option"]))}<p class="quote">{quote}</p>{source}{last_error}'
        f'<form method="post" action="/watch">{controls}{amount_field(watch["target_cents"])}'
        '<button type="submit" name="action" value="target">保存目标价</button></form>'
        f'<form method="post" action="/watch">{controls}{recheck}{toggle_button}'
        f'<a href="{escape(monitor.flight_link(watch, trip))}" target="_blank" rel="noopener">飞猪查航班</a></form>'
        f'{history}</article>'
    )


def page(db, params, holidays, today, token, message=""):
    trips = monitor.planned_trips(holidays, today)
    dates = {trip.dates for trip in trips}
    watches = db.execute("SELECT * FROM flight_watches ORDER BY enabled DESC, outbound, created_at DESC, id")
    items = "".join(watch_card(db, watch, dates, today, token) for watch in watches)
    return TEMPLATE.read_text(encoding="utf-8").format(
        comparison=escape(monitor.comparison_link()), preview=preview_markup(db, params, trips, token),
        watches=items or '<p>还没有关注的航班。可从「选去哪」或「航班详情」添加具体往返。</p>',
        message=escape(message),
    )
