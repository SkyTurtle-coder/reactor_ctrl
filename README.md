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
- `GET|POST /api/device-servers`
- `GET|PATCH|DELETE /api/device-servers/<device_server_id>`
- `GET|POST /api/device-connections`
- `POST /api/device-connections/<connection_id>/probe`
- `GET|PATCH|DELETE /api/device-connections/<connection_id>`
- `GET|POST /api/devices`
- `GET|PATCH|DELETE /api/devices/<device_id>`
- `PUT|DELETE /api/devices/<device_id>/binding`

## Beispielablauf

1. Device-Server anlegen:

```json
{
  "server_code": "MOXA-01",
  "display_name": "Moxa NPort 5610-8-DT",
  "host": "192.168.1.50"
}
```

2. Verbindung fuer Port 1 anlegen:

```json
{
  "device_server_id": 1,
  "port_number": 1,
  "baud_rate": 9600,
  "parity": "N"
}
```

Wenn `tcp_port` nicht angegeben wird, setzt die API fuer die Basis automatisch `4000 + port_number`.

3. Geraet an den Port binden:

```json
{
  "connection_id": 1,
  "quality_state": "configured",
  "is_online": false
}
```

4. Verbindung pruefen:

```text
POST /api/device-connections/1/probe
```

## Start

```powershell
python -m pip install -r requirements.txt
python app.py
```
