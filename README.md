# Wolf SmartSet Write (`wolflink_write`)

Minimale Home-Assistant-Custom-Component, die **schreibenden** Zugriff auf das
Wolf SmartSet Portal nachrüstet. Die offizielle `wolflink`-Integration ist
read-only; diese Komponente registriert den Service
`wolflink_write.set_value`, der über die bereits installierte Bibliothek
`wolf-comm` (Methode `WolfClient.write_value`, Endpoint
`api/portal/WriteParameterValues`) Parameterwerte setzt.

Portiert aus dem nicht gemergten Core-PR
[home-assistant/core#128988](https://github.com/home-assistant/core/pull/128988);
die Schreibfunktion selbst ist seit wolf-comm 0.0.16
([janrothkegel/wolf-comm#14](https://github.com/janrothkegel/wolf-comm/pull/14),
gemerged Okt. 2024) fester Bestandteil der Bibliothek.

## Voraussetzungen

- Funktionierende **Wolf SmartSet Service (wolflink)** Integration.
  Zugangsdaten, Gateway- und System-ID werden zur Laufzeit automatisch aus
  deren Konfigurationseintrag gelesen – nichts muss doppelt gepflegt werden.

## Installation

1. Ordner `custom_components/wolflink_write/` nach `/config/custom_components/`
   kopieren (Ergebnis: `/config/custom_components/wolflink_write/__init__.py`).
2. In `configuration.yaml` eine Zeile ergänzen:

   ```yaml
   wolflink_write:
   ```

3. Home Assistant neu starten.

## Erster Test (kontrolliert, einmalig)

Entwicklerwerkzeuge → Aktionen → `wolflink_write.set_value`:

```yaml
action: wolflink_write.set_value
data:
  value_id: 22004200000   # 1x Warmwasserladung (Kaskade, System 76873)
  state: 1                # 1 = Ein
  system_id: 76873
```

Erwartung: Innerhalb von ~1–2 Minuten (Cloud-Polling) springt
`sensor.dhw_1_1x_dhw_2` auf `ein` und die Warmwasserladung startet.
Zurücksetzen mit `state: 0`. Schlägt `state: 1` fehl, `state: "1"`
(als Text) testen – das Portal erwartet je nach Parameter Zahl oder String.

## Schalter-Entität

Nach erfolgreichem Test in `configuration.yaml` (oder einem Package):

```yaml
switch:
  - platform: template
    switches:
      warmwasser_1x_ladung:
        friendly_name: "1x Warmwasserladung"
        unique_id: wolf_1x_warmwasserladung
        value_template: "{{ is_state('sensor.dhw_1_1x_dhw_2', 'ein') }}"
        turn_on:
          action: wolflink_write.set_value
          data:
            value_id: 22004200000
            state: 1
            system_id: 76873
        turn_off:
          action: wolflink_write.set_value
          data:
            value_id: 22004200000
            state: 0
            system_id: 76873
```

Hinweis: Die Statusrückmeldung kommt über das Cloud-Polling der
wolflink-Integration und hinkt dem Schaltvorgang bis zu ~90 s hinterher.

## ValueIds ermitteln

Die Unique-ID jeder wolflink-Entität hat das Format `systemId:valueId`
(z. B. `76873:22004200000`). Alternativ: im SmartSet-Portal per
Browser-DevTools (Netzwerk-Tab) den `WriteParameterValues`-Request beim
manuellen Schalten ansehen.

## Betriebshinweise

- **Session-Wiederverwendung:** Der WolfClient wird pro Konfigurationseintrag
  gecacht; nach einem Fehler wird die Session verworfen und beim nächsten
  Aufruf neu aufgebaut. Wolf sperrt IPs zeitweise bei zu vielen
  Auth-Vorgängen – daher Schaltvorgänge nicht im Sekundentakt auslösen.
- **Inoffizielle API:** Wolf kann das Protokoll jederzeit ändern; davon wäre
  die offizielle Integration gleichermaßen betroffen.
- Nach **HA-Core-Updates** (mögliche wolf-comm-Versionssprünge) einmal
  testweise schalten.
