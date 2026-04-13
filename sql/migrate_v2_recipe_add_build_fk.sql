-- Migration: add reactor_build_id FK to recipe table
-- Apply once on existing installations before deploying the updated recipe editor.

ALTER TABLE recipe
  ADD COLUMN IF NOT EXISTS reactor_build_id BIGINT UNSIGNED NULL AFTER steps_json,
  ADD KEY IF NOT EXISTS idx_recipe_build (reactor_build_id);

-- Add FK separately (IF NOT EXISTS not supported for constraints in older MySQL)
-- Run only once; skip this block manually if the constraint already exists.
ALTER TABLE recipe
  ADD CONSTRAINT fk_recipe_reactor_build
    FOREIGN KEY (reactor_build_id) REFERENCES reactor_build (reactor_build_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL;
