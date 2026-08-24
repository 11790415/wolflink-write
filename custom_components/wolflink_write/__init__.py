"""Wolf SmartSet Write - schreibender Zugriff auf das Wolf SmartSet Portal.

Registriert den Service `wolflink_write.set_value`, der Parameterwerte ueber
die bereits von der Core-Integration "wolflink" installierte Bibliothek
`wolf-comm` (Methode WolfClient.write_value, Endpoint
api/portal/WriteParameterValues) in das Wolf SmartSet Portal schreibt.

Zugangsdaten, Gateway- und System-ID werden automatisch aus dem bestehenden
wolflink-Konfigurationseintrag gelesen - es muss nichts doppelt konfiguriert
werden.

Portiert aus dem nicht gemergten Core-PR home-assistant/core#128988.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from wolf_comm.wolf_client import WolfClient

try:  # Konstanten der Bibliothek bevorzugen, Fallback auf bekannte Literale
    from wolf_comm.constants import STATE, VALUE_ID
except ImportError:  # pragma: no cover
    VALUE_ID = "ValueId"
    STATE = "State"

_LOGGER = logging.getLogger(__name__)

DOMAIN = "wolflink_write"
WOLFLINK_DOMAIN = "wolflink"
SERVICE_SET_VALUE = "set_value"

ATTR_VALUE_ID = "value_id"
ATTR_STATE = "state"
ATTR_GATEWAY_ID = "gateway_id"
ATTR_SYSTEM_ID = "system_id"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VALUE_ID): vol.Coerce(int),
        vol.Required(ATTR_STATE): vol.Any(int, float, str),
        vol.Optional(ATTR_GATEWAY_ID): vol.Coerce(int),
        vol.Optional(ATTR_SYSTEM_ID): vol.Coerce(int),
    }
)

# Ein Schreibvorgang zur Zeit + Session-Wiederverwendung (IP-Sperr-Schutz)
_WRITE_LOCK = asyncio.Lock()
_CLIENT_CACHE: dict[str, WolfClient] = {}


def _first_present(data: Any, keys: tuple[str, ...]) -> Any:
    """Ersten vorhandenen Wert aus einem Mapping lesen (defensiv)."""
    for key in keys:
        try:
            value = data.get(key)
        except AttributeError:
            return None
        if value is not None:
            return value
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Service registrieren."""

    async def async_set_value(call: ServiceCall) -> None:
        entries = hass.config_entries.async_entries(WOLFLINK_DOMAIN)
        if not entries:
            raise HomeAssistantError(
                "Keine Wolf SmartSet (wolflink) Integration gefunden - "
                "wolflink_write benoetigt deren Zugangsdaten."
            )

        system_id: int | None = call.data.get(ATTR_SYSTEM_ID)
        gateway_id: int | None = call.data.get(ATTR_GATEWAY_ID)

        # Passenden Konfigurationseintrag waehlen: bei angegebener system_id
        # den Eintrag mit passender Geraete-ID, sonst den ersten Eintrag.
        entry = None
        if system_id is not None:
            for candidate in entries:
                candidate_system = _first_present(
                    candidate.data, ("device_id", "system_id")
                )
                if candidate_system is not None and str(candidate_system) == str(
                    system_id
                ):
                    entry = candidate
                    break
        if entry is None:
            entry = entries[0]

        username = _first_present(entry.data, ("username",))
        password = _first_present(entry.data, ("password",))
        if system_id is None:
            resolved = _first_present(entry.data, ("device_id", "system_id"))
            system_id = int(resolved) if resolved is not None else None
        if gateway_id is None:
            resolved = _first_present(
                entry.data, ("device_gateway", "gateway_id", "gateway")
            )
            gateway_id = int(resolved) if resolved is not None else None

        if not username or not password or system_id is None or gateway_id is None:
            raise HomeAssistantError(
                "Zugangsdaten oder Gateway-/System-ID konnten nicht aus dem "
                f"wolflink-Eintrag '{entry.title}' ermittelt werden. "
                "gateway_id/system_id koennen alternativ im Serviceaufruf "
                "angegeben werden."
            )

        value = {VALUE_ID: int(call.data[ATTR_VALUE_ID]), STATE: call.data[ATTR_STATE]}

        async with _WRITE_LOCK:
            client = _CLIENT_CACHE.get(entry.entry_id)
            if client is None:
                client = WolfClient(username, password)
                _CLIENT_CACHE[entry.entry_id] = client
            try:
                result = await client.write_value(
                    int(gateway_id), int(system_id), value
                )
            except Exception as err:
                # Session verwerfen, damit der naechste Versuch frisch startet
                _CLIENT_CACHE.pop(entry.entry_id, None)
                raise HomeAssistantError(
                    f"WriteParameterValues fehlgeschlagen: {err}"
                ) from err

        _LOGGER.info(
            "Wolf SmartSet: ValueId %s auf %s gesetzt (System %s, Gateway %s). "
            "Antwort: %s",
            call.data[ATTR_VALUE_ID],
            call.data[ATTR_STATE],
            system_id,
            gateway_id,
            result,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_VALUE, async_set_value, schema=SERVICE_SCHEMA
    )
    _LOGGER.debug("Service %s.%s registriert", DOMAIN, SERVICE_SET_VALUE)
    return True
