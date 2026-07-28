-- Migration v13: Add pause/resume support to recipe_program_state
--
-- Purpose:
--   The recipe program runtime only supported start/stop. Pausing sets
--   status='paused' and records paused_at so the reconciler (which only
--   claims rows with status='running') naturally stops advancing the
--   step timeline and stops applying new targets without any change to
--   locking/queueing. Resuming shifts step_started_at forward by the
--   paused duration so the paused interval is not counted as elapsed
--   step/recipe time.
--
-- Safe to run more than once: the app's own AUTO_CREATE_SCHEMA startup
-- step (reactor_app/__init__.py, _OPTIONAL_COLUMN_SPECS) may already have
-- added the paused_at column on a normal restart, so every statement here
-- checks information_schema first instead of assuming a fresh state.
--
-- Usage on MariaDB/MySQL:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v13_recipe_program_pause.sql

USE reactor_ctrl;

-- 1. paused_at column + index (idempotent)
SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'recipe_program_state' AND COLUMN_NAME = 'paused_at'
);
SET @ddl = IF(@col_exists = 0,
    'ALTER TABLE recipe_program_state ADD COLUMN paused_at DATETIME(3) NULL AFTER stop_requested',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'recipe_program_state' AND INDEX_NAME = 'idx_recipe_program_state_paused_at'
);
SET @ddl = IF(@idx_exists = 0,
    'ALTER TABLE recipe_program_state ADD INDEX idx_recipe_program_state_paused_at (paused_at)',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. Widen the status CHECK constraints added by migrate_v9_phase1_integrity.sql
--    to also allow 'paused' (idempotent: only touches them if they still
--    exclude 'paused').
SET @needs_state_check = (
    SELECT COUNT(*) FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_recipe_program_state_status'
      AND CHECK_CLAUSE NOT LIKE '%paused%'
);
SET @ddl = IF(@needs_state_check > 0,
    'ALTER TABLE recipe_program_state DROP CONSTRAINT chk_recipe_program_state_status',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(@needs_state_check > 0,
    'ALTER TABLE recipe_program_state ADD CONSTRAINT chk_recipe_program_state_status '
    'CHECK (status IN (''idle'', ''running'', ''paused'', ''completed'', ''stopped'', ''error''))',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @needs_run_check = (
    SELECT COUNT(*) FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_recipe_program_run_status'
      AND CHECK_CLAUSE NOT LIKE '%paused%'
);
SET @ddl = IF(@needs_run_check > 0,
    'ALTER TABLE recipe_program_run DROP CONSTRAINT chk_recipe_program_run_status',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(@needs_run_check > 0,
    'ALTER TABLE recipe_program_run ADD CONSTRAINT chk_recipe_program_run_status '
    'CHECK (status IN (''running'', ''paused'', ''completed'', ''stopped'', ''error''))',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- End of migration
