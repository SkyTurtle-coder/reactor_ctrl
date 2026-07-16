# Mettler Toledo ICS435 MT-SICS Ethernet Driver

## Zweck

Der Treiber `mettler_toledo_ics435` bindet eine Mettler Toledo ICS435 ueber COM2/Ethernet an `reactor_ctrl` an. Die Anwendung ist TCP-Client, die Waage laeuft im TCP-Server-Modus. Die Kommunikation nutzt MT-SICS ASCII-Telegramme mit `CR LF` (`\r\n`).

Der Treiber verwendet keine serielle Bibliothek und kein `pyserial`. Er nutzt den bestehenden `tcp_socket`-Transport, die zentrale Runtime-Queue, die vorhandenen Prioritaeten und den bestehenden Device-Lock.

## Offizielle Quellen

- Mettler Toledo, `ICS425/ICS429/ICS435/ICS439 User Manual`, `30243626_F_MAN_UM_ICS42_-ICS43_en.pdf`: https://www.mt.com/dam/ind/BenchScales/User-Manuals/ICS4_9/30243626_F_MAN_UM_ICS42_-ICS43_en.pdf
- Mettler Toledo, `MT-SICS Interface Command Reference Manual`, `11781363_La_MAN_RM_MTSICS-APW_en.pdf`: https://www.mt.com/dam/product_organizations/industry/apw/generic/11781363_La_MAN_RM_MTSICS-APW_en.pdf

## Verifizierte Protokolldetails

- ICS435 gehoert zur ICS42x/43x-Familie; COM2 kann als optionale Ethernet-Schnittstelle konfiguriert sein.
- Fuer PC-gesteuerte SICS-Kommandos muss das Waagenterminal als `TCP Server` laufen; die Python-Anwendung verbindet sich als TCP-Client.
- Der im ICS-Handbuch gezeigte lokale Ethernet-Port ist `4305`; er ist am Geraet konfigurierbar und daher in der App nicht hart codiert.
- MT-SICS-Kommandos sind ASCII, nutzen Uppercase-Kommandonamen und enden mit `CR LF`.
- Gewichtswertantworten haben das Muster `S <Status> <WeightValue> <Unit>`.
- Status `S` bedeutet stabil, `D` dynamisch/instabil.
- Allgemeine Fehler sind unter anderem `ES` Syntax/Command, `ET` Uebertragungsfehler und `EL` logischer Fehler.
- Befehlsspezifische Fehler enthalten unter anderem `I` nicht momentan ausfuehrbar, `L` logischer Fehler/Parameterfehler, `+` Ueberlast und `-` Unterlast.
- `I0` ist mehrzeilig: Zwischenzeilen nutzen Status `B`, die letzte Zeile Status `A`.
- `I4` kann nach Einschalten oder Cancel unaufgefordert erscheinen; der Treiber ueberspringt solche unaufgeforderten Zeilen, bis die Antwort zum aktuellen Befehl kommt.

## Unterstuetzte Driver-Kommandos

- `initialize` / `identify` / `read_device_info`: liest `I4`, `I3`, `I2`, `I1`, danach `I0`.
- `list_commands` / `supported_commands` / `i0`: liest die per `I0` gemeldeten Kommandos.
- `read_weight` / `get_weight` / `weight` / `read_live_telemetry`: liest per `SI`, ausser `payload.weight_command` ist `S`.
- `read_stable_weight` / `get_stable_weight`: liest per `S`.
- `tare` / `t`: sendet `T`.
- `clear_tare` / `tac`: sendet `TAC`.
- `zero` / `z`: sendet `Z`.
- `raw` / `send_raw` / `manual_text`: sendet ein explizites ASCII-Kommando fuer Diagnosezwecke.

`T`, `TAC` und `Z` werden nie automatisch beim Verbindungsaufbau oder Polling gesendet.

## Konfiguration

Die TCP-Zieladresse liegt wie bei anderen Geraeten in `device_server` und `device_connection`:

```json
{
  "server_code": "ICS435-01",
  "display_name": "Mettler Toledo ICS435",
  "vendor": "Mettler Toledo",
  "model": "ICS435",
  "host": "<WAAGEN-IP>",
  "serial_standard": "ethernet",
  "port_count": 1
}
```

```json
{
  "device_server_id": 1,
  "port_number": 1,
  "connection_label": "COM2 Ethernet",
  "transport_type": "tcp_socket",
  "tcp_host": "<WAAGEN-IP>",
  "tcp_port": 4305,
  "read_timeout_ms": 1200,
  "write_timeout_ms": 1200,
  "reconnect_delay_ms": 1000,
  "is_enabled": true
}
```

```json
{
  "asset_serial": "ICS435-01",
  "display_name": "ICS435 Balance",
  "device_type": "scale",
  "protocol": "mettler_toledo_ics435",
  "is_active": true
}
```

Fuer die aktuelle Laborwaage mit IP `192.168.55.29` kann diese Konfiguration
idempotent ueber das Projekt-Skript angelegt oder aktualisiert werden:

```bash
python configure_ics435_scale.py --host 192.168.55.29 --probe
```

Das Skript verwendet standardmaessig:

- `server_code`: `ICS435-01`
- `connection_label`: `COM2 Ethernet`
- `tcp_host`: `192.168.55.29`
- `tcp_port`: `4305`
- `device_type`: `scale`
- `protocol`: `mettler_toledo_ics435`

Falls die IP spaeter wechselt, bleibt die Builder-Zuordnung gleich; nur dieses
Provisioning-Skript wird mit der neuen `--host`-Adresse erneut ausgefuehrt.

Optionale `.env`-Werte:

```bash
ICS435_POLLER_INTERVAL_MS=1000
ICS435_RESPONSE_TIMEOUT_MS=1200
ICS435_CONNECT_TIMEOUT_MS=3000
ICS435_WRITE_TIMEOUT_MS=1200
ICS435_MAX_RETRIES=1
ICS435_RETRY_DELAY_MS=250
ICS435_WEIGHT_COMMAND=SI
ICS435_LOG_RAW_TELEGRAMS=false
```

## Polling und Messwerte

Der Manual-Reconciler seedet aktive ICS435-Geraete automatisch und pollt sie mit `CommandPriority.POLLING`. Pending Polls werden von wichtigeren Befehlen verdrangt. Bei aktivem Device-Lock wird Polling uebersprungen oder spaeter wieder versucht.

Persistierter Measurement-Channel:

- `channel_code`: `weight`
- `display_name`: `Weight`
- `numeric_value`: Gewichtswert
- `unit`: Einheit aus der Waagenantwort
- `quality_score`: `1.0` fuer stabil, `0.5` fuer dynamisch
- `raw_payload`: Rohantwort, Stabilitaet, Einheit und Driver-Metadaten

## Verbindungsmanagement

Der Treiber markiert `persistent_transport = True`. `device_runtime` haelt dadurch pro Prozess einen persistenten `tcp_socket` fuer denselben DeviceConnection-/Timeout-Key offen. Bei Timeout, Runtime-Cancellation oder Socket-Fehler wird der Socket aus dem Cache entfernt und beim naechsten Befehl neu verbunden.

Die Produktion nutzt laut `gunicorn.conf.py` `workers = 1`; dadurch oeffnet nicht jeder Gunicorn-Prozess eine eigene permanente Verbindung. Bei einer spaeteren Umstellung auf mehrere Prozesse muss die Runtime externalisiert oder ein pro Device eindeutiger Verbindungseigentuemer eingefuehrt werden.

## Diagnoseschritte

Linux:

```bash
ping <WAAGEN-IP>
nc -vz <WAAGEN-IP> <PORT>
printf 'I4\r\n' | nc -w 2 <WAAGEN-IP> <PORT>
printf 'I0\r\n' | nc -w 2 <WAAGEN-IP> <PORT>
printf 'SI\r\n' | nc -w 2 <WAAGEN-IP> <PORT>
```

PowerShell:

```powershell
Test-Connection <WAAGEN-IP> -Count 4
Test-NetConnection <WAAGEN-IP> -Port <PORT>
```

## Erster Hardwaretest

1. Am Terminal pruefen: COM2 ist Ethernet, Dialogmodus/SICS ist aktiv, TCP Mode ist `Server`, Local Port ist bekannt.
2. `ping <WAAGEN-IP>` ausfuehren.
3. TCP-Port pruefen: `nc -vz <WAAGEN-IP> <PORT>` oder PowerShell `Test-NetConnection`.
4. Nur lesend starten: `printf 'I4\r\n' | nc -w 2 <WAAGEN-IP> <PORT>`.
5. `I2` und `I3` abfragen.
6. `I0` vollstaendig auslesen und die unterstuetzten Kommandos dokumentieren.
7. Einmalig `SI` senden.
8. Rohantwort exakt dokumentieren, inklusive Leerzeichen, Status und Einheit.
9. Gewicht auflegen und `SI` erneut senden; Status `S`/`D` beobachten.
10. Erst danach gezielt `T`, `TAC` und `Z` testen.

## Bekannte offene Punkte

- Die tatsaechlich installierte COM2-Option, Firmware und Portnummer muessen am realen Geraet geprueft werden.
- Die konkrete Kommandoverfuegbarkeit muss per `I0` am realen Geraet bestaetigt werden.
- Verhalten bei mehreren gleichzeitigen TCP-Verbindungen muss am Geraet verifiziert werden; die App ist auf eine Verbindung pro Worker/Device ausgelegt.
- `SIR` wird bewusst nicht als Standard genutzt, weil request-response Polling sauberer mit Queue, Tara und Nullstellen koordinierbar ist.
