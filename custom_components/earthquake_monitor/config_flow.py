# Version 1.4.1 by FOF, April 2026
# change-log: no changes from 1.4.0

from homeassistant import config_entries
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE

from .const import DOMAIN, DEFAULT_NAME


class EarthquakeMonitorFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Earthquake Monitor integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_PUSH

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        # Get the coordinates of defined zone or home zone as fallback
        # Prefer a user-defined zone.earthquake_reference if it exists.
        # Otherwise fall back to zone.home for coordinates only.
        reference_zone = (
            self.hass.states.get("zone.earthquake_reference")
            or self.hass.states.get("zone.earthquakereference")
        )
        home_zone = self.hass.states.get("zone.home")

        default_latitude = None
        default_longitude = None
        default_radius_km = None

        if reference_zone:
            default_latitude = reference_zone.attributes.get(CONF_LATITUDE)
            default_longitude = reference_zone.attributes.get(CONF_LONGITUDE)

            zone_radius_m = reference_zone.attributes.get("radius")
            if zone_radius_m is not None:
                default_radius_km = float(round(float(zone_radius_m) / 1000))

        elif home_zone:
            default_latitude = home_zone.attributes.get(CONF_LATITUDE)
            default_longitude = home_zone.attributes.get(CONF_LONGITUDE)

        if default_latitude is not None:
            default_latitude = round(float(default_latitude), 5)
        if default_longitude is not None:
            default_longitude = round(float(default_longitude), 5)

        if user_input is not None:
            # Check that the outside-radius threshold is not lower than the local threshold
            if user_input["total_max_mag"] < user_input["min_mag"]:
                errors["base"] = "global_mag_lt_local_mag"
            else:
                return self.async_create_entry(
                    title=user_input.get("name", DEFAULT_NAME),
                    data=user_input,
                )

        if default_radius_km is None:
            default_radius_km = 100.0

        schema = vol.Schema(
            {
                vol.Optional("name", default=DEFAULT_NAME): str,
                vol.Required("center_latitude", default=default_latitude): vol.All(
                    vol.Coerce(float), vol.Range(min=-90, max=90)
                ),
                vol.Required("center_longitude", default=default_longitude): vol.All(
                    vol.Coerce(float), vol.Range(min=-180, max=180)
                ),
                vol.Required("radius_km", default=default_radius_km): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False, max=500)
                ),
                vol.Required("min_mag", default=2.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
                vol.Required("total_max_mag", default=8.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return EarthquakeMonitorOptionsFlowHandler()


class EarthquakeMonitorOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for the Earthquake Monitor integration."""

    async def async_step_init(self, user_input=None):
        """Manage the options for the integration."""
        errors = {}

        if user_input is not None:
            # Check that the outside-radius threshold is not lower than the local threshold
            if user_input["total_max_mag"] < user_input["min_mag"]:
                errors["base"] = "global_mag_lt_local_mag"
            else:
                # Deliberately store updated settings back into config entry data
                # rather than splitting them into data/options, to keep one single
                # source of truth for this small integration.
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=user_input
                )
                return self.async_create_entry(
                    title=self.config_entry.title, data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(
                    "center_latitude",
                    default=self.config_entry.data.get("center_latitude"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=-90, max=90)
                ),
                vol.Required(
                    "center_longitude",
                    default=self.config_entry.data.get("center_longitude"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=-180, max=180)
                ),
                vol.Required(
                    "radius_km",
                    default=self.config_entry.data.get("radius_km"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False, max=500)
                ),
                vol.Required(
                    "min_mag",
                    default=self.config_entry.data.get("min_mag"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
                vol.Required(
                    "total_max_mag",
                    default=self.config_entry.data.get("total_max_mag"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
