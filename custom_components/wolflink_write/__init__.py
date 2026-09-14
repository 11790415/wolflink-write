"""Wolf SmartSet Write - schreibender Zugriff auf das Wolf SmartSet Portal.

Registriert den Service `wolflink_write.set_value`, der Parameterwerte ueber
die Bibliothek `wolf-comm` (WolfClient.write_value, Endpoint
api/portal/WriteParameterValues) in das Wolf SmartSet Portal schreibt.

Aufloesung von Client, Gateway- und System-ID (in dieser Reihenfolge):
1. Laufende Koordinatoren der wolflink-Integration (entry.runtime_data,
   auch als Dict mit mehreren Geraeten, sowie hass.data) - deren
   _wolf_client/_gateway_id/device_id werden wiederverwendet.
2. Eigener WolfClient aus username/password des Konfigurationseintrags;
   Gateway-/System-ID werden dann per fetch_system_list() von der API
   aufgeloest (Ergebnis wird gecacht).

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
from homeassistant.helpers import discovery
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
ATTR_BUNDLE_ID = "bundle_id"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VALUE_ID): vol.Coerce(int),
        vol.Required(ATTR_STATE): vol.Any(int, float, str),
        vol.Optional(ATTR_GATEWAY_ID): vol.Coerce(int),
        vol.Optional(ATTR_SYSTEM_ID): vol.Coerce(int),
        vol.Optional(ATTR_BUNDLE_ID): vol.Coerce(int),
    }
)

_WRITE_LOCK = asyncio.Lock()
_CLIENT_CACHE: dict[str, WolfClient] = {}
_DEVICE_CACHE: dict[str, list[tuple[Any, Any]]] = {}  # entry_id -> [(system, gateway)]

_CLIENT_ATTRS = ("_wolf_client", "wolf_client", "client", "_client")
_GATEWAY_ATTRS = ("_gateway_id", "gateway_id", "_device_gateway", "device_gateway")
_SYSTEM_ATTRS = ("device_id", "_device_id", "system_id", "_system_id")


def _first_attr(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _coordinator_like(hass: HomeAssistant, entry: Any) -> list[Any]:
    """Alle Objekte einsammeln, die Koordinatoren der Integration sein koennten.

    runtime_data und hass.data-Eintraege koennen das Objekt selbst, ein Dict
    mit mehreren Geraeten oder verschachtelte Container sein - eine Ebene
    Dict-/Listen-Werte wird jeweils mit durchsucht.
    """
    seeds: list[Any] = []
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        seeds.append(runtime)
    stored = hass.data.get(WOLFLINK_DOMAIN)
    if isinstance(stored, dict):
        per_entry = stored.get(entry.entry_id)
        if per_entry is not None:
            seeds.append(per_entry)

    result: list[Any] = []
    for seed in seeds:
        queue = [seed]
        for _ in range(50):
            if not queue:
                break
            obj = queue.pop()
            if isinstance(obj, dict):
                queue.extend(obj.values())
            elif isinstance(obj, (list, tuple, set)):
                queue.extend(obj)
            else:
                result.append(obj)
                nested = getattr(obj, "coordinator", None)
                if nested is not None:
                    result.append(nested)
    return result


async def _resolve_devices(client: WolfClient, entry_id: str) -> list[tuple[Any, Any]]:
    """(system_id, gateway_id)-Paare per API ermitteln (mit Cache)."""
    cached = _DEVICE_CACHE.get(entry_id)
    if cached:
        return cached
    devices = await client.fetch_system_list()
    pairs: list[tuple[Any, Any]] = []
    for device in devices or []:
        system = _first_attr(device, ("id", "system_id", "device_id"))
        gateway = _first_attr(device, ("gateway", "gateway_id", "device_gateway"))
        if system is not None and gateway is not None:
            pairs.append((system, gateway))
    if pairs:
        _DEVICE_CACHE[entry_id] = pairs
    return pairs


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Service registrieren."""

    async def async_set_value(call: ServiceCall) -> None:
        entries = hass.config_entries.async_entries(WOLFLINK_DOMAIN)
        if not entries:
            raise HomeAssistantError(
                "Keine Wolf SmartSet (wolflink) Integration gefunden."
            )
        entry = entries[0]

        req_system: int | None = call.data.get(ATTR_SYSTEM_ID)
        gateway_id: Any = call.data.get(ATTR_GATEWAY_ID)
        system_id: Any = req_system

        # 1. Laufende Koordinatoren durchsuchen; bei angegebener system_id
        #    bevorzugt den Koordinator des passenden Geraets verwenden.
        client: WolfClient | None = None
        fallback_client: WolfClient | None = None
        fallback_gateway: Any = None
        fallback_system: Any = None
        for holder in _coordinator_like(hass, entry):
            h_client = _first_attr(holder, _CLIENT_ATTRS)
            if not isinstance(h_client, WolfClient):
                continue
            h_system = _first_attr(holder, _SYSTEM_ATTRS)
            h_gateway = _first_attr(holder, _GATEWAY_ATTRS)
            if fallback_client is None:
                fallback_client = h_client
                fallback_gateway = h_gateway
                fallback_system = h_system
            if req_system is None or (
                h_system is not None and str(h_system) == str(req_system)
            ):
                client = h_client
                if gateway_id is None:
                    gateway_id = h_gateway
                if system_id is None:
                    system_id = h_system
                break

        borrowed = client is not None
        if client is None and fallback_client is not None:
            # Koordinator gefunden, aber nicht fuer die gewuenschte system_id:
            # Client trotzdem mitbenutzen, IDs unten sauber aufloesen.
            client = fallback_client
            borrowed = True
            if req_system is None:
                if gateway_id is None:
                    gateway_id = fallback_gateway
                if system_id is None:
                    system_id = fallback_system

        # 2. Fallback: eigener Client aus Zugangsdaten des Eintrags
        if client is None:
            username = entry.data.get("username") or entry.data.get("user")
            password = entry.data.get("password")
            if not username or not password:
                raise HomeAssistantError(
                    "Kein WolfClient auffindbar und keine Zugangsdaten im "
                    f"wolflink-Eintrag '{entry.title}' (Keys: "
                    f"{sorted(entry.data.keys())})."
                )
            client = _CLIENT_CACHE.get(entry.entry_id)
            if client is None:
                client = WolfClient(username, password)
                _CLIENT_CACHE[entry.entry_id] = client
            borrowed = False

        # Fehlende IDs per API aufloesen
        if gateway_id is None or system_id is None:
            pairs = await _resolve_devices(client, entry.entry_id)
            if system_id is not None:
                for p_system, p_gateway in pairs:
                    if str(p_system) == str(system_id):
                        gateway_id = gateway_id or p_gateway
                        break
            elif pairs:
                system_id, gateway_id = pairs[0]

        if gateway_id is None or system_id is None:
            raise HomeAssistantError(
                "Gateway-/System-ID konnten nicht ermittelt werden "
                f"(system={system_id}, gateway={gateway_id}). Bitte beide "
                "im Serviceaufruf angeben."
            )

        value_id = int(call.data[ATTR_VALUE_ID])
        state = call.data[ATTR_STATE]
        write_dict = {VALUE_ID: value_id, STATE: state}

        # BundleId ermitteln: Serviceaufruf > geladene Parameter der
        # Integration > Fallback 1000 (Standard-Bundle "Uebersicht").
        bundle_id = call.data.get(ATTR_BUNDLE_ID)
        if bundle_id is None:
            for holder in _coordinator_like(hass, entry):
                params_obj = getattr(holder, "parameters", None)
                if not params_obj:
                    continue
                iterable = (
                    params_obj.values() if isinstance(params_obj, dict) else params_obj
                )
                for param in iterable:
                    pid = _first_attr(
                        param, ("value_id", "_value_id", "values_id", "id")
                    )
                    if pid is not None and str(pid) == str(value_id):
                        bundle_id = _first_attr(
                            param, ("bundle_id", "_bundle_id", "bundle", "BundleId")
                        )
                        break
                if bundle_id is not None:
                    break
        if bundle_id is None:
            bundle_id = 1000
            _LOGGER.warning(
                "BundleId fuer ValueId %s nicht ermittelbar - verwende "
                "Fallback 1000. Falls der Schreibvorgang scheitert, bundle_id "
                "im Serviceaufruf angeben.",
                value_id,
            )

        import inspect

        try:
            sig_params = list(inspect.signature(client.write_value).parameters)
        except (TypeError, ValueError):  # pragma: no cover
            sig_params = []
        _LOGGER.debug(
            "write_value-Signatur: %s; bundle_id=%s", sig_params, bundle_id
        )

        async with _WRITE_LOCK:
            try:
                if len(sig_params) >= 4:
                    result = await client.write_value(
                        int(gateway_id), int(system_id), int(bundle_id), write_dict
                    )
                else:
                    result = await client.write_value(
                        int(gateway_id), int(system_id), write_dict
                    )
            except Exception as err:
                try:
                    src = inspect.getsource(client.write_value)[:1500]
                except (OSError, TypeError):  # pragma: no cover
                    src = "<Quellcode nicht verfuegbar>"
                _LOGGER.error(
                    "write_value fehlgeschlagen (%s). Signatur=%s, bundle_id=%s. "
                    "Quellcode:\n%s",
                    err,
                    sig_params,
                    bundle_id,
                    src,
                )
                raise HomeAssistantError(
                    f"WriteParameterValues fehlgeschlagen: {err}"
                ) from err

        _LOGGER.info(
            "Wolf SmartSet: ValueId %s auf %s gesetzt (System %s, Gateway %s, "
            "Client %s). Antwort: %s",
            call.data[ATTR_VALUE_ID],
            call.data[ATTR_STATE],
            system_id,
            gateway_id,
            "wolflink-Integration" if borrowed else "eigene Session",
            result,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_VALUE, async_set_value, schema=SERVICE_SCHEMA
    )
    _LOGGER.debug("Service %s.%s registriert", DOMAIN, SERVICE_SET_VALUE)
    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )
    return True
