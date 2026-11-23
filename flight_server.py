"""Serve on-demand itinerary details to the local fare dashboard."""

import json
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import flight_details as details
import flight_watch
import monitor
import party_quote
import trip_comparison

ORIGIN = urlsplit(monitor.DETAILS_ORIGIN)
ASSETS = {
    "/assets/theme.css": "text/css; charset=utf-8",
    "/assets/shanghai-blue-hour.png": "image/png",
}
REQUEST_TOKEN = secrets.token_urlsafe(24)
TEMPLATE = Path(__file__).with_name("flight_page.html")


def route_from(params, db):
    try:
        trip = monitor.Trip(date.fromisoformat(params["outbound"][0]), date.fromisoformat(params["returning"][0]))
        airport = params["airport"][0]
    except (KeyError, IndexError, ValueError):
        return None
    if trip.returning < trip.outbound or not re.fullmatch(r"[A-Z]{3}", airport):
        return None
    fare = db.execute("""
        SELECT city, airport, day, cents FROM fares
        WHERE outbound = ? AND return_date = ? AND airport = ?
        ORDER BY day DESC, cents LIMIT 1
    """, (*trip.dates, airport)).fetchone()
    return (trip, fare) if fare else None


def leg_card(name, leg):
    dep, arr = leg["departure"], leg["arrival"]
    return (
        f'<div class="leg"><h3>{name} <span>{escape(leg["airline"])} {escape(leg["number"])}'
        f' · 直飞 {details.duration(leg["minutes"])}</span></h3>'
        f'<div class="times"><div><strong>{escape(dep["time"][5:])}</strong>'
        f'<span>{escape(dep["name"])} ({escape(dep["code"])})</span></div>'
        f'<span aria-hidden="true">→</span><div><strong>{escape(arr["time"][5:])}</strong>'
        f'<span>{escape(arr["name"])} ({escape(arr["code"])})</span></div></div></div>'
    )


def itinerary_cards(snapshots, selected, trip, airport):
    history = {}
    for snapshot in snapshots:
        for option in json.loads(snapshot["options"]):
            history.setdefault(details.signature(option), []).append((snapshot["day"], option["cents"]))

    airports = {}
    cards = []
    for option in json.loads(selected["options"]):
        out, back = option["out"], option["back"]
        origin = out["departure"]["code"]
        airports[origin] = details.AIRPORT_LABELS.get(origin, origin)
        overnight = out["departure"]["time"][:10] != out["arrival"]["time"][:10]
        back_overnight = back["departure"]["time"][:10] != back["arrival"]["time"][:10]
        red_eye = details.is_red_eye(out) or details.is_red_eye(back)
        tags = []
        if overnight:
            tags.append("去程次日到达")
        if back_overnight:
            tags.append("返程次日到达")
        if origin != back["arrival"]["code"]:
            tags.append("出发和抵沪机场不同")
        if out["arrival"]["code"] != back["departure"]["code"]:
            tags.append("目的地到达和离开机场不同")
        stay = datetime.fromisoformat(back["departure"]["time"]) - datetime.fromisoformat(out["arrival"]["time"])
        days_count, hours = divmod(stay.seconds // 3600 + stay.days * 24, 24)
        stay_label = f"{days_count} 天 {hours} 小时" if days_count else details.duration(int(stay.total_seconds() // 60))
        badges = "".join(f'<span class="tag">{tag}</span>' for tag in tags)
        baggage = (
            " · ".join(escape(note) for note in option["baggage"])
            or "数据源未列出这套组合的行李额度，购票时请核对。"
        )
        days = history[details.signature(option)]
        timeline = "".join(f"<li>{day} <strong>{monitor.money(cents)}</strong></li>" for day, cents in days)
        watch = escape(monitor.itinerary_link("watch", trip, airport, option))
        party = escape(monitor.itinerary_link("group-quote", trip, airport, option))
        cards.append(
            f'<article data-price="{option["cents"]}" data-arrive="{out["arrival"]["time"]}" '
            f'data-back="{back["arrival"]["time"]}" data-origin="{origin}" '
            f'data-overnight="{int(overnight)}" data-red-eye="{int(red_eye)}">'
            f'<header><strong class="price">{monitor.money(option["cents"])}</strong> <span>单人往返</span>{badges}</header>'
            f'{leg_card("去程", out)}{leg_card("返程", back)}'
            f'<p class="fine">去程落地至返航起飞约 {stay_label}。{baggage}</p>'
            f'<p class="actions"><a href="{party}">按人数核价</a><a href="{watch}">关注此往返</a></p>'
            f'<details><summary>同一组航班的历史报价 · {len(days)} 天</summary>'
            f'<ul>{timeline}</ul></details></article>'
        )
    return "".join(cards), "".join(f'<option value="{code}">{name}</option>' for code, name in airports.items())


def flight_page(db, trip, fare, holidays, message="", selected_day=""):
    today = datetime.now(monitor.CHINA_TIME).date()
    snapshots = monitor.saved_options(db, trip, fare["airport"])
    form = ""
    if trip in monitor.planned_trips(holidays, today):
        fields = {"outbound": trip.dates[0], "returning": trip.dates[1],
                  "airport": fare["airport"], "token": REQUEST_TOKEN}
        inputs = "".join(f'<input type="hidden" name="{name}" value="{value}">' for name, value in fields.items())
        button = "更新往返方案" if snapshots else "查询往返方案"
        form = f'<form id="refresh" method="post" action="/flights">{inputs}<button type="submit">{button}</button></form>'

    cards, airports = ("", "")
    quote_status = "暂无已配对的航班。查询后会显示去程、返程和组合报价。"
    archive = ""
    if snapshots:
        selected = next((row for row in snapshots if row["day"] == selected_day), snapshots[0])
        cards, airports = itinerary_cards(snapshots, selected, trip, fare["airport"])
        checked = datetime.fromisoformat(selected["checked_at"])
        quote_status = f"组合报价采集于 {checked:%Y-%m-%d %H:%M}（北京时间）"
        if len(snapshots) > 1:
            links = []
            for row in snapshots:
                day = row["day"]
                url = f'{monitor.detail_link(fare["airport"], trip)}&day={day}'
                active = ' aria-current="true"' if day == selected["day"] else ""
                links.append(f'<a href="{escape(url)}"{active}>{day}</a>')
            archive = '<nav aria-label="组合报价采集日期">' + " ".join(links) + "</nav>"
    return TEMPLATE.read_text(encoding="utf-8").format(
        city=escape(fare["city"]), date_range=f"{trip.dates[0]} → {trip.dates[1]}",
        quote_status=quote_status, form=form, cards=cards, airports=airports,
        hidden="" if cards else " hidden", message=escape(message), archive=archive,
        fliggy=escape(monitor.flight_link(fare, trip)), red_eye_rule=escape(details.RED_EYE_RULE),
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_content(self, content, status=200, content_type="text/html; charset=utf-8"):
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'self'; img-src 'self'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") != ORIGIN.netloc:
            return self.send_error(403)
        url = urlsplit(self.path)
        if url.path in ASSETS:
            asset = monitor.ROOT / url.path.lstrip("/")
            if asset.is_file():
                return self.send_content(asset.read_bytes(), content_type=ASSETS[url.path])
            return self.send_error(404)
        if url.path == "/health":
            return self.send_content("shanghai-fare-details", content_type="text/plain")
        if url.path == "/":
            return self.send_content(monitor.REPORT.read_text(encoding="utf-8"))
        if url.path == "/compare":
            return self.show_comparison(parse_qs(url.query))
        if url.path == "/watch":
            return self.show_watch(parse_qs(url.query))
        if url.path == "/group-quote":
            return self.show_group_quote(parse_qs(url.query))
        if url.path != "/flights":
            return self.send_error(404)
        self.show_flight(parse_qs(url.query))

    def do_POST(self):
        if (self.headers.get("Host") != ORIGIN.netloc or self.headers.get("Origin") != monitor.DETAILS_ORIGIN
                or self.path not in ("/flights", "/compare", "/watch", "/group-quote")):
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self.send_error(400)
            limit = {"/watch": 7, "/group-quote": 6}.get(self.path, 4)
            params = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=limit)
        except (UnicodeError, ValueError):
            return self.send_error(400)
        token = params.get("token", [""])[0]
        if not secrets.compare_digest(token.encode("utf-8"), REQUEST_TOKEN.encode("utf-8")):
            return self.send_error(403)
        if self.path == "/compare":
            return self.show_comparison(params, update=True)
        if self.path == "/watch":
            return self.show_watch(params, update=True)
        if self.path == "/group-quote":
            return self.show_group_quote(params, update=True)
        self.show_flight(params, update=True)

    def redirect(self, url, message):
        separator = "&" if "?" in url else "?"
        self.send_response(303)
        self.send_header("Location", url + separator + urlencode({"message": message}))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def show_watch(self, params, update=False):
        try:
            holidays = monitor.load_holidays()
            today = datetime.now(monitor.CHINA_TIME).date()
            with closing(monitor.open_database()) as db:
                if update:
                    try:
                        message = flight_watch.mutate_watch(db, params, holidays, today)
                    except monitor.ApiError as error:
                        message = str(error)
                    monitor.write_report(db, holidays, today)
                    return self.redirect(monitor.itinerary_link("watch"), message)
                message = params.get("message", [""])[0][:250]
                page = flight_watch.page(db, params, holidays, today, REQUEST_TOKEN, message)
            self.send_content(page)
        except (OSError, ValueError, sqlite3.Error) as error:
            logging.error("关注航班页面加载失败（%s）", type(error).__name__)
            self.send_content('暂时无法显示关注航班。<a href="/">返回机票看板</a>', status=500)

    def show_group_quote(self, params, update=False):
        try:
            holidays = monitor.load_holidays()
            today = datetime.now(monitor.CHINA_TIME).date()
            with closing(monitor.open_database()) as db:
                selected = flight_watch.selection(db, params, monitor.planned_trips(holidays, today))
                if not selected:
                    return self.send_content(
                        '这套航班目前无法核价。<a href="/compare">返回选去哪</a>', status=404)
                adults = party_quote.adult_count(params)
                if update:
                    try:
                        adults = party_quote.adult_count(params, strict=True)
                        message = party_quote.quote(db, holidays, selected[3], adults)
                    except monitor.ApiError as error:
                        message = str(error)
                    url = monitor.itinerary_link("group-quote", selected[0], selected[1], selected[3])
                    return self.redirect(url + "&" + urlencode({"adults": adults}), message)
                message = params.get("message", [""])[0][:250]
                page = party_quote.page(db, selected, adults, REQUEST_TOKEN, today, message)
            self.send_content(page)
        except (OSError, ValueError, sqlite3.Error) as error:
            logging.error("按人数核价页面加载失败（%s）", type(error).__name__)
            self.send_content('暂时无法显示人数报价。<a href="/compare">返回选去哪</a>', status=500)

    def show_comparison(self, params, update=False):
        try:
            holidays = monitor.load_holidays()
            today = datetime.now(monitor.CHINA_TIME).date()
            with closing(monitor.open_database()) as db:
                message = params.get("message", [""])[0][:250]
                if update:
                    trip, _ = trip_comparison.select_trip(params, holidays, today)
                    if trip is None:
                        return self.send_content('这组日期目前未监控。<a href="/">返回机票看板</a>', status=404)
                    message = trip_comparison.extend_shortlist(db, trip, holidays, today)
                    return self.redirect(monitor.comparison_link(trip), message)
                page = trip_comparison.comparison_page(db, params, holidays, today, REQUEST_TOKEN, message)
            self.send_content(page)
        except (OSError, ValueError, sqlite3.Error, monitor.ApiError) as error:
            logging.error("目的地比较页面加载失败（%s）", type(error).__name__)
            self.send_content('暂时无法显示目的地比较。<a href="/">返回机票看板</a>', status=500)

    def show_flight(self, params, update=False):
        try:
            holidays = monitor.load_holidays()
            with closing(monitor.open_database()) as db:
                route = route_from(params, db)
                if not route:
                    return self.send_content('找不到该行程。<a href="/">返回机票看板</a>', status=404)
                trip, fare = route
                message = ""
                if update:
                    today = datetime.now(monitor.CHINA_TIME).date()
                    try:
                        if trip not in monitor.planned_trips(holidays, today):
                            raise monitor.ApiError("这组日期目前未监控，可查看已保存的航班方案。")
                        key = monitor.api_key(monitor.DATA / "serpapi.dpapi")
                        if not key:
                            raise monitor.ApiError("请先配置 SerpApi 密钥。")
                        calls = monitor.refresh_options(db, key, holidays, trip, fare["airport"])
                        message = "已更新航班方案。" if calls else "过去一小时已查过，已显示这次采集的方案。"
                        if calls:
                            monitor.write_report(db, holidays, today)
                    except monitor.ApiError as error:
                        message = str(error)
                selected_day = params.get("day", [""])[0] if not update else ""
                page = flight_page(db, trip, fare, holidays, message, selected_day)
            self.send_content(page)
        except (OSError, ValueError, sqlite3.Error) as error:
            logging.error("航班详情页面加载失败（%s）", type(error).__name__)
            self.send_content('暂时无法显示航班详情。<a href="/">返回机票看板</a>', status=500)


if __name__ == "__main__":
    monitor.DATA.mkdir(exist_ok=True)
    logging.basicConfig(filename=monitor.DATA / "flight_details.log", encoding="utf-8", level=logging.INFO)
    with ThreadingHTTPServer((ORIGIN.hostname, ORIGIN.port), Handler) as server:
        logging.info("航班详情已在本机启动。")
        server.serve_forever()
