-- Migration: add persistent runtime state for process recipe programs.

CREATE TABLE IF NOT EXISTS recipe_program_state (
  recipe_program_state_id BIGINT UNSIGNED NOT NULL,
  recipe_id BIGINT UNSIGNED NULL,
  reactor_build_id BIGINT UNSIGNED NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'idle',
  requested_by VARCHAR(100) NOT NULL DEFAULT 'system',
  recipe_title VARCHAR(120) NULL,
  operator_name VARCHAR(120) NULL,
  snapshot_json JSON NULL,
  last_applied_targets_json JSON NULL,
  active_step_index INT NOT NULL DEFAULT 0,
  step_started_at DATETIME(3) NULL,
  started_at DATETIME(3) NULL,
  finished_at DATETIME(3) NULL,
  last_progress_at DATETIME(3) NULL,
  stop_requested TINYINT(1) NOT NULL DEFAULT 0,
  last_error VARCHAR(500) NULL,
  lease_owner VARCHAR(64) NULL,
  lease_expires_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recipe_program_state_id),
  KEY idx_recipe_program_state_status (status),
  KEY idx_recipe_program_state_recipe (recipe_id),
  KEY idx_recipe_program_state_build (reactor_build_id),
  KEY idx_recipe_program_state_lease (lease_expires_at),
  CONSTRAINT fk_recipe_program_state_recipe
    FOREIGN KEY (recipe_id) REFERENCES recipe (recipe_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL,
  CONSTRAINT fk_recipe_program_state_build
    FOREIGN KEY (reactor_build_id) REFERENCES reactor_build (reactor_build_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
