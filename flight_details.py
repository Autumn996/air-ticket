"""Match outbound and return flights and render the resulting itineraries."""

from datetime import datetime
from hashlib import sha256
from html import escape
import json

SHANGHAI = {"PVG", "SHA"}
CO_TERMINALS = {
    "PEK": ("PEK", "PKX"),
    "PKX": ("PEK", "PKX"),
    "CTU": ("CTU", "TFU"),
    "TFU": ("CTU", "TFU"),
}
AIRPORT_LABELS = {"SHA": "上海虹桥", "PVG": "上海浦东"}
RED_EYE_RULE = "排除任一程跨日抵达，或在当地时间 23:00 至次日 06:00 前起飞或降落的组合。"


def flight_groups(response):
    for field in ("best_flights", "other_flights"):
        groups = response.get(field)
        if isinstance(groups, list):
            yield from (group for group in groups if isinstance(group, dict))


def direct_leg(group, day, origins, destinations):
    flights = group.get("flights")
    if not isinstance(flights, list) or len(flights) != 1 or not isinstance(flights[0], dict):
        return None
    flight = flights[0]
    departure = flight.get("departure_airport")
    arrival = flight.get("arrival_airport")
    if not isinstance(departure, dict) or not isinstance(arrival, dict):
        return None
    if departure.get("id") not in origins or arrival.get("id") not in destinations:
        return None
    try:
        leaves = datetime.fromisoformat(departure["time"])
        lands = datetime.fromisoformat(arrival["time"])
        if leaves.date().isoformat() != day or lands <= leaves:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    number = flight.get("flight_number")
    if not isinstance(number, str) or not number.strip():
        return None

    def airport_info(info, time):
        code = info["id"]
        name = info.get("name")
        return {"code": code, "name": name if isinstance(name, str) else code,
                "time": time.isoformat(sep=" ", timespec="minutes")}

    return {
        "departure": airport_info(departure, leaves),
        "arrival": airport_info(arrival, lands),
        "number": number,
        "airline": flight.get("airline") if isinstance(flight.get("airline"), str) else "",
        "minutes": round((lands - leaves).total_seconds() / 60),
    }


def quote_price(group):
    amount = group.get("price")
    if type(amount) in (int, float) and 0 < amount < 1_000_000:
        return round(amount * 100)
    return None


def select_departures(groups, day, destinations, count):
    candidates = {}
    for group in groups:
        leg = direct_leg(group, day, SHANGHAI, destinations)
        token, price = group.get("departure_token"), quote_price(group)
        if leg is None or price is None or not isinstance(token, str) or not token:
            continue
        identity = leg["number"], leg["departure"]["time"], leg["departure"]["code"]
        if identity not in candidates or price < candidates[identity][0]:
            candidates[identity] = (price, leg, token)

    ranked = sorted(candidates.values(), key=lambda item: (item[0], item[1]["arrival"]["time"]))
    if not ranked:
        return []
    selected = ranked[:1]
    if count > 1 and len(ranked) > 1:
        original = datetime.fromisoformat(ranked[0][1]["departure"]["time"])
        for alternate in ranked[1:]:
            leg = alternate[1]
            leaves = datetime.fromisoformat(leg["departure"]["time"])
            arrival = datetime.fromisoformat(leg["arrival"]["time"])
            if (leaves.date() == arrival.date() and 7 <= leaves.hour < 20
                    and abs((leaves - original).total_seconds()) >= 3 * 3600):
                return [*selected, alternate]
        selected.append(ranked[1])
    return selected


def return_options(groups, day, destinations, outbound):
    choices = {}
    for group in groups:
        leg = direct_leg(group, day, destinations, SHANGHAI)
        price = quote_price(group)
        if leg is None or price is None or leg["departure"]["time"] <= outbound["arrival"]["time"]:
            continue
        notes = group.get("extensions")
        baggage = []
        if isinstance(notes, list):
            baggage = [
                note for note in notes
                if isinstance(note, str)
                and any(term in note.lower() for term in ("行李", "baggage", "carry-on"))
            ]
        item = {"out": outbound, "back": leg, "cents": price, "baggage": baggage}
        identity = signature(item)
        if identity not in choices or price < choices[identity]["cents"]:
            choices[identity] = item
    return sorted(choices.values(), key=lambda item: (item["cents"], item["back"]["arrival"]["time"]))


def signature(option):
    return tuple(
        (leg["number"], leg["departure"]["code"], leg["departure"]["time"],
         leg["arrival"]["code"], leg["arrival"]["time"])
        for leg in (option["out"], option["back"])
    )


def is_red_eye(leg):
    departure = leg["departure"]["time"]
    arrival = leg["arrival"]["time"]
    return departure[:10] != arrival[:10] or any(
        time[11:16] >= "23:00" or time[11:16] < "06:00" for time in (departure, arrival)
    )


def booking_selection(option):
    selected = {}
    for field, leg in (("outbound", option["out"]), ("return", option["back"])):
        selected[field] = [{
            "flight_number": "".join(leg["number"].split()).upper(),
            "departure_id": leg["departure"]["code"],
            "arrival_id": leg["arrival"]["code"],
            "date": leg["departure"]["time"][:10],
        }]
    return selected


def watch_id(option):
    payload = json.dumps(booking_selection(option), sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("ascii")).hexdigest()[:24]


def booking_quote(result, original):
    groups = result.get("selected_flights")
    if not isinstance(groups, list) or len(groups) != 2:
        return None
    legs = {}
    for tag, group in zip(("out", "back"), groups):
        if not isinstance(group, dict):
            return None
        leg = original[tag]
        matched = direct_leg(group, leg["departure"]["time"][:10],
                             {leg["departure"]["code"]}, {leg["arrival"]["code"]})
        if not matched:
            return None
        legs[tag] = matched
    if booking_selection(legs) != booking_selection(original):
        return None
    if legs["back"]["departure"]["time"] <= legs["out"]["arrival"]["time"]:
        return None

    offers = []
    results = result.get("booking_options")
    if not isinstance(results, list):
        return None
    for offer in results:
        if not isinstance(offer, dict) or offer.get("separate_tickets"):
            continue
        together = offer.get("together")
        if not isinstance(together, dict) or not (cents := quote_price(together)):
            continue
        seller = together.get("book_with")
        baggage = together.get("baggage_prices")
        offers.append({
            "cents": cents, "seller": seller if isinstance(seller, str) else "",
            "baggage": [note for note in baggage if isinstance(note, str)] if isinstance(baggage, list) else [],
            **legs,
        })
    return min(offers, key=lambda offer: offer["cents"], default=None)


def duration(minutes):
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分" if hours else f"{minutes} 分钟"


def short_leg(leg):
    dep, arr = leg["departure"], leg["arrival"]
    next_day = "次日 " if arr["time"][:10] > dep["time"][:10] else ""
    dep_name = AIRPORT_LABELS.get(dep["code"]) or dep["name"].removesuffix("国际机场").removesuffix("机场")
    arr_name = AIRPORT_LABELS.get(arr["code"]) or arr["name"].removesuffix("国际机场").removesuffix("机场")
    return (
        f"{escape(dep['time'][11:])} {escape(dep_name)} → "
        f"{next_day}{escape(arr['time'][11:])} {escape(arr_name)}"
    )
