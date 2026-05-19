-- reactor_ctrl - Ethernet serial server schema v2-in-place
-- Zielplattform: Moxa NPort 5610-8-DT, 8x RS-232 via Ethernet fuer Reaktorkomponenten
-- Voraussetzung: MariaDB 10.6+ oder MySQL 8.0+

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE DATABASE IF NOT EXISTS reactor_ctrl
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE reactor_ctrl;

CREATE TABLE IF NOT EXISTS device_server (
  device_server_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  server_code VARCHAR(64) NOT NULL,
  display_name VARCHAR(120) NOT NULL,
  vendor VARCHAR(64) NOT NULL DEFAULT 'Moxa',
  model VARCHAR(64) NULL,
  host VARCHAR(255) NOT NULL,
  management_port INT UNSIGNED NULL,
  serial_standard VARCHAR(16) NOT NULL DEFAULT 'rs232',
  port_count SMALLINT UNSIGNED NOT NULL DEFAULT 8,
  notes TEXT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (device_server_id),
  UNIQUE KEY uq_device_server_code (server_code),
  UNIQUE KEY uq_device_server_host (host),
  KEY idx_device_server_active (is_active),
  CONSTRAINT chk_device_server_serial_standard CHECK (serial_standard IN ('rs232', 'rs422', 'rs485'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_connection (
  connection_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_server_id BIGINT UNSIGNED NOT NULL,
  port_number SMALLINT UNSIGNED NOT NULL,
  connection_label VARCHAR(120) NULL,
  transport_type VARCHAR(16) NOT NULL DEFAULT 'tcp_socket',
  tcp_host VARCHAR(255) NOT NULL,
  tcp_port INT UNSIGNED NOT NULL,
  baud_rate INT UNSIGNED NOT NULL DEFAULT 9600,
  data_bits TINYINT UNSIGNED NOT NULL DEFAULT 8,
  parity CHAR(1) NOT NULL DEFAULT 'N',
  stop_bits TINYINT UNSIGNED NOT NULL DEFAULT 1,
  flow_control VARCHAR(16) NOT NULL DEFAULT 'none',
  read_timeout_ms INT UNSIGNED NOT NULL DEFAULT 1200,
  write_timeout_ms INT UNSIGNED NOT NULL DEFAULT 1200,
  reconnect_delay_ms INT UNSIGNED NOT NULL DEFAULT 1000,
  last_seen_at DATETIME(3) NULL,
  last_error TEXT NULL,
  is_enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (connection_id),
  UNIQUE KEY uq_connection_server_port (device_server_id, port_number),
  UNIQUE KEY uq_connection_tcp_endpoint (tcp_host, tcp_port),
  KEY idx_connection_enabled (is_enabled),
  KEY idx_connection_tcp_endpoint_lookup (tcp_host, tcp_port),
  CONSTRAINT chk_connection_transport_type CHECK (transport_type IN ('tcp_socket', 'rfc2217')),
  CONSTRAINT chk_connection_parity CHECK (parity IN ('N', 'E', 'O')),
  CONSTRAINT chk_connection_flow_control CHECK (flow_control IN ('none', 'rtscts', 'xonxoff')),
  CONSTRAINT fk_connection_device_server
    FOREIGN KEY (device_server_id) REFERENCES device_server (device_server_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device (
  device_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  asset_serial VARCHAR(64) NOT NULL,
  manufacturer_serial VARCHAR(128) NULL,
  display_name VARCHAR(120) NOT NULL,
  device_type VARCHAR(64) NOT NULL,
  protocol VARCHAR(32) NOT NULL,
  firmware_version VARCHAR(64) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (device_id),
  UNIQUE KEY uq_device_asset_serial (asset_serial),
  UNIQUE KEY uq_device_manufacturer_serial_protocol (manufacturer_serial, protocol),
  KEY idx_device_type (device_type),
  KEY idx_device_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_binding_current (
  device_id BIGINT UNSIGNED NOT NULL,
  connection_id BIGINT UNSIGNED NOT NULL,
  first_seen_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_seen_at DATETIME(3) NULL,
  is_online TINYINT(1) NOT NULL DEFAULT 0,
  quality_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
  PRIMARY KEY (device_id),
  UNIQUE KEY uq_binding_connection (connection_id),
  KEY idx_binding_online (is_online),
  CONSTRAINT fk_binding_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_binding_connection
    FOREIGN KEY (connection_id) REFERENCES device_connection (connection_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_binding_history (
  binding_history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id BIGINT UNSIGNED NOT NULL,
  connection_id BIGINT UNSIGNED NOT NULL,
  bound_from DATETIME(3) NOT NULL,
  bound_to DATETIME(3) NULL,
  reason VARCHAR(255) NULL,
  PRIMARY KEY (binding_history_id),
  KEY idx_binding_history_device_time (device_id, bound_from),
  KEY idx_binding_history_connection_time (connection_id, bound_from),
  CONSTRAINT fk_binding_history_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_binding_history_connection
    FOREIGN KEY (connection_id) REFERENCES device_connection (connection_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS measurement_channel (
  channel_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id BIGINT UNSIGNED NOT NULL,
  channel_code VARCHAR(64) NOT NULL,
  display_name VARCHAR(120) NOT NULL,
  unit VARCHAR(32) NOT NULL,
  value_type VARCHAR(16) NOT NULL DEFAULT 'float',
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (channel_id),
  UNIQUE KEY uq_channel_device_code (device_id, channel_code),
  KEY idx_channel_active (is_active),
  CONSTRAINT chk_channel_value_type CHECK (value_type IN ('float', 'int', 'bool', 'text')),
  CONSTRAINT fk_channel_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS measurement (
  measurement_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id BIGINT UNSIGNED NOT NULL,
  channel_id BIGINT UNSIGNED NULL,
  channel_code VARCHAR(64) NOT NULL,
  measured_at DATETIME(3) NOT NULL,
  ingested_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  numeric_value DOUBLE NULL,
  text_value VARCHAR(255) NULL,
  unit VARCHAR(32) NULL,
  quality_score DECIMAL(5,4) NULL,
  raw_payload JSON NULL,
  source VARCHAR(32) NOT NULL DEFAULT 'poller',
  PRIMARY KEY (measurement_id),
  KEY idx_measurement_device_time (device_id, measured_at),
  KEY idx_measurement_channel_time (channel_code, measured_at),
  KEY idx_measurement_source_time (source, measured_at),
  CONSTRAINT chk_measurement_value_present CHECK (numeric_value IS NOT NULL OR text_value IS NOT NULL),
  CONSTRAINT chk_measurement_source CHECK (source IN ('poller', 'event', 'manual', 'import')),
  CONSTRAINT fk_measurement_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_measurement_channel
    FOREIGN KEY (channel_id) REFERENCES measurement_channel (channel_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS control_command (
  command_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id BIGINT UNSIGNED NOT NULL,
  request_uuid CHAR(36) NOT NULL,
  requested_by VARCHAR(100) NOT NULL DEFAULT 'system',
  command_name VARCHAR(100) NOT NULL,
  command_payload JSON NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'queued',
  requested_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  scheduled_for DATETIME(3) NULL,
  sent_at DATETIME(3) NULL,
  ack_at DATETIME(3) NULL,
  finished_at DATETIME(3) NULL,
  retry_count INT UNSIGNED NOT NULL DEFAULT 0,
  error_message TEXT NULL,
  PRIMARY KEY (command_id),
  UNIQUE KEY uq_command_request_uuid (request_uuid),
  KEY idx_command_status_schedule (status, scheduled_for),
  KEY idx_command_device_time (device_id, requested_at),
  CONSTRAINT chk_command_status CHECK (status IN ('queued', 'sent', 'acked', 'failed', 'timeout', 'cancelled')),
  CONSTRAINT fk_command_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS control_command_event (
  command_event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  command_id BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  event_payload JSON NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (command_event_id),
  KEY idx_command_event_command_time (command_id, created_at),
  CONSTRAINT fk_command_event_command
    FOREIGN KEY (command_id) REFERENCES control_command (command_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_manual_state (
  device_id BIGINT UNSIGNED NOT NULL,
  desired_is_on TINYINT(1) NULL,
  desired_speed INT NULL,
  desired_version INT UNSIGNED NOT NULL DEFAULT 0,
  applied_version INT UNSIGNED NOT NULL DEFAULT 0,
  requested_by VARCHAR(100) NOT NULL DEFAULT 'system',
  last_desired_at DATETIME(3) NULL,
  reported_is_on TINYINT(1) NULL,
  reported_setpoint_rpm INT NULL,
  actual_rpm DOUBLE NULL,
  torque_ncm DOUBLE NULL,
  last_reported_at DATETIME(3) NULL,
  queue_status VARCHAR(16) NOT NULL DEFAULT 'idle',
  last_error TEXT NULL,
  next_poll_at DATETIME(3) NULL,
  watch_expires_at DATETIME(3) NULL,
  lease_owner VARCHAR(64) NULL,
  lease_expires_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (device_id),
  KEY idx_device_manual_state_status (queue_status),
  KEY idx_device_manual_state_next_poll (next_poll_at),
  KEY idx_device_manual_state_watch (watch_expires_at),
  KEY idx_device_manual_state_lease (lease_expires_at),
  CONSTRAINT fk_device_manual_state_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS soft_sensor_model (
  soft_sensor_model_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  model_code VARCHAR(64) NOT NULL,
  model_version VARCHAR(32) NOT NULL,
  description TEXT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (soft_sensor_model_id),
  UNIQUE KEY uq_soft_sensor_model_code_version (model_code, model_version),
  KEY idx_soft_sensor_model_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS soft_sensor_estimate (
  soft_sensor_estimate_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  soft_sensor_model_id BIGINT UNSIGNED NOT NULL,
  device_id BIGINT UNSIGNED NULL,
  metric_name VARCHAR(64) NOT NULL,
  estimated_at DATETIME(3) NOT NULL,
  numeric_value DOUBLE NOT NULL,
  unit VARCHAR(32) NOT NULL,
  confidence DECIMAL(5,4) NULL,
  input_snapshot JSON NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (soft_sensor_estimate_id),
  KEY idx_soft_sensor_estimate_device_metric_time (device_id, metric_name, estimated_at),
  KEY idx_soft_sensor_estimate_model_time (soft_sensor_model_id, estimated_at),
  CONSTRAINT fk_soft_sensor_estimate_model
    FOREIGN KEY (soft_sensor_model_id) REFERENCES soft_sensor_model (soft_sensor_model_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT,
  CONSTRAINT fk_soft_sensor_estimate_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS reactor_build (
  reactor_build_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  build_name VARCHAR(120) NOT NULL,
  build_date DATE NOT NULL,
  created_by VARCHAR(120) NOT NULL,
  updated_by VARCHAR(120) NULL,
  definition_json JSON NOT NULL,
  notes TEXT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (reactor_build_id),
  KEY idx_reactor_build_name (build_name),
  KEY idx_reactor_build_active (is_active),
  KEY idx_reactor_build_date (build_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS recipe (
  recipe_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title VARCHAR(120) NOT NULL,
  operator_name VARCHAR(120) NOT NULL,
  version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  reactor_build_id BIGINT UNSIGNED NULL,
  steps_json JSON NOT NULL,
  created_by VARCHAR(120) NOT NULL,
  updated_by VARCHAR(120) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recipe_id),
  KEY idx_recipe_title (title),
  KEY idx_recipe_status (status),
  KEY idx_recipe_active (is_active),
  KEY idx_recipe_build (reactor_build_id),
  CONSTRAINT fk_recipe_reactor_build
    FOREIGN KEY (reactor_build_id) REFERENCES reactor_build (reactor_build_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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
  last_error TEXT NULL,
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
  last_error TEXT NULL,
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

CREATE OR REPLACE VIEW v_latest_measurement_per_channel AS
SELECT m.*
FROM measurement m
JOIN (
  SELECT device_id, channel_code, MAX(measured_at) AS max_measured_at
  FROM measurement
  GROUP BY device_id, channel_code
) x
  ON x.device_id = m.device_id
 AND x.channel_code = m.channel_code
 AND x.max_measured_at = m.measured_at;
