-- Migration v8: Add safe_state_json to recipe
-- Stores per-actor setpoints to apply when the program stops or errors.
-- NULL means the built-in defaults are used (20 °C for thermostats, 0 RPM for stirrers).

USE reactor_ctrl;

ALTER TABLE recipe
    ADD COLUMN safe_state_json JSON NULL
    COMMENT 'Per-actor safe setpoints applied on program stop/error. NULL = use built-in defaults.';

-- End of migration
