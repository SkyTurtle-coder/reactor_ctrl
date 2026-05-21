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
- `GET /api/devices/<device_id>/measurements`
- `GET /api/commands/<command_id>`

## Web-Oberflaeche

Die HTML-Basis ist bewusst serverseitig und schlank gehalten, damit spaeter ohne grossen Umbau eine Bootstrap- oder eigene CSS-Oberflaeche daruebergelegt werden kann.

- `GET /` fuer die Software-Auswahl zwischen `Reactor Control System` und `InfraredCamera`
- `GET /reactor-control`
- `GET /reactor-control-system`
- `GET /infrared-camera`
- `GET /devices`
- `GET /device-servers`
- `GET /device-connections`
- `GET /commands`

Die Templates verwenden bereits eine gemeinsame Layout-Struktur mit Navigation, Karten- und Tabellenblöcken sowie einer separaten Datei `static/css/app.css`.

## API-Authentifizierung

Alle schreibenden API-Endpunkte (`POST`, `PATCH`, `PUT`, `DELETE`) sind fuer den produktiven Betrieb tokenbasiert abgesichert.

- Konfiguration ueber `API_AUTH_REQUIRED=true`
- Token ueber `API_AUTH_TOKEN=<dein-langer-zufallstoken>`
- Header entweder `Authorization: Bearer <token>` oder `X-API-Token: <token>`

Fuer den Reactor Builder wird kein globaler API-Token mehr in die HTML-Seite eingebettet. Die Builder-Seite erzeugt stattdessen serverseitig einen kurzlebigen, auf `POST|PATCH /api/reactor-builds` begrenzten Write-Token.

Beispiel:

```powershell
curl -H "Authorization: Bearer <token>" `
  -H "Content-Type: application/json" `
  -d "{\"asset_serial\":\"R-001\",\"display_name\":\"Reaktor 1\",\"device_type\":\"reactor_component\",\"protocol\":\"generic_text\"}" `
  http://127.0.0.1:5000/api/devices
```

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
  "baud_rate": 115200,
  "data_bits": 8,
  "stop_bits": 1,
  "parity": "N",
  "flow_control": "none"
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

## Messwerte aus Command-Antworten speichern

Ein erfolgreicher RS-232-Command kann optional direkt als `measurement` in SQL persistiert werden.

Beispiel fuer Antworten wie `OK;TEMP_C=24.0`:

```json
{
  "command_name": "query_text",
  "requested_by": "poller",
  "payload": {
    "text": "TEMP?",
    "line_ending": "crlf",
    "expect_response": true,
    "measurement": {
      "channel_code": "temp_c",
      "display_name": "Temperature",
      "unit": "C",
      "parser": "float",
      "key": "TEMP_C",
      "source": "poller"
    }
  }
}
```

Unterstuetzte Parser:

- `text`
- `float`
- `int`
- `bool`

Gespeicherte Messwerte koennen danach ueber `GET /api/devices/<device_id>/measurements` gelesen werden.

## Persistente Prozesslauf-Historie

Der aktuelle Rezeptlauf wird weiterhin in `recipe_program_state` als Live-Zustand gehalten. Zusaetzlich wird jetzt jede Ausfuehrung dauerhaft in SQL protokolliert:

- `recipe_program_run` speichert jeden gestarteten Prozesslauf mit Start-/Endzeit, Status, Recipe/Floatsheet-Referenz und dem Snapshot der verwendeten Bindings und Schritte.
- `recipe_program_event` speichert den zeitlichen Ablauf des Laufs als Event-Log, z. B. `started`, `step_started`, `targets_applied`, `completed`, `stopped` oder `error`.

Damit bleibt nicht nur der aktuelle Zustand sichtbar, sondern auch die Historie vergangener Prozesslaeufe und Sollwertwechsel.

## Lokaler NPort-Simulator

Solange die echte `Moxa NPort 5610-8-DT` noch nicht vorhanden ist, kann ein lokaler Multi-Port-Simulator verwendet werden. Er verhaelt sich wie ein transparenter TCP-Endpunkt fuer `RS-232 over Ethernet` und stellt standardmaessig Ports `4001..4008` bereit.

Start:

```powershell
python run_nport_simulator.py
```

Optional mit eigener Basis-Portnummer:

```powershell
python run_nport_simulator.py --host 127.0.0.1 --base-tcp-port 5000 --port-count 4
```

Standardgeraete pro Port:

- Port 1 -> `tcp://127.0.0.1:4001` -> `Reactor-Sim-01`
- Port 2 -> `tcp://127.0.0.1:4002` -> `Reactor-Sim-02`
- ...

Unterstuetzte Textkommandos:

- `PING`
- `HELP?`
- `IDENT?`
- `STATUS?`
- `TEMP?`
- `PRESSURE?`
- `START`
- `STOP`
- `TEMP=<wert>`
- `PRESSURE=<wert>`

Beispiel fuer die API mit Simulator statt echter Moxa:

```json
{
  "server_code": "SIM-01",
  "display_name": "Local NPort Simulator",
  "host": "127.0.0.1"
}
```

Danach kann eine `device_connection` fuer `port_number: 1` angelegt werden; der Standard-TCP-Port wird automatisch auf `4001` gesetzt.

## Produktive Moxa-Konfiguration

Fuer die produktive NPort-Anbindung verwendet die Software pro Port eine direkte TCP-Socket-Verbindung auf den Datenport des Moxa-Geraets. Die Web- oder Management-Schnittstelle des NPort wird fuer die eigentliche Geraetekommunikation nicht verwendet.

Die TCP-Abbildung bleibt pro Port gleich, die seriellen Parameter sind jedoch **geraetespezifisch** und muessen immer dem Herstellerprotokoll des angeschlossenen RS-232-Geraets entsprechen.

Moxa-Weboberflaeche pro Port:

- `Interface = RS-232`
- `FIFO = Enable`
- `Operating mode = TCP Server`

Typische serielle Parameter wie `baud_rate`, `data_bits`, `parity`, `stop_bits` und `flow_control` werden pro Geraetetyp gesetzt und anschliessend in `device_connection` gespiegelt.

App-seitiges Port-Mapping:

- Port `1` -> TCP `4001`
- Port `2` -> TCP `4002`
- Port `3` -> TCP `4003`
- Port `4` -> TCP `4004`
- Port `5` -> TCP `4005`
- Port `6` -> TCP `4006`
- Port `7` -> TCP `4007`
- Port `8` -> TCP `4008`

Fuer die wiederholbare Einrichtung des Device-Servers und aller `8` Verbindungen gibt es ein CLI-Skript:

```powershell
python configure_moxa_nport.py `
  --base-url http://127.0.0.1:5000 `
  --api-token <token> `
  --host 10.90.95.178 `
  --server-code MOXA-01 `
  --display-name "Moxa NPort 5610-8-DT" `
  --probe
```

Das Skript legt den `device_server` an oder aktualisiert ihn und provisioniert danach alle Ports mit:

- `transport_type = tcp_socket`
- `serial_standard = rs232`
- `baud_rate = 115200` (Default, bei Bedarf per CLI ueberschreiben)
- `data_bits = 8` (Default)
- `parity = N` (Default)
- `stop_bits = 1` (Default)
- `flow_control = none` (Default)
- `tcp_port = 4000 + port_number`

Wenn dein NPort eine andere IP oder andere serielle Parameter verwendet, koennen diese direkt ueber die CLI-Argumente angepasst werden.

Beispiel fuer einen IKA-Port mit `9600 / 7E1 / none`:

```powershell
python configure_moxa_nport.py `
  --base-url http://127.0.0.1:5000 `
  --api-token <token> `
  --host 10.90.95.178 `
  --server-code MOXA-01 `
  --display-name "Moxa NPort 5610-8-DT" `
  --baud-rate 9600 `
  --data-bits 7 `
  --parity E `
  --stop-bits 1 `
  --flow-control none `
  --probe
```

## Huber Unistat / Pilot ONE ueber RS-232

Fuer Huber Unistat / Pilot ONE wird das Huber PB-Protokoll verwendet. Laut Huber-Datenkommunikationshandbuch sind die RS-232-Parameter:

- `Baud rate = 9600`
- `Data bits = 8`
- `Parity = None`
- `Stop bits = 1`
- `Handshake = None`
- `Encoding = ascii`
- `Line ending = CRLF`

Moxa-Port fuer Huber entsprechend provisionieren:

```powershell
python configure_moxa_nport.py `
  --base-url http://127.0.0.1:5000 `
  --api-token <token> `
  --host 10.90.95.178 `
  --server-code MOXA-01 `
  --display-name "Moxa NPort 5610-8-DT" `
  --device-preset huber_unistat_430 `
  --only-port 1 `
  --probe
```

Falls nur ein einzelner Huber-Port getestet werden soll, zuerst die Moxa-Weboberflaeche fuer diesen Port auf `TCP Server`, `RS-232`, `9600 / 8N1`, `Flow control = None` setzen. Danach kann ohne Datenbank/API ein Lesetest gemacht werden:

```powershell
python run_huber_smoke_test.py --host 10.90.95.178 --port 4001 --command get_internal_temp
```

Der Smoke-Test sendet nur ein PB-Lesekommando, z. B. `{M01****<CR><LF>`, und erwartet eine Antwort wie `{S0109C4<CR><LF>`. `09C4` entspricht `25.00 C`.

Unterstuetzte Huber-Kommandos im App-Treiber:

- `get_setpoint`
- `get_internal_temp`
- `get_return_temp`
- `get_pump_pressure`
- `get_process_temp`
- `get_error`
- `get_warning`
- `get_status`
- `set_setpoint`
- `start`
- `stop`
- `set_circulation`
- `clear_error`
- `clear_warning`
- `read_var` / `write_var` fuer rohe PB-Adressen

## Validierter IKA EUROSTAR 60 Betrieb

Die erste reale Inbetriebnahme wurde erfolgreich mit einem `IKA EUROSTAR 60` ueber `MOXA-01 / Port 1 / TCP 4001` verifiziert.

Validierte serielle Parameter:

- `Baud rate = 9600`
- `Data bits = 7`
- `Parity = Even`
- `Stop bits = 1`
- `Flow ctrl = None`
- `Encoding = ascii`
- `Line ending = blank + CRLF`

Validierte IKA-Kommandos:

- `IN_NAME` -> Antwort z. B. `IKA ES 60`
- `IN_MODE` -> Antwort z. B. `IN_MODE_1`
- `IN_SP_4` -> Sollwert Drehzahl
- `IN_PV_4` -> Istwert Drehzahl
- `IN_PV_5` -> weiterer Geraeterueckkanal
- `START_4`
- `OUT_SP_4 <wert>`
- `STOP_4`

Wichtige Betriebsbeobachtungen aus dem Realtest:

- Die echte Bewegung wird ueber `IN_PV_4` nachgewiesen, nicht ueber `IN_SP_4`.
- `IN_SP_4` bestaetigt nur den Sollwert, z. B. `300.0 4`.
- Nach `START_4` plus `OUT_SP_4 300` lieferte `IN_PV_4` real `299.07 4`.
- Schreibkommandos ohne Rueckantwort erscheinen API-seitig als `acked`; die fachliche Wirkung wird anschliessend mit einem Lesekommando geprueft.

Empfohlene Reihenfolge fuer den ersten Porttest:

1. `IN_NAME`
2. `IN_MODE`
3. `START_4`
4. `OUT_SP_4 <wert>`
5. `IN_SP_4`
6. `IN_PV_4`
7. `STOP_4`

## Wiederholbarer Funktionstest

Der End-to-End-Test prueft genau den spaeter benoetigten Zielpfad:

`RS-232-Geraet -> Moxa/NPort TCP-Port -> Flask-Server -> SQL measurement`

Start gegen den lokalen Server:

```powershell
python run_function_test.py --base-url http://127.0.0.1:5000 --api-token <token> --server-host 127.0.0.1
```

Spaeter gegen die echte Moxa nur `--server-host` anpassen:

```powershell
python run_function_test.py --base-url http://127.0.0.1:5000 --api-token <token> --server-host 192.168.1.50 --port-number 1
```

## Start

```powershell
python -m pip install -r requirements.txt
python app.py
```

Der Befehl oben ist nur fuer lokale Entwicklung gedacht. Fuer einen Server, der durchgaengig laufen und sich bei Fehlern automatisch neu starten soll, wird `gunicorn` unter `systemd` verwendet.

## Dauerbetrieb auf Linux-Server

Die produktive Startkette ist:

`systemd -> gunicorn -> Flask app`

Vorbereitung:

```bash
cd /home/pthuerlemann/reactor_ctrl
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Service-Datei installieren:

```bash
sudo cp deploy/reactor_ctrl.service /etc/systemd/system/reactor_ctrl.service
sudo systemctl daemon-reload
sudo systemctl enable --now reactor_ctrl
```

Status und Logs:

```bash
sudo systemctl status reactor_ctrl
sudo journalctl -u reactor_ctrl -f
```

Manuelles Neuladen nach einem `git pull`:

```bash
sudo systemctl restart reactor_ctrl
```

Die Gunicorn-Konfiguration liegt in `gunicorn.conf.py` und bindet standardmaessig an `127.0.0.1:5000`. Damit bleibt der Dienst sauber im Hintergrund aktiv, auch wenn die SSH-Verbindung getrennt wird.

## Automatischer Datenbank-Backup

Der Server kann taeglich einen komprimierten SQL-Dump erzeugen, der danach vom zentralen Backup-System gesichert werden kann. Der Dump-Runner liest `DATABASE_URL` aus `.env`, schreibt keine Passwoerter in die Prozessargumente und verwendet `mariadb-dump`/`mysqldump` mit `--single-transaction`, damit der Betrieb nicht durch Tabellen-Locks blockiert wird.

Standardziel:

```bash
/home/pthuerlemann/backups/reactor_ctrl/sql
```

Optionale `.env`-Werte:

```bash
DB_BACKUP_DIR=/home/pthuerlemann/backups/reactor_ctrl/sql
DB_BACKUP_RETENTION_DAYS=30
DB_BACKUP_TIMEOUT_SECONDS=1800
DB_BACKUP_DUMP_BINARY=mariadb-dump
```

Systemd-Timer installieren:

```bash
cd /home/pthuerlemann/reactor_ctrl
sudo install -m 644 deploy/reactor_ctrl_db_backup.service /etc/systemd/system/reactor_ctrl_db_backup.service
sudo install -m 644 deploy/reactor_ctrl_db_backup.timer /etc/systemd/system/reactor_ctrl_db_backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now reactor_ctrl_db_backup.timer
```

Testlauf und Kontrolle:

```bash
sudo systemctl start reactor_ctrl_db_backup.service
sudo systemctl status reactor_ctrl_db_backup.service --no-pager
systemctl list-timers reactor_ctrl_db_backup.timer --no-pager
ls -lh /home/pthuerlemann/backups/reactor_ctrl/sql
```

Der Timer laeuft taeglich um 23:30 Uhr mit einer kleinen zufaelligen Verzoegerung. `Persistent=true` sorgt dafuer, dass ein verpasster Lauf nach dem naechsten Serverstart nachgeholt wird.

## Server-Hinweis

Beim App-Start werden fehlende Tabellen standardmaessig automatisch angelegt (`AUTO_CREATE_SCHEMA=true`). Das verhindert, dass ein Deployment zwar den Code aktualisiert, aber neue Tabellen wie `device_server` oder `device_connection` in MySQL noch fehlen.

Beim Upgrade von der frueheren USB-/RS-485-Struktur werden veraltete Tabellen `device_binding_current` und `device_binding_history` automatisch als Backup unter Namen wie `device_binding_current_legacy_rs485` archiviert und danach im aktuellen NPort-Format neu erstellt. Die alten Binding-Daten bleiben damit erhalten, werden aber nicht mehr aktiv verwendet.

Fuer produktive Deployments sollte `FLASK_DEBUG` auf `false` bleiben, `SECRET_KEY` nicht auf dem Default stehen und `API_AUTH_TOKEN` gesetzt sein. Falls ein vorhandenes Datenbankschema noch aus der alten USB/RS-485-Struktur stammt, muessen die Legacy-Tabellen manuell auf das aktuelle NPort-Schema umgestellt werden.
