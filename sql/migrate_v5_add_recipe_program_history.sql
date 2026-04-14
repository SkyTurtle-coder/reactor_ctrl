-- Migration: add persistent run/event history for process recipe programs.

CREATE TABLE IF NOT EXISTS recipe_program_run (
  recipe_program_run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  recipe_id BIGINT UNSIGNED NULL,
  reactor_build_id BIGINT UNSIGNED NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'running',
  requested_by VARCHAR(100) NOT NULL DEFAULT 'system',
  recipe_title VARCHAR(120) NULL,
  operator_name VARCHAR(120) NULL,
  snapshot_json JSON NULL,
  started_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  finished_at DATETIME(3) NULL,
  last_progress_at DATETIME(3) NULL,
  last_error VARCHAR(500) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recipe_program_run_id),
  KEY idx_recipe_program_run_status (status),
  KEY idx_recipe_program_run_recipe (recipe_id),
  KEY idx_recipe_program_run_build (reactor_build_id),
  KEY idx_recipe_program_run_started (started_at),
  KEY idx_recipe_program_run_finished (finished_at),
  KEY idx_recipe_program_run_progress (last_progress_at),
  CONSTRAINT fk_recipe_program_run_recipe
    FOREIGN KEY (recipe_id) REFERENCES recipe (recipe_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL,
  CONSTRAINT fk_recipe_program_run_build
    FOREIGN KEY (reactor_build_id) REFERENCES reactor_build (reactor_build_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS recipe_program_event (
  recipe_program_event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  recipe_program_run_id BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  active_step_index INT NULL,
  event_payload JSON NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recipe_program_event_id),
  KEY idx_recipe_program_event_run_time (recipe_program_run_id, created_at),
  KEY idx_recipe_program_event_type_time (event_type, created_at),
  KEY idx_recipe_program_event_step_time (active_step_index, created_at),
  CONSTRAINT fk_recipe_program_event_run
    FOREIGN KEY (recipe_program_run_id) REFERENCES recipe_program_run (recipe_program_run_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
