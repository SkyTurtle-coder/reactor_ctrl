import re
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

    def setUp(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True

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
        self.assertIn("process-plot-window-value", html)
        self.assertIn("process-plot-window-unit", html)
        self.assertIn("process-plot-targets", html)
        self.assertIn("process-source-build-btn", html)
        self.assertIn("process-source-recipe-btn", html)
        self.assertIn("process-recipe-select", html)
        self.assertIn("process-program-card", html)
        self.assertIn("process-program-start-button", html)
        self.assertIn("process-program-stop-button", html)
        self.assertIn("process-program-stop-dialog", html)
        self.assertIn("process-confirm-dialog-icon", html)

        forbidden_strings = (
            "Read status",
            "Apply actuator",
            "Direct command",
            "No command sent",
            "LIVE VALUES",
            "Device status",
            "Protocol note",
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

    def test_process_view_template_starts_plot_panel_collapsed(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "process.html"
        source = template_path.read_text(encoding="utf-8")

        self.assertIn('<details class="card process-plot-panel ui-collapsible-details" id="process-plot-panel">', source)
        self.assertNotIn('id="process-plot-panel" {% if selected_build %}open{% endif %}', source)

    def test_process_stop_dialog_uses_opaque_warning_panel(self):
        css_path = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
        source = css_path.read_text(encoding="utf-8")

        self.assertIn(".process-confirm-dialog-icon", source)
        self.assertIn("background: var(--surface);", source)
        self.assertNotIn("background: var(--card);", source)

    def test_process_view_script_no_longer_contains_legacy_manual_ui_labels(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        forbidden_strings = (
            "Read status",
            "Apply actuator",
            "Direct command",
            "No command sent",
            "LIVE VALUES",
            "Device status",
            "Protocol note",
        )
        for text in forbidden_strings:
            self.assertNotIn(text, source)

    def test_process_view_script_uses_manual_state_snapshot_and_queue_endpoints(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('fetchJson(`/api/devices/${target.device_id}/manual-state?${params.toString()}`', source)
        self.assertIn('fetchJson(`/api/devices/${target.device_id}/manual-state`, {', source)
        self.assertIn('requested_by: "process_manual"', source)
        self.assertIn('params.set("watch", settings.watch === false ? "0" : "1");', source)
        self.assertIn('params.set("refresh", "1");', source)
        self.assertNotIn('sendManualCommand("START_4"', source)
        self.assertNotIn('sendManualCommand(`OUT_SP_4 ${speed}`', source)
        self.assertNotIn('sendManualCommand("IN_SP_4"', source)
        self.assertNotIn('sendManualCommand("IN_PV_4"', source)
        self.assertNotIn('sendManualCommand("IN_PV_5"', source)

    def test_process_view_script_supports_recipe_program_selection_and_runtime(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('document.getElementById("process-recipe-select")', source)
        self.assertIn('document.getElementById("process-program-start-button")', source)
        self.assertIn('document.getElementById("process-program-stop-button")', source)
        self.assertIn('document.getElementById("process-program-stop-dialog")', source)
        self.assertIn('function confirmRecipeProgramStop()', source)
        self.assertIn('window.confirm("Are You sure?")', source)
        self.assertIn('normalizeStoppedProgramPayload', source)
        self.assertIn('fetchJson("/api/process-program"', source)
        self.assertIn('fetchJson("/api/process-program/start"', source)
        self.assertIn('fetchJson("/api/process-program/stop"', source)
        self.assertIn('navigateToProcessSelection("recipe", recipeId);', source)
        self.assertIn('navigateToProcessSelection("build", buildId);', source)

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
        self.assertIn("function applyManualStateSnapshot(nodeId, target, snapshot, options)", source)
        self.assertIn("clearManualInputsDirty(node.id);", source)
        self.assertIn('manualToggleButton.textContent = "Manual";', source)
        self.assertNotIn('manualToggleButton.textContent = state.manualMode ? "Disable" : "Enable";', source)

    def test_process_view_script_updates_device_status_from_telemetry(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("function updateManualDeviceStatus(target, telemetry)", source)
        self.assertIn("function setManualStatusFromTelemetry(telemetry, options)", source)
        self.assertIn("updateManualDeviceStatus(target, telemetry);", source)
        self.assertIn("setManualStatusFromTelemetry(telemetry, {", source)

    def test_process_view_script_uses_unistat_manual_setpoint_limits(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("function huberSetpointLimits(target)", source)
        self.assertIn("return { min: -40, max: 150 };", source)
        self.assertIn('protocol === "huber_cc230"', source)
        self.assertIn("function isCC230ThermostatTarget(node, target)", source)
        self.assertIn('document.getElementById("process-manual-sensor-input")', source)
        self.assertIn('"select_external_sensor"', source)
        self.assertIn('"select_internal_sensor"', source)
        self.assertIn("manualSpeedInput.min = String(limits.min);", source)
        self.assertIn("manualSpeedInput.max = String(limits.max);", source)
        self.assertIn("{ temp_c: setpointC, min_setpoint_c: limits.min, max_setpoint_c: limits.max }", source)

    def test_process_view_script_supports_dynamic_plot_series(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('const plotTargetData = parseJsonScript("process-plot-targets", {});', source)
        self.assertIn("function buildPlotSeriesOptions(targets)", source)
        self.assertIn("function groupStoredPlotOptionsByDevice(options)", source)
        self.assertIn("function defaultLivePlotSeriesIds()", source)
        self.assertIn("function renderPlotSelection()", source)
        self.assertIn("function loadPlotMeasurements(options)", source)
        self.assertIn("const PROCESS_PLOT_REFRESH_MS = 1000;", source)
        self.assertIn("const PROCESS_PLOT_LIVE_CACHE_SECONDS = 1;", source)
        self.assertIn('document.getElementById("process-plot-window-value")', source)
        self.assertIn('document.getElementById("process-plot-window-unit")', source)
        self.assertIn('params.set("since_seconds", String(rangeOption.sinceSeconds));', source)
        self.assertIn('params.set("max_points", String(rangeOption.maxPoints));', source)
        self.assertIn('params.set("cache_seconds", String(PROCESS_PLOT_LIVE_CACHE_SECONDS));', source)
        self.assertIn('params.append("series", seriesKey);', source)
        self.assertIn('`/api/plot-series/live?${params.toString()}`', source)
        self.assertIn("function normalizePlotWindow(payloads, requestedWindowEndIso, rangeOption)", source)
        self.assertIn("function attachPlotHover(frame, seriesItems, bounds)", source)
        self.assertIn("process-plot-tooltip", source)
        self.assertIn("data-plot-crosshair", source)
        self.assertIn("state.plotBackoffUntil = Date.now() + PROCESS_PLOT_ERROR_BACKOFF_MS;", source)
        self.assertIn("plotWindowValue", source)
        self.assertIn("plotWindowUnit", source)
        self.assertIn("plotPanelOpen", source)
        self.assertIn("plotWindow", source)
        self.assertIn("hasPersistedPlotSeriesSelection", source)
        self.assertIn("Array.from(new Set(persistedPlotSeriesIds))", source)
        self.assertNotIn("new Set([...liveDefaultPlotSeriesIds, ...persistedPlotSeriesIds])", source)
        self.assertIn("renderPlotSelection();", source)
        self.assertIn("void loadPlotMeasurements({ quiet: true });", source)

    def test_process_view_script_groups_plot_series_by_unit(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn('const unitKey = asString(series.unit, "");', source)
        self.assertIn("fragment.appendChild(renderPlotChartCard(unitKey, group, plotWindow || state.plotWindow));", source)
        self.assertIn("Selected series with the same unit are rendered together.", source)

    def test_process_view_styles_keep_displays_transparent_and_highlight_actors(self):
        stylesheet = (Path(__file__).resolve().parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn(".builder-display-box", stylesheet)
        self.assertIn("background: transparent;", stylesheet)
        self.assertIn(".process-node.is-program-active .builder-node-body", stylesheet)
        self.assertIn(".process-node.is-selected .builder-node-body", stylesheet)

    def test_process_view_script_uses_measurement_only_plot_loading(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertNotIn("runtime_fallback", source)
        self.assertNotIn("function syncRuntimePlotTelemetry(nodeId, telemetry, timestampMs)", source)
        self.assertNotIn("function loadRuntimePlotSnapshot(nodeId, options)", source)
        self.assertNotIn("ensureRuntimePlotSamples", source)
        self.assertIn("const seriesItems = storedSeries;", source)
        self.assertIn("No data in this window", source)

    def test_process_view_api_supports_manual_state_endpoints(self):
        source = (Path(__file__).resolve().parents[1] / "reactor_app" / "api.py").read_text(encoding="utf-8")

        self.assertIn('@api_bp.get("/devices/<int:device_id>/manual-state")', source)
        self.assertIn('@api_bp.post("/devices/<int:device_id>/manual-state")', source)
        self.assertIn('process-program/(start|stop)', source)
        self.assertIn('@api_bp.get("/process-program")', source)
        self.assertIn('@api_bp.post("/process-program/start")', source)
        self.assertIn('@api_bp.post("/process-program/stop")', source)

    def test_process_view_server_adds_expected_ika_measurement_channels(self):
        source = (Path(__file__).resolve().parents[1] / "reactor_app" / "web.py").read_text(encoding="utf-8")

        self.assertIn("def _default_measurement_plot_channels_for_target", source)
        self.assertIn('"channel_code": "ika_actual_rpm"', source)
        self.assertIn('"channel_code": "ika_torque_ncm"', source)
        self.assertIn('"channel_code": "setpoint_C"', source)
        self.assertIn('"channel_code": "actual_temp_C"', source)
        self.assertIn('"channel_code": "bath_temp_C"', source)
        self.assertIn('"channel_code": "internal_temp_C"', source)
        self.assertIn('"channel_code": "external_temp_C"', source)
        self.assertIn('"data_source": "measurement"', source)
        self.assertIn('"port_number": connection.port_number if connection is not None else None', source)

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
        self.assertIn(".ui-collapsible-details[open] > .ui-collapsible-panel", stylesheet)

    def test_process_trends_selection_is_content_sized(self):
        stylesheet = (Path(__file__).resolve().parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")
        selection_match = re.search(r"\.process-plot-selection\s*\{(?P<body>[^}]*)\}", stylesheet, re.DOTALL)

        self.assertIsNotNone(selection_match)
        selection_body = selection_match.group("body")
        self.assertIn("min-width: 0;", selection_body)
        self.assertNotIn("max-height", selection_body)
        self.assertNotRegex(selection_body, r"\boverflow(?:-[xy])?\s*:")
        self.assertIn("align-items: start;", stylesheet)
        self.assertIn("overflow-wrap: anywhere;", stylesheet)


if __name__ == "__main__":
    unittest.main()
