# Live Acquisition Analysis

Gate: OK. The production configuration in this repository starts exactly one Gunicorn worker process (`gunicorn.conf.py:6`) with four threads (`gunicorn.conf.py:7`), and the systemd unit uses that config (`deploy/reactor_ctrl.service:12`). A process-local RAM snapshot is therefore architecturally possible under the current deployment, but only while `workers = 1` remains true.

Context used for this analysis:

- Repo path: `C:\Users\ma1166514\OneDrive - FHNW\WORK_FHNW\reactor_ctrl`
- Web framework: Flask (`app.py`, `reactor_app/__init__.py`)
- DB: SQLAlchemy with MySQL/PyMySQL by default (`config.py:43`, `config.py:47`)
- Deployment: systemd -> Gunicorn (`deploy/reactor_ctrl.service:12`)
- Test command: `pytest -q`
- Lint command: not configured
- Hardware availability in this workspace: none
- Production logs: systemd journal on `v002020`, expected via `journalctl -u reactor_ctrl`

## A) Trigger-Matrix

| Trigger | Geraet | Codepfad (datei:zeile) | loest Hardware-I/O aus? | tatsaechliche Frequenz | Beleg |
|---|---|---|---|---|---|
| Process-Display tick | Alle ausgewaehlten Measurement-Series | `static/js/process_view.js:2284`, `static/js/process_view.js:2319`, `static/js/process_view.js:2302`, `static/js/process_view.js:2344` | indirekt | Browser alle 1500 ms (`static/js/process_view.js:96`, `static/js/process_view.js:4748`) | Display liest zuerst `/api/plot-series/live`, verlaengert danach Watches und mischt Snapshot-Fallbacks aus `/manual-state` ein. Keine direkte Treiber-I/O im Display-Request. |
| Plot-Tick | Alle ausgewaehlten Measurement-Series | `static/js/process_view.js:2365`, `static/js/process_view.js:2417`, `static/js/process_view.js:2400`, `static/js/process_view.js:2401` | indirekt | Browser alle 1000 ms (`static/js/process_view.js:88`, `static/js/process_view.js:4737`) | Plot liest Measurement-Rows, laedt Snapshot-Punkte und verlaengert Watches. |
| Plot stale/no-data refresh | Geraete mit stale/no-data Series | `static/js/process_view.js:2453`, `static/js/process_view.js:2477`, `static/js/process_view.js:94`, `static/js/process_view.js:93` | indirekt | Nur bei stale/no-data; Cooldown 15000 ms, Await 1200 ms | Der Browser ruft `/manual-state` mit `refresh=1` und `await_ms=1200` auf. Das setzt serverseitig `next_poll_at = now`. |
| Measurement-History-Endpoint fuer Plot | Kein direktes Geraet | `reactor_app/api.py:1823`, `reactor_app/services/measurement_plot.py:273`, `reactor_app/services/measurement_plot.py:351`, `reactor_app/services/measurement_plot.py:601` | nein | Durch Display/Plot-Ticks: 1000 ms bzw. 1500 ms | `/api/plot-series/live` liest nur `Measurement`-Rows und kann mit `history_fallback=True` bis zum Lookback zurueckgehen (`reactor_app/api.py:1856`, `reactor_app/services/measurement_plot.py:677`). |
| Manuelle Geraeteseite, Huber im Process-View | Huber | `static/js/process_view.js:3813`, `static/js/process_view.js:3824`, `static/js/process_view.js:3838`, `static/js/process_view.js:3845`, `static/js/process_view.js:3848` | ja | Browser alle 1500 ms (`static/js/process_view.js:84`, `static/js/process_view.js:4724`) | Huber liest teils `/manual-state?watch=1`, ruft aber auch `executeDeviceCommand()` fuer `get_setpoint`, `get_status`, Temperaturbefehle auf; der API-Pfad dispatcht synchron (`reactor_app/api.py:2030`). |
| Manuelle Geraeteseite, Waage | ICS435 | `static/js/process_view.js:3911`, `static/js/process_view.js:3939`, `static/js/process_view.js:3951`, `static/js/process_view.js:4037` | indirekt | Browser alle 1500 ms | Waage nutzt `/manual-state` und optional `await_ms`, keine direkte `read_weight`-I/O aus dem Browser. |
| Manuelle Geraeteseite, IKA | IKA Eurostar | `static/js/process_view.js:4033`, `reactor_app/services/device_manual_runtime.py:1091`, `reactor_app/services/device_manual_runtime.py:1098`, `reactor_app/services/device_manual_runtime.py:1104` | indirekt bis ja, je nach Pfad | Browser alle 1500 ms; Reconciler-Poll nach Manual-State-Plan | IKA-Telemetrie wird im Manual-Reconciler ueber `IN_SP_4`, `IN_PV_4`, `IN_PV_5` gelesen. Browser soll ueber State konsumieren; direkte Command-Buttons koennen weiterhin `/commands` nutzen. |
| Recipe-Scheduler | Huber, IKA, konfigurierte Actor | `reactor_app/services/recipe_program_runtime.py:208`, `reactor_app/services/recipe_program_runtime.py:1644`, `reactor_app/services/recipe_program_runtime.py:1699`, `reactor_app/services/recipe_program_runtime.py:1710` | ja | Reconciler-Loop mindestens 250 ms, default 1000 ms (`config.py:73`); Huber Setpoint min. 10 s bzw. 0.2 degC (`config.py:79`, `config.py:80`) | Recipe-Kommandos laufen mit `CommandPriority.RECIPE` und koennen Polling verdrangen. Huber-Sequenzen halten einen Device-Sequence-Lock. |
| Watchdog | Huber / Geraete | unbestaetigt | unbestaetigt | nicht feststellbar | Code-Suche nach `watchdog`, `WATCHDOG`, `WDT`, `communication watchdog` in App-Code ergab keinen belastbaren Geraete-Watchdog-Pfad. Safe-state/Recipe-Stop ist vorhanden, aber kein Kommunikations-Watchdog. |
| Hintergrund-Loop Manual-Reconciler | IKA, Huber, ICS435 | `reactor_app/services/device_manual_runtime.py:78`, `reactor_app/services/device_manual_runtime.py:1842`, `reactor_app/services/device_manual_runtime.py:2042`, `reactor_app/services/device_manual_runtime.py:2221`, `reactor_app/services/device_manual_runtime.py:2226`, `reactor_app/services/device_manual_runtime.py:2245` | ja | Loop 500 ms default (`config.py:68`), pro Loop maximal ein Device-Claim | Der Reconciler claimed ein Device und verarbeitet es. Aktive Watches pollt er mindestens alle 1000 ms (`reactor_app/services/device_manual_runtime.py:263`), Waage mindestens alle 500 ms (`reactor_app/services/device_manual_runtime.py:287`). |
| `refresh=1` / `await_ms` | DeviceManualState-Geraete | `reactor_app/api.py:1931`, `reactor_app/api.py:1932`, `reactor_app/api.py:1955`, `reactor_app/services/device_manual_runtime.py:899`, `reactor_app/services/device_manual_runtime.py:914`, `reactor_app/services/device_manual_runtime.py:916`, `reactor_app/services/device_manual_runtime.py:994` | indirekt | Durch HTTP-Caller; serverseitig `next_poll_at = now`, optional HTTP-Warten bis Timeout | Ein HTTP-Request fuehrt nicht selbst die serielle Transaktion aus, kann aber einen Poll sofort faellig machen und auf frischen State warten. |

## B) Nebenlaeufigkeit

Es gibt drei relevante Schutzebenen:

- Runtime-Scheduler: pro `device_id` darf nur ein aktiver Runtime-Command laufen. Das wird ueber `_active_by_device` abgebildet (`reactor_app/services/runtime_scheduler.py:123`), beim Claim geprueft (`reactor_app/services/runtime_scheduler.py:183`), beim Start gesetzt (`reactor_app/services/runtime_scheduler.py:237`) und beim Ende geloescht (`reactor_app/services/runtime_scheduler.py:259`).
- Device-Lock: echte Geraetekommunikation geht durch `_device_command_lock()` (`reactor_app/services/device_runtime.py:575`). Bei MySQL/MariaDB wird `GET_LOCK()` verwendet (`reactor_app/services/device_runtime.py:581`), sonst ein lokaler `RLock` (`reactor_app/services/device_runtime.py:606`). Der oeffentliche Sequenz-Lock liegt in `device_command_sequence_lock()` (`reactor_app/services/device_runtime.py:620`).
- Recipe-Sequenzen: Huber-Rezepte halten den Sequence-Lock fuer mehrstufige Kommandos (`reactor_app/services/recipe_program_runtime.py:1699`) und setzen interne Einzelkommandos mit bereits gehaltenem Lock ab (`reactor_app/services/recipe_program_runtime.py:1710`).

Der gemeinsame Scheduler hat mehrere Worker-Threads (`config.py:81`, `reactor_app/services/command_dispatcher.py:502`, `reactor_app/services/runtime_scheduler.py:589`), aber die genannten Guards verhindern parallele Runtime-Commands fuer dasselbe `device_id`.

Restrisiko: der Lock ist nach `device_id` aufgebaut, nicht nach physischem MOXA-Port. Wenn zwei Device-Rows faelschlich auf dieselbe `device_connection` bzw. denselben NPort-Port gebunden sind, koennen zwei verschiedene `device_id`s denselben seriellen Port parallel benutzen. Externe Smoke-Tests oder Shell-Skripte gegen `10.90.95.178:4004` laufen ebenfalls ausserhalb dieser Locks.

Polling ist absichtlich nachrangig:

- Prioritaeten: `RECIPE=3`, `MANUAL=5`, `POLLING=9` (`reactor_app/services/command_model.py:49`, `reactor_app/services/command_model.py:50`, `reactor_app/services/command_model.py:51`).
- Polling-Timeouts: Queue 2 s, Execution 10 s, Total 15 s (`reactor_app/services/command_dispatcher.py:80`).
- Neue Polls ueberschreiben alte pending Polls desselben Devices (`reactor_app/services/runtime_scheduler.py:399`, `reactor_app/services/runtime_scheduler.py:408`).
- Hoeher priorisierte Commands skippen pending Polls (`reactor_app/services/runtime_scheduler.py:412`, `reactor_app/services/runtime_scheduler.py:421`).

Das schuetzt Control-Kommandos, kann Measurement-Polls unter Recipe-/Manual-Last aber verzoegern oder verwerfen.

## C) Deployment

Belegt aus echter Deployment-Konfiguration im Repo:

- `gunicorn.conf.py:6`: `workers = 1`
- `gunicorn.conf.py:7`: `threads = 4`
- `deploy/reactor_ctrl.service:12`: systemd startet `gunicorn --config /home/pthuerlemann/reactor_ctrl/gunicorn.conf.py app:app`

Beim App-Start werden Runtime-Scheduler, Manual-Reconciler und Recipe-Reconciler im selben Prozess gestartet (`reactor_app/__init__.py:411`, `reactor_app/__init__.py:412`, `reactor_app/__init__.py:413`). Prozesslokale Strukturen sind deshalb aktuell fuer HTTP-Threads und Hintergrundthreads sichtbar:

- Scale Live Snapshot: `reactor_app/services/device_manual_runtime.py:65`
- Persistent TCP Transports: `reactor_app/services/device_runtime.py:42`
- Runtime active queue state: `reactor_app/services/runtime_scheduler.py:123`

Konsequenz: RAM-Snapshots sind unter dem aktuellen Deployment valide. Sie werden falsch, sobald `workers > 1` gesetzt wird, weil jeder Worker eigene Threads, Queues, Transport-Caches und Snapshots haette. Die Warnung dazu steht bereits in `gunicorn.conf.py:3`.

## D) Latenzbudget pro Geraet

Serielle Basisrechnung: 9600 Baud, 8N1 bedeutet 10 Bit pro Zeichen. Damit dauert ein Zeichen ca. `10 / 9600 = 1.04 ms`.

### ICS435 Waage

Pfad: Manual-Reconciler -> `_read_scale_status()` -> `read_live_telemetry` -> MT-SICS `SI`/`S` -> `receive_until("\n")` -> Parser -> `reported_extra`/Measurement.

- Default-Kommando: `SI` (`config.py:93`, `reactor_app/services/device_manual_runtime.py:1162`, `reactor_app/services/drivers/mettler_toledo_ics435.py:507`).
- Erlaubt: `S` oder `SI`; `SIR` wird nicht als Live-Poll erlaubt (`reactor_app/services/drivers/mettler_toledo_ics435.py:509`), nur beim Response-Matching erkannt (`reactor_app/services/drivers/mettler_toledo_ics435.py:408`).
- Terminator: sendet CRLF (`reactor_app/services/drivers/mettler_toledo_ics435.py:339`), liest bis LF (`reactor_app/services/drivers/mettler_toledo_ics435.py:355`).
- Default Timeouts/Retry: response 1200 ms, connect 3000 ms, write 1200 ms, max retries 1, retry delay 250 ms (`config.py:88`, `config.py:89`, `config.py:90`, `config.py:91`, `config.py:92`).
- Poll-Intervall: mindestens 500 ms (`config.py:87`, `reactor_app/services/device_manual_runtime.py:287`).

Leitung: `SI\r\n` = 4 Zeichen = ca. 4.2 ms. Eine typische Antwort mit ca. 15 bis 25 Zeichen liegt bei ca. 16 bis 26 ms. Reine serielle Uebertragung liegt damit deutlich unter 50 ms. Sekundenlange Polls stecken nicht in 9600 Baud, sondern in Terminator-Wartezeit, Queue/Lock-Wartezeit, Retry, NPort/TCP oder einem `S`-Kommando, falls `.env` `ICS435_WEIGHT_COMMAND=S` setzt.

### Huber Ministat cc

Pfad: Manual-Reconciler -> `_read_huber_status()` -> `read_live_telemetry` -> PP-Commands -> `receive_until("\n")` -> Temperaturparser -> `reported_extra`/Measurement.

- Preset: 9600 8N1, no flow control, read/write 1200 ms (`configure_moxa_nport.py:51`, `configure_moxa_nport.py:57`, `configure_moxa_nport.py:58`).
- Poll-Kommandos: `SP?`, `TI?`, `TE?` (`reactor_app/services/drivers/huber_ministat_cc.py:273`, `reactor_app/services/drivers/huber_ministat_cc.py:276`, `reactor_app/services/drivers/huber_ministat_cc.py:279`).
- Optional Status: `TEMP?`, dann optional `CA?` (`reactor_app/services/drivers/huber_ministat_cc.py:294`, `reactor_app/services/drivers/huber_ministat_cc.py:299`), wird im schnellen Poll deaktiviert (`reactor_app/services/device_manual_runtime.py:1143`, `reactor_app/services/device_manual_runtime.py:1144`).
- Optional Fehler: `FSW?` (`reactor_app/services/drivers/huber_ministat_cc.py:307`), nicht Teil des schnellen Polls.
- Terminator: sendet CRLF (`reactor_app/services/drivers/huber_ministat_cc.py:225`), liest bis LF (`reactor_app/services/drivers/huber_ministat_cc.py:235`).
- Response-Timeout schneller Poll: 1200 ms (`reactor_app/services/device_manual_runtime.py:90`, `reactor_app/services/device_manual_runtime.py:1143`).

Leitung: ein PP-Befehl wie `SP?\r\n` = 5 Zeichen = ca. 5.2 ms. Eine typische Antwort wie `TI +00018\r\n` liegt bei ca. 11 Zeichen = ca. 11.5 ms. Drei Poll-Kommandos liegen netto grob unter 60 ms. Wenn ein Ministat-Poll Sekunden dauert, wartet er auf Terminator/Timeout, Queue/Lock, Verbindungsaufbau, Retry oder auf eine spaete/stale Antwort, nicht auf Leitungskapazitaet.

### Huber CC230 / Unistat PB

CC230 ist nicht der neue Ministat-Pfad, bleibt aber relevant fuer die Huber-Familie:

- CC230 hat einen festen Inter-Command-Delay von 0.20 s (`reactor_app/services/drivers/huber_cc230.py:37`) und nutzt ihn in `read_live_telemetry()` (`reactor_app/services/drivers/huber_cc230.py:497`).
- Schneller CC230-Poll nutzt 2500 ms Response-Timeout (`reactor_app/services/device_manual_runtime.py:89`, `reactor_app/services/device_manual_runtime.py:1140`).
- Unistat/Pilot nutzt PB-Frames (`reactor_app/services/drivers/huber_unistat.py:490`, `reactor_app/services/drivers/huber_unistat.py:529`) und hat im Manual-Reconciler 800 ms Response-Timeout (`reactor_app/services/device_manual_runtime.py:91`, `reactor_app/services/device_manual_runtime.py:1147`).

Hier koennen fixe Sleeps und Fallback-Ketten relevante Kosten erzeugen. Fuer den Ministat cc ist das nicht der dominante Pfad.

### IKA Eurostar

Pfad: Manual-Reconciler -> `_read_ika_status()` -> drei `manual_text`-Reads -> IKA line protocol -> `receive_until("\r\n")` -> Parser -> State/Measurement.

- Poll-Kommandos: `IN_SP_4`, `IN_PV_4`, `IN_PV_5` (`reactor_app/services/device_manual_runtime.py:1091`, `reactor_app/services/device_manual_runtime.py:1098`, `reactor_app/services/device_manual_runtime.py:1104`).
- Payload: `space_crlf`, Antwortterminator `crlf` fuer `IN_`-Commands (`reactor_app/services/device_manual_runtime.py:297`, `reactor_app/services/device_manual_runtime.py:302`, `reactor_app/services/device_manual_runtime.py:303`).
- Driver liest bis Terminator (`reactor_app/services/drivers/ika_eurostar.py:116`).
- Moxa-Preset fuer IKA: 9600 7E1, read 5000 ms, write 2000 ms (`configure_moxa_nport.py:63`, `configure_moxa_nport.py:65`, `configure_moxa_nport.py:66`, `configure_moxa_nport.py:45`, `configure_moxa_nport.py:46`).

Leitung: drei kurze ASCII-Commands liegen netto deutlich unter 100 ms. Das 5000-ms-Lesebudget ist der dominante Worst-Case, falls ein Terminator ausbleibt.

## E) Konkrete Treiber-Pruefpunkte

### Waage (MT-SICS)

- Kommando `SI` als Default: bestaetigt (`config.py:93`, `reactor_app/services/device_manual_runtime.py:1162`, `reactor_app/services/drivers/mettler_toledo_ics435.py:508`).
- Kommando `S` als Root-Cause im Default: widerlegt. `S` ist nur erlaubt, wenn Payload oder Spezialkommando `read_stable_weight` es waehlt (`reactor_app/services/drivers/mettler_toledo_ics435.py:513`). Es bleibt ein Root-Cause-Kandidat, wenn `.env` `ICS435_WEIGHT_COMMAND=S` setzt.
- `SIR` als kontinuierlicher Live-Modus: nicht implementiert fuer Live-Poll. Der Matcher kennt `SIR` (`reactor_app/services/drivers/mettler_toledo_ics435.py:408`), aber `payload.weight_command` akzeptiert nur `S` und `SI` (`reactor_app/services/drivers/mettler_toledo_ics435.py:509`).
- Terminator korrekt: bestaetigt. Sendet CRLF (`reactor_app/services/drivers/mettler_toledo_ics435.py:339`) und liest bis LF (`reactor_app/services/drivers/mettler_toledo_ics435.py:355`), nicht per fixed read.
- Unsolicited/stale Responses: teilweise bestaetigt robust. Der Treiber draint vor Commands (`reactor_app/services/drivers/mettler_toledo_ics435.py:319`) und skippt nicht passende Antwortzeilen (`reactor_app/services/drivers/mettler_toledo_ics435.py:374`).

### Huber

- Ministat cc nutzt im Code PP-ASCII, nicht PB: bestaetigt (`reactor_app/services/drivers/huber_ministat_cc.py:188`, `reactor_app/services/drivers/huber_ministat_cc.py:273`, `reactor_app/services/drivers/huber_ministat_cc.py:276`, `reactor_app/services/drivers/huber_ministat_cc.py:279`).
- NAMUR-Kommandoset: widerlegt fuer den aktuellen Code. Suche nach `NAMUR`/`namur` in App-Code ergab keinen implementierten Pfad; implementiert sind PP (`huber_ministat_cc.py`) und PB (`reactor_app/services/drivers/huber_unistat.py:490`).
- Falsches PB fuer Ministat: im neuen Ministat-Treiber widerlegt, weil `huber_ministat_cc` getrennt registriert ist und PP-Commands sendet (`reactor_app/services/drivers/__init__.py:18`, `reactor_app/services/drivers/huber_ministat_cc.py:557`).
- Kombiniertes Statuskommando als Poll-Kosten: teilweise widerlegt. `read_live_telemetry()` kann Status lesen (`reactor_app/services/drivers/huber_ministat_cc.py:432`, `reactor_app/services/drivers/huber_ministat_cc.py:445`), aber der Manual-Reconciler setzt `include_status=False` (`reactor_app/services/device_manual_runtime.py:1143`, `reactor_app/services/device_manual_runtime.py:1144`).
- Kommunikations-Watchdog: nicht feststellbar. Kein belegter Watchdog-Pfad im Code.

### Transport / MOXA

- TCP-Verbindung pro Poll: fuer Huber/IKA bestaetigt, fuer Waage widerlegt. Default `persistent_transport=False` (`reactor_app/services/drivers/base.py:51`), ICS435 setzt `persistent_transport=True` (`reactor_app/services/drivers/mettler_toledo_ics435.py:415`). Non-persistent Transports werden pro Command mit Context Manager benutzt (`reactor_app/services/device_runtime.py:2043`, `reactor_app/services/device_runtime.py:2044`); persistent Transports werden gecached (`reactor_app/services/device_runtime.py:239`, `reactor_app/services/device_runtime.py:263`).
- Persistent Transport nach Timeout: wird vergessen/geschlossen (`reactor_app/services/device_runtime.py:2077`, `reactor_app/services/device_runtime.py:2078`).
- `TCP_NODELAY`: nicht gesetzt. `TcpSocketTransport.connect()` nutzt `socket.create_connection()` und `settimeout()` (`reactor_app/services/transports/tcp_socket.py:109`, `reactor_app/services/transports/tcp_socket.py:114`), aber keine `setsockopt(TCP_NODELAY)`.
- MOXA Force-Transmit/Delimiter: nicht im App-Code konfiguriert. `configure_moxa_nport.py` speichert App-seitige Connection-Parameter wie TCP-Port, Baudrate, Data Bits, Parity, Stop Bits, Timeouts (`configure_moxa_nport.py:122`, `configure_moxa_nport.py:123`, `configure_moxa_nport.py:124`, `configure_moxa_nport.py:125`, `configure_moxa_nport.py:126`, `configure_moxa_nport.py:128`, `configure_moxa_nport.py:129`), nicht die NPort-Packing-Parameter.

## F) Frame-Desynchronisation

Nach Timeouts gibt es Schutz, aber nicht gleich stark fuer alle Treiber:

- TCP liest bis Terminator und wirft Timeout, wenn kein Terminator kommt (`reactor_app/services/transports/tcp_socket.py:183`, `reactor_app/services/transports/tcp_socket.py:212`, `reactor_app/services/transports/tcp_socket.py:229`).
- ICS435 draint vor dem Senden (`reactor_app/services/drivers/mettler_toledo_ics435.py:319`) und skippt Zeilen, die nicht zum erwarteten Command passen (`reactor_app/services/drivers/mettler_toledo_ics435.py:374`).
- Ministat cc draint vor dem Senden (`reactor_app/services/drivers/huber_ministat_cc.py:199`, `reactor_app/services/drivers/huber_ministat_cc.py:224`) und ueberspringt Echo-Antworten (`reactor_app/services/drivers/huber_ministat_cc.py:134`, `reactor_app/services/drivers/huber_ministat_cc.py:233`).
- Ministat cc validiert Temperaturantworten aber nicht gegen den erwarteten Prefix. `_temperature_from_pp_response()` nimmt den letzten numerischen Token (`reactor_app/services/drivers/huber_ministat_cc.py:96`, `reactor_app/services/drivers/huber_ministat_cc.py:97`) und die Kanal-Methoden rufen sie direkt fuer `SP?`, `TI?`, `TE?` auf (`reactor_app/services/drivers/huber_ministat_cc.py:273`, `reactor_app/services/drivers/huber_ministat_cc.py:276`, `reactor_app/services/drivers/huber_ministat_cc.py:279`).

Risiko: Wenn eine verspaetete `SP`-Antwort nach einem Timeout in den naechsten `TI?`-Read faellt und nicht rechtzeitig gedraint wird, koennte der Wert nicht nur alt, sondern dem falschen Kanal zugeordnet werden. Das ist nicht bewiesen, aber ein konkreter Pruefpunkt. Fuer ICS435 ist diese Klasse robuster abgefangen; fuer Ministat sollte eine Prefix-Validierung oder request/response matching geprueft werden.

## G) Datenweg zum Frontend

Es gibt zwei parallele Anzeigequellen:

- Trend/Plot und Display lesen `Measurement`-Rows ueber `/api/plot-series/live` (`reactor_app/api.py:1823`, `reactor_app/services/measurement_plot.py:273`, `reactor_app/services/measurement_plot.py:351`).
- Danach mischt das Frontend Runtime/Manual-State-Snapshots ein (`static/js/process_view.js:2171`, `static/js/process_view.js:2400`, `static/js/process_view.js:2446`).

Ja, es gibt Zustaende, in denen der Treiber bzw. Runtime-State neuer ist als die Measurement-History:

- Waage: RAM-Fast-Path wird vor/nebens DB-Persistenz aktualisiert (`reactor_app/services/device_manual_runtime.py:65`, `reactor_app/services/device_manual_runtime.py:1414`, `reactor_app/services/device_manual_runtime.py:1924`). Der Code kommentiert explizit, dass `GET /manual-state` diesen Snapshot direkt liest (`reactor_app/services/device_manual_runtime.py:1920`).
- Huber: kein gleichwertiger Huber-RAM-Fast-Path gefunden. Huber geht ueber `DeviceManualState.reported_extra` und Measurement-Persistenz (`reactor_app/services/device_manual_runtime.py:1899`, `reactor_app/services/device_manual_runtime.py:1901`, `reactor_app/services/device_manual_runtime.py:1910`).
- Persistence-Best-Effort: Measurement-Persistenz darf fehlschlagen, waehrend der Manual-State aktualisiert bleibt (`reactor_app/services/device_manual_runtime.py:1663`, `reactor_app/services/device_manual_runtime.py:1669`, `reactor_app/services/device_manual_runtime.py:1681`).

Das erklaert, warum Live-Anzeige und Plot unterschiedliche Aktualitaet zeigen koennen. Der Plot kann alt sein, obwohl der Manual-State neuer ist; umgekehrt kann ein History-Fallback alte Punkte anzeigen, wenn keine neuen Rows persistiert wurden.

## H) Falsifikation

Die Ausgangshypothesen werden nicht pauschal bestaetigt:

- "9600 Baud ist zu langsam": widerlegt. Die Nutzdaten liegen im Millisekundenbereich; Timeouts/Minuten-Stale kommen nicht aus der Leitung.
- "Waage blockiert, weil der Default `S` auf stabilen Wert wartet": widerlegt fuer Default. Der Default ist `SI` (`config.py:93`). Nur eine `.env`-Abweichung auf `ICS435_WEIGHT_COMMAND=S` wuerde diese These wieder aktivieren.
- "Plot/Display rufen direkt Treiberfunktionen auf": widerlegt fuer `/api/plot-series/live`. Dieser Endpoint liest Measurement-Rows (`reactor_app/api.py:1823`, `reactor_app/services/measurement_plot.py:273`).
- "Mehr RAM oder mehr Prozesse loesen das Problem": widerlegt als Haupthebel. Mehr Gunicorn-Prozesse wuerden die prozesslokalen Queues/Snapshots brechen; aktuelle Konfiguration ist bewusst `workers = 1` (`gunicorn.conf.py:6`).
- "Huber Ministat spricht im Code PB": widerlegt. Der Ministat-Treiber sendet PP-ASCII (`reactor_app/services/drivers/huber_ministat_cc.py:188`, `reactor_app/services/drivers/huber_ministat_cc.py:273`).
- "Alle alten Werte kommen aus der DB": widerlegt. Die Waage hat einen RAM-Snapshot, der neuer als Measurement-Rows sein kann (`reactor_app/services/device_manual_runtime.py:65`, `reactor_app/services/device_manual_runtime.py:1414`).

## Wahrscheinlichste Ursachen

1. Polling wird unter Recipe-/Manual-Last nachrangig behandelt und aktiv geskippt. Belege: Prioritaeten `reactor_app/services/command_model.py:49`, `reactor_app/services/command_model.py:51`; Supersede/Skip `reactor_app/services/runtime_scheduler.py:399`, `reactor_app/services/runtime_scheduler.py:412`; Polling-Queue nur 2 s `reactor_app/services/command_dispatcher.py:80`.
2. Browser-Pfade duerfen noch Poll-Druck erzeugen. Belege: Plot-Stale-Refresh `static/js/process_view.js:2453`; `refresh=1` setzt `next_poll_at` `reactor_app/services/device_manual_runtime.py:916`; `await_ms` wartet im HTTP-Request `reactor_app/api.py:1955`.
3. Huber-Frontend ist noch nicht sauber entkoppelt. Belege: `loadHuberStateSnapshot()` `static/js/process_view.js:3813`; direkte `executeDeviceCommand()`-Fallbacks `static/js/process_view.js:3838`, `static/js/process_view.js:3845`, `static/js/process_view.js:3848`; synchroner Dispatch `reactor_app/api.py:2030`.

Was diese Ursachen widerlegen wuerde: keine skipped/superseded/timeout Polls in `control_command` unter Last; frische `DeviceManualState.last_reported_at` bei gleichzeitig altem Plot; Produktions-Logs mit stabilen Poll-Dauern unter 500 ms und ohne Queue-Wartezeit waehrend der beobachteten Stale-Phasen.
