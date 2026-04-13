-- Migration: add recipe table
-- Apply once against the live reactor_ctrl database.
-- Safe to re-run: CREATE TABLE IF NOT EXISTS skips if already present.

CREATE TABLE IF NOT EXISTS recipe (
  recipe_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title VARCHAR(120) NOT NULL,
  operator_name VARCHAR(120) NOT NULL,
  version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  steps_json JSON NOT NULL,
  created_by VARCHAR(120) NOT NULL,
  updated_by VARCHAR(120) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recipe_id),
  KEY idx_recipe_title (title),
  KEY idx_recipe_status (status),
  KEY idx_recipe_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
