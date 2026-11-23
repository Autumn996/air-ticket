"""Quote an exact round trip for a selected number of adults."""

import json
import re
from html import escape
from pathlib import Path
from urllib.parse import urlencode

import flight_details as details
import flight_watch
import monitor

TEMPLATE = Path(__file__).with_name("party_quote.html")


def adult_count(params, *, strict=False):
    text = flight_watch.input_value(params, "adults")
    if re.fullmatch(r"[1-9]", text):
        return int(text)
    if strict:
        raise monitor.ApiError("请填写 1 至 9 位成人。")
    return 2


def quote(db, holidays, option, adults):
    key = monitor.api_key(monitor.DATA / "serpapi.dpapi")
    if not key:
        raise monitor.ApiError("请先配置票价接口密钥。")
    row, attempted = monitor.check_party_quote(db, key, holidays, option, adults)
    if row["error"]:
        return f"{adults} 位成人 · {row['error']}"
    return "已按所选人数核价。" if attempted else "过去一小时已核价，显示这次采集的报价。"


def page(db, selected, adults, token, today, message=""):
    trip, airport, city, option = selected
    row = monitor.saved_party_quote(db, option, adults)
    displayed = option
    summary = ""
    if row:
        stamp = row["checked_at"][:16].replace("T", " ")
        if row["cents"] is not None:
            shown = "全员往返报价" if row["checked_at"][:10] == today.isoformat() else "上次全员往返报价"
            displayed = json.loads(row["offer"])
            seller = escape(displayed["seller"]) or "卖家未标明"
            baggage = flight_watch.offer_baggage(displayed) or "行李额度待复核"
            summary = (
                f'<p class="quote">{adults} 位成人 · {shown} <strong>{monitor.money(row["cents"])}</strong></p>'
                f'<p class="muted">{stamp} · {seller} · {baggage}</p>'
            )
        else:
            summary = f'<p class="muted">{stamp} · {adults} 位成人 · {escape(row["error"])}</p>'
    else:
        summary = f'<p class="muted">等待查询这套往返的 {adults} 人报价。</p>'

    values = {
        "token": token, "outbound": trip.dates[0], "returning": trip.dates[1],
        "airport": airport, "choice": details.watch_id(option),
    }
    fields = "".join(flight_watch.field(name, value) for name, value in values.items())
    comparison = monitor.comparison_link(trip) + "&" + urlencode({"adults": adults})
    return TEMPLATE.read_text(encoding="utf-8").format(
        city=escape(city), dates=f"{trip.dates[0]} → {trip.dates[1]}",
        trip=flight_watch.pair_markup(displayed), adults=adults, fields=fields,
        summary=summary, message=escape(message), estimate=monitor.money(option["cents"] * adults),
        comparison=escape(comparison), details_url=escape(monitor.detail_link(airport, trip)),
        fliggy=escape(monitor.flight_link({"airport": airport, "city": city}, trip)),
    )
