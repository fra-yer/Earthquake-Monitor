# Earthquake Monitor
# Based on EMSC Earthquake https://github.com/febalci/ha_emsc_earthquake by febalci
# Extended with improved event-selection and location-description logic
# See accompanying README.md for details
# Version 1.3.1 by FOF, April 2026

import asyncio
import json
import logging
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from math import radians, degrees, cos, sin, sqrt, atan2
from typing import Any

import websockets
from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

WEBSOCKET_URL = "wss://www.seismicportal.eu/standing_order/websocket"
PING_INTERVAL = 15  # seconds

# Thread pool executor for SSL context creation
ssl_executor = ThreadPoolExecutor(max_workers=1)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the Earthquake Monitor sensor from a config entry."""
    config = hass.data[DOMAIN][config_entry.entry_id]

    name = config.get("name", DEFAULT_NAME)
    center_latitude = config.get("center_latitude")
    center_longitude = config.get("center_longitude")
    radius_km = config.get("radius_km")
    total_max_mag = config.get("total_max_mag")
    min_mag = config.get("min_mag")

    sensor = EarthquakeMonitorSensor(
        name=name,
        center_latitude=center_latitude,
        center_longitude=center_longitude,
        radius_km=radius_km,
        min_mag=min_mag,
        total_max_mag=total_max_mag,
    )

    async_add_entities([sensor], True)
    hass.loop.create_task(sensor.connect_to_websocket())


class EarthquakeMonitorSensor(RestoreSensor):
    """Representation of an Earthquake Monitor sensor."""

    def __init__(
        self,
        name: str,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
        min_mag: float,
        total_max_mag: float,
    ) -> None:
        """Initialize the sensor."""
        self._name = name
        self._state = None
        self._attributes: dict[str, Any] = {}
        self._ssl_context = None

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
        return f"{DOMAIN}_{self._name}"

    @property
    def icon(self):
        """Return the icon for the sensor."""
        return "mdi:waveform"

    async def async_added_to_hass(self):
        """Restore the last accepted earthquake after HA restart."""
        await super().async_added_to_hass()

        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data is None:
            return

        self._state = last_sensor_data.native_value

        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        self._attributes = dict(last_state.attributes)

        self._current_unid = self._attributes.get("unid")
        self._current_event_time = self.parse_emsc_datetime(
            self._attributes.get("time_utc_raw")
        )
        self._current_lastupdate = self.parse_emsc_datetime(
            self._attributes.get("lastupdate_utc_raw")
        )

    async def connect_to_websocket(self):
        """Connect to the EMSC WebSocket API and process messages."""
        while True:
            try:
                self._ssl_context = await self.async_create_ssl_context()
                _LOGGER.info("Connecting to WebSocket: %s", WEBSOCKET_URL)

                async with websockets.connect(
                    WEBSOCKET_URL,
                    ssl=self._ssl_context,
                    ping_interval=PING_INTERVAL,
                ) as websocket:
                    _LOGGER.info("Connected to WebSocket. Listening for messages...")
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

    def is_within_radius(self, earthquake_latitude: float, earthquake_longitude: float) -> bool:
        """Check if the given earthquake is within the specified radius."""
        return self.calculate_distance_km(earthquake_latitude, earthquake_longitude) <= self.radius_km

    def calculate_distance_km(self, earthquake_latitude: float, earthquake_longitude: float) -> float:
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

    def calculate_bearing_deg(self, earthquake_latitude: float, earthquake_longitude: float) -> float:
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
        index = int((bearing_deg + 11.25) // 22.5) % 16  # Correct rounding - gives cleaner compass-sector boundaries
        return directions[index]

    def parse_emsc_datetime(self, value: Any) -> datetime | None:
        """Parse EMSC datetime strings into timezone-aware UTC datetimes."""
        if not value or not isinstance(value, str):
            return None

        text = value.strip()

        # Normalize a trailing Z to +00:00 for fromisoformat
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # Fallback patterns for slightly inconsistent payloads
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
        - A different event is accepted only if its origin time is newer, i.e. updates to older events are discarded.
        - If timestamps are missing, be conservative and do not replace an existing event
          unless it is the same unid.
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
            passes_filter = (within_radius and mag >= self.min_mag) or (mag >= self.total_max_mag)

            if not passes_filter:
                _LOGGER.debug(
                    "Skipping event outside criteria: unid=%s action=%s mag=%s within_radius=%s "
                    "min_mag=%s total_max_mag=%s region=%s time=%s lastupdate=%s",
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
            
            event_time_local = dt_util.as_local(event_time) if event_time else None
            lastupdate_local = dt_util.as_local(lastupdate) if lastupdate else None

            if not self.should_accept_event(unid, event_time):
                _LOGGER.info(
                    "Ignored update for older earthquake: current_unid=%s incoming_unid=%s "
                    "current_time=%s incoming_time=%s action=%s mag=%s region=%s lastupdate=%s",
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
            
            self._state = mag
            self._attributes = {
                "action": action,
                "unid": unid,
                "time": event_time_local.strftime("%-d. %B %Y %H:%M:%S") if event_time_local else None,
                "time_utc": event_time.strftime("%-d. %B %Y %H:%M:%S") if event_time else None,
                "lastupdate": lastupdate_local.strftime("%-d. %B %Y %H:%M:%S") if lastupdate_local else None,
                "lastupdate_utc": lastupdate.strftime("%-d. %B %Y %H:%M:%S") if lastupdate else None,
                "time_raw": event_time_str,
                "time_utc_raw": event_time.isoformat() if event_time else None,
                "lastupdate_raw": lastupdate_str,
                "lastupdate_utc_raw": lastupdate.isoformat() if lastupdate else None,
                "magnitude": mag,
                "region": info.get("flynn_region"),
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
                "Accepted earthquake event: unid=%s action=%s mag=%s time=%s lastupdate=%s "
                "region=%s distance_km=%s bearing=%s depth=%s",
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

        except Exception as e:
            _LOGGER.error("Error processing WebSocket message: %s", e)
