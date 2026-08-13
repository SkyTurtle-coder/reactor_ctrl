# Live Acquisition Baseline

Ziel: Vor jeder Aenderung den IST-Zustand messbar machen. Die Messung soll Poll-Dauer, Queue-Wartezeit, Alter des angezeigten Werts und Polls pro Minute pro Geraet erfassen, ohne Setpoints oder andere Write-Kommandos an echte Hardware zu senden.

## Messgroessen

| Groesse | Zweck | Quelle heute | Luecke |
|---|---|---|---|
| `snapshot_age_s` | Alter des zuletzt gelesenen Runtime-/Manual-State-Werts | `GET /api/devices/<id>/manual-state` (`updated_at`, `last_reported_at`, `reported_extra`) | Huber hat keinen eigenen RAM-Fast-Path wie die Waage. |
| `measurement_age_s` | Alter der letzten persistierten Measurement-Row | `GET /api/plot-series/live?history_fallback=0` oder DB Query auf `measurement` | Plot-Fallback kann alte Historie verdecken, deshalb `history_fallback=0` verwenden. |
| `polls_per_minute` | Tatsaechliche Acquisition-Rate | `control_command` / command history endpoint | Muss nach `command_type=read_live_telemetry` und Device gefiltert werden. |
| `queue_wait_ms` | Wartezeit von request bis start/sent | `control_command` Events/Status, falls `requested_at`, `started_at`, `sent_at` vorhanden | Falls Zeiten fehlen: Logging-Diff unten. |
| `execution_ms` | Geraete-/Transportdauer | `control_command` Events/Status oder Log | Muss pro Command/Device aggregiert werden. |
| `skipped_or_superseded` | Poll-Verlust unter Last | `control_command.status` und Events; Scheduler loggt skipped/preempted Polling | Debug-Level koennte im Journal fehlen. |
| `frontend_age_s` | Alter dessen, was Display/Plot wirklich zeigt | API-Antwort aus `/plot-series/live` plus `/manual-state`; Browser-Screenshot nur als Symptom | Frontend mischt History und Snapshots; API getrennt messen. |

## Minimaler Read-Only Messlauf auf `v002020`

Voraussetzungen:

```bash
cd /home/pthuerlemann/reactor_ctrl
export BaseUrl="http://127.0.0.1:5000"
export Token="$(grep '^API_AUTH_TOKEN=' .env | cut -d= -f2-)"
export Headers=(-H "Authorization: Bearer $Token")
```

Device-IDs ermitteln:

```bash
curl -s "${Headers[@]}" "$BaseUrl/api/devices" | python -m json.tool
curl -s "${Headers[@]}" "$BaseUrl/api/device-connections" | python -m json.tool
```

Manual-State-Age fuer Huber, Waage, IKA messen:

```bash
for id in 5 1 2; do
  curl -s "${Headers[@]}" "$BaseUrl/api/devices/$id/manual-state?watch=1" \
    | python -m json.tool \
    | sed -n '1,120p'
done
```

Wichtig: `refresh=1` und `await_ms` in der Baseline nicht verwenden, sonst wird die Messung selbst zum Poll-Trigger.

Plot-/Measurement-Age ohne History-Fallback messen:

```bash
curl -s "${Headers[@]}" \
  "$BaseUrl/api/plot-series/live?since_seconds=300&history_fallback=0&cache_seconds=0" \
  | python -m json.tool
```

Wenn konkrete Series benoetigt werden, Series explizit angeben, z.B.:

```bash
curl -s "${Headers[@]}" \
  "$BaseUrl/api/plot-series/live?since_seconds=300&history_fallback=0&cache_seconds=0&series=5:huber_internal_temp_C&series=5:huber_external_temp_C&series=5:huber_setpoint_C" \
  | python -m json.tool
```

Command-History fuer Polls/Timeouts/Supersedes:

```bash
curl -s "${Headers[@]}" "$BaseUrl/api/devices/5/commands" | python -m json.tool
curl -s "${Headers[@]}" "$BaseUrl/api/devices/1/commands" | python -m json.tool
```

Produktionslog fuer Poll-Skip/Timeout-Muster:

```bash
sudo journalctl -u reactor_ctrl --since "30 min ago" --no-pager \
  | grep -Ei "poll|timeout|skipped|preempted|superseded|device.*busy|read_live_telemetry|manual-state"
```

## Empfohlenes kleines Messskript

Dieses Skript liest nur HTTP-APIs und sendet keine Geraete-Write-Kommandos. Device-IDs muessen vorher ersetzt werden.

```bash
python - <<'PY'
import datetime as dt
import json
import os
import urllib.request

base = os.environ["BaseUrl"].rstrip("/")
token = os.environ["Token"]
device_ids = [5, 1, 2]

def get(path):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))

def parse_ts(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))

now = dt.datetime.now(dt.timezone.utc)
for device_id in device_ids:
    payload = get(f"/api/devices/{device_id}/manual-state?watch=1")
    state = payload.get("state") or payload
    last = parse_ts(state.get("updated_at") or state.get("last_reported_at"))
    age = None if last is None else round((now - last).total_seconds(), 3)
    print(json.dumps({
        "device_id": device_id,
        "queue_status": state.get("queue_status"),
        "updated_at": state.get("updated_at"),
        "last_reported_at": state.get("last_reported_at"),
        "snapshot_age_s": age,
        "reported_extra_keys": sorted((state.get("reported_extra") or {}).keys()),
    }, sort_keys=True))
PY
```

## DB-Query falls Shell-Zugriff auf die Produktiv-DB vorhanden ist

Falls `DATABASE_URL` eine MySQL-URL ist und Credentials lokal verfuegbar sind, kann die sauberste Baseline direkt aus den Tabellen kommen. Keine Writes ausfuehren.

```sql
SELECT
  device_id,
  command_type,
  command_priority,
  status,
  COUNT(*) AS count_commands,
  MIN(requested_at) AS first_requested_at,
  MAX(updated_at) AS last_updated_at
FROM control_command
WHERE requested_at >= UTC_TIMESTAMP() - INTERVAL 30 MINUTE
  AND command_type IN ('read_live_telemetry', 'manual_text')
GROUP BY device_id, command_type, command_priority, status
ORDER BY device_id, command_type, status;

SELECT
  device_id,
  channel_code,
  MAX(measured_at) AS last_measured_at,
  TIMESTAMPDIFF(SECOND, MAX(measured_at), UTC_TIMESTAMP()) AS measurement_age_s,
  COUNT(*) AS rows_30min
FROM measurement
WHERE measured_at >= UTC_TIMESTAMP() - INTERVAL 30 MINUTE
GROUP BY device_id, channel_code
ORDER BY device_id, channel_code;
```

Wenn Event-Zeiten in `control_command_event` vorhanden sind:

```sql
SELECT
  c.device_id,
  c.command_type,
  c.command_priority,
  c.status,
  TIMESTAMPDIFF(MICROSECOND, c.requested_at, c.started_at) / 1000 AS queue_wait_ms,
  TIMESTAMPDIFF(MICROSECOND, c.started_at, c.updated_at) / 1000 AS execution_ms,
  c.requested_at,
  c.started_at,
  c.updated_at
FROM control_command c
WHERE c.requested_at >= UTC_TIMESTAMP() - INTERVAL 30 MINUTE
  AND c.command_type IN ('read_live_telemetry', 'manual_text')
ORDER BY c.command_id DESC
LIMIT 200;
```

Die exakten Spaltennamen muessen gegen das Produktivschema verifiziert werden, weil das Repo Schema-Erweiterungen beim Start automatisch anlegt (`reactor_app/__init__.py:305`, `reactor_app/__init__.py:307`).

## Kleinstmoeglicher Logging-Diff, falls Metriken fehlen

Noch nicht anwenden. Fuer die Implementation-Session waere der kleinste sinnvolle Diff:

1. In `reactor_app/services/device_manual_runtime.py` um `_run_logged_driver_command()` (`reactor_app/services/device_manual_runtime.py:1056`) und `_process_manual_state()` (`reactor_app/services/device_manual_runtime.py:1842`) monotone Zeitpunkte erfassen:
   - `poll_claimed_at`
   - `dispatch_requested_at`
   - `dispatch_finished_at`
   - `measured_at`
   - `device_id`
   - `protocol`
   - `command_name`
   - `queue_status`
   - `success`
   - `error_kind`
   - `poll_duration_ms`
2. Nach jedem Poll genau eine strukturierte Logzeile schreiben:
   - `acquisition_poll device_id=... protocol=... command=read_live_telemetry success=... duration_ms=... next_poll_at=...`
3. Optional dieselben Werte in `DeviceManualState.reported_extra["_acquisition"]` ablegen, aber keine Credentials oder Verbindungsparameter.
4. Keine neue Tabelle fuer Phase A. Erst messen, dann entscheiden.

## Zielwerte fuer den Vorher/Nachher-Vergleich

Die Baseline sollte mindestens 30 Minuten unter drei Situationen laufen:

- Kein Rezept, Browser offen.
- Rezept laeuft, Browser offen.
- Rezept laeuft, Browser geschlossen.

Pro Situation erfassen:

- Polls/min pro Device.
- P50/P95/P99 `queue_wait_ms`.
- P50/P95/P99 `execution_ms`.
- Anzahl `timeout`, `skipped`, `superseded`, `busy`.
- `snapshot_age_s` P50/P95/P99.
- `measurement_age_s` P50/P95/P99.
- Differenz `measurement_age_s - snapshot_age_s`.

Interpretation:

- Hohe `queue_wait_ms`, aber niedrige `execution_ms`: Scheduler/Lock/Prioritaeten sind Engpass.
- Hohe `execution_ms`: Geraet, MOXA, Terminator, Timeout, Retry oder Verbindungsaufbau.
- Frischer Snapshot, alte Measurements: Persistenz/DB/Plot-History-Pfad.
- Frische Measurements, alte Frontend-Anzeige: Browser/API/Cache/Rendering.
- Viele skipped/superseded Polls: Polling verhungert durch Prioritaet oder Browser erzeugt Poll-Druck.
