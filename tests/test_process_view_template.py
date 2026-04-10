import unittest
from pathlib import Path

import config as app_config

from reactor_app import create_app


class ProcessViewTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED

        app_config.Config.SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False

        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required

    def test_process_view_uses_simplified_manual_controls(self):
        response = self.client.get("/process")
        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)

        self.assertIn("process-manual-settings-form", html)
        self.assertIn("process-manual-state-input", html)
        self.assertIn("process-manual-speed-input", html)
        self.assertIn("process-manual-submit-button", html)
        self.assertIn("process-manual-actual-rpm", html)
        self.assertIn("process-manual-torque-ncm", html)
        self.assertIn("process-plot-panel", html)
        self.assertIn("process-plot-selection", html)
        self.assertIn("process-plot-chart-stack", html)
        self.assertIn("process-plot-targets", html)

        forbidden_strings = (
            "Status lesen",
            "Aktor anwenden",
            "Direktbefehl",
            "Kein Befehl gesendet",
            "LIVE-WERTE",
            "Geraetestatus",
            "Gerätestatus",
            "Protokollhinweis",
            "Actual Value Plot",
            "Select sensor and actuator values from the loaded flowsheet to plot their recent measurements.",
            "Checkboxes are derived from the mapped sensors and actuators on this flowsheet.",
        )
        for text in forbidden_strings:
            self.assertNotIn(text, html)

    def test_process_view_disables_html_caching(self):
        response = self.client.get("/process")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")

    def test_process_view_script_no_longer_contains_legacy_manual_ui_labels(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        forbidden_strings = (
            "Status lesen",
            "Aktor anwenden",
            "Direktbefehl",
            "Kein Befehl gesendet",
            "LIVE-WERTE",
            "Geraetestatus",
            "Gerätestatus",
            "Protokollhinweis",
        )
        for text in forbidden_strings:
            self.assertNotIn(text, source)

    def test_process_view_script_keeps_verified_ika_workflow_order(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('await sendManualCommand("START_4", { quiet: true })', source)
        self.assertIn('await sendManualCommand(`OUT_SP_4 ${speed}`, { quiet: true })', source)
        self.assertLess(
            source.index('await sendManualCommand("START_4", { quiet: true })'),
            source.index('await sendManualCommand(`OUT_SP_4 ${speed}`, { quiet: true })'),
        )
        self.assertIn('await sendManualCommand("IN_SP_4", { quiet: true })', source)
        self.assertIn('await sendManualCommand("IN_PV_4", { quiet: true })', source)
        self.assertIn('await sendManualCommand("IN_PV_5", { quiet: true })', source)

    def test_process_view_script_preserves_dirty_manual_inputs_during_refresh(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("inputsDirtyForNodeId", source)
        self.assertIn('manualStateInput?.addEventListener("change", () => {', source)
        self.assertIn('manualSpeedInput?.addEventListener("input", () => {', source)
        self.assertIn(
            'renderOperatorControls(currentNode, target, { preserveInputs: shouldPreserveManualInputs(nodeId) });',
            source,
        )
        self.assertIn("clearManualInputsDirty(node.id);", source)

    def test_process_view_script_updates_device_status_from_telemetry(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("function updateManualDeviceStatus(target, telemetry)", source)
        self.assertIn("function setManualStatusFromTelemetry(telemetry, options)", source)
        self.assertIn("updateManualDeviceStatus(target, telemetry);", source)
        self.assertIn("setManualStatusFromTelemetry(telemetry, {", source)

    def test_process_view_script_supports_dynamic_plot_series(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('const plotTargetData = parseJsonScript("process-plot-targets", {});', source)
        self.assertIn("function buildPlotSeriesOptions(targets)", source)
        self.assertIn("function renderPlotSelection()", source)
        self.assertIn("function loadPlotMeasurements(options)", source)
        self.assertIn("renderPlotSelection();", source)
        self.assertIn("void loadPlotMeasurements({ quiet: true });", source)

    def test_process_view_script_groups_plot_series_by_unit(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('const unitKey = asString(series.unit, "");', source)
        self.assertIn("fragment.appendChild(renderPlotChartCard(unitKey, group));", source)
        self.assertIn("Selected series with the same unit are rendered together.", source)

    def test_process_view_script_uses_runtime_plot_fallback_for_ika_motor(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('option.dataSource === "runtime_fallback"', source)
        self.assertIn("function syncRuntimePlotTelemetry(nodeId, telemetry, timestampMs)", source)
        self.assertIn("await ensureRuntimePlotSamples(runtimeOptions);", source)
        self.assertIn("syncRuntimePlotTelemetry(nodeId, telemetry, Date.now());", source)
        self.assertIn("syncRuntimePlotTelemetry(node.id, telemetry, Date.now());", source)

    def test_process_view_server_adds_ika_plot_fallback_channels(self):
        source = (Path(__file__).resolve().parents[1] / "reactor_app" / "web.py").read_text(encoding="utf-8")

        self.assertIn("def _fallback_plot_channels_for_target", source)
        self.assertIn('"channel_code": "ika_actual_rpm"', source)
        self.assertIn('"channel_code": "ika_torque_ncm"', source)
        self.assertIn('"data_source": "runtime_fallback"', source)

    def test_collapsible_ui_uses_shared_chevron_and_animation_styles(self):
        repo_root = Path(__file__).resolve().parents[1]
        process_template = (repo_root / "templates" / "process.html").read_text(encoding="utf-8")
        builder_template = (repo_root / "templates" / "reactor_builder.html").read_text(encoding="utf-8")
        stylesheet = (repo_root / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn("process-plot-panel ui-collapsible-details", process_template)
        self.assertIn("process-plot-summary ui-collapsible-summary", process_template)
        self.assertIn("ui-collapsible-chevron", process_template)
        self.assertIn("builder-category ui-collapsible-details", builder_template)
        self.assertIn("builder-category-summary ui-collapsible-summary", builder_template)
        self.assertIn("ui-collapsible-chevron", builder_template)
        self.assertIn("--collapsible-duration", stylesheet)
        self.assertIn(".ui-collapsible-panel", stylesheet)
        self.assertIn(".ui-collapsible-chevron svg", stylesheet)
        self.assertIn(".process-manual-card.is-panel-collapsed .process-manual-body", stylesheet)


if __name__ == "__main__":
    unittest.main()
