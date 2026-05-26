-- Migration v9: Phase 1 – Datenbankintegrität und Runtime-Stabilisierung
--
-- Zweck:
--   1. CHECK-Constraints auf Status- und Zustandsfelder, die bisher unkontrolliert
--      beliebige Strings akzeptierten.
--   2. device_binding_history.connection_id: FK von RESTRICT → SET NULL, Spalte
--      nullable – erlaubt das operative Löschen alter Verbindungen ohne manuelle
--      Verlaufsbereinigung.
--   3. Performance-Index für den _find_open_program_run()-Query-Pfad
--      (WHERE finished_at IS NULL ORDER BY recipe_program_run_id DESC).
--
-- Voraussetzungen:
--   MariaDB 10.6+ / MySQL 8.0+  (CHECK constraints werden ab diesen Versionen
--   tatsächlich erzwungen).
--
-- Anwendung:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v9_phase1_integrity.sql
--
-- ACHTUNG: Dieser Script ist für bestehende Daten sicher.
--          Neue Constraints validieren nur zukünftige Writes – bestehende Zeilen
--          mit abweichenden Werten werden NICHT rückwirkend abgelehnt (MySQL/InnoDB-
--          Verhalten bei ALTER TABLE ... ADD CONSTRAINT CHECK mit vorhandenen Daten).
--          Falls unbekannte Status-Werte in der Datenbank vorhanden sind, schlägt
--          das ALTER TABLE fehl. In diesem Fall zunächst:
--              SELECT DISTINCT queue_status FROM device_manual_state;
--              SELECT DISTINCT status FROM recipe_program_state;
--              SELECT DISTINCT status FROM recipe_program_run;
--          und anschließend die Migration anpassen oder die Daten bereinigen.
-- ============================================================

USE reactor_ctrl;

-- ============================================================
-- 1. device_manual_state: CHECK auf queue_status
-- ============================================================
-- Erlaubt nur die vier bekannten Zustände; verhindert, dass ein Tippfehler
-- oder ein neuer Code-Pfad einen undokumentierten Status schreibt.
ALTER TABLE device_manual_state
    ADD CONSTRAINT chk_manual_state_queue_status
        CHECK (queue_status IN ('idle', 'queued', 'running', 'error'));

-- ============================================================
-- 2. device_manual_state: CHECK auf active_control_sensor
-- ============================================================
-- Nur 'internal' und 'external' sind gültige Sensorwerte.
-- NULL ist erlaubt (Spalte ist optional / Gerät nicht CC230).
ALTER TABLE device_manual_state
    ADD CONSTRAINT chk_manual_state_active_control_sensor
        CHECK (active_control_sensor IS NULL
               OR active_control_sensor IN ('internal', 'external'));

-- ============================================================
-- 3. recipe_program_state: CHECK auf status
-- ============================================================
ALTER TABLE recipe_program_state
    ADD CONSTRAINT chk_recipe_program_state_status
        CHECK (status IN ('idle', 'running', 'completed', 'stopped', 'error'));

-- ============================================================
-- 4. recipe_program_run: CHECK auf status
-- ============================================================
ALTER TABLE recipe_program_run
    ADD CONSTRAINT chk_recipe_program_run_status
        CHECK (status IN ('running', 'completed', 'stopped', 'error'));

-- ============================================================
-- 5. device_binding_history: connection_id nullable + FK SET NULL
-- ============================================================
-- Bisher war connection_id NOT NULL / ON DELETE RESTRICT, was das Löschen
-- einer device_connection verhindert, solange Historien-Einträge existieren.
-- Durch Nullable + SET NULL bleiben Historien-Datensätze erhalten, aber
-- der Verweis wird auf NULL gesetzt – die Verbindung kann gelöscht werden.
--
-- Schritt 1: bestehende FK-Constraint entfernen
ALTER TABLE device_binding_history
    DROP FOREIGN KEY fk_binding_history_connection;

-- Schritt 2: Spalte auf NULL-fähig ändern
ALTER TABLE device_binding_history
    MODIFY COLUMN connection_id BIGINT UNSIGNED NULL;

-- Schritt 3: FK neu anlegen mit ON DELETE SET NULL
ALTER TABLE device_binding_history
    ADD CONSTRAINT fk_binding_history_connection
        FOREIGN KEY (connection_id) REFERENCES device_connection (connection_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL;

-- ============================================================
-- 6. Performance-Index: _find_open_program_run()
-- ============================================================
-- Der Query lautet: WHERE finished_at IS NULL ORDER BY recipe_program_run_id DESC LIMIT 1
-- Ein Index auf (finished_at) beschleunigt dies erheblich sobald die Tabelle wächst.
-- IF NOT EXISTS wird hier nicht unterstützt (< MariaDB 10.7), daher:
-- Fehler "Duplicate key name" bedeutet der Index existiert bereits – kann ignoriert werden.
CREATE INDEX ix_recipe_program_run_open
    ON recipe_program_run (finished_at, recipe_program_run_id);

-- ============================================================
-- Migration v9 abgeschlossen.
-- ============================================================
