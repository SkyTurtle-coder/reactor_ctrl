-- reactor_ctrl - MySQL schema v1
-- Ziel: Serverbetrieb mit mehreren USB-RS485-Adaptern, Geraeteerkennung und Historisierung
-- Voraussetzung: MySQL 8.0+

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE DATABASE IF NOT EXISTS reactor_ctrl
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE reactor_ctrl;

CREATE TABLE IF NOT EXISTS usb_hub (
  hub_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  hub_name VARCHAR(100) NOT NULL,
  hub_serial VARCHAR(128) NULL,
  host_name VARCHAR(100) NULL,
  physical_location VARCHAR(255) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (hub_id),
  UNIQUE KEY uq_usb_hub_serial (hub_serial),
  KEY idx_usb_hub_host (host_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS serial_adapter (
  adapter_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  adapter_uid CHAR(36) NOT NULL,
  hub_id BIGINT UNSIGNED NULL,
  adapter_label VARCHAR(100) NULL,
  usb_vendor_id VARCHAR(8) NULL,
  usb_product_id VARCHAR(8) NULL,
  usb_serial VARCHAR(128) NULL,
  usb_location_path VARCHAR(255) NULL,
  driver_info VARCHAR(255) NULL,
  last_seen_port VARCHAR(64) NULL,
  last_seen_at DATETIME(3) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (adapter_id),
  UNIQUE KEY uq_adapter_uid (adapter_uid),
  UNIQUE KEY uq_adapter_usb_serial (usb_serial),
  KEY idx_adapter_hub (hub_id),
  KEY idx_adapter_port (last_seen_port),
  CONSTRAINT fk_adapter_hub
    FOREIGN KEY (hub_id) REFERENCES usb_hub (hub_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS rs485_bus (
  bus_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  adapter_id BIGINT UNSIGNED NOT NULL,
  bus_name VARCHAR(100) NOT NULL,
  protocol VARCHAR(32) NOT NULL DEFAULT 'modbus_rtu',
  baud_rate INT UNSIGNED NOT NULL DEFAULT 9600,
  data_bits TINYINT UNSIGNED NOT NULL DEFAULT 8,
  parity CHAR(1) NOT NULL DEFAULT 'N',
  stop_bits TINYINT UNSIGNED NOT NULL DEFAULT 1,
  poll_interval_ms INT UNSIGNED NOT NULL DEFAULT 1000,
  timeout_ms INT UNSIGNED NOT NULL DEFAULT 1200,
  is_enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (bus_id),
  UNIQUE KEY uq_bus_adapter (adapter_id),
  UNIQUE KEY uq_bus_name (bus_name),
  KEY idx_bus_enabled (is_enabled),
  CONSTRAINT chk_bus_parity CHECK (parity IN ('N', 'E', 'O')),
  CONSTRAINT fk_bus_adapter
    FOREIGN KEY (adapter_id) REFERENCES serial_adapter (adapter_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS device_binding_current (
  device_id BIGINT UNSIGNED NOT NULL,
  bus_id BIGINT UNSIGNED NOT NULL,
  rs485_address SMALLINT UNSIGNED NOT NULL,
  register_profile VARCHAR(120) NULL,
  first_seen_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_seen_at DATETIME(3) NULL,
  is_online TINYINT(1) NOT NULL DEFAULT 0,
  quality_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
  PRIMARY KEY (device_id),
  UNIQUE KEY uq_binding_bus_address (bus_id, rs485_address),
  KEY idx_binding_online (is_online),
  CONSTRAINT chk_binding_address CHECK (rs485_address BETWEEN 1 AND 247),
  CONSTRAINT fk_binding_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_binding_bus
    FOREIGN KEY (bus_id) REFERENCES rs485_bus (bus_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS device_binding_history (
  binding_history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id BIGINT UNSIGNED NOT NULL,
  bus_id BIGINT UNSIGNED NOT NULL,
  rs485_address SMALLINT UNSIGNED NOT NULL,
  bound_from DATETIME(3) NOT NULL,
  bound_to DATETIME(3) NULL,
  reason VARCHAR(255) NULL,
  PRIMARY KEY (binding_history_id),
  KEY idx_binding_history_device_time (device_id, bound_from),
  KEY idx_binding_history_bus_addr_time (bus_id, rs485_address, bound_from),
  CONSTRAINT chk_binding_history_address CHECK (rs485_address BETWEEN 1 AND 247),
  CONSTRAINT fk_binding_history_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_binding_history_bus
    FOREIGN KEY (bus_id) REFERENCES rs485_bus (bus_id)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
  error_message VARCHAR(500) NULL,
  PRIMARY KEY (command_id),
  UNIQUE KEY uq_command_request_uuid (request_uuid),
  KEY idx_command_status_schedule (status, scheduled_for),
  KEY idx_command_device_time (device_id, requested_at),
  CONSTRAINT chk_command_status CHECK (status IN ('queued', 'sent', 'acked', 'failed', 'timeout', 'cancelled')),
  CONSTRAINT fk_command_device
    FOREIGN KEY (device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS discovery_run (
  discovery_run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  bus_id BIGINT UNSIGNED NOT NULL,
  started_by VARCHAR(100) NOT NULL DEFAULT 'system',
  started_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  finished_at DATETIME(3) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'running',
  notes VARCHAR(255) NULL,
  PRIMARY KEY (discovery_run_id),
  KEY idx_discovery_run_bus_time (bus_id, started_at),
  CONSTRAINT chk_discovery_run_status CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
  CONSTRAINT fk_discovery_run_bus
    FOREIGN KEY (bus_id) REFERENCES rs485_bus (bus_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS discovery_result (
  discovery_result_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  discovery_run_id BIGINT UNSIGNED NOT NULL,
  rs485_address SMALLINT UNSIGNED NOT NULL,
  protocol VARCHAR(32) NOT NULL,
  device_type_guess VARCHAR(64) NULL,
  manufacturer_serial VARCHAR(128) NULL,
  raw_identity JSON NULL,
  matched_device_id BIGINT UNSIGNED NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (discovery_result_id),
  UNIQUE KEY uq_discovery_run_addr (discovery_run_id, rs485_address),
  KEY idx_discovery_result_serial (manufacturer_serial),
  CONSTRAINT chk_discovery_result_address CHECK (rs485_address BETWEEN 1 AND 247),
  CONSTRAINT fk_discovery_result_run
    FOREIGN KEY (discovery_run_id) REFERENCES discovery_run (discovery_run_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE,
  CONSTRAINT fk_discovery_result_device
    FOREIGN KEY (matched_device_id) REFERENCES device (device_id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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
