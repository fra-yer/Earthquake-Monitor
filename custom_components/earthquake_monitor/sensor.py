# Earthquake Monitor
# Inspired by original work of febalci in EMSC Earthquake https://github.com/febalci/ha_emsc_earthquake
# Extended with improved event-selection and location-description logic
# See accompanying README.md for details
# Version 1.6.1 by FOF, May 2026
# change-log:
#   initialize new entities with status = "cleared"
#   change offshore fallback value from "international waters" to "offshore"

import asyncio
import io
import json
import logging
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from math import radians, degrees, cos, sin, sqrt, atan2
from pathlib import Path
from typing import Any

import reverse_geocoder as rg
import websockets
from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from shapely.geometry import Point, shape

from .const import DOMAIN, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

WEBSOCKET_URL = "wss://www.seismicportal.eu/standing_order/websocket"
PING_INTERVAL = 15  # seconds

# Thread pool executor for SSL context creation
ssl_executor = ThreadPoolExecutor(max_workers=1)

# Paths relative to this file (sensor.py)
INTEGRATION_DIR = Path(__file__).resolve().parent
CITIES_CSV = INTEGRATION_DIR / "geodata" / "cities25000.csv"
COUNTRIES_GEOJSON = INTEGRATION_DIR / "geodata" / "ne_10m_admin_0_countries.geojson"


@lru_cache(maxsize=1)
def get_city_geocoder():
    """Load city geocoder once."""
    return rg.RGeocoder(
        mode=2,
        stream=io.StringIO(CITIES_CSV.read_text(encoding="utf-8-sig")),
    )


@lru_cache(maxsize=1)
def get_countries():
    """Load country polygons once."""
    with COUNTRIES_GEOJSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    countries = []
    for feature in data["features"]:
        name = feature["properties"].get("NAME", "Unknown")
        geom = shape(feature["geometry"])
        countries.append((name, geom))

    return countries


def preload_geodata() -> None:
    """Warm up cached geodata resources."""
    get_countries()
    get_city_geocoder()


def distance_km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between any two lat/lon points."""
    r_earth_km = 6371.0

    lat1 = radians(lat1)
    lon1 = radians(lon1)
    lat2 = radians(lat2)
    lon2 = radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return r_earth_km * c


def nearest_city(lat: float, lon: float) -> str:
    """Nearest city name, or 'none' if farther than 500 km."""
    result = get_city_geocoder().query([(lat, lon)])[0]
    city_lat = float(result["lat"])
    city_lon = float(result["lon"])

    distance = distance_km_between(lat, lon, city_lat, city_lon)
    if distance > 500.0:
        return "none"

    return result.get("name", "Unknown")


def country_of_epicenter(lat: float, lon: float) -> str:
    """Country containing epicenter, or 'offshore' if offshore."""
    point = Point(lon, lat)  # lon, lat order

    for name, poly in get_countries():
        if poly.covers(point):
            return name

    return "offshore"


def lookup_geodata(lat: float, lon: float) -> tuple[str, str]:
    """Return country and nearest city for an epicenter."""
    country = country_of_epicenter(lat, lon)
    city = nearest_city(lat, lon)
    return country, city


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the Earthquake Monitor sensor from a config entry."""
    config = hass.data[DOMAIN][config_entry.entry_id]["config"]

    await hass.async_add_executor_job(preload_geodata)

    name = config.get("name") or config_entry.title or DEFAULT_NAME
    center_latitude = config.get("center_latitude")
    center_longitude = config.get("center_longitude")
    radius_km = config.get("radius_km")
    total_max_mag = config.get("total_max_mag")
    min_mag = config.get("min_mag")
    reset_after_hours = config.get("reset_after_hours", 0)
    timestamp_format = config.get("timestamp_format", "dmy_dot")

    sensor = EarthquakeMonitorSensor(
        entry_id=config_entry.entry_id,
        name=name,
        center_latitude=center_latitude,
        center_longitude=center_longitude,
        radius_km=radius_km,
        min_mag=min_mag,
        total_max_mag=total_max_mag,
        reset_after_hours=reset_after_hours,
        timestamp_format=timestamp_format,
    )

    async_add_entities([sensor], True)


class EarthquakeMonitorSensor(RestoreSensor):
    """Representation of an Earthquake Monitor sensor."""

    def __init__(
        self,
        entry_id: str,
        name: str,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
        min_mag: float,
        total_max_mag: float,
        reset_after_hours: float,
        timestamp_format: str,
    ) -> None:
        """Initialize the sensor."""
        self._entry_id = entry_id
        self._name = name
        self._state = None
        self._attributes: dict[str, Any] = {"status": "cleared"}
        self._ssl_context = None
        self._ws_task = None
        self.reset_after_hours = float(reset_after_hours)
        self.timestamp_format = timestamp_format
        self._clear_task = None

        self.center_latitude = float(center_latitude)
        self.center_longitude = float(center_longitude)
        self.radius_km = float(radius_km)
        self.total_max_mag = float(total_max_mag)
        self.min_mag = float(min_mag)

        # Track the currently stored "latest earthquake"
        self._current_unid: str | None = None
        self._current_event_time: datetime | None = None
        self._current_lastupdate: datetime | None = None

    @property
    def name(self):
        return self._name

    @property
    def native_value(self):
        """Return the main sensor value."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return additional attributes of the sensor."""
        return self._attributes

    @property
    def unique_id(self):
        """Return a unique ID for this sensor."""
        return f"{DOMAIN}_{self._entry_id}"

    @property
    def icon(self):
        """Return the icon for the sensor."""
        return "mdi:waveform"

    @property
    def should_poll(self) -> bool:
        """This entity is updated via websocket."""
        return False

    def get_reference_clear_time(self) -> datetime | None:
        """Return the timestamp used for auto-clear timing, preferring last update over origin time."""
        if self._current_lastupdate is not None:
            return self._current_lastupdate
        return self._current_event_time

    def clear_earthquake_state(self) -> None:
        """Clear the current earthquake data."""
        self._state = None
        self._attributes = {"status": "cleared"}
        self._current_unid = None
        self._current_event_time = None
        self._current_lastupdate = None
        self.async_write_ha_state()

    async def auto_clear_after_delay(self, delay_seconds: float) -> None:
        """Clear the entity after a delay."""
        try:
            await asyncio.sleep(delay_seconds)
            self.clear_earthquake_state()
            _LOGGER.info(
                "[%s | %s] Cleared earthquake entity after configured timeout",
                self._name,
                self._entry_id,
            )
        except asyncio.CancelledError:
            raise

    def schedule_auto_clear(self) -> None:
        """Schedule auto-clear based on the configured timeout."""
        if self._clear_task is not None:
            self._clear_task.cancel()
            self._clear_task = None

        if self.reset_after_hours <= 0:
            return

        reference_time = self.get_reference_clear_time()
        if reference_time is None:
            return

        clear_at = reference_time.timestamp() + (self.reset_after_hours * 3600)
        delay_seconds = clear_at - datetime.now(timezone.utc).timestamp()

        if delay_seconds <= 0:
            self.clear_earthquake_state()
            _LOGGER.info(
                "[%s | %s] Cleared earthquake entity immediately because timeout had already elapsed",
                self._name,
                self._entry_id,
            )
            return

        self._clear_task = asyncio.create_task(
            self.auto_clear_after_delay(delay_seconds)
        )

    def format_friendly_datetime(
        self,
        dt: datetime | None,
        use_utc: bool = False,
    ) -> str | None:
        """Format a datetime according to the configured timestamp style."""
        if dt is None:
            return None

        work_dt = dt.astimezone(timezone.utc) if use_utc else dt_util.as_local(dt)

        if self.timestamp_format == "dmy_dot":
            return work_dt.strftime("%d.%m.%Y %H:%M:%S")
        if self.timestamp_format == "dmy_slash":
            return work_dt.strftime("%d/%m/%Y %H:%M:%S")
        if self.timestamp_format == "mdy_slash_12h":
            return work_dt.strftime("%m/%d/%Y %I:%M:%S %p")
        if self.timestamp_format == "ymd_dash":
            return work_dt.strftime("%Y-%m-%d %H:%M:%S")

        return work_dt.strftime("%d.%m.%Y %H:%M:%S")

    def format_utc_text(self, dt: datetime | None) -> str | None:
        """Format UTC datetime as plain text so HA does not reinterpret it."""
        if dt is None:
            return None
        return f"{self.format_friendly_datetime(dt, use_utc=True)} UTC"

    async def async_added_to_hass(self):
        """Restore the last accepted earthquake after HA restart."""
        await super().async_added_to_hass()

        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data is not None:
            self._state = last_sensor_data.native_value

            last_state = await self.async_get_last_state()
            if last_state is not None:
                self._attributes = dict(last_state.attributes)

                self._current_unid = self._attributes.get("unid")
                self._current_event_time = self.parse_emsc_datetime(
                    self._attributes.get("time_utc_raw")
                )
                self._current_lastupdate = self.parse_emsc_datetime(
                    self._attributes.get("lastupdate_utc_raw")
                )

        # Backfill missing status for entities restored from older versions
        if self._current_unid is not None and "status" not in self._attributes:
            self._attributes["status"] = "active"

        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self.connect_to_websocket())

        self.schedule_auto_clear()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed from Home Assistant."""
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._clear_task is not None:
            self._clear_task.cancel()
            try:
                await self._clear_task
            except asyncio.CancelledError:
                pass
            self._clear_task = None

        await super().async_will_remove_from_hass()

    async def connect_to_websocket(self):
        """Connect to the EMSC WebSocket API and process messages."""
        while True:
            try:
                self._ssl_context = await self.async_create_ssl_context()
                _LOGGER.info(
                    "[%s | %s] Connecting to WebSocket: %s",
                    self._name,
                    self._entry_id,
                    WEBSOCKET_URL,
                )

                async with websockets.connect(
                    WEBSOCKET_URL,
                    ssl=self._ssl_context,
                    ping_interval=PING_INTERVAL,
                ) as websocket:
                    _LOGGER.info(
                        "[%s | %s] Connected to WebSocket. Listening for messages...",
                        self._name,
                        self._entry_id,
                    )
                    await self.listen_to_websocket(websocket)

            except Exception as e:
                _LOGGER.error("WebSocket error: %s", e)
                await asyncio.sleep(10)

    async def async_create_ssl_context(self):
        """Create and return SSL context in a separate thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(ssl_executor, self.create_ssl_context)

    def create_ssl_context(self):
        """Create SSL context."""
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        return ssl_context

    async def listen_to_websocket(self, websocket):
        """Listen for messages on the WebSocket."""
        try:
            async for message in websocket:
                await self.process_message(message)
        except websockets.ConnectionClosed:
            _LOGGER.warning("WebSocket connection closed.")
        except Exception as e:
            _LOGGER.error("Error while listening to WebSocket: %s", e)

    def is_within_radius(
        self,
        earthquake_latitude: float,
        earthquake_longitude: float,
    ) -> bool:
        """Check if the given earthquake is within the specified radius."""
        return (
            self.calculate_distance_km(earthquake_latitude, earthquake_longitude)
            <= self.radius_km
        )

    def calculate_distance_km(
        self,
        earthquake_latitude: float,
        earthquake_longitude: float,
    ) -> float:
        """Calculate distance between configured center and earthquake location."""
        r_earth_km = 6371.0

        lat1 = radians(self.center_latitude)
        lon1 = radians(self.center_longitude)
        lat2 = radians(earthquake_latitude)
        lon2 = radians(earthquake_longitude)

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return r_earth_km * c

    def calculate_bearing_deg(
        self,
        earthquake_latitude: float,
        earthquake_longitude: float,
    ) -> float:
        """Calculate initial bearing from home position to earthquake location."""
        lat1 = radians(self.center_latitude)
        lon1 = radians(self.center_longitude)
        lat2 = radians(earthquake_latitude)
        lon2 = radians(earthquake_longitude)

        dlon = lon2 - lon1

        x = sin(dlon) * cos(lat2)
        y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)

        bearing = atan2(x, y)
        bearing_deg = (degrees(bearing) + 360) % 360
        return bearing_deg

    def bearing_deg_to_text(self, bearing_deg: float) -> str:
        """Convert bearing in degrees to 16-point compass direction."""
        directions = [
            "N", "NNE", "NE", "ENE",
            "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW",
            "W", "WNW", "NW", "NNW",
        ]
        index = int((bearing_deg + 11.25) // 22.5) % 16
        return directions[index]

    def parse_emsc_datetime(self, value: Any) -> datetime | None:
        """Parse EMSC datetime strings into timezone-aware UTC datetimes."""
        if not value or not isinstance(value, str):
            return None

        text = value.strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                return None

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    def should_accept_event(
        self,
        incoming_unid: str | None,
        incoming_event_time: datetime | None,
    ) -> bool:
        """
        Decide whether this message should become the current stored earthquake event.

        Rules:
        - First event matching the defined criteria (radius, magnitude) is accepted.
        - Updates to the last event (same unid) are accepted.
        - A different event is accepted only if its origin time is newer.
        - If timestamps are missing, do not replace an existing event unless it is the same unid.
        """
        if self._current_unid is None:
            return True

        if incoming_unid and incoming_unid == self._current_unid:
            return True

        if incoming_event_time is None:
            return False

        if self._current_event_time is None:
            return True

        return incoming_event_time > self._current_event_time

    async def process_message(self, message):
        """Process an incoming WebSocket message."""
        _LOGGER.debug("Received WebSocket message: %s", message)

        try:
            data = json.loads(message)
            action = data.get("action", "unknown")
            info = data.get("data", {}).get("properties", {})

            lat = info.get("lat")
            lon = info.get("lon")
            mag = info.get("mag")
            unid = info.get("unid")

            if lat is None or lon is None or mag is None:
                _LOGGER.debug("Skipping event with missing lat/lon/mag.")
                return

            try:
                lat = float(lat)
                lon = float(lon)
                mag = float(mag)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "Skipping event with invalid lat/lon/mag: unid=%s lat=%s lon=%s mag=%s",
                    unid,
                    lat,
                    lon,
                    mag,
                )
                return

            within_radius = self.is_within_radius(lat, lon)
            passes_filter = (
                (within_radius and mag >= self.min_mag)
                or (mag >= self.total_max_mag)
            )

            if not passes_filter:
                _LOGGER.debug(
                    "[%s | %s] Skipping event outside criteria: unid=%s action=%s mag=%s within_radius=%s "
                    "min_mag=%s total_max_mag=%s region=%s time=%s lastupdate=%s",
                    self._name,
                    self._entry_id,
                    unid,
                    action,
                    mag,
                    within_radius,
                    self.min_mag,
                    self.total_max_mag,
                    info.get("flynn_region"),
                    info.get("time"),
                    info.get("lastupdate"),
                )
                return

            event_time_str = info.get("time")
            lastupdate_str = info.get("lastupdate")

            event_time = self.parse_emsc_datetime(event_time_str)
            lastupdate = self.parse_emsc_datetime(lastupdate_str)

            if not self.should_accept_event(unid, event_time):
                _LOGGER.info(
                    "[%s | %s] Ignored older earthquake event ('update' or late-arriving 'create'): current_unid=%s incoming_unid=%s "
                    "current_time=%s incoming_time=%s action=%s mag=%s region=%s lastupdate=%s",
                    self._name,
                    self._entry_id,
                    self._current_unid,
                    unid,
                    self._current_event_time,
                    event_time,
                    action,
                    mag,
                    info.get("flynn_region"),
                    lastupdate_str,
                )
                return

            distance_km = round(self.calculate_distance_km(lat, lon), 1)
            bearing_deg = round(self.calculate_bearing_deg(lat, lon), 1)
            bearing_text = self.bearing_deg_to_text(bearing_deg)
            relative_location = f"{distance_km} km {bearing_text} of reference point"

            try:
                country, city = await self.hass.async_add_executor_job(
                    lookup_geodata,
                    lat,
                    lon,
                )
            except Exception as e:
                _LOGGER.debug("Geodata lookup failed: %s", e)
                country = "lookup failed"
                city = "lookup failed"

            self._state = mag
            self._attributes = {
                "status": "active",
                "action": action,
                "unid": unid,
                "time": self.format_friendly_datetime(event_time, use_utc=False),
                "time_utc": self.format_utc_text(event_time),
                "lastupdate": self.format_friendly_datetime(lastupdate, use_utc=False),
                "lastupdate_utc": self.format_utc_text(lastupdate),
                "time_raw": event_time_str,
                "time_utc_raw": event_time.isoformat() if event_time else None,
                "lastupdate_raw": lastupdate_str,
                "lastupdate_utc_raw": lastupdate.isoformat() if lastupdate else None,
                "magnitude": mag,
                "region": info.get("flynn_region"),
                "country": country,
                "nearest_city": city,
                "depth": info.get("depth"),
                "latitude": lat,
                "longitude": lon,
                "magtype": info.get("magtype"),
                "distance_km": distance_km,
                "bearing_deg": bearing_deg,
                "bearing_text": bearing_text,
                "relative_location": relative_location,
                "within_radius": within_radius,
            }

            self._current_unid = unid
            self._current_event_time = event_time
            self._current_lastupdate = lastupdate

            _LOGGER.info(
                "[%s | %s] Accepted earthquake event: unid=%s action=%s mag=%s time=%s lastupdate=%s "
                "region=%s distance_km=%s bearing=%s depth=%s",
                self._name,
                self._entry_id,
                unid,
                action,
                mag,
                event_time_str,
                lastupdate_str,
                info.get("flynn_region"),
                distance_km,
                bearing_text,
                info.get("depth"),
            )
            self.async_write_ha_state()
            self.schedule_auto_clear()

        except Exception as e:
            _LOGGER.error("Error processing WebSocket message: %s", e)
