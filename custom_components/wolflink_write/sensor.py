"""Sensor-Plattform fuer wolflink_write.

Liest die Luft-Temperaturen der Aussengeraete aus dem Wolf SmartSet Portal, die
die offizielle wolflink-Integration nicht anlegt.

Hintergrund: wolf-comm baut die Entitaetenliste in fetch_parameters() nur aus
MENU_ITEMS[0].TAB_VIEWS, also dem Benutzermenue. Zuluft- und Ablufttemperatur
liegen in der Fachmann-Ebene und fallen dort heraus. Zum reinen *Lesen* der
Werte ist das egal: GetParameterValues akzeptiert beliebige ValueIds, solange
die passende BundleId mitgeschickt wird. Deshalb sind die IDs hier fest
hinterlegt und es wird kein zweiter WolfClient mit expert_p=True erzeugt - das
wuerde eine zweite Sitzung auf demselben Konto oeffnen und die Sitzung der
offiziellen Integration entwerten koennen.

Der WolfClient der laufenden wolflink-Integration wird wiederverwendet.

Warum nicht client.fetch_value(): Diese Methode sendet Bundle=False. Die Werte
der Fachmann-Ebene werden vom Gateway dann offenbar nicht frisch ueber den eBus
geholt, sondern es kommt der zuletzt zwischengespeicherte Stand zurueck - in der
Praxis eingefroren auf den Moment, in dem die Seite zuletzt im SmartSet-Portal
geoeffnet war. Deshalb wird hier direkt mit Bundle=True angefragt, was das
Gateway veranlasst, das komplette Bundle neu einzulesen.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from wolf_comm.models import Temperature

from . import (
    _CLIENT_ATTRS,
    _GATEWAY_ATTRS,
    _SYSTEM_ATTRS,
    _coordinator_like,
    _first_attr,
    WOLFLINK_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=120)

PARAMETER_VALUES_PATH = "api/portal/GetParameterValues"

# ValueId, BundleId, Anzeigename, eindeutige Kennung
# Ermittelt ueber einen einmaligen Lauf mit expert_p=True.
# Die letzten beiden Ziffern der ValueId sind die Geraetenummer der Kaskade,
# die fuehrenden Stellen entsprechen den eBus-Registern 270040 und 270043.
AIR_SENSORS: list[tuple[int, int, str, str]] = [
    (27004000001, 3200, "WP1 Zulufttemperatur", "wp1_zuluft"),
    (27004300001, 3200, "WP1 Ablufttemperatur", "wp1_abluft"),
    (27004000002, 4900, "WP2 Zulufttemperatur", "wp2_zuluft"),
    (27004300002, 4900, "WP2 Ablufttemperatur", "wp2_abluft"),
]


def _build_parameters() -> list[Temperature]:
    """Temperature-Objekte bauen, die fetch_value() erwartet.

    fetch_value() wertet nur value_id und bundle_id aus; parameter_id, parent
    und read_only sind fuer den Lesevorgang ohne Bedeutung.
    """
    return [
        Temperature(value_id, name, None, 0, bundle_id, True)
        for value_id, bundle_id, name, _key in AIR_SENSORS
    ]


def _resolve_client(hass: HomeAssistant) -> tuple[Any, Any, Any]:
    """Client, Gateway-ID und System-ID der laufenden wolflink-Integration holen."""
    for entry in hass.config_entries.async_entries(WOLFLINK_DOMAIN):
        for candidate in _coordinator_like(hass, entry):
            client = _first_attr(candidate, _CLIENT_ATTRS)
            gateway = _first_attr(candidate, _GATEWAY_ATTRS)
            system = _first_attr(candidate, _SYSTEM_ATTRS)
            if client is not None and gateway is not None and system is not None:
                return client, gateway, system
    return None, None, None


def _to_float(raw: Any) -> float | None:
    """Wolf liefert Werte als Text; Komma und Punkt beide zulassen."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Plattform einrichten."""
    parameters = _build_parameters()

    # ValueIds nach BundleId gruppieren - pro Bundle eine Anfrage
    bundles: dict[int, list[int]] = {}
    for value_id, bundle_id, _name, _key in AIR_SENSORS:
        bundles.setdefault(bundle_id, []).append(value_id)

    async def _fetch_bundle(client, gateway, system, bundle_id, value_ids) -> dict:
        """Ein Bundle mit Bundle=True abfragen.

        Der private Request-Helfer der Bibliothek wird wiederverwendet, damit
        Authentifizierung, Session-Erneuerung und Fehlerbehandlung identisch
        zur offiziellen Integration bleiben.
        """
        request = getattr(client, "_WolfClient__request", None)
        if request is None:
            raise UpdateFailed(
                "wolf-comm hat sich geaendert - interner Request-Helfer fehlt"
            )
        payload = {
            "BundleId": bundle_id,
            "Bundle": True,
            "ValueIdList": value_ids,
            "GatewayId": gateway,
            "SystemId": system,
            "GuiIdChanged": False,
            "SessionId": client.session_id,
            "LastAccess": None,
        }
        return await request(
            "post",
            PARAMETER_VALUES_PATH,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    async def _async_update() -> dict[str, Any]:
        client, gateway, system = _resolve_client(hass)
        if client is None:
            raise UpdateFailed(
                "Kein laufender wolflink-Koordinator gefunden - "
                "ist die offizielle Wolf-Integration eingerichtet?"
            )
        if getattr(client, "session_id", None) is None:
            raise UpdateFailed("Noch keine Wolf-Sitzung - naechster Versuch spaeter")

        by_id: dict[str, float | None] = {}
        for bundle_id, value_ids in bundles.items():
            try:
                res = await _fetch_bundle(client, gateway, system, bundle_id, value_ids)
            except UpdateFailed:
                raise
            except Exception as err:  # noqa: BLE001 - Bibliotheksfehler durchreichen
                raise UpdateFailed(
                    f"Abruf Bundle {bundle_id} fehlgeschlagen: {err}"
                ) from err

            if not isinstance(res, dict):
                raise UpdateFailed(f"Unerwartete Antwort fuer Bundle {bundle_id}")
            for entry in res.get("Values", []) or []:
                if "Value" in entry:
                    by_id[str(entry.get("ValueId"))] = _to_float(entry.get("Value"))

        result: dict[str, Any] = {}
        for value_id, _bundle_id, _name, key in AIR_SENSORS:
            value = by_id.get(str(value_id))
            if value is not None:
                result[key] = value
        if not result:
            raise UpdateFailed("Keine Werte erhalten")
        result["_abgerufen"] = dt_util.now().isoformat(timespec="seconds")
        _LOGGER.debug("Luft-Temperaturen abgerufen: %s", result)
        return result

    coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Diese Integration hat keinen Config-Entry (Start ueber async_setup).
        # config_entry=None sagt das ausdruecklich, sonst meldet HA einen
        # Framework-Verstoss.
        config_entry=None,
        name="wolflink_write Luft-Temperaturen",
        update_method=_async_update,
        update_interval=SCAN_INTERVAL,
    )

    # Bewusst async_refresh() statt async_config_entry_first_refresh():
    # Letzteres ist Config-Entry-Integrationen vorbehalten und wirft sonst
    # ConfigEntryError. async_refresh() wirft nicht - schlaegt der erste Abruf
    # fehl (etwa weil wolflink beim Start noch nicht geladen ist), werden die
    # Entitaeten trotzdem angelegt und beim naechsten Zyklus gefuellt.
    await coordinator.async_refresh()

    async_add_entities(
        WolfAirTemperature(coordinator, name, key)
        for _value_id, _bundle_id, name, key in AIR_SENSORS
    )


class WolfAirTemperature(CoordinatorEntity, SensorEntity):
    """Eine Luft-Temperatur eines Wolf-Aussengeraets."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        name: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"wolflink_write_{key}"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Zeitpunkt des letzten erfolgreichen Abrufs - zur Fehlersuche."""
        if not self.coordinator.data:
            return {}
        return {"abgerufen": self.coordinator.data.get("_abgerufen")}

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self._key in self.coordinator.data
        )
