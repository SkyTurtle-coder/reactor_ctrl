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
-- Usage on MariaDB:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v13_recipe_program_pause.sql

USE reactor_ctrl;

ALTER TABLE recipe_program_state
    ADD COLUMN paused_at DATETIME(3) NULL AFTER stop_requested,
    ADD INDEX idx_recipe_program_state_paused_at (paused_at);

-- migrate_v9_phase1_integrity.sql added CHECK constraints restricting
-- status to the pre-pause status set. Widen both to allow 'paused' too.
ALTER TABLE recipe_program_state
    DROP CONSTRAINT chk_recipe_program_state_status;

ALTER TABLE recipe_program_state
    ADD CONSTRAINT chk_recipe_program_state_status
        CHECK (status IN ('idle', 'running', 'paused', 'completed', 'stopped', 'error'));

ALTER TABLE recipe_program_run
    DROP CONSTRAINT chk_recipe_program_run_status;

ALTER TABLE recipe_program_run
    ADD CONSTRAINT chk_recipe_program_run_status
        CHECK (status IN ('running', 'paused', 'completed', 'stopped', 'error'));

-- End of migration
