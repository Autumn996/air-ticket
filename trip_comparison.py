"""Compare the cost and usable time of paired flights across destinations."""

import json
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median

import flight_details as details
import monitor

ROOT = Path(__file__).resolve().parent
BATCH_SIZE = 2


def select_trip(params, holidays, today):
    trips = [(holiday, trip) for holiday in monitor.monitored_holidays(holidays, today)
             for trip in holiday.trips() if today <= trip.outbound <= today + monitor.LOOKAHEAD]
    dates = params.get("outbound", [""])[0], params.get("returning", [""])[0]
    if any(dates):
        selected = next((trip for _, trip in trips if trip.dates == dates), None)
    else:
        selected = trips[0][1] if trips else None
    return selected, trips


def snapshot_rows(db, trip):
    cities = {}
    for row in db.execute("""
        SELECT airport, day, checked_at, options FROM flight_options
        WHERE outbound = ? AND return_date = ?
        ORDER BY day DESC
    """, trip.dates):
        cities.setdefault(row["airport"], []).append(row)
    return cities


def batch_candidates(db, trip, fares, snapshots, today):
    day = today.isoformat()
    attempted = monitor.detail_attempts(db, trip, today)
    return [
        fare for fare in fares
        if fare["airport"] not in attempted
        and (fare["airport"] not in snapshots or snapshots[fare["airport"]][0]["day"] != day)
    ]


def extend_shortlist(db, trip, holidays, today):
    _, fares, _ = monitor.trip_history(db, trip)
    candidates = batch_candidates(db, trip, fares, snapshot_rows(db, trip), today)
    if not candidates:
        return "可补查的城市已经查完。"
    key = monitor.api_key(monitor.DATA / "serpapi.dpapi")
    if not key:
        return "请先配置 SerpApi 密钥。"
    checked = []
    unavailable = []
    message = ""
    for fare in candidates[:BATCH_SIZE]:
        airport = fare["airport"]
        try:
            monitor.refresh_options(db, key, holidays, trip, airport)
            checked.append(fare["city"])
        except monitor.ApiError as error:
            if error.stop:
                message = str(error)
                break
            monitor.mark_detail_attempt(db, trip, airport, today)
            unavailable.append(fare["city"])
    if checked:
        monitor.write_report(db, holidays, today)
        message = f"已补查{'、'.join(checked)}。{message}"
    if unavailable:
        message += f"{'、'.join(unavailable)}本次未取得可核对的组合。"
    return message


def comparison_data(db, fares, snapshots, trip, today):
    party_quotes = {}
    for row in db.execute("""
        SELECT option_id, adults, cents, checked_at FROM party_quotes
        WHERE checked_at >= ? AND checked_at < ? AND cents IS NOT NULL
    """, (today.isoformat(), (today + monitor.DAY).isoformat())):
        party_quotes.setdefault(row["option_id"], {})[row["adults"]] = {
            "cents": row["cents"], "checked": row["checked_at"][11:16],
        }
    inventory = []
    attempted = monitor.detail_attempts(db, trip, today)
    for fare in fares:
        rows = snapshots.get(fare["airport"], [])
        city = {
            "id": fare["city_id"], "name": fare["city"], "ref": fare["cents"], "refDay": fare["day"],
            "detail": monitor.detail_link(fare["airport"], trip), "attempted": fare["airport"] in attempted,
            "booking": monitor.flight_link(fare, trip), "options": [],
        }
        if rows:
            latest, *older = rows
            city["updated"] = latest["checked_at"][:16].replace("T", " ")
            city["stale"] = latest["day"] != today.isoformat()
            historical = {}
            for previous in older[:7]:
                for option in json.loads(previous["options"]):
                    historical.setdefault(details.signature(option), []).append(option["cents"])
            for option in json.loads(latest["options"]):
                outbound, returning = option["out"], option["back"]
                stay = datetime.fromisoformat(returning["departure"]["time"]) - datetime.fromisoformat(outbound["arrival"]["time"])
                previous = historical.get(details.signature(option), [])
                city["options"].append({
                    "out": outbound, "back": returning, "cents": option["cents"],
                    "stay": int(stay.total_seconds() // 60),
                    "redEye": details.is_red_eye(outbound) or details.is_red_eye(returning),
                    "priorDays": len(previous), "median": round(median(previous)) if len(previous) >= 3 else None,
                    "watch": monitor.itinerary_link("watch", trip, fare["airport"], option),
                    "party": monitor.itinerary_link("group-quote", trip, fare["airport"], option),
                    "partyQuotes": party_quotes.get(details.watch_id(option), {}),
                })
        inventory.append(city)
    return inventory


def comparison_page(db, params, holidays, today, token, message=""):
    trip, trips = select_trip(params, holidays, today)
    navigation = " ".join(
        f'<a href="{escape(monitor.comparison_link(option))}"'
        f'{" aria-current=true" if trip == option else ""}>{escape(holiday.name)} · {option.label}</a>'
        for holiday, option in trips
    ) if len(trips) > 1 else ""
    if trip is None:
        return '<meta charset="utf-8"><p>这组日期目前未监控。</p><a href="/">返回机票看板</a>'

    _, fares, _ = monitor.trip_history(db, trip)
    snapshots = snapshot_rows(db, trip)
    payload = {
        "outbound": trip.dates[0], "returning": trip.dates[1],
        "cities": comparison_data(db, fares, snapshots, trip, today),
    }
    candidates = batch_candidates(db, trip, fares, snapshots, today)
    next_cities = "、".join(fare["city"] for fare in candidates[:BATCH_SIZE])
    button = f"补查 {escape(next_cities)}" if candidates else "今日暂无可补查的城市"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return (ROOT / "comparison.html").read_text(encoding="utf-8").format(
        dates=f"{trip.dates[0]} → {trip.dates[1]}", outbound=trip.dates[0], returning=trip.dates[1],
        holiday_name=escape(next(holiday.name for holiday, option in trips if option == trip)),
        token=escape(token), navigation=navigation, message=escape(message),
        watch_url=escape(monitor.itinerary_link("watch")),
        batch_button=button, disabled="" if candidates else " disabled", payload=data,
        red_eye_rule=escape(details.RED_EYE_RULE),
        script=(ROOT / "comparison.js").read_text(encoding="utf-8"),
    )
