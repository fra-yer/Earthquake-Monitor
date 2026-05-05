# Version 1.7.0 by FOF, May 2026
# change-log of 1.7.0
#    no changes from 1.6.2

from homeassistant import config_entries
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import DOMAIN, DEFAULT_NAME


def get_localized_default_name(hass) -> str:
    """Return a localized default name for the configured monitor instance."""
    language = hass.config.language.lower().replace("-", "_")

    localized_names = {
        "en": "Latest Earthquake",
        "de": "Letztes Erdbeben",
        "el": "Τελευταίος σεισμός",
        "es": "Último terremoto",
        "fr": "Dernier séisme",
        "it": "Ultimo terremoto",
        "nl": "Laatste aardbeving",
        "ja": "最新の地震",
        "pl": "Ostatnie trzęsienie ziemi",
        "pt": "Último sismo",
        "pt_br": "Último terremoto",
        "tr": "Son deprem",
        "uk": "Останній землетрус",
        "zh": "最新地震",
        "zh_hant": "最新地震",
        "id": "Gempa Bumi Terbaru",
    }

    # Exact match first, e.g. pt_br or zh_hant
    if language in localized_names:
        return localized_names[language]

    # Fallback to base language, e.g. pt from pt_pt or zh from zh_tw
    base_language = language.split("_")[0]
    return localized_names.get(base_language, DEFAULT_NAME)


class EarthquakeMonitorFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Earthquake Monitor integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_PUSH

    def __init__(self):
        """Initialize the config flow."""
        self._user_data = {}

    async def async_step_user(self, user_input=None):
        """Handle step 1: reference point and thresholds."""
        errors = {}

        reference_zone = (
            self.hass.states.get("zone.earthquake_reference")
            or self.hass.states.get("zone.earthquakereference")
        )
        home_zone = self.hass.states.get("zone.home")

        default_name = get_localized_default_name(self.hass)
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

        if default_radius_km is None:
            default_radius_km = 100.0

        if user_input is not None:
            if user_input["total_max_mag"] < user_input["min_mag"]:
                errors["base"] = "global_mag_lt_local_mag"
            else:
                self._user_data = dict(user_input)
                return await self.async_step_retention()

        schema = vol.Schema(
            {
                vol.Optional("name", default=default_name): str,
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
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_retention(self, user_input=None):
        """Handle step 2: lifetime of latest event and user-defined timestamp format."""
        if user_input is not None:
            self._user_data["reset_after_hours"] = user_input["retention_settings"][
                "reset_after_hours"
            ]
            self._user_data["timestamp_format"] = user_input["timestamp_settings"][
                "timestamp_format"
            ]

            return self.async_create_entry(
                title=self._user_data.get("name", DEFAULT_NAME),
                data=self._user_data,
            )

        schema = vol.Schema(
            {
                vol.Required("retention_settings"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                "reset_after_hours", default=48.0
                            ): vol.All(
                                vol.Coerce(float), vol.Range(min=0, max=8760)
                            ),
                        }
                    )
                ),
                vol.Required("timestamp_settings"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                "timestamp_format",
                                default="dmy_dot",
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=[
                                        "dmy_dot",
                                        "dmy_slash",
                                        "mdy_slash_12h",
                                        "ymd_dash",
                                    ],
                                    translation_key="timestamp_format",
                                )
                            ),
                        }
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="retention",
            data_schema=schema,
            errors={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return EarthquakeMonitorOptionsFlowHandler()


class EarthquakeMonitorOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for the Earthquake Monitor integration."""

    async def async_step_init(self, user_input=None):
        """Manage step 1: reference point and thresholds."""
        errors = {}

        if not hasattr(self, "_user_data"):
            self._user_data = dict(self.config_entry.data)

        if user_input is not None:
            if user_input["total_max_mag"] < user_input["min_mag"]:
                errors["base"] = "global_mag_lt_local_mag"
            else:
                self._user_data.update(user_input)
                return await self.async_step_retention()

        schema = vol.Schema(
            {
                vol.Required(
                    "center_latitude",
                    default=self.config_entry.data.get("center_latitude"),
                ): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
                vol.Required(
                    "center_longitude",
                    default=self.config_entry.data.get("center_longitude"),
                ): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
                vol.Required(
                    "radius_km",
                    default=self.config_entry.data.get("radius_km"),
                ): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False, max=500)
                ),
                vol.Required(
                    "min_mag",
                    default=self.config_entry.data.get("min_mag"),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Required(
                    "total_max_mag",
                    default=self.config_entry.data.get("total_max_mag"),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_retention(self, user_input=None):
        """Manage step 2: event retention and timestamp format."""
        if user_input is not None:
            self._user_data["reset_after_hours"] = user_input["retention_settings"][
                "reset_after_hours"
            ]
            self._user_data["timestamp_format"] = user_input["timestamp_settings"][
                "timestamp_format"
            ]

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=self._user_data,
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)

            return self.async_create_entry(
                title=self.config_entry.title,
                data={},
            )

        schema = vol.Schema(
            {
                vol.Required("retention_settings"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                "reset_after_hours",
                                default=self.config_entry.data.get(
                                    "reset_after_hours", 48.0
                                ),
                            ): vol.All(
                                vol.Coerce(float), vol.Range(min=0, max=8760)
                            ),
                        }
                    )
                ),
                vol.Required("timestamp_settings"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                "timestamp_format",
                                default=self.config_entry.data.get(
                                    "timestamp_format", "dmy_dot"
                                ),
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=[
                                        "dmy_dot",
                                        "dmy_slash",
                                        "mdy_slash_12h",
                                        "ymd_dash",
                                    ],
                                    translation_key="timestamp_format",
                                )
                            ),
                        }
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="retention",
            data_schema=schema,
            errors={},
        )
