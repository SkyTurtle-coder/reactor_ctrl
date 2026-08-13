# reactor_ctrl - Agent-Regeln

## Kommandos
- Tests: `pytest -q`
- Lint: nicht konfiguriert
- Start: `python app.py` lokal; Produktion via `systemd -> gunicorn --config gunicorn.conf.py app:app`
- Einzeltest bevorzugen, nicht die volle Suite bei jeder Iteration.

## Hardware
- In dieser Umgebung ist keine direkte Hardware am lokalen Workspace angeschlossen; reale Geraete laufen auf `v002020`.
- Kein Kommando gegen echte Geraete ohne explizite Freigabe.
- Setpoint-/Write-Kommandos an Huber oder IKA NIE testweise absetzen.

## Architektur-Invarianten (nicht verhandelbar)
- Pro physischem Geraet ist zu jedem Zeitpunkt hoechstens EINE serielle Transaktion offen.
- Ein HTTP-Request darf niemals synchron Geraetekommunikation ausloesen.
- Kein zweiter Konfigurationsmechanismus neben dem bestehenden.

## Stil
- Minimale Aenderung, die die Anforderung erfuellt. Keine neuen Abstraktionsebenen "fuer spaeter". Keine spekulative Flexibilitaet.
- Bestehende Tests nicht umschreiben, um sie gruen zu bekommen.
