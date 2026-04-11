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

        # Get the coordinates of zone.home
        home_zone = self.hass.states.get("zone.home")
        if home_zone:
            home_latitude = home_zone.attributes.get(CONF_LATITUDE)
            home_longitude = home_zone.attributes.get(CONF_LONGITUDE)

            # and round it to 5 digits (corresponding to around 1m accuracy)
            if home_latitude is not None:
                home_latitude = round(float(home_latitude), 5)
            if home_longitude is not None:
                home_longitude = round(float(home_longitude), 5)
        else:
            home_latitude = None
            home_longitude = None

        if user_input is not None:
            # Check that total_max_mag >= min_mag
            if user_input["total_max_mag"] < user_input["min_mag"]:
                errors["base"] = "global_mag_lt_local_mag"
            else:
                return self.async_create_entry(
                    title=user_input.get("name", DEFAULT_NAME),
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Optional("name", default=DEFAULT_NAME): str,
                vol.Required("center_latitude", default=home_latitude): vol.All(
                    vol.Coerce(float), vol.Range(min=-90, max=90)
                ),
                vol.Required("center_longitude", default=home_longitude): vol.All(
                    vol.Coerce(float), vol.Range(min=-180, max=180)
                ),
                vol.Required("radius_km"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False, max=500)
                ),
                vol.Required("min_mag"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
                vol.Required("total_max_mag"): vol.All(
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
            # Check that total_max_mag >= min_mag
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