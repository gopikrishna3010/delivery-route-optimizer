"""Delivery Route Optimizer — coordinates or pasted-address edition.

Run with:  python -m streamlit run app.py

This prototype intentionally uses free, public OpenStreetMap services by default:
* Nominatim resolves pasted addresses (with a one-request-per-second throttle).
* OSRM supplies road-distance and driving-time estimates.

Both services can occasionally be unavailable, so road routing has a clearly marked
straight-line fallback.  Google Maps links work without an API key and are meant for
the driver to verify every resolved location before starting the route.
"""

from __future__ import annotations

import itertools
import hashlib
import math
import re
import time
import unicodedata
from typing import Any
from urllib.parse import quote_plus

import pydeck as pdk
import requests
import streamlit as st


st.set_page_config(
    page_title="Delivery Route Optimizer",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org"
MAX_TOTAL_PARCELS = 250
ROAD_MATRIX_GROUP_SIZE = 80
ROAD_LINE_GROUP_SIZE = 80
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
LAT_LABEL = re.compile(rf"\b(?:lat|latitude)\b\s*(?:[:=]|is)?\s*\(?\s*({NUMBER})", re.I)
LON_LABEL = re.compile(rf"\b(?:lng|lon|long|longitude)\b\s*(?:[:=]|is)?\s*\(?\s*({NUMBER})", re.I)
PAIR = re.compile(
    rf"(?<![\w.])({NUMBER})\s*(,|;|\||/|\s+)\s*({NUMBER})(?![\w.])"
)


def normalise_text(value: str) -> str:
    """Make copied mobile-app text predictable without changing its meaning."""
    return unicodedata.normalize("NFKC", value).replace("\u00a0", " ").strip()


def valid_coordinates(latitude: float, longitude: float) -> bool:
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def extract_coordinates(value: str) -> tuple[float, float] | None:
    """Find latitude/longitude in common raw or labelled pasted formats.

    Examples accepted include ``17.4069790, 78.5874740``,
    ``Lat: 17.4069790, Lng: 78.5874740``, and a Google Maps URL or message
    containing a coordinate pair.
    """
    text = normalise_text(value)
    latitude_match = LAT_LABEL.search(text)
    longitude_match = LON_LABEL.search(text)
    if latitude_match and longitude_match:
        latitude, longitude = float(latitude_match.group(1)), float(longitude_match.group(1))
        if valid_coordinates(latitude, longitude):
            return latitude, longitude

    for match in PAIR.finditer(text):
        first, separator, second = match.groups()
        # Unlabelled GPS coordinates virtually always include a decimal point.
        # Requiring one prevents ordinary address numbers (for example,
        # "12, 5th Street") from becoming a false map pin.  Labelled integer
        # coordinates are still accepted by the branch above.
        if "." not in first and "." not in second:
            continue
        latitude, longitude = float(first), float(second)
        if valid_coordinates(latitude, longitude):
            return latitude, longitude
    return None


def google_maps_link(latitude: float, longitude: float) -> str:
    # A Maps search URL opens the coordinate pin without a Google API key.
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
        f"{latitude:.7f},{longitude:.7f}"
    )


def google_search_link(query: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def coordinate_location(latitude: float, longitude: float) -> dict[str, Any]:
    return {
        "ok": True,
        "lat": latitude,
        "lon": longitude,
        "label": f"{latitude:.6f}, {longitude:.6f}",
        "source": "Coordinates detected in pasted text",
    }


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def geocode_with_nominatim(query: str, country_hint: str) -> dict[str, Any]:
    """Resolve one interactive lookup with free OpenStreetMap/Nominatim."""
    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
    }
    if country_hint == "India":
        params["countrycodes"] = "in"
    try:
        response = requests.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": "DeliveryRouteOptimizerPrototype/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException as error:
        return {"ok": False, "error": f"Free address lookup is unavailable right now: {error}"}
    if not results:
        return {"ok": False, "error": "No matching address was found. Add city, area, or PIN code."}

    result = results[0]
    latitude, longitude = float(result["lat"]), float(result["lon"])
    if not valid_coordinates(latitude, longitude):
        return {"ok": False, "error": "The address service returned invalid coordinates."}
    return {
        "ok": True,
        "lat": latitude,
        "lon": longitude,
        "label": result.get("display_name", query),
        "source": "Free OpenStreetMap address lookup",
    }


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def geocode_with_google(query: str, api_key: str) -> dict[str, Any]:
    """Optional paid/billed Google Geocoding API route, never the default."""
    try:
        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": api_key},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        return {"ok": False, "error": f"Google address lookup is unavailable right now: {error}"}

    if payload.get("status") != "OK" or not payload.get("results"):
        detail = payload.get("error_message") or payload.get("status", "unknown response")
        return {"ok": False, "error": f"Google could not find this address: {detail}"}
    result = payload["results"][0]
    location = result["geometry"]["location"]
    latitude, longitude = float(location["lat"]), float(location["lng"])
    if not valid_coordinates(latitude, longitude):
        return {"ok": False, "error": "Google returned invalid coordinates."}
    return {
        "ok": True,
        "lat": latitude,
        "lon": longitude,
        "label": result.get("formatted_address", query),
        "source": "Google Geocoding API",
    }


def resolve_location(
    pasted_value: str, provider: str, api_key: str, country_hint: str
) -> dict[str, Any]:
    value = normalise_text(pasted_value)
    if not value:
        return {"ok": False, "error": "Paste a coordinate or an address."}
    coordinates = extract_coordinates(value)
    if coordinates:
        return coordinate_location(*coordinates)

    if provider == "Google Maps Geocoding (API key + billing)":
        if not api_key.strip():
            return {
                "ok": False,
                "error": "Enter a Google Geocoding API key, or switch back to the free lookup.",
            }
        return geocode_with_google(value, api_key.strip())
    return geocode_with_nominatim(value, country_hint)


def haversine_metres(first: dict[str, Any], second: dict[str, Any]) -> float:
    radius = 6_371_000
    lat1, lon1 = math.radians(first["lat"]), math.radians(first["lon"])
    lat2, lon2 = math.radians(second["lat"]), math.radians(second["lon"])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


@st.cache_data(ttl=60 * 60, show_spinner=False)
def road_distance_matrix(coordinate_pairs: tuple[tuple[float, float], ...]) -> dict[str, Any]:
    """Ask OSRM for one road-distance table; gracefully fall back if it fails."""
    encoded_points = ";".join(f"{longitude:.7f},{latitude:.7f}" for latitude, longitude in coordinate_pairs)
    try:
        response = requests.get(
            f"{OSRM_URL}/table/v1/driving/{encoded_points}",
            params={"annotations": "distance,duration"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        distances, durations = payload.get("distances"), payload.get("durations")
        if payload.get("code") == "Ok" and distances and durations and all(
            value is not None for row in distances for value in row
        ):
            return {"distances": distances, "durations": durations, "mode": "road"}
    except requests.RequestException:
        pass

    locations = [coordinate_location(latitude, longitude) for latitude, longitude in coordinate_pairs]
    distances = [[haversine_metres(origin, destination) for destination in locations] for origin in locations]
    # A conservative city-driving estimate.  It is deliberately labelled approximate.
    durations = [[distance / (30_000 / 3600) for distance in row] for row in distances]
    return {"distances": distances, "durations": durations, "mode": "approximate"}


def optimise_stop_order(distances: list[list[float]]) -> tuple[list[int], str]:
    """Start at index 0. Use exact routing for up to eight deliveries, then greedy."""
    delivery_indexes = list(range(1, len(distances)))
    if len(delivery_indexes) <= 8:
        best_order = min(
            itertools.permutations(delivery_indexes),
            key=lambda order: sum(distances[a][b] for a, b in zip((0,) + order, order)),
        )
        return list(best_order), "Exact best order"

    remaining = set(delivery_indexes)
    current = 0
    order: list[int] = []
    while remaining:
        next_stop = min(remaining, key=lambda candidate: distances[current][candidate])
        order.append(next_stop)
        remaining.remove(next_stop)
        current = next_stop
    return order, "Fast nearest-stop order (more than 8 deliveries)"


@st.cache_data(ttl=60 * 60, show_spinner=False)
def road_line_segment(coordinate_pairs: tuple[tuple[float, float], ...]) -> tuple[list[list[float]], bool]:
    """Return one driveable OSRM line. A straight line is only a fallback."""
    encoded_points = ";".join(f"{longitude:.7f},{latitude:.7f}" for latitude, longitude in coordinate_pairs)
    try:
        response = requests.get(
            f"{OSRM_URL}/route/v1/driving/{encoded_points}",
            params={"overview": "full", "geometries": "geojson"},
            timeout=25,
        )
        response.raise_for_status()
        route = response.json().get("routes", [])[0]
        coordinates = route["geometry"]["coordinates"]
        return coordinates, True
    except (requests.RequestException, IndexError, KeyError, TypeError):
        return [[longitude, latitude] for latitude, longitude in coordinate_pairs], False


@st.cache_data(ttl=60 * 60, show_spinner=False)
def road_line(coordinate_pairs: tuple[tuple[float, float], ...]) -> tuple[list[list[float]], bool]:
    """Build a route line in small requests so 100+ stops remain usable."""
    if len(coordinate_pairs) <= ROAD_LINE_GROUP_SIZE:
        return road_line_segment(coordinate_pairs)

    full_line: list[list[float]] = []
    all_segments_are_road = True
    # Each part overlaps the prior part's final stop, keeping the visual line continuous.
    for start in range(0, len(coordinate_pairs) - 1, ROAD_LINE_GROUP_SIZE - 1):
        part = coordinate_pairs[start : start + ROAD_LINE_GROUP_SIZE]
        segment, uses_road = road_line_segment(part)
        if full_line and segment:
            segment = segment[1:]
        full_line.extend(segment)
        all_segments_are_road = all_segments_are_road and uses_road
    return full_line, all_segments_are_road


def geographic_seed_order(points: list[dict[str, Any]]) -> list[int]:
    """Make compact groups for large jobs without making hundreds of API calls."""
    remaining = set(range(1, len(points)))
    current = 0
    order: list[int] = []
    while remaining:
        next_stop = min(remaining, key=lambda candidate: haversine_metres(points[current], points[candidate]))
        order.append(next_stop)
        remaining.remove(next_stop)
        current = next_stop
    return order


def create_route_plan(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Plan a route with road data, grouping large delivery batches safely."""
    if len(points) <= ROAD_MATRIX_GROUP_SIZE:
        coordinate_pairs = tuple((point["lat"], point["lon"]) for point in points)
        matrix = road_distance_matrix(coordinate_pairs)
        order, method = optimise_stop_order(matrix["distances"])
        distance_total = sum(
            matrix["distances"][from_index][to_index]
            for from_index, to_index in zip((0,) + tuple(order), order)
        )
        duration_total = sum(
            matrix["durations"][from_index][to_index]
            for from_index, to_index in zip((0,) + tuple(order), order)
        )
        return {
            "order": order,
            "method": method,
            "distance_total": distance_total,
            "duration_total": duration_total,
            "mode": matrix["mode"],
        }

    # Public routing servers tend to cap very large table requests.  Grouping lets
    # a 100+ stop run continue to use road distances inside every delivery group.
    seed_order = geographic_seed_order(points)
    current = 0
    order: list[int] = []
    distance_total = 0.0
    duration_total = 0.0
    all_road = True
    for start in range(0, len(seed_order), ROAD_MATRIX_GROUP_SIZE - 1):
        group_indexes = seed_order[start : start + ROAD_MATRIX_GROUP_SIZE - 1]
        local_indexes = [current] + group_indexes
        local_points = [points[index] for index in local_indexes]
        matrix = road_distance_matrix(tuple((point["lat"], point["lon"]) for point in local_points))
        local_order, _ = optimise_stop_order(matrix["distances"])
        previous = 0
        for local_stop in local_order:
            distance_total += matrix["distances"][previous][local_stop]
            duration_total += matrix["durations"][previous][local_stop]
            order.append(local_indexes[local_stop])
            previous = local_stop
        current = local_indexes[local_order[-1]]
        all_road = all_road and matrix["mode"] == "road"
    return {
        "order": order,
        "method": f"Scalable road-based groups ({len(order)} deliveries)",
        "distance_total": distance_total,
        "duration_total": duration_total,
        "mode": "road" if all_road else "approximate",
    }


def make_map(locations: list[dict[str, Any]], path: list[list[float]] | None = None) -> pdk.Deck:
    map_rows = [
        {
            "name": location["name"],
            "lat": location["lat"],
            "lon": location["lon"],
            "colour": [30, 100, 220] if location.get("is_start") else [225, 70, 55],
        }
        for location in locations
    ]
    center_lat = sum(row["lat"] for row in map_rows) / len(map_rows)
    center_lon = sum(row["lon"] for row in map_rows) / len(map_rows)
    layers: list[Any] = []
    if path and len(path) > 1:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": path}],
                get_path="path",
                get_color=[20, 110, 190],
                width_scale=3,
                width_min_pixels=3,
                pickable=False,
            )
        )
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            data=map_rows,
            get_position="[lon, lat]",
            get_fill_color="colour",
            get_radius=70,
            radius_min_pixels=7,
            radius_max_pixels=16,
            pickable=True,
        )
    )
    # Hundreds of permanent text labels make a phone map unreadable. Pins stay
    # tappable, and labels remain visible for normal-size jobs.
    if len(map_rows) <= 30:
        layers.append(
            pdk.Layer(
                "TextLayer",
                data=map_rows,
                get_position="[lon, lat]",
                get_text="name",
                get_color=[30, 30, 30],
                get_size=14,
                get_alignment_baseline="bottom",
            )
        )
    return pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=12, pitch=0),
        tooltip={"text": "{name}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    )


def values_signature(
    start: str, parcels: list[dict[str, str]], provider: str, country: str, api_key: str
) -> tuple[Any, ...]:
    # Keep the actual optional API key out of session state while still refreshing
    # results if the user changes it after an unsuccessful Google lookup.
    key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else ""
    return (start, tuple((parcel["name"], parcel["text"]) for parcel in parcels), provider, country, key_fingerprint)


def bulk_parcels(value: str, first_stop_number: int) -> list[dict[str, str]]:
    """Read mobile-friendly bulk lines: ``Name | location`` or just ``location``."""
    parcels: list[dict[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, location = (part.strip() for part in line.split("|", 1))
        else:
            name, location = "", line
        stop_number = first_stop_number + len(parcels)
        parcels.append({"name": name or f"Stop {stop_number}", "text": location})
    return parcels


def location_card(location: dict[str, Any], title: str, key: str) -> None:
    st.markdown(f"#### {title}")
    st.success("Location found")
    st.caption(location["source"])
    st.write(location["label"])
    st.caption(f"{location['lat']:.6f}, {location['lon']:.6f}")
    st.link_button("Open in Google Maps", google_maps_link(location["lat"], location["lon"]), use_container_width=True, key=key)
    one_pin = [{**location, "name": title, "is_start": location.get("is_start", False)}]
    st.pydeck_chart(make_map(one_pin), use_container_width=True, height=260)


def resolve_all(
    start_text: str,
    parcels: list[dict[str, str]],
    provider: str,
    api_key: str,
    country_hint: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with st.spinner("Checking pasted locations…"):
        start = resolve_location(start_text, provider, api_key, country_hint)
        if start_text.strip() and not extract_coordinates(start_text) and provider.startswith("Free"):
            time.sleep(1.05)
        resolved_parcels: list[dict[str, Any]] = []
        # Public Nominatim asks clients to keep requests to one per second.  Coordinates
        # never need that pause, and cache hits return immediately.
        for parcel in parcels:
            resolved = resolve_location(parcel["text"], provider, api_key, country_hint)
            resolved["name"] = parcel["name"] or "Parcel"
            resolved["raw_text"] = parcel["text"]
            resolved_parcels.append(resolved)
            if not extract_coordinates(parcel["text"]) and provider.startswith("Free"):
                time.sleep(1.05)
    return start, resolved_parcels


st.title("🚚 Delivery Route Optimizer")
st.caption("Paste Flipkart coordinates, a Google Maps link, or a full address — then verify every stop before you ride.")

with st.sidebar:
    st.header("Location lookup")
    provider = st.selectbox(
        "Address lookup service",
        ["Free prototype: OpenStreetMap", "Google Maps Geocoding (API key + billing)"],
        help="Coordinates always work without a key. Google Maps links also work without a key.",
    )
    if provider == "Google Maps Geocoding (API key + billing)":
        api_key = st.text_input("Google Geocoding API key", type="password")
        st.warning("Google address lookup requires a billing-enabled Geocoding API. The free option is selected by default.")
    else:
        api_key = ""
        st.info("Free address lookup uses OpenStreetMap. For best matches, paste the area, city, and PIN code.")
    country_hint = st.selectbox("Address country hint", ["India", "Worldwide"], help="Use India for quicker, more relevant Flipkart address results.")

if "parcel_count" not in st.session_state:
    st.session_state.parcel_count = 3

parcel_count = st.slider(
    "Individual parcel boxes",
    min_value=0,
    max_value=10,
    key="parcel_count",
    help="For more than 10 parcels, use the bulk-paste box below.",
)
input_problem = ""

with st.form("delivery_inputs", border=False):
    st.subheader("1. Paste your start location")
    start_text = st.text_area(
        "Start location",
        key="start_text",
        placeholder="Example: Lat: 17.4069790, Lng: 78.5874740 — or paste your address",
        height=80,
    )
    st.caption("Accepted coordinate examples: `17.4069790, 78.5874740` • `Lat: 17.4069790, Lng: 78.5874740` • any pasted text that contains a coordinate pair.")

    st.subheader("2. Paste parcel locations")
    parcels: list[dict[str, str]] = []
    for index in range(parcel_count):
        with st.container(border=True):
            default_name = chr(65 + index) if index < 26 else f"Stop {index + 1}"
            name, text_value = st.columns([1, 3])
            with name:
                parcel_name = st.text_input("Parcel name", value=default_name, key=f"parcel_name_{index}")
            with text_value:
                parcel_text = st.text_area(
                    "Coordinates or address",
                    key=f"parcel_text_{index}",
                    placeholder="Paste a coordinate, Google Maps link, or full customer address",
                    height=80,
                    label_visibility="visible" if index == 0 else "collapsed",
                )
            if parcel_text.strip():
                parcels.append({"name": parcel_name.strip() or default_name, "text": parcel_text})

    st.markdown("##### Bulk paste 100+ parcel locations")
    bulk_text = st.text_area(
        "Bulk locations",
        placeholder="Customer A | 17.4069790, 78.5874740\nCustomer B | Lat: 17.3850440, Lng: 78.4866710\nCustomer C | Full address, area, city, PIN code",
        height=220,
        help="Use one parcel per line. Add a name before a | character, or paste only the location.",
    )
    bulk_entries = bulk_parcels(bulk_text, len(parcels) + 1)
    if len(parcels) + len(bulk_entries) > MAX_TOTAL_PARCELS:
        input_problem = f"This prototype supports up to {MAX_TOTAL_PARCELS} parcels at once."
    else:
        parcels.extend(bulk_entries)
    st.caption(
        "You can add up to 250 parcels. Coordinates are fastest. Free address lookups are checked one at a time and may take about one second per address."
    )

    check_locations, optimise_route = st.columns(2)
    with check_locations:
        check_pressed = st.form_submit_button("📍 Check locations", use_container_width=True)
    with optimise_route:
        optimise_pressed = st.form_submit_button("🚚 Find best road route", type="primary", use_container_width=True)

signature = values_signature(start_text, parcels, provider, country_hint, api_key)
if check_pressed or optimise_pressed:
    if input_problem:
        st.error(input_problem)
    elif not parcels:
        st.error("Add at least one parcel location before checking or optimizing.")
    else:
        start_result, parcel_results = resolve_all(start_text, parcels, provider, api_key, country_hint)
        st.session_state.location_results = {"signature": signature, "start": start_result, "parcels": parcel_results}
        st.session_state.pop("route_result", None)

        if optimise_pressed and start_result.get("ok") and all(result.get("ok") for result in parcel_results):
            points = [start_result] + parcel_results
            with st.spinner("Finding the best road order…"):
                plan = create_route_plan(points)
                ordered_points = [points[0]] + [points[index] for index in plan["order"]]
                route_coordinates = tuple((point["lat"], point["lon"]) for point in ordered_points)
                path, road_line_available = road_line(route_coordinates)
            st.session_state.route_result = {
                "signature": signature,
                **plan,
                "path": path,
                "road_line_available": road_line_available,
            }

results = st.session_state.get("location_results")
if results and results.get("signature") == signature:
    start_result = results["start"]
    parcel_results = results["parcels"]
    st.divider()
    st.subheader("3. Verify locations")

    if not start_result.get("ok"):
        st.error(f"Start location: {start_result['error']}")
        if start_text.strip():
            st.link_button("Search this start text in Google Maps", google_search_link(start_text), use_container_width=True)

    failed = [result for result in parcel_results if not result.get("ok")]
    for result in failed:
        st.error(f"{result['name']}: {result['error']}")
        if result.get("raw_text", "").strip():
            st.link_button(
                f"Search {result['name']} text in Google Maps",
                google_search_link(result["raw_text"]),
                use_container_width=True,
                key=f"search_failed_{result['name']}",
            )

    valid_locations: list[dict[str, Any]] = []
    if start_result.get("ok"):
        valid_locations.append({**start_result, "name": "Start", "is_start": True})
    valid_locations.extend({**result, "is_start": False} for result in parcel_results if result.get("ok"))
    if valid_locations:
        st.markdown("##### All resolved locations")
        st.pydeck_chart(make_map(valid_locations), use_container_width=True, height=420)

    if len(parcel_results) <= 20:
        with st.expander("Show each location and its Google Maps verification link", expanded=False):
            if start_result.get("ok"):
                location_card({**start_result, "is_start": True}, "Start", "maps_start")
            for index, result in enumerate(parcel_results):
                if result.get("ok"):
                    location_card(result, result["name"], f"maps_parcel_{index}")
    else:
        st.info("For a large batch, use the overview map now and the Navigate buttons in the route list after optimization. Individual preview cards are hidden to keep the phone view fast.")

    route = st.session_state.get("route_result")
    if route and route.get("signature") == signature:
        st.divider()
        st.subheader("4. Recommended delivery order")
        points = [start_result] + parcel_results
        ordered_points = [points[0]] + [points[index] for index in route["order"]]
        ordered_map_points = [
            {**point, "name": "Start" if index == 0 else f"{index}. {point['name']}", "is_start": index == 0}
            for index, point in enumerate(ordered_points)
        ]
        distance_box, time_box, method_box = st.columns(3)
        distance_box.metric("Estimated distance", f"{route['distance_total'] / 1000:.1f} km")
        time_box.metric("Estimated drive time", f"{route['duration_total'] / 60:.0f} min")
        method_box.metric("Route method", route["method"])

        if route["mode"] == "approximate":
            st.warning("The free road-routing service could not be reached, so this order and estimate use straight-line distances. Try again when online before starting.")
        elif not route["road_line_available"]:
            st.info("The order and estimates use roads, but the map line is straight because the road-line preview was unavailable.")
        else:
            st.success("Road distance and driving time were used for this route.")

        st.pydeck_chart(make_map(ordered_map_points, route["path"]), use_container_width=True, height=500)
        st.markdown("##### Ride in this order")
        for stop_number, point in enumerate(ordered_points[1:], start=1):
            left, right = st.columns([3, 2])
            with left:
                st.write(f"**{stop_number}. {point['name']}** — {point['label']}")
            with right:
                st.link_button(
                    f"Navigate to {point['name']}",
                    google_maps_link(point["lat"], point["lon"]),
                    use_container_width=True,
                    key=f"navigate_{stop_number}",
                )
elif results:
    st.info("Your inputs have changed. Tap **Check locations** again to refresh the map and route.")

st.divider()
st.caption("Prototype note: free address lookup and road routing use public OpenStreetMap services. Always open the Google Maps link to verify a customer location before delivery.")
