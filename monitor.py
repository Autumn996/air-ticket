"""Collect and compare Shanghai holiday airfares."""

import argparse
import ctypes
import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import flight_details as details

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORT = ROOT / "机票看板.html"
CHINA_TIME = timezone(timedelta(hours=8))
ORIGIN = "PVG,SHA"
CHINA_REGION = "/m/0d05w3"
LOOKAHEAD = timedelta(days=120)
HOLIDAY_LIMIT = 2
DAY = timedelta(days=1)
API_ROOT = "https://serpapi.com"
DETAILS_ORIGIN = "http://127.0.0.1:8767"
DAILY_SEARCH_BUDGET = 8
DAILY_WATCH_LIMIT = 2


@dataclass(frozen=True)
class Trip:
    outbound: date
    returning: date

    @property
    def dates(self):
        return self.outbound.isoformat(), self.returning.isoformat()

    @property
    def label(self):
        if self.outbound == self.returning:
            return f"{self.outbound:%m/%d} 当天往返"
        return f"{self.outbound:%m/%d} 出发 · {self.returning:%m/%d} 返程"


@dataclass(frozen=True)
class Holiday:
    name: str
    start: date
    end: date
    planned_trip: Trip | None = None
    paused: bool = False

    @property
    def last_day(self):
        return max(self.end, self.planned_trip.returning) if self.planned_trip else self.end

    def trips(self):
        if self.planned_trip:
            return [self.planned_trip]
        if self.end - self.start <= DAY:
            return [Trip(self.start, self.end)]
        short_stay = min(self.end - self.start - DAY, DAY * 2)
        return [
            Trip(self.start, self.end),
            Trip(self.start, self.start + short_stay),
            Trip(self.end - short_stay, self.end),
        ]


class ApiError(Exception):
    def __init__(self, message, *, stop=False):
        super().__init__(message)
        self.stop = stop


def load_holidays():
    document = json.loads((ROOT / "holidays.json").read_text(encoding="utf-8"))
    holidays = []
    for item in document["holidays"]:
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if start > end:
            raise ValueError("节假日的开始日期应早于或等于结束日期")
        plan = item.get("planned_trip")
        trip = Trip(date.fromisoformat(plan["outbound"]), date.fromisoformat(plan["returning"])) if plan else None
        if trip and trip.outbound > trip.returning:
            raise ValueError("行程的出发日期应早于或等于返程日期")
        holidays.append(Holiday(item["name"], start, end, trip, item.get("paused", False)))
    return sorted(holidays, key=lambda holiday: holiday.start)


def monitored_holidays(holidays, today):
    return [
        holiday
        for holiday in holidays
        if not holiday.paused
        and any(today <= trip.outbound <= today + LOOKAHEAD for trip in holiday.trips())
    ][:HOLIDAY_LIMIT]


def api_key(path):
    if not path.is_file():
        return os.environ.get("SERPAPI_API_KEY", "").strip()

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]

    try:
        encrypted = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except ValueError:
        raise ApiError("本地密钥文件格式异常，请重新配置。", stop=True) from None
    buffer = ctypes.create_string_buffer(encrypted)
    protected = DataBlob(len(encrypted), ctypes.addressof(buffer))
    plaintext = DataBlob()
    no_ui = 0x1
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(protected), None, None, None, None, no_ui, ctypes.byref(plaintext),
    ):
        raise ApiError("无法读取本地密钥，请用原 Windows 账户重新配置。", stop=True)
    try:
        return ctypes.string_at(plaintext.data, plaintext.size).decode("utf-16-le").strip()
    finally:
        ctypes.memset(plaintext.data, 0, plaintext.size)
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(plaintext.data))


def request_json(endpoint, key, **params):
    url = f"{API_ROOT}/{endpoint}?" + urlencode({**params, "api_key": key})
    try:
        with urlopen(url, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        if error.code in (401, 403):
            raise ApiError("票价接口拒绝访问，请检查密钥及账户状态。", stop=True) from None
        if error.code == 429:
            raise ApiError("票价接口达到调用限制，请检查账户额度。", stop=True) from None
        raise ApiError(f"票价接口暂时不可用（HTTP {error.code}）。") from None
    except (URLError, OSError, ValueError):
        raise ApiError("票价接口连接失败或返回异常，请稍后重试。") from None
    if not isinstance(result, dict):
        raise ApiError("票价接口返回了无法识别的数据。")
    return result


def text_field(value):
    return value.strip() if isinstance(value, str) else ""


def parse_fares(result, trip):
    metadata = result.get("search_metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "Success":
        raise ApiError("票价接口未完成本次查询。")
    params = result.get("search_parameters") or {}
    if not isinstance(params, dict):
        raise ApiError("票价接口返回了无法识别的查询参数。")
    currency = params.get("currency", "CNY")
    if text_field(currency).upper() != "CNY":
        raise ApiError("票价接口返回的币种与人民币查询不符。")
    destinations = result.get("destinations", [])
    if not isinstance(destinations, list):
        raise ApiError("票价接口返回了无法识别的目的地列表。")

    cheapest = {}
    for item in destinations:
        if not isinstance(item, dict):
            continue
        if (item.get("start_date"), item.get("end_date")) != trip.dates:
            continue
        price = item.get("flight_price")
        airport_data = item.get("destination_airport") or {}
        if not isinstance(airport_data, dict):
            continue
        airport = text_field(airport_data.get("code"))
        city = text_field(airport_data.get("location")) or text_field(item.get("name"))
        city_id = text_field(airport_data.get("location_id")) or text_field(item.get("destination_id"))
        if (type(price) not in (int, float) or not 0 < price < 1_000_000
                or not re.fullmatch("[A-Z]{3}", airport) or airport in ORIGIN.split(",")
                or not city):
            continue
        if item.get("number_of_stops") not in (None, 0):
            continue
        city_id = city_id or city.casefold()
        fare = (city_id, city, airport, round(price * 100), text_field(item.get("airline")))
        if city_id not in cheapest or fare[3] < cheapest[city_id][3]:
            cheapest[city_id] = fare
    if destinations and not cheapest:
        has_priced_flights = any(
            isinstance(item, dict) and item.get("flight_price") for item in destinations
        )
        if has_priced_flights:
            raise ApiError("来源返回了票价，但没有能核对为指定日期的直飞往返。")
    return list(cheapest.values())


def fetch_fares(key, trip):
    outbound, returning = trip.dates
    result = request_json(
        "search.json", key, engine="google_travel_explore", departure_id=ORIGIN,
        arrival_area_id=CHINA_REGION, type=1, outbound_date=outbound, return_date=returning,
        travel_class=1, adults=1, stops=1, travel_mode=1, currency="CNY", hl="zh-cn", gl="us",
    )
    return parse_fares(result, trip)


def planned_trips(holidays, today):
    return list(dict.fromkeys(
        trip for holiday in monitored_holidays(holidays, today)
        for trip in holiday.trips() if today <= trip.outbound <= today + LOOKAHEAD
    ))


def detail_allowance(db, key, holidays, today, *, for_watch=False):
    account = request_json("account.json", key)
    left = account.get("total_searches_left")
    try:
        renewal = date.fromisoformat(account["plan_renewal_date"])
    except (KeyError, TypeError, ValueError):
        raise ApiError("暂时无法核对本期额度，请稍后再试。") from None
    if type(left) is not int:
        raise ApiError("暂时无法核对本期额度，请稍后再试。")

    days = max(0, (renewal - today).days + 1)
    future = [planned_trips(holidays, today + DAY * i) for i in range(days)]
    reserve = sum(map(len, future))
    if not for_watch:
        routes = [tuple(row) for row in db.execute(
            "SELECT outbound, return_date FROM flight_watches WHERE enabled = 1"
        )]
        for trips in future:
            dates = {trip.dates for trip in trips}
            reserve += min(watch_budget(trips), sum(route in dates for route in routes))
    return max(0, left - reserve)


def flight_results(key, params, **extra):
    result = request_json("search.json", key, **params, **extra)
    metadata = result.get("search_metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "Success":
        raise ApiError("数据源暂时没有完成航班查询。")
    search_params = result.get("search_parameters") or {}
    if not isinstance(search_params, dict) or text_field(search_params.get("currency", "CNY")).upper() != "CNY":
        raise ApiError("航班报价币种异常，请稍后再试。")
    return details.flight_groups(result)


def saved_options(db, trip, airport):
    return db.execute("""
        SELECT * FROM flight_options
        WHERE outbound = ? AND return_date = ? AND airport = ?
        ORDER BY day DESC
    """, (*trip.dates, airport)).fetchall()


def refresh_options(db, key, holidays, trip, airport, max_calls=3, *, automated=False):
    if max_calls < 2:
        return 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(CHINA_TIME)
        today = now.date()
        cached = saved_options(db, trip, airport)
        if cached:
            recent = datetime.fromisoformat(cached[0]["checked_at"])
            if recent.date() == today and (automated or now - recent < timedelta(hours=1)):
                return 0

        allowance = min(max_calls, detail_allowance(db, key, holidays, today))
        if allowance < 2:
            raise ApiError("本期航班详情额度不足，剩余额度留给每日票价监控。", stop=True)

        outbound, returning = trip.dates
        destinations = details.CO_TERMINALS.get(airport, (airport,))
        params = dict(
            engine="google_flights", departure_id=ORIGIN, arrival_id=",".join(destinations),
            type=1, outbound_date=outbound, return_date=returning, adults=1,
            travel_class=1, stops=1, currency="CNY", hl="zh-cn", gl="us",
        )
        groups = flight_results(key, params)
        selected = details.select_departures(groups, outbound, destinations, allowance - 1)
        options = []
        last_error = None
        calls = 1
        for _, leg, token in selected:
            calls += 1
            try:
                returns = flight_results(key, params, departure_token=token)
                options.extend(details.return_options(returns, returning, destinations, leg))
            except ApiError as error:
                last_error = error
                if error.stop:
                    break
        if not options:
            if last_error:
                raise last_error
            raise ApiError("数据源没有返回可核对的直飞往返组合。")
        options.sort(key=lambda item: (item["cents"], item["out"]["arrival"]["time"]))
        db.execute(
            "INSERT OR REPLACE INTO flight_options VALUES (?, ?, ?, ?, ?, ?)",
            (today.isoformat(), *trip.dates, airport, now.isoformat(timespec="seconds"),
             json.dumps(options, ensure_ascii=False, separators=(",", ":"))),
        )
        return calls


def watch_budget(trips):
    return max(0, min(DAILY_WATCH_LIMIT, DAILY_SEARCH_BUDGET - len(trips)))


def watch_trips(db, trips):
    dates = {trip.dates for trip in trips}
    rows = db.execute("""
        SELECT w.*, MAX(o.day) AS recent_day
        FROM flight_watches w LEFT JOIN watch_observations o ON o.watch_id = w.id
        WHERE w.enabled = 1 GROUP BY w.id ORDER BY recent_day, w.created_at, w.id
    """)
    return [row for row in rows if (row["outbound"], row["return_date"]) in dates]


def booking_offer(key, option, adults):
    response = request_json(
        "search.json", key, engine="google_flights", type=1, travel_class=1, adults=adults,
        currency="CNY", hl="zh-cn", gl="us",
        selected_flights_json=json.dumps(details.booking_selection(option), separators=(",", ":")),
    )
    metadata = response.get("search_metadata")
    parameters = response.get("search_parameters")
    if (isinstance(metadata, dict) and metadata.get("status") == "Success"
            and isinstance(parameters, dict) and str(parameters.get("adults", 1)) == str(adults)
            and text_field(parameters.get("currency")).upper() == "CNY"):
        offer = details.booking_quote(response, option)
        if offer:
            return offer
    raise ApiError("来源暂未返回这套往返的可核对报价。")


def check_watch(db, key, holidays, watch, *, automated=False):
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(CHINA_TIME)
        today = now.date()
        day = today.isoformat()
        current = db.execute("""
            SELECT checked_at FROM watch_observations WHERE watch_id = ? AND day = ?
        """, (watch["id"], day)).fetchone()
        if current and (automated or now - datetime.fromisoformat(current["checked_at"]) < timedelta(hours=1)):
            return None, False
        if detail_allowance(db, key, holidays, today, for_watch=True) < 1:
            raise ApiError("本期核价额度不足，剩余额度留给每日票价监控。", stop=True)
        option = json.loads(watch["option"])
        offer = None
        error = None
        try:
            offer = booking_offer(key, option, 1)
        except ApiError as caught:
            error = caught
        db.execute(
            "INSERT OR REPLACE INTO watch_observations VALUES (?, ?, ?, ?, ?, ?)",
            (watch["id"], day, now.isoformat(timespec="seconds"), offer["cents"] if offer else None,
             json.dumps(offer, ensure_ascii=False, separators=(",", ":")) if offer else None,
             str(error) if error else None),
        )
        return error, True


def saved_party_quote(db, option, adults):
    return db.execute("""
        SELECT * FROM party_quotes WHERE option_id = ? AND adults = ?
    """, (details.watch_id(option), adults)).fetchone()


def check_party_quote(db, key, holidays, option, adults):
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(CHINA_TIME)
        prior = saved_party_quote(db, option, adults)
        if prior:
            checked = datetime.fromisoformat(prior["checked_at"])
            if checked.date() == now.date() and now - checked < timedelta(hours=1):
                return prior, False
        if detail_allowance(db, key, holidays, now.date()) < 1:
            raise ApiError("本期按人数核价额度不足，剩余额度留给自动监控。", stop=True)
        offer = None
        error = None
        try:
            offer = booking_offer(key, option, adults)
        except ApiError as caught:
            error = str(caught)
        db.execute(
            "INSERT OR REPLACE INTO party_quotes VALUES (?, ?, ?, ?, ?, ?)",
            (details.watch_id(option), adults, now.isoformat(timespec="seconds"),
             offer["cents"] if offer else None,
             json.dumps(offer, ensure_ascii=False, separators=(",", ":")) if offer else None,
             error),
        )
        return saved_party_quote(db, option, adults), True


def refresh_watches(db, key, holidays, today, trips):
    watches = watch_trips(db, trips)
    used = sum(watch["recent_day"] == today.isoformat() for watch in watches)
    for watch in watches:
        if used >= watch_budget(trips):
            break
        if watch["recent_day"] == today.isoformat():
            continue
        try:
            error, attempted = check_watch(db, key, holidays, watch, automated=True)
            used += int(attempted)
            if error and error.stop:
                break
        except ApiError as error:
            logging.info("关注航班：%s", error)
            break
    return used


def detail_attempts(db, trip, today):
    return {row["airport"] for row in db.execute("""
        SELECT airport FROM comparison_attempts
        WHERE day = ? AND outbound = ? AND return_date = ?
    """, (today.isoformat(), *trip.dates))}


def mark_detail_attempt(db, trip, airport, today):
    with db:
        db.execute("INSERT OR IGNORE INTO comparison_attempts VALUES (?, ?, ?, ?)",
                   (today.isoformat(), *trip.dates, airport))


def due_detail_routes(db, trips, today):
    pending = []
    day = today.isoformat()
    for trip in trips:
        checked = {
            row["airport"]: row["latest"] for row in db.execute("""
                SELECT airport, MAX(day) AS latest FROM (
                    SELECT airport, day FROM flight_options WHERE outbound = ? AND return_date = ?
                    UNION ALL
                    SELECT airport, day FROM comparison_attempts WHERE outbound = ? AND return_date = ?
                )
                GROUP BY airport
            """, (*trip.dates, *trip.dates))
        }
        for fare in db.execute("""
            SELECT airport, MIN(cents) AS cents FROM fares
            WHERE day = ? AND outbound = ? AND return_date = ?
            GROUP BY airport
        """, (day, *trip.dates)):
            airport = fare["airport"]
            latest = checked.get(airport, "")
            if latest != day:
                pending.append((trip, airport, fare["cents"], latest))
    return sorted(pending, key=lambda route: (route[3], route[2], route[0].dates, route[1]))


def prefill_options(db, key, holidays, today, trips, budget):
    if budget < 2:
        return
    pending = due_detail_routes(db, trips, today)
    if not pending:
        return
    with db:
        reserved = db.execute("INSERT OR IGNORE INTO automatic_detail_runs VALUES (?)",
                              (today.isoformat(),)).rowcount
    if not reserved:
        return
    anchor = None
    if budget >= 5:
        # Refresh a low-price candidate and keep at least two calls for the rotation.
        anchor = min(pending, key=lambda route: route[2])
        pending.remove(anchor)
        pending.insert(0, anchor)
    for route in pending:
        if budget < 2:
            break
        trip, airport, _, _ = route
        limit = 3 if route == anchor or budget == 3 else 2
        try:
            budget -= refresh_options(db, key, holidays, trip, airport, limit, automated=True)
        except ApiError as error:
            logging.info("航班详情 %s：%s", airport, error)
            budget -= limit
            if error.stop:
                break
            mark_detail_attempt(db, trip, airport, today)


def open_database():
    db = sqlite3.connect(DATA / "fares.sqlite3", timeout=125)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS searches (
            day TEXT NOT NULL,
            outbound TEXT NOT NULL,
            return_date TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            error TEXT,
            PRIMARY KEY (day, outbound, return_date)
        );
        CREATE TABLE IF NOT EXISTS fares (
            day TEXT NOT NULL,
            outbound TEXT NOT NULL,
            return_date TEXT NOT NULL,
            city_id TEXT NOT NULL,
            city TEXT NOT NULL,
            airport TEXT NOT NULL,
            cents INTEGER NOT NULL CHECK (cents > 0),
            airline TEXT NOT NULL,
            PRIMARY KEY (day, outbound, return_date, city_id)
        );
        CREATE INDEX IF NOT EXISTS fares_trip ON fares (outbound, return_date, day);
        CREATE TABLE IF NOT EXISTS run_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            checked_at TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_options (
            day TEXT NOT NULL,
            outbound TEXT NOT NULL,
            return_date TEXT NOT NULL,
            airport TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            options TEXT NOT NULL,
            PRIMARY KEY (day, outbound, return_date, airport)
        );
        CREATE INDEX IF NOT EXISTS flight_options_trip ON flight_options (outbound, return_date, airport, day);
        CREATE TABLE IF NOT EXISTS comparison_attempts (
            day TEXT NOT NULL,
            outbound TEXT NOT NULL,
            return_date TEXT NOT NULL,
            airport TEXT NOT NULL,
            PRIMARY KEY (day, outbound, return_date, airport)
        );
        CREATE TABLE IF NOT EXISTS automatic_detail_runs (
            day TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS flight_watches (
            id TEXT PRIMARY KEY,
            outbound TEXT NOT NULL,
            return_date TEXT NOT NULL,
            airport TEXT NOT NULL,
            city TEXT NOT NULL,
            option TEXT NOT NULL,
            target_cents INTEGER NOT NULL CHECK (target_cents > 0),
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS watch_observations (
            watch_id TEXT NOT NULL REFERENCES flight_watches(id),
            day TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            cents INTEGER,
            offer TEXT,
            error TEXT,
            PRIMARY KEY (watch_id, day)
        );
        CREATE TABLE IF NOT EXISTS party_quotes (
            option_id TEXT NOT NULL,
            adults INTEGER NOT NULL CHECK (adults BETWEEN 1 AND 9),
            checked_at TEXT NOT NULL,
            cents INTEGER,
            offer TEXT,
            error TEXT,
            PRIMARY KEY (option_id, adults)
        );
    """)
    return db


def collect_trip(db, key, trip, day):
    with db:
        # Hold the writer lock through one search so manual and scheduled runs share a daily snapshot.
        db.execute("BEGIN IMMEDIATE")
        identity = (day.isoformat(), *trip.dates)
        existing = db.execute(
            "SELECT error FROM searches WHERE day = ? AND outbound = ? AND return_date = ?", identity,
        ).fetchone()
        if existing is not None and existing["error"] is None:
            return None, False
        checked_at = datetime.now(CHINA_TIME).isoformat(timespec="seconds")
        error = None
        try:
            fares = fetch_fares(key, trip)
        except ApiError as caught:
            error = caught
        db.execute(
            "INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?, ?)",
            (*identity, checked_at, str(error) if error else None),
        )
        if error is None:
            db.executemany(
                "INSERT INTO fares VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(*identity, *fare) for fare in fares],
            )
    return error, True


def collect(db, holidays, today):
    trips = planned_trips(holidays, today)
    if not trips:
        return "目前没有进入查询范围的行程。", 0
    key = api_key(DATA / "serpapi.dpapi")
    if not key:
        return "待配置 SerpApi 密钥。配置后即可开始记录票价。", 1
    checked = failures = skipped = 0
    last_error = None
    for trip in trips:
        last_error_for_trip, attempted = collect_trip(db, key, trip, today)
        if not attempted:
            skipped += 1
        elif last_error_for_trip:
            failures += 1
            last_error = last_error_for_trip
            if last_error.stop:
                break
        else:
            checked += 1
    if not last_error or not last_error.stop:
        # Check saved itineraries before spending the remaining calls on new candidates.
        used = refresh_watches(db, key, holidays, today, trips)
        prefill_options(db, key, holidays, today, trips, DAILY_SEARCH_BUDGET - len(trips) - used)
    if failures:
        return f"今日完成 {checked + skipped} 组日期查询。{last_error}", 1
    return f"今日已完成 {checked + skipped} 组日期查询。", 0


def trip_history(db, trip):
    searches = db.execute(
        "SELECT * FROM searches WHERE outbound = ? AND return_date = ? ORDER BY day DESC",
        trip.dates,
    ).fetchall()
    latest_success = next((record for record in searches if record["error"] is None), None)
    if latest_success is None:
        return searches, [], {}
    records = db.execute(
        "SELECT * FROM fares WHERE outbound = ? AND return_date = ? ORDER BY day DESC, cents",
        trip.dates,
    ).fetchall()
    history = {}
    for fare in records:
        history.setdefault(fare["city_id"], []).append(fare)
    return searches, [fare for fare in records if fare["day"] == latest_success["day"]], history


def money(cents):
    return f"¥{cents / 100:,.2f}".rstrip("0").rstrip(".")


def flight_link(fare, trip):
    outbound, returning = trip.dates
    airport = fare["airport"]
    city_code = {"PEK": "BJS", "PKX": "BJS", "TFU": "CTU"}.get(airport, airport)
    query = urlencode({
        "tripType": 1,
        "depCity": "SHA",
        "depCityName": "上海",
        "arrCity": city_code,
        "arrCityName": fare["city"].removesuffix("市"),
        "depDate": outbound,
        "arrDate": returning,
    })
    return f"https://sjipiao.fliggy.com/flight_search_result.htm?{query}"


def detail_link(airport, trip):
    outbound, returning = trip.dates
    query = urlencode({"airport": airport, "outbound": outbound, "returning": returning})
    return f"{DETAILS_ORIGIN}/flights?{query}"


def comparison_link(trip=None):
    if not trip:
        return f"{DETAILS_ORIGIN}/compare"
    outbound, returning = trip.dates
    return f"{DETAILS_ORIGIN}/compare?" + urlencode({"outbound": outbound, "returning": returning})


def itinerary_link(page, trip=None, airport=None, option=None):
    url = f"{DETAILS_ORIGIN}/{page}"
    if not trip:
        return url
    outbound, returning = trip.dates
    return url + "?" + urlencode({
        "outbound": outbound, "returning": returning, "airport": airport,
        "choice": details.watch_id(option),
    })


def options_for_trip(db, trip):
    cache = {}
    rows = db.execute("""
        SELECT airport, day, options FROM flight_options
        WHERE outbound = ? AND return_date = ?
        ORDER BY day DESC
    """, trip.dates)
    for row in rows:
        if row["airport"] not in cache:
            cache[row["airport"]] = row
    return cache


def flight_preview(snapshot, today, *, public=False):
    if not snapshot:
        label = "暂无已采集的航班组合" if public else "点击航班详情查询"
        return f"<small>{label}</small>"
    option = json.loads(snapshot["options"])[0]
    age = "" if snapshot["day"] == today.isoformat() else f"<small>{snapshot['day']} 采集</small>"
    return (
        f"<strong>{money(option['cents'])}</strong> <small class='quoted-label'>组合报价</small>"
        f"<small>去 {details.short_leg(option['out'])} · {escape(option['out']['number'])}</small>"
        f"<small>回 {details.short_leg(option['back'])} · {escape(option['back']['number'])}</small>{age}"
    )


def fare_rows(fares, history, trip, flight_cache, today, *, public=False):
    for fare in fares:
        samples = history[fare["city_id"]]
        price = fare["cents"]
        previous = [sample["cents"] for sample in samples[1:8]]
        baseline = median(previous) if len(previous) >= 3 else None
        discount = round(100 * (1 - price / baseline)) if baseline else 0
        detail = "".join(
            f"<li><time>{row['day']}</time><b>{money(row['cents'])}</b></li>"
            for row in samples[:30]
        )
        badge = f'<span class="drop">较近期中位价低 {discount}%</span>' if discount >= 15 else ""
        detail_action = ""
        if not public:
            detail_action = (f'<a href="{escape(detail_link(fare["airport"], trip))}"'
                             ' target="_blank" rel="noopener">航班详情</a>')
        preview = flight_preview(flight_cache.get(fare["airport"]), today, public=public)
        yield (
            f"<tr><td><strong>{escape(fare['city'])}</strong>"
            f"<small>{escape(fare['airport'])} {escape(fare['airline'])}</small></td>"
            f'<td class="price">{money(price)}{badge}</td>'
            f'<td class="flight-preview">{preview}</td>'
            f"<td><details><summary>{money(min(row['cents'] for row in samples))}"
            f"<small>{len(samples)} 天有价</small></summary><ol>{detail}</ol></details></td>"
            f'<td class="fare-actions">{detail_action}'
            f'<a href="{escape(flight_link(fare, trip))}" target="_blank" rel="noopener">飞猪查航班</a></td></tr>'
        )


def trip_section(trip, searches, fares, history, flight_cache, today, opened, *, public=False):
    if not searches:
        return f'<div class="empty"><strong>{trip.label}</strong><p>等待首次查询。</p></div>'
    latest = searches[0]
    status = ""
    if latest["error"]:
        status = f'<p class="warning">{latest["day"]} 查询失败。{escape(latest["error"])}</p>'
    success = next((row for row in searches if row["error"] is None), None)
    if success is None:
        content = "<p>暂无可展示的票价。</p>"
    else:
        checked_at = datetime.fromisoformat(success["checked_at"]).strftime("%m/%d %H:%M")
        stale = " · 旧报价" if success["day"] != today.isoformat() else ""
        status += f'<p class="timestamp">报价采集于 {checked_at}{stale}</p>'
        if not fares:
            content = "<p>来源当天没有返回符合条件的报价。</p>"
        else:
            rows = "".join(fare_rows(fares, history, trip, flight_cache, today, public=public))
            content = (
                '<div class="table"><table><thead><tr><th>目的地</th><th>往返参考价</th>'
                f"<th>去程与返程</th><th>历史最低</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div>"
            )
        quoted = {fare["city_id"] for fare in fares}
        other = sorted(
            (samples[0] for city_id, samples in history.items() if city_id not in quoted),
            key=lambda fare: fare["cents"],
        )
        if other:
            content += '<details class="old-fares"><summary>查看其他曾有报价的城市</summary><ol>'
            content += "".join(
                f"<li>{escape(fare['city'])} · {fare['day']}<b>{money(fare['cents'])}</b></li>"
                for fare in other
            )
            content += "</ol></details>"
    count = f" · {len(fares)} 个目的地" if fares else ""
    comparison = ""
    if not public and fares and trip.outbound >= today:
        comparison = f'<p><a href="{escape(comparison_link(trip))}">比较这组日期去哪儿</a></p>'
    return (
        f'<details class="trip"{" open" if opened else ""}><summary>'
        f"<strong>{trip.label}</strong>{count}</summary>{status}{comparison}{content}</details>"
    )


def holiday_sections(db, holidays, today, *, public=False):
    active = monitored_holidays(holidays, today)
    for holiday in holidays:
        sections = []
        fresh = []
        for trip in [] if holiday.paused else holiday.trips():
            searches, fares, history = trip_history(db, trip)
            if not searches and trip.outbound < today:
                continue
            if fares and trip.outbound >= today and fares[0]["day"] == today.isoformat():
                fresh.append((fares[0]["cents"], fares[0]["city"], trip.label, len(sections)))
            sections.append((trip, searches, fares, history, options_for_trip(db, trip)))
        date_label = f"{holiday.start:%m/%d}–{holiday.end:%m/%d}"
        if holiday.planned_trip:
            date_label = f"计划出行 {holiday.planned_trip.outbound:%m/%d}–{holiday.planned_trip.returning:%m/%d}"
        elif holiday.start == holiday.end:
            date_label = f"{holiday.start:%m/%d} · 1 天"
        bottom = ""
        opened = 0
        if fresh:
            price, city, label, opened = min(fresh)
            bottom = f'<p class="best">{escape(city)} <strong>{money(price)}</strong><small>{label}</small></p>'
        elif holiday.paused:
            bottom = '<p>已暂停监控。<a href="#history">查看已采集报价</a></p>'
        elif not any(trip.outbound >= today for trip in holiday.trips()):
            bottom = '<p>出发日期已过。<a href="#history">查看历史报价</a></p>'
        elif holiday not in active:
            bottom = "<p>等待进入每日查询范围。</p>"
        elif not any(section[1] for section in sections):
            bottom = "<p>等待首次数据。</p>"
        else:
            bottom = "<p>今天暂无可比较的报价。</p>"
        body = "".join(
            trip_section(*section, today, opened=index == opened, public=public)
            for index, section in enumerate(sections) if section[1] or holiday in active
        )
        yield (
            f'<section id="h-{holiday.start}"><div class="heading"><h2>{escape(holiday.name)}'
            f"</h2><span>{date_label}</span></div>{bottom}{body}</section>"
        )


def price_history(db, holidays, today, *, public=False):
    current = {trip.dates for holiday in monitored_holidays(holidays, today) for trip in holiday.trips()}
    itineraries = {}
    with_options = set()
    if not public:
        with_options = {tuple(row) for row in db.execute(
            "SELECT DISTINCT outbound, return_date, airport FROM flight_options"
        )}
    rows = db.execute("""
        SELECT outbound, return_date, city_id, city, airport, day, cents, airline
        FROM fares
        ORDER BY outbound DESC, return_date DESC, day DESC, cents, city_id
    """)
    for row in rows:
        dates = (row["outbound"], row["return_date"])
        cities = itineraries.setdefault(dates, {})
        city = cities.setdefault(row["city_id"], {"name": row["city"], "quotes": []})
        city["quotes"].append([row["day"], row["cents"], row["airport"], row["airline"]])
        if "flight_details" not in city and (*dates, row["airport"]) in with_options:
            trip = Trip(date.fromisoformat(dates[0]), date.fromisoformat(dates[1]))
            city["flight_details"] = detail_link(row["airport"], trip)

    history = []
    for (outbound, returning), cities in sorted(itineraries.items(), key=lambda item: item[0] not in current):
        departure = date.fromisoformat(outbound)
        name = next(
            (holiday.name for holiday in holidays if holiday.start <= departure <= holiday.end),
            "",
        )
        label = f"{outbound} → {returning}"
        if name:
            label = f"{name} · {label}"
        cheapest_first = sorted(cities.values(), key=lambda city: city["quotes"][0][1])
        history.append({"label": label, "cities": cheapest_first})
    return json.dumps(history, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def write_report(db, holidays, today, *, public_output=None):
    public = public_output is not None
    report_path = public_output if public else REPORT
    status = db.execute("SELECT * FROM run_status WHERE id = 1").fetchone()
    message = "等待首次采集。" if public else "尚未查询。运行「启用每日监控」配置密钥并采集首批报价。"
    if status:
        checked_at = datetime.fromisoformat(status["checked_at"]).strftime("%Y-%m-%d %H:%M")
        message = f"{checked_at} · {status['message']}"
    upcoming = [holiday for holiday in holidays if holiday.last_day >= today]
    navigation = "".join(
        f'<a href="#h-{holiday.start}">{escape(holiday.name)}</a>' for holiday in upcoming
    )
    sections = "".join(holiday_sections(db, upcoming, today, public=public))
    if not upcoming:
        sections = "<section><h2>暂无已配置的未来假期</h2><p>新的放假通知公布后，请更新 holidays.json。</p></section>"
    template = (ROOT / "report_template.html").read_text(encoding="utf-8")
    local_links = detail_note = ""
    if not public:
        reached = 0
        for watch in watch_trips(db, planned_trips(holidays, today)):
            price = db.execute("""
                SELECT cents FROM watch_observations WHERE watch_id = ? AND day = ?
            """, (watch["id"], today.isoformat())).fetchone()
            reached += bool(price and price["cents"] and price["cents"] <= watch["target_cents"])
        watch_status = f" · {reached} 个达标" if reached else ""
        local_links = (
            f'<a href="{escape(comparison_link())}">选去哪</a>'
            f'<a href="{escape(itinerary_link("watch"))}">关注航班{watch_status}</a>'
        )
        detail_note = "完整时刻和采集记录在「航班详情」。"
    report = template.format(
        status=escape(message), navigation=navigation, sections=sections, generated=today.isoformat(),
        history_data=price_history(db, holidays, today, public=public),
        local_links=local_links, detail_note=detail_note,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=report_path.parent, suffix=".tmp", delete=False,
    ) as output:
        temp_path = Path(output.name)
        output.write(report)
    try:
        temp_path.replace(report_path)
    finally:
        temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "report", "check-key"])
    parser.add_argument("--key-file", type=Path, default=DATA / "serpapi.dpapi")
    parser.add_argument("--public-output", type=Path, help="Write the read-only public dashboard here.")
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=DATA / "monitor.log", encoding="utf-8", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.command == "check-key":
            key = api_key(args.key_file)
            if not key:
                raise ApiError("请输入 SerpApi 密钥。")
            account = request_json("account.json", key)
            if type(account.get("total_searches_left")) is not int:
                raise ApiError("无法确认 SerpApi 账户额度。")
            print(f"密钥有效，本期剩余 {account['total_searches_left']} 次查询。")
            return 0

        today = datetime.now(CHINA_TIME).date()
        holidays = load_holidays()
        with closing(open_database()) as db:
            code = 0
            if args.command == "collect":
                try:
                    message, code = collect(db, holidays, today)
                except ApiError as error:
                    message, code = str(error), 1
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO run_status VALUES (1, ?, ?)",
                        (datetime.now(CHINA_TIME).isoformat(timespec="seconds"), message),
                    )
                logging.info(message)
                print(message)
            write_report(db, holidays, today, public_output=args.public_output)
        return code
    except ApiError as error:
        print(error)
        return 1
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        logging.error("本地执行失败（%s）。", type(error).__name__)
        print(f"本地执行失败（{type(error).__name__}），请检查配置和 data/monitor.log。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
