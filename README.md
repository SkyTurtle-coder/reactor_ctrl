# reactor_ctrl

Grundlage fuer einen Flask-Server, der Reaktorkomponenten ueber einen `Moxa NPort 5610-8-DT` als `RS-232 over Ethernet` ansteuert.

## Zielbild

- `device_server` beschreibt den Ethernet-Geraeteserver, aktuell auf `Moxa NPort 5610-8-DT` mit `8` RS-232-Ports ausgerichtet.
- `device_connection` beschreibt einen konkreten seriellen Kanal des NPort.
- `device` beschreibt die eigentliche Reaktorkomponente.
- `device_binding_current` und `device_binding_history` halten fest, welche Komponente aktuell bzw. historisch an welchem Port haengt.

Fuer die Basis wird die Moxa-Standardabbildung verwendet:

- `port_number` `1..8`
- Standard-TCP-Ports `4001..4008`
- `serial_standard = rs232`

## Wichtige API-Endpunkte

- `GET /api/`
- `GET /api/device-protocols`
- `GET|POST /api/device-servers`
- `GET|PATCH|DELETE /api/device-servers/<device_server_id>`
- `GET|POST /api/device-connections`
- `POST /api/device-connections/<connection_id>/probe`
- `GET|PATCH|DELETE /api/device-connections/<connection_id>`
- `GET|POST /api/devices`
- `GET|PATCH|DELETE /api/devices/<device_id>`
- `PUT|DELETE /api/devices/<device_id>/binding`
- `GET|POST /api/devices/<device_id>/commands`
- `GET /api/commands/<command_id>`

## Beispielablauf

1. Reaktorkomponente anlegen:

```json
{
  "asset_serial": "R-001",
  "display_name": "Reaktor 1",
  "device_type": "reactor_component",
  "protocol": "generic_text"
}
```

2. Device-Server anlegen:

```json
{
  "server_code": "MOXA-01",
  "display_name": "Moxa NPort 5610-8-DT",
  "host": "192.168.1.50"
}
```

3. Verbindung fuer Port 1 anlegen:

```json
{
  "device_server_id": 1,
  "port_number": 1,
  "baud_rate": 9600,
  "parity": "N"
}
```

Wenn `tcp_port` nicht angegeben wird, setzt die API fuer die Basis automatisch `4000 + port_number`.

4. Geraet an den Port binden:

```json
{
  "connection_id": 1,
  "quality_state": "configured",
  "is_online": false
}
```

5. Verbindung pruefen:

```text
POST /api/device-connections/1/probe
```

6. Erstes RS-232-Kommando senden:

```json
{
  "command_name": "query_text",
  "requested_by": "api",
  "payload": {
    "text": "STATUS?",
    "line_ending": "crlf",
    "expect_response": true
  }
}
```

Aktuell ist als erster Treiber `generic_text` implementiert. Er eignet sich fuer textbasierte RS-232-Protokolle und sendet/empfaengt ASCII oder UTF-8 ueber die dem Geraet zugeordnete `device_connection`.

## Start

```powershell
python -m pip install -r requirements.txt
python app.py
```

## Server-Hinweis

Beim App-Start werden fehlende Tabellen standardmaessig automatisch angelegt (`AUTO_CREATE_SCHEMA=true`). Das verhindert, dass ein Deployment zwar den Code aktualisiert, aber neue Tabellen wie `device_server` oder `device_connection` in MySQL noch fehlen.

Fuer produktive Deployments sollte `FLASK_DEBUG` auf `false` bleiben. Falls ein vorhandenes Datenbankschema noch aus der alten USB/RS-485-Struktur stammt, muessen die Legacy-Tabellen manuell auf das aktuelle NPort-Schema umgestellt werden.
