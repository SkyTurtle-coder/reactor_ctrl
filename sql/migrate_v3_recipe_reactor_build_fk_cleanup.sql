-- Migration: normalize reactor_build_id signedness and add the recipe FK.
-- Use on installations where sql/migrate_v2_recipe_add_build_fk.sql
-- created recipe.reactor_build_id but the foreign key could not be added.

ALTER TABLE reactor_build
  MODIFY COLUMN reactor_build_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT;

ALTER TABLE recipe
  MODIFY COLUMN reactor_build_id BIGINT UNSIGNED NULL;

ALTER TABLE recipe
  ADD CONSTRAINT fk_recipe_reactor_build
    FOREIGN KEY (reactor_build_id) REFERENCES reactor_build (reactor_build_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL;
