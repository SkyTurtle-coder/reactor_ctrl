(function () {
    const surface = document.getElementById("process-flowsheet-surface");
    if (!surface) {
        return;
    }

    const edgeLayer = document.getElementById("process-edge-layer");
    const nodeLayer = document.getElementById("process-node-layer");
    const emptyState = document.getElementById("process-flowsheet-empty");
    const processPickerForm = document.getElementById("process-picker-form");
    const processSelectionModeInput = document.getElementById("process-selection-mode-input");
    const processSourceToggle = document.getElementById("process-source-toggle");
    const processSourceBuildButton = document.getElementById("process-source-build-btn");
    const processSourceRecipeButton = document.getElementById("process-source-recipe-btn");
    const processBuildField = document.getElementById("process-build-field");
    const processBuildSelect = document.getElementById("process-build-select");
    const processRecipeField = document.getElementById("process-recipe-field");
    const processRecipeSelect = document.getElementById("process-recipe-select");
    const processClearSelectionLink = document.getElementById("process-clear-selection");
    const manualToggleButton = document.getElementById("process-manual-mode-toggle");
    const programCard = document.getElementById("process-program-card");
    const programTitle = document.getElementById("process-program-title");
    const programSubtitle = document.getElementById("process-program-subtitle");
    const programStatusBadge = document.getElementById("process-program-status-badge");
    const programStartButton = document.getElementById("process-program-start-button");
    const programStopButton = document.getElementById("process-program-stop-button");
    const programRecipeName = document.getElementById("process-program-recipe-name");
    const programBuildName = document.getElementById("process-program-build-name");
    const programStepLabel = document.getElementById("process-program-step-label");
    const programTimeLabel = document.getElementById("process-program-time-label");
    const programProgressFill = document.getElementById("process-program-progress-fill");
    const programProgressLabel = document.getElementById("process-program-progress-label");
    const programTargetList = document.getElementById("process-program-target-list");
    const programStatus = document.getElementById("process-program-status");
    const programStopDialog = document.getElementById("process-program-stop-dialog");
    const manualCard = document.getElementById("process-manual-card");
    const manualTargetTitle = document.getElementById("process-manual-target-title");
    const manualTargetSubtitle = document.getElementById("process-manual-target-subtitle");
    const manualControls = document.getElementById("process-manual-controls");
    const manualSettingsForm = document.getElementById("process-manual-settings-form");
    const manualDeviceHeading = document.getElementById("process-manual-device-heading");
    const manualStateLabel = document.getElementById("process-manual-state-label");
    const manualValueLabel = document.getElementById("process-manual-value-label");
    const manualStateInput = document.getElementById("process-manual-state-input");
    const manualSpeedInput = document.getElementById("process-manual-speed-input");
    const manualSensorField = document.getElementById("process-manual-sensor-field");
    const manualSensorInput = document.getElementById("process-manual-sensor-input");
    const manualSubmitButton = document.getElementById("process-manual-submit-button");
    const manualPrimaryMetricLabel = document.getElementById("process-manual-primary-metric-label");
    const manualSecondaryMetricLabel = document.getElementById("process-manual-secondary-metric-label");
    const manualActualRpm = document.getElementById("process-manual-actual-rpm");
    const manualTorqueNcm = document.getElementById("process-manual-torque-ncm");
    const manualPort = document.getElementById("process-manual-port");
    const manualServer = document.getElementById("process-manual-server");
    const manualProtocol = document.getElementById("process-manual-protocol");
    const manualDeviceStatus = document.getElementById("process-manual-device-status");
    const manualStatus = document.getElementById("process-manual-status");
    const plotPanel = document.getElementById("process-plot-panel");
    const plotSelection = document.getElementById("process-plot-selection");
    const plotSelectionCount = document.getElementById("process-plot-selection-count");
    const plotSelectionEmpty = document.getElementById("process-plot-selection-empty");
    const plotRangeSelect = document.getElementById("process-plot-range-select");
    const plotChartStack = document.getElementById("process-plot-chart-stack");
    const plotStatus = document.getElementById("process-plot-status");
    const PROCESS_VIEW_STORAGE_KEY = "reactor_ctrl.processView.v3";
    const manualToggleInitiallyDisabled = Boolean(manualToggleButton?.disabled);
    const MANUAL_LIVE_POLL_MS = 1500;
    const PROCESS_PROGRAM_POLL_MS = 1200;
    const PROCESS_PLOT_REFRESH_MS = 5000;
    const PROCESS_PLOT_ERROR_BACKOFF_MS = 15000;
    const PROCESS_PLOT_LIVE_CACHE_SECONDS = 3;
    const PROCESS_PLOT_STALE_AFTER_MS = 60000;
    const PROCESS_PLOT_MAX_LOOKBACK_MINUTES = 30 * 24 * 60;
    const DEFAULT_PROCESS_PLOT_RANGE_ID = "1h";
    const PROCESS_PLOT_RANGE_OPTIONS = Object.freeze([
        { id: "30m", label: "Last 30 min", sinceMinutes: 30, maxPoints: 180 },
        { id: "1h", label: "Last hour", sinceMinutes: 60, maxPoints: 240 },
        { id: "5h", label: "Last 5 hours", sinceMinutes: 300, maxPoints: 600 },
        { id: "all", label: "All (max 30 days)", sinceMinutes: PROCESS_PLOT_MAX_LOOKBACK_MINUTES, maxPoints: 960 },
    ]);
    const PROCESS_PLOT_RANGE_OPTION_MAP = new Map(PROCESS_PLOT_RANGE_OPTIONS.map((option) => [option.id, option]));
    const PROCESS_PLOT_COLORS = [
        "#0f766e",
        "#dc2626",
        "#2563eb",
        "#ca8a04",
        "#7c3aed",
        "#0891b2",
        "#be123c",
        "#4d7c0f",
    ];

    function parseJsonScript(id, fallback) {
        const element = document.getElementById(id);
        if (!element || !element.textContent) {
            return fallback;
        }
        try {
            return JSON.parse(element.textContent);
        } catch (error) {
            console.error("Failed to parse JSON script", id, error);
            return fallback;
        }
    }

    function clamp(value, min, max) {
        return Math.max(min, Math.min(value, max));
    }

    function asNumber(value, fallback) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : fallback;
    }

    function asString(value, fallback) {
        const text = String(value ?? "").trim();
        return text || fallback;
    }

    function portNumberFromTarget(target) {
        const directPort = Number(target?.port_number);
        if (Number.isInteger(directPort) && directPort > 0) {
            return directPort;
        }
        const label = asString(target?.connection_label, "");
        const match = label.match(/\bport\s*(\d+)\b/i);
        if (!match) {
            return null;
        }
        const parsedPort = Number(match[1]);
        return Number.isInteger(parsedPort) && parsedPort > 0 ? parsedPort : null;
    }

    function normalizePlotRangeId(value) {
        const candidate = asString(value, DEFAULT_PROCESS_PLOT_RANGE_ID);
        return PROCESS_PLOT_RANGE_OPTION_MAP.has(candidate) ? candidate : DEFAULT_PROCESS_PLOT_RANGE_ID;
    }

    function plotRangeOptionForId(value) {
        return PROCESS_PLOT_RANGE_OPTION_MAP.get(normalizePlotRangeId(value)) || PROCESS_PLOT_RANGE_OPTIONS[1];
    }

    function boundedIntegerInputValue(inputElement, fallback) {
        const element = inputElement;
        if (!element) {
            return fallback;
        }
        const minValue = Number(element.min);
        const maxValue = Number(element.max);
        let nextValue = Math.round(asNumber(element.value, fallback));
        if (Number.isFinite(minValue)) {
            nextValue = Math.max(nextValue, minValue);
        }
        if (Number.isFinite(maxValue)) {
            nextValue = Math.min(nextValue, maxValue);
        }
        return nextValue;
    }

    function boundedNumberInputValue(inputElement, fallback) {
        const element = inputElement;
        if (!element) {
            return fallback;
        }
        const minValue = Number(element.min);
        const maxValue = Number(element.max);
        let nextValue = asNumber(element.value, fallback);
        if (Number.isFinite(minValue)) {
            nextValue = Math.max(nextValue, minValue);
        }
        if (Number.isFinite(maxValue)) {
            nextValue = Math.min(nextValue, maxValue);
        }
        return Math.round(nextValue * 100) / 100;
    }

    function formatRoundedMetric(value, unit, digits) {
        if (!Number.isFinite(value)) {
            return "-";
        }
        const precision = Number.isInteger(digits) ? digits : 0;
        return `${value.toFixed(precision)} ${unit}`;
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    function formatPlotTimestamp(timestampMs) {
        if (!Number.isFinite(timestampMs)) {
            return "";
        }
        return new Date(timestampMs).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
    }

    function formatPlotValue(value, unit) {
        if (!Number.isFinite(value)) {
            return "-";
        }
        const abs = Math.abs(value);
        const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
        return unit ? `${value.toFixed(digits)} ${unit}` : `${value.toFixed(digits)}`;
    }

    function cssThemeValue(name, fallback) {
        const computedValue = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return computedValue || fallback;
    }

    function plotThemeTokens() {
        return {
            panelFill: cssThemeValue("--plot-surface", "rgba(255,255,255,0.82)"),
            panelStroke: cssThemeValue("--plot-surface-border", "rgba(0,0,0,0.08)"),
            gridY: cssThemeValue("--plot-grid-y", "rgba(0,0,0,0.10)"),
            gridX: cssThemeValue("--plot-grid-x", "rgba(0,0,0,0.06)"),
            axis: cssThemeValue("--plot-axis", "rgba(0,0,0,0.20)"),
            label: cssThemeValue("--plot-label", "rgba(0,0,0,0.66)"),
            pointStroke: cssThemeValue("--plot-point-outline", "#ffffff"),
        };
    }

    function formatDurationLabel(totalSeconds) {
        if (!Number.isFinite(totalSeconds) || totalSeconds < 0) {
            return "-";
        }
        const rounded = Math.max(0, Math.round(totalSeconds));
        const hours = Math.floor(rounded / 3600);
        const minutes = Math.floor((rounded % 3600) / 60);
        const seconds = rounded % 60;
        if (hours > 0) {
            return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        }
        return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    function directionToSide(direction, xRatio, yRatio) {
        const normalized = String(direction || "").trim().toLowerCase();
        if (normalized === "west" || normalized === "east" || normalized === "north" || normalized === "south") {
            return normalized;
        }
        if (xRatio <= 0) {
            return "west";
        }
        if (xRatio >= 1) {
            return "east";
        }
        if (yRatio <= 0) {
            return "north";
        }
        if (yRatio >= 1) {
            return "south";
        }
        return "east";
    }

    function roundCanvasValue(value) {
        return Math.round(asNumber(value, 0) * 100) / 100;
    }

    function coerceBoolean(value, fallback) {
        if (typeof value === "boolean") {
            return value;
        }
        if (typeof value === "number" && (value === 0 || value === 1)) {
            return Boolean(value);
        }
        if (typeof value === "string") {
            const normalized = value.trim().toLowerCase();
            if (["true", "1", "yes", "y", "on", "ein"].includes(normalized)) {
                return true;
            }
            if (["false", "0", "no", "n", "off", "aus"].includes(normalized)) {
                return false;
            }
        }
        return fallback;
    }

    const actuatorProfileData = parseJsonScript("process-actuator-profiles", []);
    const supportedProtocolData = parseJsonScript("process-supported-protocols", []);
    const actuatorProfiles = Array.isArray(actuatorProfileData)
        ? actuatorProfileData
              .filter((profile) => profile && typeof profile === "object")
              .map((profile) => ({
                  id: asString(profile.id, ""),
                  label: asString(profile.label, profile.id || "Profil"),
                  allowed_symbols: Array.isArray(profile.allowed_symbols)
                      ? profile.allowed_symbols.map((item) => asString(item, "")).filter(Boolean)
                      : [],
                  fields: Array.isArray(profile.fields)
                      ? profile.fields
                            .filter((field) => field && typeof field === "object")
                            .map((field) => ({
                                key: asString(field.key, ""),
                                label: asString(field.label, field.key || "Feld"),
                                type: asString(field.type, "text"),
                                mode: asString(field.mode, ""),
                                unit: asString(field.unit, ""),
                                min: field.min == null ? null : asNumber(field.min, null),
                                max: field.max == null ? null : asNumber(field.max, null),
                                step: field.step == null ? null : asNumber(field.step, null),
                                default: field.default,
                            }))
                            .filter((field) => field.key)
                      : [],
                  command_sequence: Array.isArray(profile.command_sequence)
                      ? profile.command_sequence
                            .filter((item) => item && typeof item === "object")
                            .map((item) => ({
                                kind: asString(item.kind, ""),
                                field: asString(item.field, ""),
                                true: asString(item.true, ""),
                                false: asString(item.false, ""),
                                template: asString(item.template, ""),
                            }))
                      : [],
              }))
              .filter((profile) => profile.id)
        : [];
    const protocolLabelMap = new Map(
        (Array.isArray(supportedProtocolData) ? supportedProtocolData : [])
            .map((item) => {
                if (item && typeof item === "object") {
                    const id = asString(item.id, "");
                    return id ? [id, asString(item.label, id)] : null;
                }
                const id = asString(item, "");
                return id ? [id, id] : null;
            })
            .filter(Boolean),
    );
    const actuatorProfileById = new Map(actuatorProfiles.map((profile) => [profile.id, profile]));

    function profileForSymbol(symbolId) {
        return actuatorProfiles.find((profile) => profile.allowed_symbols.includes(String(symbolId || ""))) || null;
    }

    function normalizeProfileConfig(profile, config) {
        if (!profile) {
            return null;
        }
        const payload = config && typeof config === "object" ? config : {};
        const normalized = {};
        for (const field of profile.fields) {
            if (field.type === "boolean") {
                normalized[field.key] = coerceBoolean(payload[field.key], Boolean(field.default));
                continue;
            }

            const fallback = field.default == null ? 0 : field.default;
            let nextValue = asNumber(payload[field.key], asNumber(fallback, 0));
            if (field.min != null) {
                nextValue = Math.max(nextValue, field.min);
            }
            if (field.max != null) {
                nextValue = Math.min(nextValue, field.max);
            }
            if (field.mode === "int") {
                nextValue = Math.round(nextValue);
            } else {
                nextValue = Math.round(nextValue * 1000) / 1000;
            }
            normalized[field.key] = nextValue;
        }
        return normalized;
    }

    function defaultControlForSymbol(symbolId) {
        const profile = profileForSymbol(symbolId);
        if (!profile) {
            return null;
        }
        return {
            profile_id: profile.id,
            config: normalizeProfileConfig(profile, {}),
        };
    }

    function normalizeControl(control, symbolId) {
        const fallback = defaultControlForSymbol(symbolId);
        if (!fallback) {
            return null;
        }
        const payload = control && typeof control === "object" ? control : {};
        const profileId = asString(payload.profile_id, fallback.profile_id);
        const profile = actuatorProfileById.get(profileId) || actuatorProfileById.get(fallback.profile_id);
        if (!profile || !profile.allowed_symbols.includes(String(symbolId || ""))) {
            return fallback;
        }
        return {
            profile_id: profile.id,
            config: normalizeProfileConfig(profile, payload.config),
        };
    }

    function normalizeAnchor(anchor, index) {
        return {
            id: asString(anchor?.id, `anchor-${index + 1}`),
            x_ratio: clamp(asNumber(anchor?.x_ratio, 0.5), 0, 1),
            y_ratio: clamp(asNumber(anchor?.y_ratio, 0.5), 0, 1),
            side: anchor?.side ? asString(anchor.side, "") : null,
        };
    }

    function normalizeNode(node) {
        const width = Math.max(40, asNumber(node?.width, 120));
        const height = Math.max(40, asNumber(node?.height, 80));
        const anchors = Array.isArray(node?.anchors) && node.anchors.length > 0
            ? node.anchors.map(normalizeAnchor)
            : [
                  {
                      id: "center",
                      x_ratio: 0.5,
                      y_ratio: 0.5,
                      side: "east",
                  },
              ];
        return {
            id: asString(node?.id, ""),
            symbol_id: asString(node?.symbol_id, ""),
            instance_id: asString(node?.instance_id, ""),
            label: asString(node?.label, node?.symbol_id || "Symbol"),
            category: asString(node?.category, ""),
            svg_url: asString(node?.svg_url, ""),
            x: roundCanvasValue(node?.x),
            y: roundCanvasValue(node?.y),
            width,
            height,
            control: normalizeControl(node?.control, node?.symbol_id),
            anchors,
        };
    }

    function getNodeById(nodeId) {
        return state.nodes.find((node) => node.id === nodeId) || null;
    }

    function getAnchorById(node, anchorId) {
        if (!node || !Array.isArray(node.anchors) || node.anchors.length === 0) {
            return null;
        }
        if (!anchorId) {
            return node.anchors[0];
        }
        return node.anchors.find((anchor) => anchor.id === anchorId) || node.anchors[0] || null;
    }

    function normalizeEdge(edge, nodes) {
        const sourceNode = nodes.find((node) => node.id === String(edge?.source_node_id || "")) || null;
        const targetNode = nodes.find((node) => node.id === String(edge?.target_node_id || "")) || null;
        const sourceAnchor = getAnchorById(sourceNode, edge?.source_anchor_id);
        const targetAnchor = getAnchorById(targetNode, edge?.target_anchor_id);
        return {
            id: asString(edge?.id, ""),
            source_node_id: asString(edge?.source_node_id, ""),
            source_anchor_id: sourceAnchor ? sourceAnchor.id : null,
            target_node_id: asString(edge?.target_node_id, ""),
            target_anchor_id: targetAnchor ? targetAnchor.id : null,
            route_points: Array.isArray(edge?.route_points)
                ? edge.route_points
                      .filter((point) => point && typeof point === "object")
                      .map((point) => ({
                          x: roundCanvasValue(point.x),
                          y: roundCanvasValue(point.y),
                      }))
                : [],
        };
    }

    function anchorPoint(node, anchorId) {
        const anchor = getAnchorById(node, anchorId);
        if (!anchor) {
            return {
                x: node.x + node.width / 2,
                y: node.y + node.height / 2,
            };
        }
        return {
            x: node.x + node.width * anchor.x_ratio,
            y: node.y + node.height * anchor.y_ratio,
        };
    }

    function anchorSide(node, anchorId) {
        const anchor = getAnchorById(node, anchorId);
        if (!anchor) {
            return "east";
        }
        return anchor.side || directionToSide("", anchor.x_ratio, anchor.y_ratio);
    }

    function offsetPoint(point, side, distance) {
        if (side === "west") {
            return { x: point.x - distance, y: point.y };
        }
        if (side === "east") {
            return { x: point.x + distance, y: point.y };
        }
        if (side === "north") {
            return { x: point.x, y: point.y - distance };
        }
        return { x: point.x, y: point.y + distance };
    }

    function compressOrthogonalPoints(points) {
        const compressed = [];
        for (const point of points) {
            const rounded = {
                x: roundCanvasValue(point.x),
                y: roundCanvasValue(point.y),
            };
            const previous = compressed[compressed.length - 1];
            if (previous && previous.x === rounded.x && previous.y === rounded.y) {
                continue;
            }
            compressed.push(rounded);
            if (compressed.length < 3) {
                continue;
            }

            const a = compressed[compressed.length - 3];
            const b = compressed[compressed.length - 2];
            const c = compressed[compressed.length - 1];
            const vertical = a.x === b.x && b.x === c.x;
            const horizontal = a.y === b.y && b.y === c.y;
            if (vertical || horizontal) {
                compressed.splice(compressed.length - 2, 1);
            }
        }
        return compressed;
    }

    function buildAutoEdgePoints(sourcePoint, sourceSide, targetPoint, targetSide, obstacles) {
        const stubDistance = 28;
        const obs = Array.isArray(obstacles) ? obstacles : [];
        const sourceStub = offsetPoint(sourcePoint, sourceSide, stubDistance);
        const targetStub = offsetPoint(targetPoint, targetSide, stubDistance);
        const sourceHorizontal = sourceSide === "west" || sourceSide === "east";
        const targetHorizontal = targetSide === "west" || targetSide === "east";
        const points = [sourcePoint, sourceStub];

        if (sourceHorizontal && targetHorizontal) {
            let middleX = roundCanvasValue((sourceStub.x + targetStub.x) / 2);
            if (obs.length > 0) middleX = findClearX(middleX, sourceStub.y, targetStub.y, obs);
            points.push({ x: middleX, y: sourceStub.y });
            points.push({ x: middleX, y: targetStub.y });
        } else if (!sourceHorizontal && !targetHorizontal) {
            let middleY = roundCanvasValue((sourceStub.y + targetStub.y) / 2);
            if (obs.length > 0) middleY = findClearY(middleY, sourceStub.x, targetStub.x, obs);
            points.push({ x: sourceStub.x, y: middleY });
            points.push({ x: targetStub.x, y: middleY });
        } else if (sourceHorizontal) {
            let cornerX = targetStub.x;
            let cornerY = sourceStub.y;
            if (obs.length > 0) {
                const blocked =
                    obs.some((ob) => hSegHitsBox(cornerY, sourceStub.x, cornerX, ob)) ||
                    obs.some((ob) => vSegHitsBox(cornerX, cornerY, targetStub.y, ob));
                if (blocked) {
                    cornerX = sourceStub.x;
                    cornerY = targetStub.y;
                }
            }
            points.push({ x: cornerX, y: cornerY });
        } else {
            let cornerX = sourceStub.x;
            let cornerY = targetStub.y;
            if (obs.length > 0) {
                const blocked =
                    obs.some((ob) => vSegHitsBox(cornerX, sourceStub.y, cornerY, ob)) ||
                    obs.some((ob) => hSegHitsBox(cornerY, cornerX, targetStub.x, ob));
                if (blocked) {
                    cornerX = targetStub.x;
                    cornerY = sourceStub.y;
                }
            }
            points.push({ x: cornerX, y: cornerY });
        }

        points.push(targetStub);
        points.push(targetPoint);
        return compressOrthogonalPoints(points);
    }

    function edgeRoutePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide) {
        if (Array.isArray(edge.route_points) && edge.route_points.length > 0) {
            return edge.route_points.map((point) => ({
                x: roundCanvasValue(point.x),
                y: roundCanvasValue(point.y),
            }));
        }
        const excludeIds = [edge.source_node_id, edge.target_node_id].filter(Boolean);
        const obstacles = state.nodes
            .filter((node) => !excludeIds.includes(node.id))
            .map((node) => nodeHitBox(node, 18));
        return buildAutoEdgePoints(sourcePoint, sourceSide, targetPoint, targetSide, obstacles).slice(1, -1);
    }

    function edgePolylinePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide) {
        return [sourcePoint, ...edgeRoutePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide), targetPoint];
    }

    function edgePathFromPoints(points) {
        return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
    }

    // --- Bridge / crossing helpers ---

    function segmentIntersect(p1, p2, p3, p4) {
        const h12 = Math.abs(p1.y - p2.y) < 0.5;
        const h34 = Math.abs(p3.y - p4.y) < 0.5;
        if (h12 === h34) return null;
        const hP = h12 ? [p1, p2] : [p3, p4];
        const vP = h12 ? [p3, p4] : [p1, p2];
        const hY = hP[0].y;
        const hX1 = Math.min(hP[0].x, hP[1].x);
        const hX2 = Math.max(hP[0].x, hP[1].x);
        const vX = vP[0].x;
        const vY1 = Math.min(vP[0].y, vP[1].y);
        const vY2 = Math.max(vP[0].y, vP[1].y);
        const eps = 4;
        if (vX > hX1 + eps && vX < hX2 - eps && hY > vY1 + eps && hY < vY2 - eps) {
            return { x: vX, y: hY, onHoriz: h12 };
        }
        return null;
    }

    function collectBridgePoints(edgePolylines) {
        const BRIDGE_R = 8;
        const bridgeMap = new Map();
        for (let i = 0; i < edgePolylines.length; i++) {
            for (let j = i + 1; j < edgePolylines.length; j++) {
                const polyA = edgePolylines[i];
                const polyB = edgePolylines[j];
                if (!polyA || !polyB) continue;
                for (let a = 0; a < polyA.length - 1; a++) {
                    for (let b = 0; b < polyB.length - 1; b++) {
                        const cross = segmentIntersect(polyA[a], polyA[a + 1], polyB[b], polyB[b + 1]);
                        if (!cross) continue;
                        if (cross.onHoriz) {
                            const hX1 = Math.min(polyA[a].x, polyA[a + 1].x);
                            const hX2 = Math.max(polyA[a].x, polyA[a + 1].x);
                            if (cross.x - BRIDGE_R > hX1 && cross.x + BRIDGE_R < hX2) {
                                if (!bridgeMap.has(i)) bridgeMap.set(i, []);
                                bridgeMap.get(i).push({ point: cross, segIndex: a });
                            }
                        } else {
                            const hX1 = Math.min(polyB[b].x, polyB[b + 1].x);
                            const hX2 = Math.max(polyB[b].x, polyB[b + 1].x);
                            if (cross.x - BRIDGE_R > hX1 && cross.x + BRIDGE_R < hX2) {
                                if (!bridgeMap.has(j)) bridgeMap.set(j, []);
                                bridgeMap.get(j).push({ point: cross, segIndex: b });
                            }
                        }
                    }
                }
            }
        }
        return bridgeMap;
    }

    function edgePathWithBridges(polylinePoints, bridges) {
        const BRIDGE_R = 8;
        if (!bridges || bridges.length === 0) return edgePathFromPoints(polylinePoints);
        const sorted = bridges.slice().sort((a, b) => {
            if (a.segIndex !== b.segIndex) return a.segIndex - b.segIndex;
            const p = polylinePoints[a.segIndex];
            return (Math.abs(a.point.x - p.x) + Math.abs(a.point.y - p.y)) -
                   (Math.abs(b.point.x - p.x) + Math.abs(b.point.y - p.y));
        });
        let d = "";
        for (let seg = 0; seg < polylinePoints.length - 1; seg++) {
            const sp = polylinePoints[seg];
            const ep = polylinePoints[seg + 1];
            if (seg === 0) d += `M ${sp.x} ${sp.y}`;
            const segBridges = sorted.filter((br) => br.segIndex === seg);
            if (segBridges.length === 0) {
                d += ` L ${ep.x} ${ep.y}`;
                continue;
            }
            const r = BRIDGE_R;
            const goingRight = ep.x >= sp.x;
            for (const br of segBridges) {
                const bx = br.point.x;
                const by = br.point.y;
                const x1 = goingRight ? bx - r : bx + r;
                const x2 = goingRight ? bx + r : bx - r;
                const sweep = goingRight ? 0 : 1;
                d += ` L ${x1} ${by} A ${r} ${r} 0 0 ${sweep} ${x2} ${by}`;
            }
            d += ` L ${ep.x} ${ep.y}`;
        }
        return d;
    }

    function nodeHitBox(node, margin) {
        return {
            x: node.x - margin,
            y: node.y - margin,
            w: node.width + 2 * margin,
            h: node.height + 2 * margin,
        };
    }

    function hSegHitsBox(y, x1, x2, box) {
        const start = Math.min(x1, x2);
        const end = Math.max(x1, x2);
        return y > box.y && y < box.y + box.h && end > box.x && start < box.x + box.w;
    }

    function vSegHitsBox(x, y1, y2, box) {
        const start = Math.min(y1, y2);
        const end = Math.max(y1, y2);
        return x > box.x && x < box.x + box.w && end > box.y && start < box.y + box.h;
    }

    function findClearX(preferred, y1, y2, obstacles) {
        if (!obstacles.some((ob) => vSegHitsBox(preferred, y1, y2, ob))) {
            return preferred;
        }
        for (let delta = 28; delta <= 280; delta += 28) {
            if (!obstacles.some((ob) => vSegHitsBox(preferred - delta, y1, y2, ob))) {
                return preferred - delta;
            }
            if (!obstacles.some((ob) => vSegHitsBox(preferred + delta, y1, y2, ob))) {
                return preferred + delta;
            }
        }
        return preferred;
    }

    function findClearY(preferred, x1, x2, obstacles) {
        if (!obstacles.some((ob) => hSegHitsBox(preferred, x1, x2, ob))) {
            return preferred;
        }
        for (let delta = 28; delta <= 280; delta += 28) {
            if (!obstacles.some((ob) => hSegHitsBox(preferred - delta, x1, x2, ob))) {
                return preferred - delta;
            }
            if (!obstacles.some((ob) => hSegHitsBox(preferred + delta, x1, x2, ob))) {
                return preferred + delta;
            }
        }
        return preferred;
    }

    function parseCanvasSize(definition, nodes) {
        const canvas = definition && typeof definition.canvas === "object" ? definition.canvas : {};
        const configuredWidth = Math.max(0, asNumber(canvas?.width, 0));
        const configuredHeight = Math.max(0, asNumber(canvas?.height, 0));
        if (configuredWidth >= 200 && configuredHeight >= 200) {
            return { width: configuredWidth, height: configuredHeight };
        }

        const padding = 160;
        let maxX = 1100;
        let maxY = 720;
        for (const node of nodes) {
            maxX = Math.max(maxX, node.x + node.width + padding);
            maxY = Math.max(maxY, node.y + node.height + padding);
        }
        return {
            width: roundCanvasValue(maxX),
            height: roundCanvasValue(maxY),
        };
    }

    function renderEdges() {
        while (edgeLayer.firstChild) {
            edgeLayer.removeChild(edgeLayer.firstChild);
        }
        edgeLayer.setAttribute("viewBox", `0 0 ${state.canvasSize.width} ${state.canvasSize.height}`);

        // First pass: compute all polylines for bridge detection
        const edgePolylines = state.edges.map((edge) => {
            const srcNode = getNodeById(edge.source_node_id);
            const tgtNode = getNodeById(edge.target_node_id);
            if (!srcNode || !tgtNode) return null;
            return edgePolylinePoints(
                edge,
                anchorPoint(srcNode, edge.source_anchor_id),
                anchorSide(srcNode, edge.source_anchor_id),
                anchorPoint(tgtNode, edge.target_anchor_id),
                anchorSide(tgtNode, edge.target_anchor_id),
            );
        });

        const bridgeMap = collectBridgePoints(edgePolylines);

        state.edges.forEach((edge, idx) => {
            const polylinePoints = edgePolylines[idx];
            if (!polylinePoints) return;
            const bridges = bridgeMap.get(idx) || [];
            const pathData = bridges.length > 0
                ? edgePathWithBridges(polylinePoints, bridges)
                : edgePathFromPoints(polylinePoints);
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("d", pathData);
            path.setAttribute("class", "builder-edge");
            edgeLayer.appendChild(path);
        });
    }

    function isActuator(node) {
        return String(node?.category || "").trim().toLowerCase() === "actuators";
    }

    function isTargetResolved(nodeId) {
        return Boolean(manualTargets[nodeId]?.is_resolved);
    }

    function hideManualCard() {
        if (manualCard) {
            manualCard.hidden = true;
        }
    }

    function showManualCard() {
        if (manualCard) {
            manualCard.hidden = false;
        }
    }

    function resetManualSummary() {
        if (manualTargetTitle) {
            manualTargetTitle.textContent = "-";
        }
        if (manualTargetSubtitle) {
            manualTargetSubtitle.textContent = "-";
        }
        if (manualPort) {
            manualPort.textContent = "-";
        }
        if (manualServer) {
            manualServer.textContent = "-";
        }
        if (manualProtocol) {
            manualProtocol.textContent = "-";
        }
        if (manualDeviceStatus) {
            manualDeviceStatus.textContent = "-";
        }
        if (manualActualRpm) {
            manualActualRpm.textContent = "-";
        }
        if (manualTorqueNcm) {
            manualTorqueNcm.textContent = "-";
        }
    }

    function selectActuator(nodeId) {
        if (isRecipeMode()) {
            setManualStatus("Manual control is disabled while recipe mode is selected.", "muted");
            return;
        }
        if (runningProgram()) {
            setManualStatus("Manual control is locked while a recipe program is running.", "muted");
            return;
        }
        if (!state.manualMode) {
            return;
        }
        if (state.isSending) {
            setManualStatus("Please wait until the current device request is finished.", "muted");
            return;
        }
        const node = getNodeById(nodeId);
        if (!node || !isActuator(node)) {
            return;
        }
        if (state.selectedNodeId !== nodeId) {
            clearManualInputsDirty();
        }
        state.selectedNodeId = nodeId;
        persistViewState();
        renderNodes();
        updateManualPanel();
        void loadManualStateSnapshot(nodeId, { refresh: true });
    }

    function renderNodes() {
        nodeLayer.innerHTML = "";
        const activeProgramActors = new Set(programActiveActors(state.programData));

        for (const node of state.nodes) {
            const element = document.createElement("article");
            element.className = "builder-node process-node";
            if (state.selectedNodeId === node.id) {
                element.classList.add("is-selected");
            }
            if (activeProgramActors.has(asString(node.instance_id))) {
                element.classList.add("is-program-active");
                element.title = `${node.instance_id || node.label}: current recipe step`;
            }
            if (state.manualMode && isActuator(node)) {
                element.classList.add("is-manual");
                if (isTargetResolved(node.id)) {
                    element.classList.add("is-manual-ready");
                    element.title = `${node.instance_id || node.label}: manual control available`;
                } else {
                    element.classList.add("is-manual-unresolved");
                    element.title = `${node.instance_id || node.label}: no valid communication mapping`;
                }
                element.tabIndex = 0;
                element.setAttribute("role", "button");
                element.addEventListener("click", () => {
                    selectActuator(node.id);
                });
                element.addEventListener("keydown", (event) => {
                    if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectActuator(node.id);
                    }
                });
            } else {
                element.tabIndex = -1;
            }

            element.style.left = `${node.x}px`;
            element.style.top = `${node.y}px`;
            element.style.width = `${node.width}px`;
            element.style.height = `${node.height}px`;

            const body = document.createElement("div");
            body.className = "builder-node-body";

            const graphic = document.createElement("div");
            graphic.className = "builder-node-graphic";
            if (node.svg_url) {
                const image = document.createElement("img");
                image.src = node.svg_url;
                image.alt = node.label;
                graphic.appendChild(image);
            }

            const label = document.createElement("div");
            label.className = "builder-node-label";

            const instance = document.createElement("span");
            instance.className = "builder-node-instance";
            instance.textContent = node.instance_id || node.label;

            const type = document.createElement("span");
            type.className = "builder-node-type";
            type.textContent = node.symbol_id;

            label.appendChild(instance);
            label.appendChild(type);
            body.appendChild(graphic);
            body.appendChild(label);
            element.appendChild(body);
            nodeLayer.appendChild(element);
        }
    }

    function updateEmptyState() {
        emptyState.classList.toggle("is-hidden", state.nodes.length > 0);
    }

    function setManualStatus(message, tone) {
        manualStatus.textContent = message;
        manualStatus.classList.remove("muted", "error-text", "builder-status-success");
        if (tone === "error") {
            manualStatus.classList.add("error-text");
            return;
        }
        if (tone === "success") {
            manualStatus.classList.add("builder-status-success");
            return;
        }
        manualStatus.classList.add("muted");
    }

    function syncManualModeToggle() {
        if (!manualToggleButton) {
            return;
        }
        manualToggleButton.disabled =
            manualToggleInitiallyDisabled ||
            state.isSending ||
            isRecipeMode() ||
            Boolean(runningProgram()) ||
            !activeBuildId;
        manualToggleButton.setAttribute("aria-pressed", String(state.manualMode));
        manualToggleButton.classList.toggle("btn-primary", state.manualMode);
        manualToggleButton.textContent = "Manual";
    }

    function waitFor(ms) {
        return new Promise((resolve) => {
            window.setTimeout(resolve, ms);
        });
    }

    async function readJsonResponse(response) {
        const rawText = await response.text();
        if (!rawText) {
            return {};
        }
        try {
            return JSON.parse(rawText);
        } catch (_error) {
            return { raw_text: rawText };
        }
    }

    function responseMessage(payload, fallback) {
        if (!payload || typeof payload !== "object") {
            return fallback;
        }

        const parts = [];
        if (typeof payload.error === "string" && payload.error.trim()) {
            parts.push(payload.error.trim());
        }
        if (typeof payload.details === "string" && payload.details.trim()) {
            parts.push(payload.details.trim());
        }
        if (!parts.length && typeof payload.raw_text === "string" && payload.raw_text.trim()) {
            parts.push(payload.raw_text.trim().slice(0, 240));
        }
        return parts.join(" ") || fallback;
    }

    function isRetryableStatus(status) {
        return [408, 425, 429, 502, 503, 504].includes(status);
    }

    async function fetchJson(url, options) {
        const requestOptions = options || {};
        const method = asString(requestOptions.method || "GET", "GET").toUpperCase();
        const timeoutMs = asNumber(requestOptions.timeoutMs, 20000);
        const maxRetries =
            requestOptions.maxRetries == null
                ? 0
                : Math.max(0, Math.round(asNumber(requestOptions.maxRetries, 0)));
        const { timeoutMs: _timeoutMs, maxRetries: _maxRetries, ...fetchOptions } = requestOptions;

        let lastError = null;
        for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
            const controller = new AbortController();
            const timer = window.setTimeout(() => controller.abort(), timeoutMs);

            try {
                const response = await window.fetch(url, {
                    ...fetchOptions,
                    headers: {
                        Accept: "application/json",
                        ...((fetchOptions && fetchOptions.headers) || {}),
                    },
                    cache: "no-store",
                    credentials: "same-origin",
                    signal: controller.signal,
                });
                const payload = await readJsonResponse(response);
                if (!response.ok) {
                    const error = new Error(responseMessage(payload, "Command could not be sent."));
                    error.status = response.status;
                    error.payload = payload;
                    throw error;
                }
                return payload;
            } catch (error) {
                const status = error?.status;
                const retryable =
                    attempt < maxRetries &&
                    method === "GET" &&
                    (error?.name === "AbortError" || error instanceof TypeError || isRetryableStatus(status));
                if (retryable) {
                    lastError = error;
                    await waitFor(250 * (attempt + 1));
                    continue;
                }
                if (error?.name === "AbortError") {
                    throw new Error("Request timeout. Please check the connection and server status.");
                }
                if (error?.payload) {
                    error.message = responseMessage(error.payload, error.message || "Command could not be sent.");
                }
                throw error;
            } finally {
                window.clearTimeout(timer);
            }
        }

        throw new Error(lastError?.message || "Command could not be sent.");
    }

    function setPlotStatus(message, tone) {
        if (!plotStatus) {
            return;
        }
        plotStatus.textContent = message;
        plotStatus.classList.remove("muted", "error-text", "builder-status-success");
        if (tone === "error") {
            plotStatus.classList.add("error-text");
            return;
        }
        if (tone === "success") {
            plotStatus.classList.add("builder-status-success");
            return;
        }
        plotStatus.classList.add("muted");
    }

    function buildPlotSeriesOptions(targets) {
        const options = [];
        for (const [nodeId, rawTarget] of Object.entries(targets || {})) {
            const target = rawTarget && typeof rawTarget === "object" ? rawTarget : {};
            const channels = Array.isArray(target.channels) ? target.channels : [];
            for (const channel of channels) {
                const valueType = asString(channel?.value_type, "float").toLowerCase();
                if (valueType === "text") {
                    continue;
                }
                const channelCode = asString(channel?.channel_code, "");
                const deviceId = Number(target?.device_id);
                if (!channelCode || !Number.isInteger(deviceId) || deviceId <= 0) {
                    continue;
                }
                options.push({
                    id: `${nodeId}::${channelCode}`,
                    nodeId,
                    nodeLabel: asString(target?.instance_id, asString(target?.label, nodeId)),
                    nodeSubtitle: asString(target?.label, asString(target?.symbol_id, "Element")),
                    category: asString(target?.category, ""),
                    isResolved: Boolean(target?.is_resolved),
                    isOnline: Boolean(target?.is_online),
                    qualityState: asString(target?.quality_state, ""),
                    portNumber: portNumberFromTarget(target),
                    deviceId,
                    deviceDisplayName: asString(target?.device_display_name, `Device ${deviceId}`),
                    channelCode,
                    channelLabel: asString(channel?.display_name, channelCode),
                    unit: asString(channel?.unit, ""),
                    valueType,
                    symbolId: asString(target?.symbol_id, ""),
                });
            }
        }

        return options.sort((left, right) => {
            const byCategory = left.category.localeCompare(right.category);
            if (byCategory !== 0) {
                return byCategory;
            }
            const leftPort = Number.isInteger(left.portNumber) ? left.portNumber : 9999;
            const rightPort = Number.isInteger(right.portNumber) ? right.portNumber : 9999;
            if (leftPort !== rightPort) {
                return leftPort - rightPort;
            }
            const byNode = left.nodeLabel.localeCompare(right.nodeLabel);
            if (byNode !== 0) {
                return byNode;
            }
            return left.channelLabel.localeCompare(right.channelLabel);
        });
    }

    function buildPlotNodeGroups(targets, options) {
        const optionLookup = new Map();
        for (const option of options) {
            const bucket = optionLookup.get(option.nodeId) || [];
            bucket.push(option);
            optionLookup.set(option.nodeId, bucket);
        }

        return Object.entries(targets || {})
            .map(([nodeId, rawTarget]) => {
                const target = rawTarget && typeof rawTarget === "object" ? rawTarget : {};
                const nodeOptions = optionLookup.get(nodeId) || [];
                return {
                    nodeId,
                    title: asString(target?.instance_id, asString(target?.label, nodeId)),
                    subtitle: asString(target?.label, asString(target?.symbol_id, "Element")),
                    category: asString(target?.category, ""),
                    isResolved: Boolean(target?.is_resolved),
                    resolutionNote: asString(target?.resolution_note, ""),
                    deviceDisplayName: asString(target?.device_display_name, ""),
                    options: nodeOptions,
                };
            })
            .sort((left, right) => {
                const byCategory = left.category.localeCompare(right.category);
                if (byCategory !== 0) {
                    return byCategory;
                }
                return left.title.localeCompare(right.title);
            });
    }

    function groupStoredPlotOptionsByDevice(options) {
        const groups = new Map();
        for (const option of Array.isArray(options) ? options : []) {
            const deviceId = Number(option?.deviceId);
            const channelCode = asString(option?.channelCode, "");
            if (!Number.isInteger(deviceId) || deviceId <= 0 || !channelCode) {
                continue;
            }
            const bucket = groups.get(deviceId) || { deviceId, options: [] };
            bucket.options.push(option);
            groups.set(deviceId, bucket);
        }
        return Array.from(groups.values());
    }

    function syncSelectedPlotSeriesIds() {
        const unique = Array.from(new Set(Array.isArray(state.selectedPlotSeriesIds) ? state.selectedPlotSeriesIds : []));
        state.selectedPlotSeriesIds = unique.filter((item) => plotSeriesOptionMap.has(item));
    }

    function selectedPlotSeriesOptions() {
        syncSelectedPlotSeriesIds();
        return state.selectedPlotSeriesIds.map((item) => plotSeriesOptionMap.get(item)).filter(Boolean);
    }

    function defaultLivePlotSeriesIds() {
        return plotSeriesOptions
            .filter((option) => option.isResolved && option.isOnline)
            .sort((left, right) => {
                const leftPort = Number.isInteger(left.portNumber) ? left.portNumber : 9999;
                const rightPort = Number.isInteger(right.portNumber) ? right.portNumber : 9999;
                if (leftPort !== rightPort) {
                    return leftPort - rightPort;
                }
                if (left.deviceId !== right.deviceId) {
                    return left.deviceId - right.deviceId;
                }
                return left.channelLabel.localeCompare(right.channelLabel);
            })
            .map((option) => option.id);
    }

    function updatePlotSelectionSummary() {
        if (plotSelectionCount) {
            const count = state.selectedPlotSeriesIds.length;
            plotSelectionCount.textContent = `${count} selected`;
        }
    }

    function renderPlotSelection() {
        if (!plotSelection) {
            return;
        }

        plotSelection.innerHTML = "";
        updatePlotSelectionSummary();

        if (!plotNodeGroups.length) {
            if (plotSelectionEmpty) {
                plotSelectionEmpty.hidden = false;
                plotSelectionEmpty.textContent = activeBuildId
                    ? "No plottable measurement channels are available for this flowsheet yet."
                    : "Select a flowsheet to list plottable actuator and sensor values.";
            }
            return;
        }

        if (plotSelectionEmpty) {
            plotSelectionEmpty.hidden = true;
        }

        const fragment = document.createDocumentFragment();
        for (const group of plotNodeGroups) {
            const section = document.createElement("section");
            section.className = "process-plot-group";

            const header = document.createElement("div");
            header.className = "process-plot-group-header";
            header.innerHTML = `
                <strong>${escapeHtml(group.title)}</strong>
                <p>${escapeHtml(group.subtitle)}${group.deviceDisplayName ? ` | ${escapeHtml(group.deviceDisplayName)}` : ""}</p>
            `;
            section.appendChild(header);

            if (!group.isResolved) {
                const note = document.createElement("p");
                note.className = "process-plot-group-note";
                note.textContent = group.resolutionNote || "This flowsheet element is not mapped to a live device.";
                section.appendChild(note);
                fragment.appendChild(section);
                continue;
            }

            if (!group.options.length) {
                const note = document.createElement("p");
                note.className = "process-plot-group-note";
                note.textContent = "No numeric measurement channels are available for this mapped device yet.";
                section.appendChild(note);
                fragment.appendChild(section);
                continue;
            }

            const optionsWrap = document.createElement("div");
            optionsWrap.className = "process-plot-group-options";
            for (const option of group.options) {
                const label = document.createElement("label");
                label.className = "process-plot-checkbox";
                label.innerHTML = `
                    <input type="checkbox" value="${escapeHtml(option.id)}" ${state.selectedPlotSeriesIds.includes(option.id) ? "checked" : ""}>
                    <span class="process-plot-checkbox-copy">
                        <strong>${escapeHtml(option.channelLabel)}</strong>
                        <span>${escapeHtml(option.channelCode)}${option.unit ? ` | ${escapeHtml(option.unit)}` : ""}</span>
                    </span>
                `;
                const input = label.querySelector("input");
                input?.addEventListener("change", () => {
                    if (input.checked) {
                        state.selectedPlotSeriesIds = [...state.selectedPlotSeriesIds, option.id];
                    } else {
                        state.selectedPlotSeriesIds = state.selectedPlotSeriesIds.filter((item) => item !== option.id);
                    }
                    syncSelectedPlotSeriesIds();
                    persistViewState();
                    updatePlotSelectionSummary();
                    void loadPlotMeasurements();
                });
                optionsWrap.appendChild(label);
            }

            section.appendChild(optionsWrap);
            fragment.appendChild(section);
        }

        plotSelection.appendChild(fragment);
    }

    function normalizePlotWindow(payloads, requestedWindowEndIso, rangeOption) {
        const payloadList = Array.isArray(payloads) ? payloads : (payloads ? [payloads] : []);
        const firstPayload = payloadList.find((payload) => payload?.window_start && payload?.window_end);
        const requestedWindowEndMs = Date.parse(asString(requestedWindowEndIso, ""));
        const fallbackEndMs = Number.isFinite(requestedWindowEndMs) ? requestedWindowEndMs : Date.now();
        const fallbackStartMs = fallbackEndMs - (Number(rangeOption?.sinceMinutes) || 60) * 60000;
        const startMs = Date.parse(asString(firstPayload?.window_start, ""));
        const endMs = Date.parse(asString(firstPayload?.window_end, ""));
        const normalizedStartMs = Number.isFinite(startMs) ? startMs : fallbackStartMs;
        const normalizedEndMs = Number.isFinite(endMs) && endMs > normalizedStartMs ? endMs : fallbackEndMs;
        return {
            startMs: normalizedStartMs,
            endMs: normalizedEndMs,
            bucketSeconds: asNumber(firstPayload?.bucket_seconds, null),
            requestedAtMs: fallbackEndMs,
            rangeLabel: asString(rangeOption?.label, "selected range"),
        };
    }

    function normalizePlotMeasurements(option, payloadSeriesItem) {
        const items = Array.isArray(payloadSeriesItem?.items) ? payloadSeriesItem.items : [];
        const latestMeasurementMs = Date.parse(asString(payloadSeriesItem?.latest_measurement_at, ""));
        const points = items
            .map((item) => {
                const timestamp = Date.parse(asString(item?.measured_at, ""));
                const numericValue = asNumber(item?.numeric_value, null);
                if (!Number.isFinite(timestamp) || !Number.isFinite(numericValue)) {
                    return null;
                }
                return {
                    x: timestamp,
                    y: numericValue,
                };
            })
            .filter(Boolean)
            .sort((left, right) => left.x - right.x);

        return {
            ...option,
            unit: asString(payloadSeriesItem?.unit || items[0]?.unit || option.unit, option.unit),
            latestMeasurementMs: Number.isFinite(latestMeasurementMs)
                ? latestMeasurementMs
                : (points[points.length - 1]?.x ?? null),
            points,
        };
    }

    function selectedPlotRangeOption() {
        return plotRangeOptionForId(state?.selectedPlotRangeId);
    }

    function plotColor(index) {
        return PROCESS_PLOT_COLORS[index % PROCESS_PLOT_COLORS.length];
    }

    function buildPlotPath(series, bounds) {
        return series.points
            .map((point, index) => {
                const x = bounds.left + ((point.x - bounds.minX) / (bounds.maxX - bounds.minX)) * bounds.width;
                const y = bounds.top + (1 - (point.y - bounds.minY) / (bounds.maxY - bounds.minY)) * bounds.height;
                return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
            })
            .join(" ");
    }

    function plotPointToSvg(point, bounds) {
        return {
            x: bounds.left + ((point.x - bounds.minX) / (bounds.maxX - bounds.minX)) * bounds.width,
            y: bounds.top + (1 - (point.y - bounds.minY) / (bounds.maxY - bounds.minY)) * bounds.height,
        };
    }

    function buildPlotTimeTicks(minX, maxX, count) {
        const tickCount = Math.max(2, count || 5);
        return Array.from({ length: tickCount }, (_item, index) => {
            const ratio = index / (tickCount - 1);
            return {
                value: minX + (maxX - minX) * ratio,
                xRatio: ratio,
            };
        });
    }

    function formatPlotHoverTimestamp(timestampMs) {
        if (!Number.isFinite(timestampMs)) {
            return "";
        }
        return new Date(timestampMs).toLocaleString([], {
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
    }

    function nearestPlotPoint(series, targetX) {
        if (!Array.isArray(series?.points) || !series.points.length || !Number.isFinite(targetX)) {
            return null;
        }
        let nearest = series.points[0];
        let nearestDistance = Math.abs(nearest.x - targetX);
        for (const point of series.points) {
            const distance = Math.abs(point.x - targetX);
            if (distance < nearestDistance) {
                nearest = point;
                nearestDistance = distance;
            }
        }
        return nearest;
    }

    function plotSeriesFreshness(series, plotWindow) {
        const latestMs = Number.isFinite(series?.latestMeasurementMs)
            ? series.latestMeasurementMs
            : (series?.points?.[series.points.length - 1]?.x ?? null);
        if (!Number.isFinite(latestMs)) {
            return { status: "no-data", label: "no data" };
        }
        const ageMs = Math.max(0, (plotWindow?.endMs ?? Date.now()) - latestMs);
        if (ageMs > PROCESS_PLOT_STALE_AFTER_MS) {
            return { status: "stale", label: `last ${formatPlotTimestamp(latestMs)}` };
        }
        return { status: "fresh", label: `live ${formatPlotTimestamp(latestMs)}` };
    }

    function attachPlotHover(frame, seriesItems, bounds) {
        const svg = frame.querySelector(".process-plot-chart-svg");
        const crosshair = frame.querySelector("[data-plot-crosshair]");
        const tooltip = frame.querySelector(".process-plot-tooltip");
        if (!svg || !crosshair || !tooltip) {
            return;
        }
        const markers = new Map(
            Array.from(frame.querySelectorAll("[data-plot-hover-marker]")).map((marker) => [
                Number(marker.getAttribute("data-plot-hover-marker")),
                marker,
            ]),
        );

        const hideHover = () => {
            crosshair.setAttribute("visibility", "hidden");
            tooltip.hidden = true;
            markers.forEach((marker) => {
                marker.setAttribute("visibility", "hidden");
            });
        };

        const showHover = (event) => {
            const svgRect = svg.getBoundingClientRect();
            if (svgRect.width <= 0 || svgRect.height <= 0) {
                hideHover();
                return;
            }
            const svgX = ((event.clientX - svgRect.left) / svgRect.width) * bounds.viewBoxWidth;
            if (svgX < bounds.left || svgX > bounds.left + bounds.width) {
                hideHover();
                return;
            }
            const ratio = clamp((svgX - bounds.left) / bounds.width, 0, 1);
            const targetX = bounds.minX + (bounds.maxX - bounds.minX) * ratio;
            const hoverItems = seriesItems
                .map((series, index) => {
                    const point = nearestPlotPoint(series, targetX);
                    if (!point) {
                        return null;
                    }
                    return {
                        index,
                        series,
                        point,
                        position: plotPointToSvg(point, bounds),
                    };
                })
                .filter(Boolean);

            if (!hoverItems.length) {
                hideHover();
                return;
            }

            const crosshairX = bounds.left + ratio * bounds.width;
            crosshair.setAttribute("x1", crosshairX.toFixed(2));
            crosshair.setAttribute("x2", crosshairX.toFixed(2));
            crosshair.setAttribute("visibility", "visible");

            markers.forEach((marker) => {
                marker.setAttribute("visibility", "hidden");
            });
            for (const item of hoverItems) {
                const marker = markers.get(item.index);
                if (!marker) {
                    continue;
                }
                marker.setAttribute("cx", item.position.x.toFixed(2));
                marker.setAttribute("cy", item.position.y.toFixed(2));
                marker.setAttribute("visibility", "visible");
            }

            tooltip.innerHTML = `
                <strong>${escapeHtml(formatPlotHoverTimestamp(targetX))}</strong>
                <ul>
                    ${hoverItems
                        .map(
                            (item) => `
                                <li>
                                    <span class="process-plot-tooltip-swatch" style="background:${plotColor(item.index)}"></span>
                                    <span>${escapeHtml(item.series.nodeLabel)} | ${escapeHtml(item.series.channelLabel)}</span>
                                    <strong>${escapeHtml(formatPlotValue(item.point.y, item.series.unit))}</strong>
                                    <small>${escapeHtml(formatPlotHoverTimestamp(item.point.x))}</small>
                                </li>
                            `,
                        )
                        .join("")}
                </ul>
            `;
            tooltip.hidden = false;

            const frameRect = frame.getBoundingClientRect();
            const tooltipWidth = tooltip.offsetWidth || 240;
            const tooltipHeight = tooltip.offsetHeight || 120;
            const cursorX = event.clientX - frameRect.left;
            const cursorY = event.clientY - frameRect.top;
            const gap = 16;
            // Place right of cursor; if it overflows, flip to the left so the
            // data point under the cursor stays visible.
            const leftIfRight = cursorX + gap;
            const leftIfLeft = cursorX - gap - tooltipWidth;
            const left = leftIfRight + tooltipWidth + 8 <= frameRect.width
                ? leftIfRight
                : Math.max(8, leftIfLeft);
            const top = clamp(cursorY - Math.round(tooltipHeight / 2), 8, Math.max(8, frameRect.height - tooltipHeight - 8));
            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${top}px`;
        };

        frame.addEventListener("pointermove", showHover);
        frame.addEventListener("pointerleave", hideHover);
        frame.addEventListener("pointercancel", hideHover);
    }

    function renderPlotChartCard(unitKey, seriesItems, plotWindow) {
        const card = document.createElement("article");
        card.className = "process-plot-chart-card";
        const unitLabel = unitKey || "unitless";
        const points = seriesItems.flatMap((series) => series.points);
        const theme = plotThemeTokens();

        const header = document.createElement("div");
        header.className = "process-plot-chart-head";
        header.innerHTML = `
            <div>
                <span class="section-label">Unit</span>
                <h3>${escapeHtml(unitLabel)}</h3>
                <p>Selected series with the same unit are rendered together.</p>
            </div>
        `;
        card.appendChild(header);

        const legend = document.createElement("ul");
        legend.className = "process-plot-legend";
        seriesItems.forEach((series, index) => {
            const latestPoint = series.points[series.points.length - 1] || null;
            const freshness = plotSeriesFreshness(series, plotWindow);
            const item = document.createElement("li");
            item.innerHTML = `
                <span class="process-plot-legend-swatch" style="background:${plotColor(index)}"></span>
                <span>${escapeHtml(series.nodeLabel)} | ${escapeHtml(series.channelLabel)}${latestPoint ? ` (${escapeHtml(formatPlotValue(latestPoint.y, series.unit))})` : " (no data)"}</span>
                <span class="process-plot-freshness is-${escapeHtml(freshness.status)}">${escapeHtml(freshness.label)}</span>
            `;
            legend.appendChild(item);
        });
        card.appendChild(legend);

        const fallbackEndMs = Date.now();
        const minX = Number.isFinite(plotWindow?.startMs) ? plotWindow.startMs : fallbackEndMs - 60 * 60000;
        const maxX = Number.isFinite(plotWindow?.endMs) && plotWindow.endMs > minX ? plotWindow.endMs : fallbackEndMs;
        let minY = points.length ? Math.min(...points.map((point) => point.y)) : 0;
        let maxY = points.length ? Math.max(...points.map((point) => point.y)) : 1;
        if (minY === maxY) {
            const padding = Math.max(Math.abs(minY) * 0.1, 1);
            minY -= padding;
            maxY += padding;
        } else {
            const padding = (maxY - minY) * 0.08;
            minY -= padding;
            maxY += padding;
        }

        const viewBoxWidth = 860;
        const viewBoxHeight = 280;
        const bounds = {
            left: 66,
            top: 18,
            width: 770,
            height: 212,
            minX,
            maxX,
            minY,
            maxY,
            viewBoxWidth,
            viewBoxHeight,
        };

        const yTicks = Array.from({ length: 5 }, (_item, index) => {
            const ratio = index / 4;
            const value = maxY - (maxY - minY) * ratio;
            const y = bounds.top + bounds.height * ratio;
            return { value, y };
        });
        const xTicks = buildPlotTimeTicks(minX, maxX, 5).map((tick) => ({
            value: tick.value,
            x: bounds.left + bounds.width * tick.xRatio,
        }));

        const gridLines = yTicks
            .map((tick) => `<line x1="${bounds.left}" y1="${tick.y.toFixed(2)}" x2="${(bounds.left + bounds.width).toFixed(2)}" y2="${tick.y.toFixed(2)}" stroke="${theme.gridY}" stroke-width="1"/>`)
            .join("");
        const xLines = xTicks
            .map((tick) => `<line x1="${tick.x.toFixed(2)}" y1="${bounds.top}" x2="${tick.x.toFixed(2)}" y2="${(bounds.top + bounds.height).toFixed(2)}" stroke="${theme.gridX}" stroke-width="1"/>`)
            .join("");
        const paths = seriesItems
            .map((series, index) => {
                if (!series.points.length) {
                    return "";
                }
                const latestPoint = series.points[series.points.length - 1];
                const latestPosition = plotPointToSvg(latestPoint, bounds);
                const pointPath = buildPlotPath(series, bounds);
                return `
                    <path d="${pointPath}" fill="none" stroke="${plotColor(index)}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
                    <circle cx="${latestPosition.x.toFixed(2)}" cy="${latestPosition.y.toFixed(2)}" r="4.5" fill="${plotColor(index)}" stroke="${theme.pointStroke}" stroke-width="2"/>
                `;
            })
            .join("");
        const hoverMarkers = seriesItems
            .map(
                (_series, index) =>
                    `<circle class="process-plot-hover-marker" data-plot-hover-marker="${index}" cx="0" cy="0" r="5.5" fill="${plotColor(index)}" stroke="${theme.pointStroke}" stroke-width="2.4" visibility="hidden"/>`,
            )
            .join("");
        const emptyOverlay = points.length
            ? ""
            : `<text x="${(bounds.left + bounds.width / 2).toFixed(2)}" y="${(bounds.top + bounds.height / 2).toFixed(2)}" text-anchor="middle" fill="${theme.label}" font-size="13">No data in this window</text>`;
        const yLabels = yTicks
            .map((tick) => `<text x="${bounds.left - 10}" y="${(tick.y + 4).toFixed(2)}" text-anchor="end" fill="${theme.label}" font-size="11">${escapeHtml(formatPlotValue(tick.value, unitKey))}</text>`)
            .join("");
        const xLabels = xTicks
            .map((tick) => `<text x="${tick.x.toFixed(2)}" y="${viewBoxHeight - 12}" text-anchor="middle" fill="${theme.label}" font-size="11">${escapeHtml(formatPlotTimestamp(tick.value))}</text>`)
            .join("");

        const frame = document.createElement("div");
        frame.className = "process-plot-chart-frame";
        frame.innerHTML = `
            <svg class="process-plot-chart-svg" viewBox="0 0 ${viewBoxWidth} ${viewBoxHeight}" role="img" aria-label="Trend plot for ${escapeHtml(unitLabel)} values">
                <rect x="${bounds.left}" y="${bounds.top}" width="${bounds.width}" height="${bounds.height}" rx="12" fill="${theme.panelFill}" stroke="${theme.panelStroke}"/>
                ${gridLines}
                ${xLines}
                <line x1="${bounds.left}" y1="${(bounds.top + bounds.height).toFixed(2)}" x2="${(bounds.left + bounds.width).toFixed(2)}" y2="${(bounds.top + bounds.height).toFixed(2)}" stroke="${theme.axis}" stroke-width="1.2"/>
                ${paths}
                ${hoverMarkers}
                ${emptyOverlay}
                <line data-plot-crosshair x1="${bounds.left}" y1="${bounds.top}" x2="${bounds.left}" y2="${(bounds.top + bounds.height).toFixed(2)}" stroke="${theme.axis}" stroke-width="1.2" stroke-dasharray="4 4" visibility="hidden"/>
                <rect class="process-plot-hover-zone" x="${bounds.left}" y="${bounds.top}" width="${bounds.width}" height="${bounds.height}" fill="transparent"/>
                ${yLabels}
                ${xLabels}
            </svg>
            <div class="process-plot-tooltip" hidden></div>
        `;
        card.appendChild(frame);
        attachPlotHover(frame, seriesItems, bounds);
        return card;
    }

    function renderPlotCharts(seriesItems, plotWindow) {
        if (!plotChartStack) {
            return;
        }
        plotChartStack.innerHTML = "";

        if (!seriesItems.length) {
            const empty = document.createElement("p");
            empty.className = "process-plot-chart-empty";
            empty.textContent = activeBuildId
                ? (plotSeriesOptions.length
                    ? "Select one or more values from the list to render a plot."
                    : "No plottable measurement channels are available for this flowsheet yet.")
                : "Load a flowsheet first to unlock measurement plots.";
            plotChartStack.appendChild(empty);
            return;
        }

        const seriesGroups = new Map();
        for (const series of seriesItems) {
            const unitKey = asString(series.unit, "");
            const bucket = seriesGroups.get(unitKey) || [];
            bucket.push(series);
            seriesGroups.set(unitKey, bucket);
        }

        const fragment = document.createDocumentFragment();
        for (const [unitKey, group] of seriesGroups.entries()) {
            fragment.appendChild(renderPlotChartCard(unitKey, group, plotWindow || state.plotWindow));
        }
        plotChartStack.appendChild(fragment);
    }

    function plotSeriesRequestKey(option) {
        return `${option.deviceId}:${option.channelCode}`;
    }

    async function loadPlotMeasurements(options) {
        const settings = options || {};
        if (!plotChartStack || !plotStatus) {
            return;
        }
        if (settings.quiet && state.isPlotBusy) {
            return;
        }

        const selectedOptions = selectedPlotSeriesOptions();
        if (!selectedOptions.length) {
            state.plotSeriesData = [];
            state.plotWindow = null;
            renderPlotCharts([]);
            setPlotStatus(
                activeBuildId
                    ? (plotSeriesOptions.length
                        ? "Select one or more values to display a stored trend plot."
                        : "No plottable measurement channels are available for this flowsheet yet.")
                    : "Select a flowsheet with mapped sensors or actuators to display plots.",
                "muted",
            );
            return;
        }

        const requestId = state.plotRequestId + 1;
        state.plotRequestId = requestId;
        state.isPlotBusy = true;
        const rangeOption = selectedPlotRangeOption();
        const requestedWindowEndIso = new Date().toISOString();
        if (!settings.quiet) {
            setPlotStatus(`Loading trend data for ${rangeOption.label.toLowerCase()}...`, "muted");
        }

        try {
            const params = new URLSearchParams();
            const seenSeriesKeys = new Set();
            for (const option of selectedOptions) {
                const seriesKey = plotSeriesRequestKey(option);
                if (seenSeriesKeys.has(seriesKey)) {
                    continue;
                }
                seenSeriesKeys.add(seriesKey);
                params.append("series", seriesKey);
            }
            params.set("since_minutes", String(rangeOption.sinceMinutes));
            params.set("max_points", String(rangeOption.maxPoints));
            params.set("cache_seconds", String(PROCESS_PLOT_LIVE_CACHE_SECONDS));
            const payload = await fetchJson(
                `/api/plot-series/live?${params.toString()}`,
                { timeoutMs: 5000, maxRetries: 1 },
            );
            if (requestId !== state.plotRequestId) {
                return;
            }

            const plotWindow = normalizePlotWindow(payload, requestedWindowEndIso, rangeOption);
            const storedSeriesById = new Map();
            const payloadSeries = Array.isArray(payload?.series) ? payload.series : [];
            const payloadSeriesByKey = new Map(
                payloadSeries
                    .map((series) => {
                        const deviceId = Number(series?.device_id);
                        const channelCode = asString(series?.channel_code, "");
                        return Number.isInteger(deviceId) && deviceId > 0 && channelCode
                            ? [`${deviceId}:${channelCode}`, series]
                            : null;
                    })
                    .filter(Boolean),
            );
            for (const option of selectedOptions) {
                const payloadSeriesItem = payloadSeriesByKey.get(plotSeriesRequestKey(option)) || { items: [] };
                storedSeriesById.set(option.id, normalizePlotMeasurements(option, payloadSeriesItem));
            }
            const storedSeries = selectedOptions.map(
                (option) => storedSeriesById.get(option.id) || { ...option, points: [] },
            );
            const seriesItems = storedSeries;
            state.plotSeriesData = seriesItems;
            state.plotWindow = plotWindow;
            renderPlotCharts(seriesItems, plotWindow);

            const populatedSeries = seriesItems.filter((series) => series.points.length > 0).length;
            const staleSeries = seriesItems.filter((series) => plotSeriesFreshness(series, plotWindow).status === "stale").length;
            if (populatedSeries > 0) {
                setPlotStatus(
                    `Plot updated for ${rangeOption.label.toLowerCase()} ending ${formatPlotTimestamp(plotWindow.endMs)}. ${populatedSeries} selected series contain trend data${staleSeries ? `, ${staleSeries} stale` : ""}.`,
                    settings.quiet ? "muted" : "success",
                );
            } else {
                setPlotStatus(
                    `No plot data is available for the selected values in ${rangeOption.label.toLowerCase()}.`,
                    "muted",
                );
            }
            state.plotBackoffUntil = 0;
        } catch (error) {
            if (requestId !== state.plotRequestId) {
                return;
            }
            state.plotBackoffUntil = Date.now() + PROCESS_PLOT_ERROR_BACKOFF_MS;
            setPlotStatus(error?.message || "Plot data could not be loaded.", "error");
        } finally {
            if (requestId === state.plotRequestId) {
                state.isPlotBusy = false;
            }
        }
    }

    function selectedTarget() {
        return state.selectedNodeId ? manualTargets[state.selectedNodeId] || null : null;
    }

    function isRecipeMode() {
        return state.selectionMode === "recipe";
    }

    function runningProgram() {
        return state.programData && asString(state.programData.status, "idle") === "running" ? state.programData : null;
    }

    function programActiveActors(program) {
        const activeStep = program?.active_step;
        const actors = Array.isArray(activeStep?.actors)
            ? activeStep.actors.map((item) => asString(item?.actor || item)).filter(Boolean)
            : [];
        const fallbackActor = asString(activeStep?.actor, "");
        if (fallbackActor && !actors.includes(fallbackActor)) {
            actors.unshift(fallbackActor);
        }
        return actors;
    }

    function formatProgramStepActors(program) {
        const actors = programActiveActors(program);
        if (!actors.length) {
            return "";
        }
        if (actors.length <= 2) {
            return actors.join(" + ");
        }
        return `${actors.slice(0, 2).join(" + ")} +${actors.length - 2}`;
    }

    function programStatusBadgeClass(status) {
        const normalized = asString(status, "idle").toLowerCase();
        if (normalized === "running") {
            return "badge-warning";
        }
        if (normalized === "completed") {
            return "badge-success";
        }
        if (normalized === "error") {
            return "badge-danger";
        }
        return "badge-muted";
    }

    function setProgramStatus(message, tone) {
        if (!programStatus) {
            return;
        }
        programStatus.textContent = message || "";
        programStatus.classList.remove("muted", "error-text", "builder-status-success");
        if (tone === "error") {
            programStatus.classList.add("error-text");
            return;
        }
        if (tone === "success") {
            programStatus.classList.add("builder-status-success");
            return;
        }
        programStatus.classList.add("muted");
    }

    function formatProgramTarget(target) {
        const parts = [];
        if (target.is_on != null) {
            parts.push(target.is_on ? "ON" : "OFF");
        }
        if (target.is_on === false) {
            return parts.join(" | ");
        }
        if (target.rpm != null) {
            parts.push(`${Math.round(asNumber(target.rpm, 0))} rpm`);
        }
        if (target.temp != null) {
            parts.push(`${asNumber(target.temp, 0).toFixed(2)} C`);
        }
        if (target.pressure != null) {
            parts.push(`${asNumber(target.pressure, 0).toFixed(2)} mBar(A)`);
        }
        return parts.join(" | ") || "No numeric target";
    }

    function renderProgramTargets(program) {
        if (!programTargetList) {
            return;
        }
        const targets = Array.isArray(program?.current_targets) ? program.current_targets : [];
        if (!targets.length) {
            programTargetList.innerHTML = '<p class="muted">No active targets.</p>';
            return;
        }
        programTargetList.innerHTML = targets
            .map((target) => `
                <article class="process-program-target-chip">
                    <strong>${escapeHtml(asString(target.actor, "Actor"))}</strong>
                    <span>${escapeHtml(formatProgramTarget(target))}</span>
                </article>
            `)
            .join("");
    }

    function navigateToProcessSelection(mode, selectedId) {
        const normalizedMode = mode === "recipe" ? "recipe" : "build";
        const params = new URLSearchParams();
        params.set("mode", normalizedMode);
        if (normalizedMode === "recipe" && Number.isInteger(selectedId) && selectedId > 0) {
            params.set("recipe_id", String(selectedId));
        }
        if (normalizedMode === "build" && Number.isInteger(selectedId) && selectedId > 0) {
            params.set("build_id", String(selectedId));
        }
        const query = params.toString();
        window.location.assign(query ? `${window.location.pathname}?${query}` : window.location.pathname);
    }

    function syncProcessSelectionUi() {
        const recipeMode = isRecipeMode();
        processBuildField?.classList.toggle("is-hidden", recipeMode);
        processRecipeField?.classList.toggle("is-hidden", !recipeMode);
        processSourceBuildButton?.classList.toggle("is-active", !recipeMode);
        processSourceRecipeButton?.classList.toggle("is-active", recipeMode);
        processSourceBuildButton?.setAttribute("aria-pressed", String(!recipeMode));
        processSourceRecipeButton?.setAttribute("aria-pressed", String(recipeMode));
        if (processSelectionModeInput) {
            processSelectionModeInput.value = recipeMode ? "recipe" : "build";
        }
        if (programCard) {
            programCard.hidden = !recipeMode;
        }
        if (recipeMode && state.manualMode) {
            state.manualMode = false;
            state.selectedNodeId = null;
            state.manualRequestId += 1;
            clearManualInputsDirty();
        }
        if (recipeMode) {
            hideManualCard();
        }
        syncManualModeToggle();
    }

    function setSelectionMode(mode) {
        state.selectionMode = mode === "recipe" ? "recipe" : "build";
        syncProcessSelectionUi();
        renderAll();
        updateProgramCard();
        persistViewState();
    }

    function updateProgramCard() {
        if (!programCard) {
            return;
        }

        const summary = selectedRecipeSummary();
        const program = state.programData || null;
        const selectedBuildName = asString(buildData?.build_name, "");
        const programStatusValue = asString(program?.status, "idle");
        const isProgramRunning = programStatusValue === "running";
        const sameRecipeRunning = isProgramRunning && Number(program?.recipe_id) === state.selectedRecipeId;
        const otherRecipeRunning = isProgramRunning && Number(program?.recipe_id) !== state.selectedRecipeId;

        if (programTitle) {
            programTitle.textContent = summary?.title || "No recipe selected";
        }
        if (programSubtitle) {
            if (summary && selectedBuildName) {
                programSubtitle.textContent = `Flowsheet: ${selectedBuildName}`;
            } else if (summary) {
                programSubtitle.textContent = "The selected recipe is not linked to a valid flowsheet.";
            } else {
                programSubtitle.textContent = "Select a recipe to load its flowsheet and start the program.";
            }
        }
        if (programRecipeName) {
            programRecipeName.textContent = summary?.title || "-";
        }
        if (programBuildName) {
            programBuildName.textContent = selectedBuildName || "-";
        }
        if (programStatusBadge) {
            programStatusBadge.textContent = programStatusValue;
            programStatusBadge.className = `badge ${programStatusBadgeClass(programStatusValue)}`;
        }

        if (programStepLabel) {
            if (isProgramRunning && program?.active_step_number) {
                const task = asString(program?.active_step?.task, "");
                const actor = formatProgramStepActors(program);
                programStepLabel.textContent = `#${program.active_step_number}${task ? ` | ${task}` : actor ? ` | ${actor}` : ""}`;
            } else if (programStatusValue === "completed") {
                programStepLabel.textContent = "Completed";
            } else if (programStatusValue === "stopped") {
                programStepLabel.textContent = "Stopped";
            } else if (programStatusValue === "error") {
                programStepLabel.textContent = "Error";
            } else {
                programStepLabel.textContent = "-";
            }
        }

        if (programTimeLabel) {
            programTimeLabel.textContent = isProgramRunning
                ? formatDurationLabel(asNumber(program?.step_remaining_seconds, 0))
                : "-";
        }

        if (programProgressFill) {
            const progressPercent = isProgramRunning
                ? Math.round(clamp(asNumber(program?.step_progress, 0), 0, 1) * 100)
                : programStatusValue === "completed"
                    ? 100
                    : 0;
            programProgressFill.style.width = `${progressPercent}%`;
        }

        if (programProgressLabel) {
            if (sameRecipeRunning) {
                const actor = formatProgramStepActors(program);
                const task = asString(program?.active_step?.task, "");
                programProgressLabel.textContent = task
                    ? `${task}${actor ? ` | ${actor}` : ""}`
                    : actor || "Recipe program running.";
            } else if (otherRecipeRunning) {
                programProgressLabel.textContent = `Another recipe is running: ${asString(program?.recipe_title, "active program")}.`;
            } else if (programStatusValue === "completed") {
                programProgressLabel.textContent = "Program completed.";
            } else if (programStatusValue === "stopped") {
                programProgressLabel.textContent = "Program stopped.";
            } else if (programStatusValue === "error") {
                programProgressLabel.textContent = "Program ended with an error.";
            } else {
                programProgressLabel.textContent = summary ? "Ready to start." : "Select a recipe first.";
            }
        }

        renderProgramTargets(program);

        if (sameRecipeRunning) {
            setProgramStatus("Recipe program is running. Stop it at any time if you need to abort the sequence.", "muted");
        } else if (otherRecipeRunning) {
            setProgramStatus(
                `Another recipe program is currently running (${asString(program?.recipe_title, "active recipe")}). Stop it before starting a different one.`,
                "error",
            );
        } else if (programStatusValue === "completed") {
            setProgramStatus("Recipe program completed. You can start it again.", "success");
        } else if (programStatusValue === "stopped") {
            setProgramStatus("Recipe program stopped. Start it again when you are ready.", "muted");
        } else if (programStatusValue === "error") {
            setProgramStatus(asString(program?.last_error, "Recipe program failed."), "error");
        } else if (summary && !selectedBuildName) {
            setProgramStatus("The selected recipe has no valid flowsheet mapping.", "error");
        } else if (summary) {
            setProgramStatus("Start the recipe program to run the stored sequence on this flowsheet.", "muted");
        } else {
            setProgramStatus("Select a recipe to enable start and stop.", "muted");
        }

        if (programStartButton) {
            programStartButton.disabled =
                !state.selectedRecipeId ||
                !activeBuildId ||
                state.isProgramBusy ||
                sameRecipeRunning ||
                otherRecipeRunning;
        }
        if (programStopButton) {
            programStopButton.disabled = !isProgramRunning || state.isProgramBusy;
        }
    }

    async function loadProcessProgram(options) {
        const settings = options || {};
        try {
            const payload = await fetchJson("/api/process-program", {
                timeoutMs: 10000,
                maxRetries: settings.quiet ? 1 : 0,
            });
            state.programData = payload?.program || null;
            if (state.programData && asString(state.programData.status, "idle") === "running" && state.manualMode) {
                setManualMode(false);
            }
            updateProgramCard();
            syncManualModeToggle();
            renderNodes();
        } catch (error) {
            if (!settings.quiet) {
                setProgramStatus(error?.message || "Recipe program status could not be loaded.", "error");
            }
        }
    }

    async function startSelectedRecipeProgram() {
        if (!state.selectedRecipeId) {
            setProgramStatus("Select a recipe before starting the program.", "error");
            return;
        }
        if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
            setProgramStatus("No valid process token is available for this page.", "error");
            return;
        }

        state.isProgramBusy = true;
        updateProgramCard();
        setProgramStatus("Starting recipe program...", "muted");

        const headers = { "Content-Type": "application/json" };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }

        try {
            const payload = await fetchJson("/api/process-program/start", {
                method: "POST",
                headers,
                timeoutMs: 12000,
                body: JSON.stringify({
                    recipe_id: state.selectedRecipeId,
                    requested_by: "process_recipe",
                }),
            });
            state.programData = payload?.program || null;
            setProgramStatus("Recipe program started.", "success");
        } catch (error) {
            setProgramStatus(error?.message || "Recipe program could not be started.", "error");
        } finally {
            state.isProgramBusy = false;
            updateProgramCard();
            syncManualModeToggle();
            renderNodes();
        }
    }

    function normalizeStoppedProgramPayload(program) {
        const payload = program && typeof program === "object" ? { ...program } : {};
        payload.status = "stopped";
        payload.active_step_index = null;
        payload.active_step_number = null;
        payload.active_step = null;
        payload.next_step = null;
        payload.step_started_at = null;
        payload.step_duration_seconds = 0;
        payload.step_elapsed_seconds = 0;
        payload.step_remaining_seconds = 0;
        payload.step_progress = 0;
        payload.current_targets = [];
        return payload;
    }

    function confirmRecipeProgramStop() {
        if (!programStopDialog || typeof programStopDialog.showModal !== "function") {
            return Promise.resolve(window.confirm("Are You sure?"));
        }
        if (programStopDialog.open) {
            return Promise.resolve(false);
        }

        return new Promise((resolve) => {
            programStopDialog.returnValue = "";
            const onClose = () => {
                resolve(programStopDialog.returnValue === "yes");
            };
            programStopDialog.addEventListener("close", onClose, { once: true });
            programStopDialog.showModal();
        });
    }

    async function stopActiveRecipeProgram() {
        if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
            setProgramStatus("No valid process token is available for this page.", "error");
            return;
        }
        const confirmed = await confirmRecipeProgramStop();
        if (!confirmed) {
            setProgramStatus("Stop cancelled.", "muted");
            return;
        }

        state.isProgramBusy = true;
        updateProgramCard();
        setProgramStatus("Stopping recipe program and applying safe state...", "muted");

        const headers = { "Content-Type": "application/json" };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }

        try {
            const payload = await fetchJson("/api/process-program/stop", {
                method: "POST",
                headers,
                timeoutMs: 12000,
                body: JSON.stringify({ requested_by: "process_recipe" }),
            });
            const stoppedProgram = payload?.program || null;
            if (asString(stoppedProgram?.status, "") === "error") {
                state.programData = stoppedProgram;
                setProgramStatus(asString(stoppedProgram?.last_error, "Safe stop failed."), "error");
            } else {
                state.programData = normalizeStoppedProgramPayload(stoppedProgram);
                setProgramStatus("Recipe program stopped.", "success");
            }
        } catch (error) {
            setProgramStatus(error?.message || "Recipe program could not be stopped.", "error");
        } finally {
            state.isProgramBusy = false;
            updateProgramCard();
            syncManualModeToggle();
            renderNodes();
        }
    }

    function readPersistedViewState() {
        try {
            const raw = window.localStorage.getItem(PROCESS_VIEW_STORAGE_KEY);
            if (!raw) {
                return {};
            }
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch (_error) {
            return {};
        }
    }

    function clearPersistedViewState() {
        try {
            window.localStorage.removeItem(PROCESS_VIEW_STORAGE_KEY);
        } catch (_error) {
            // Ignore storage failures and continue with in-memory state only.
        }
    }

    function queryBuildId() {
        const rawValue = new URLSearchParams(window.location.search).get("build_id");
        const buildId = Number(rawValue);
        return Number.isInteger(buildId) && buildId > 0 ? buildId : null;
    }

    function queryRecipeId() {
        const rawValue = new URLSearchParams(window.location.search).get("recipe_id");
        const recipeId = Number(rawValue);
        return Number.isInteger(recipeId) && recipeId > 0 ? recipeId : null;
    }

    function currentBuildId() {
        const buildId = Number(buildData?.reactor_build_id);
        return Number.isInteger(buildId) && buildId > 0 ? buildId : null;
    }

    function currentRecipeId() {
        const recipeId = Number(selectedRecipeData?.recipe_id);
        return Number.isInteger(recipeId) && recipeId > 0 ? recipeId : null;
    }

    function selectedRecipeSummary() {
        if (state.selectedRecipeId == null) {
            return null;
        }
        return recipeSummaryMap.get(state.selectedRecipeId) || null;
    }

    function persistViewState() {
        const buildId = currentBuildId();
        const recipeId = currentRecipeId();
        if (!buildId && !recipeId) {
            clearPersistedViewState();
            return;
        }

        try {
            window.localStorage.setItem(
                PROCESS_VIEW_STORAGE_KEY,
                JSON.stringify({
                    selectionMode: state.selectionMode,
                    buildId,
                    recipeId,
                    manualMode: state.manualMode,
                    selectedNodeId: state.selectedNodeId || null,
                    selectedPlotSeriesIds: Array.isArray(state.selectedPlotSeriesIds) ? state.selectedPlotSeriesIds : [],
                    selectedPlotRangeId: normalizePlotRangeId(state.selectedPlotRangeId),
                    plotPanelOpen: Boolean(plotPanel?.open),
                }),
            );
        } catch (_error) {
            // Ignore storage failures and continue with in-memory state only.
        }
    }

    function formatDeviceStatus(target) {
        const onlineText = target.is_online ? "online" : "offline";
        return target.quality_state ? `${onlineText} | ${target.quality_state}` : onlineText;
    }

    function formatRuntimeStatus(telemetry) {
        if (!telemetry) {
            return "";
        }

        if (telemetry.kind === "huber") {
            const setpoint = telemetry.setpointC == null ? null : Number(telemetry.setpointC);
            const isOn = telemetry.isOn == null ? null : Boolean(telemetry.isOn);
            const stateText = isOn == null ? "" : isOn ? "ON" : "OFF";
            if (setpoint != null && Number.isFinite(setpoint)) {
                return stateText ? `${stateText} | setpoint ${setpoint.toFixed(2)} °C` : `setpoint ${setpoint.toFixed(2)} °C`;
            }
            return stateText;
        }

        const actualRpm = telemetry.actualRpm == null ? null : Math.max(0, Math.round(telemetry.actualRpm));
        const setpointRpm = telemetry.setpointRpm == null ? null : Math.max(0, Math.round(telemetry.setpointRpm));
        if (actualRpm != null && actualRpm > 0) {
            if (setpointRpm != null && setpointRpm !== actualRpm) {
                return `running @ ${actualRpm} rpm (setpoint ${setpointRpm} rpm)`;
            }
            return `running @ ${actualRpm} rpm`;
        }
        if (setpointRpm != null && setpointRpm > 0) {
            return `idle @ setpoint ${setpointRpm} rpm`;
        }
        if (setpointRpm != null) {
            return "idle";
        }
        return "";
    }

    function updateManualDeviceStatus(target, telemetry) {
        if (!manualDeviceStatus) {
            return;
        }
        if (!target) {
            manualDeviceStatus.textContent = formatRuntimeStatus(telemetry) || "-";
            return;
        }

        const runtimeStatus = formatRuntimeStatus(telemetry);
        const baseStatus = formatDeviceStatus(target);
        manualDeviceStatus.textContent = runtimeStatus ? `${baseStatus} | ${runtimeStatus}` : baseStatus;
    }

    function setManualStatusFromTelemetry(telemetry, options) {
        const settings = options || {};
        const tone = settings.tone || "muted";
        const prefix = asString(settings.prefix, "Status refreshed.");
        const actualRpm = telemetry?.actualRpm == null ? null : Math.max(0, Math.round(telemetry.actualRpm));
        const setpointRpm = telemetry?.setpointRpm == null ? null : Math.max(0, Math.round(telemetry.setpointRpm));
        if (actualRpm != null && actualRpm > 0) {
            const runningDetails =
                setpointRpm != null && setpointRpm !== actualRpm
                    ? `Measured speed is approximately ${actualRpm} rpm with a ${setpointRpm} rpm setpoint.`
                    : `Measured speed is approximately ${actualRpm} rpm.`;
            setManualStatus(`${prefix} ${runningDetails}`, tone);
            return;
        }

        const idleSetpoint = setpointRpm == null ? 0 : setpointRpm;
        setManualStatus(`${prefix} Stirrer is idle with a ${idleSetpoint} rpm setpoint.`, tone);
    }

    function setManualStatusFromSnapshot(snapshot, telemetry, options) {
        const settings = options || {};
        const queueStatus = asString(snapshot?.queue_status, "idle").toLowerCase();
        const desiredVersion = Math.max(0, Math.round(optionalNumber(snapshot?.desired_version) ?? 0));
        const appliedVersion = Math.max(0, Math.round(optionalNumber(snapshot?.applied_version) ?? 0));
        const lastError = asString(snapshot?.last_error, "");
        if (lastError) {
            setManualStatus(lastError, "error");
            return;
        }
        if (queueStatus === "error") {
            setManualStatus("The last device update failed. Please retry the command.", "error");
            return;
        }
        if (desiredVersion > appliedVersion || queueStatus === "queued" || queueStatus === "running") {
            const desired = snapshotDesiredState(snapshot);
            if (desired.isOn === true) {
                const speedLabel = desired.speed != null ? `${desired.speed} rpm` : "the requested setpoint";
                setManualStatus(`Change queued. Waiting for device confirmation for ${speedLabel}.`, "muted");
                return;
            }
            if (desired.isOn === false) {
                setManualStatus("Stop queued. Waiting for device confirmation.", "muted");
                return;
            }
            setManualStatus("Change queued. Waiting for device confirmation.", "muted");
            return;
        }

        if (telemetry.actualRpm != null || telemetry.setpointRpm != null) {
            setManualStatusFromTelemetry(telemetry, {
                prefix: asString(settings.prefix, settings.quiet ? "Status refreshed." : "Device state loaded."),
                tone: settings.tone || (settings.quiet ? "muted" : "success"),
            });
            return;
        }

        const prefix = asString(settings.prefix, settings.quiet ? "Status refreshed." : "Device state loaded.");
        // Distinguish between "never polled yet" and "polled but device returned
        // no valid data".  The latter most often means the device is still booting
        // after a power cycle or has a communication issue.
        const updatedAt = snapshot?.reported_state?.updated_at ?? null;
        if (updatedAt != null) {
            setManualStatus(
                `${prefix} Device responded but reported no valid data (setpoint, RPM and torque are all empty). ` +
                "The stirrer may still be booting after a power cycle. Retrying automatically.",
                "error"
            );
        } else {
            setManualStatus(`${prefix} Waiting for the first device status from the server.`, settings.tone || "muted");
        }
    }

    function isIkaMotorTarget(node, target) {
        const protocol = normalizedProtocolName(target?.protocol);
        const symbolId = asString(node?.symbol_id, "").trim().toLowerCase();
        return protocol === "ika_eurostar_60" && symbolId === "motor";
    }

    function isHuberThermostatTarget(node, target) {
        const protocol = normalizedProtocolName(target?.protocol);
        const symbolId = asString(node?.symbol_id, "").trim().toLowerCase();
        return (
            protocol === "huber_unistat_430"
            || protocol === "huber_pilot_one"
            || protocol === "huber_cc230"
        ) && symbolId === "hc_system";
    }

    function isCC230ThermostatTarget(node, target) {
        const protocol = normalizedProtocolName(target?.protocol);
        const symbolId = asString(node?.symbol_id, "").trim().toLowerCase();
        return protocol === "huber_cc230" && symbolId === "hc_system";
    }

    function isSupportedManualTarget(node, target) {
        return isIkaMotorTarget(node, target) || isHuberThermostatTarget(node, target);
    }


    function huberSetpointLimits(target) {
        return { min: -40, max: 150 };
    }

    function syncManualControlsEnabled(enabled) {
        const allow = enabled && !state.isSending;
        if (manualStateInput) {
            manualStateInput.disabled = !allow;
        }
        if (manualSpeedInput) {
            manualSpeedInput.disabled = !allow;
        }
        if (manualSensorInput) {
            manualSensorInput.disabled = !allow;
        }
        if (manualSubmitButton) {
            manualSubmitButton.disabled = !allow;
        }
        syncManualModeToggle();
    }

    function shouldPreserveManualInputs(nodeId) {
        return Boolean(nodeId) && state.inputsDirtyForNodeId === nodeId;
    }

    function markManualInputsDirty() {
        if (!state.manualMode || !state.selectedNodeId) {
            return;
        }
        state.inputsDirtyForNodeId = state.selectedNodeId;
    }

    function clearManualInputsDirty(nodeId) {
        if (!nodeId) {
            state.inputsDirtyForNodeId = null;
            return;
        }
        if (state.inputsDirtyForNodeId === nodeId) {
            state.inputsDirtyForNodeId = null;
        }
    }

    function renderOperatorControls(node, target, options) {
        const opts = options || {};
        const enabled = isSupportedManualTarget(node, target);
        manualControls?.classList.toggle("is-hidden", !enabled);
        if (!enabled) {
            return;
        }

        if (isHuberThermostatTarget(node, target)) {
            const limits = huberSetpointLimits(target);
            const isCC230 = isCC230ThermostatTarget(node, target);
            if (manualDeviceHeading) {
                manualDeviceHeading.textContent = isCC230 ? "CC230 Thermostat" : "Thermostat";
            }
            if (manualStateLabel) {
                manualStateLabel.textContent = "Status";
            }
            if (manualValueLabel) {
                manualValueLabel.textContent = "Setpoint °C";
            }
            if (manualPrimaryMetricLabel) {
                manualPrimaryMetricLabel.textContent = isCC230 ? "Process Temp" : "Setpoint";
            }
            if (manualSecondaryMetricLabel) {
                manualSecondaryMetricLabel.textContent = isCC230 ? "Bath / Status" : "Status";
            }
            manualSensorField?.classList.toggle("is-hidden", !isCC230);
            if (manualSpeedInput) {
                manualSpeedInput.min = String(limits.min);
                manualSpeedInput.max = String(limits.max);
                manualSpeedInput.step = "0.5";
                manualSpeedInput.inputMode = "decimal";
            }
            if (!opts.preserveInputs) {
                const targetTemp = asNumber(node?.control?.config?.target_temp, 25);
                if (manualSpeedInput) {
                    manualSpeedInput.value = String(targetTemp);
                }
                if (manualStateInput) {
                    manualStateInput.value = Boolean(node?.control?.config?.is_on) ? "on" : "off";
                }
                if (manualSensorInput) {
                    manualSensorInput.value = "";
                }
            }
            return;
        }

        manualSensorField?.classList.add("is-hidden");
        if (manualSensorInput) {
            manualSensorInput.value = "";
        }

        if (manualDeviceHeading) {
            manualDeviceHeading.textContent = "IKA Stirrer";
        }
        if (manualStateLabel) {
            manualStateLabel.textContent = "State";
        }
        if (manualValueLabel) {
            manualValueLabel.textContent = "RPM";
        }
        if (manualPrimaryMetricLabel) {
            manualPrimaryMetricLabel.textContent = "Actual RPM";
        }
        if (manualSecondaryMetricLabel) {
            manualSecondaryMetricLabel.textContent = "Torque (Ncm)";
        }
        if (manualSpeedInput) {
            manualSpeedInput.min = "0";
            manualSpeedInput.max = "2000";
            manualSpeedInput.step = "10";
            manualSpeedInput.inputMode = "numeric";
        }
        if (!opts.preserveInputs) {
            const speed = Math.max(0, Math.round(asNumber(node?.control?.config?.speed, 0)));
            if (manualSpeedInput) {
                manualSpeedInput.value = String(speed);
            }
            if (manualStateInput) {
                const isOn = Boolean(node?.control?.config?.is_on);
                manualStateInput.value = isOn ? "on" : "off";
            }
        }
    }

    function normalizedProtocolName(value) {
        return asString(value, "").trim().toLowerCase();
    }

    function protocolLabel(value) {
        const id = asString(value, "");
        return protocolLabelMap.get(id) || id || "n/a";
    }

    function optionalNumber(value) {
        if (value == null || value === "") {
            return null;
        }
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : null;
    }

    function updateManualLiveMetrics(telemetry) {
        if (telemetry?.kind === "huber") {
            const primaryTemp = telemetry.processTempC == null ? telemetry.setpointC : telemetry.processTempC;
            const secondaryParts = [];
            if (telemetry.bathTempC != null) {
                secondaryParts.push(formatRoundedMetric(Number(telemetry.bathTempC), "degC", 2));
            }
            if (telemetry.isOn != null) {
                secondaryParts.push(telemetry.isOn ? "ON" : "OFF");
            }
            const errorText = asString(telemetry.errorText, "");
            const warningText = asString(telemetry.warningText, "");
            if (errorText && !/^(error\s*)?0$/i.test(errorText)) {
                secondaryParts.push(`Error: ${errorText}`);
            }
            if (warningText && !/^(warn\s*)?0$/i.test(warningText)) {
                secondaryParts.push(`Warning: ${warningText}`);
            }
            if (manualActualRpm) {
                manualActualRpm.textContent = primaryTemp == null
                    ? "-"
                    : formatRoundedMetric(Number(primaryTemp), "degC", 2);
            }
            if (manualTorqueNcm) {
                manualTorqueNcm.textContent = secondaryParts.join(" | ") || "-";
            }
            return;
        }

        if (manualActualRpm) {
            manualActualRpm.textContent = telemetry?.actualRpm == null
                ? "-"
                : formatRoundedMetric(telemetry.actualRpm, "rpm", telemetry.actualRpm >= 100 ? 0 : 2);
        }
        if (manualTorqueNcm) {
            manualTorqueNcm.textContent = telemetry?.torqueNcm == null
                ? "-"
                : formatRoundedMetric(telemetry.torqueNcm, "Ncm", 2);
        }
    }

    function manualCommandHeaders() {
        const headers = { "Content-Type": "application/json" };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }
        return headers;
    }

    async function executeDeviceCommand(target, commandName, payload, options) {
        const settings = options || {};
        const response = await fetchJson(`/api/devices/${target.device_id}/commands`, {
            method: "POST",
            headers: manualCommandHeaders(),
            timeoutMs: settings.timeoutMs || 12000,
            body: JSON.stringify({
                requested_by: "process_manual",
                command_name: commandName,
                payload: payload || {},
            }),
        });
        if (settings.returnMeta) {
            return response?.result?.metadata;
        }
        return response?.result?.metadata?.value;
    }

    function canLoadIkaSettings(node, target) {
        return Boolean(node && target && target.is_resolved && target.device_id && isIkaMotorTarget(node, target));
    }

    function snapshotTelemetry(snapshot) {
        const reported = snapshot && typeof snapshot === "object" ? snapshot.reported_state || {} : {};
        return {
            setpointRpm: optionalNumber(reported.setpoint_rpm),
            actualRpm: optionalNumber(reported.actual_rpm),
            torqueNcm: optionalNumber(reported.torque_ncm),
        };
    }

    function snapshotDesiredState(snapshot) {
        const desired = snapshot && typeof snapshot === "object" ? snapshot.desired_state || {} : {};
        return {
            isOn: desired.is_on == null ? null : coerceBoolean(desired.is_on, false),
            speed: desired.speed == null ? null : Math.max(0, Math.round(optionalNumber(desired.speed) ?? 0)),
        };
    }

    async function loadHuberStateSnapshot(nodeId, options) {
        const settings = options || {};
        const node = getNodeById(nodeId);
        const target = manualTargets[nodeId] || null;
        if (!node || !target || !target.is_resolved || !target.device_id || !isHuberThermostatTarget(node, target)) {
            return;
        }

        // Extend watch_expires_at so the reconciler stores measurements at the
        // fast poll interval (2.5 s) for live Process Trends updates.
        void fetch(
            `/api/devices/${target.device_id}/manual-state?watch=1&requested_by=process_view`
        ).catch(() => {});

        const requestId = state.manualRequestId + 1;
        state.manualRequestId = requestId;
        state.isManualBusy = true;
        if (!settings.quiet) {
            setManualStatus("Loading current thermostat state...", "muted");
        }

        try {
            const isCC230 = isCC230ThermostatTarget(node, target);
            const setpointValue = await executeDeviceCommand(target, "get_setpoint", {}, { timeoutMs: 12000 });
            const statusValue = isCC230
                ? null
                : await executeDeviceCommand(target, "get_status", {}, { timeoutMs: 12000 });
            let processTempC = null;
            let bathTempC = null;
            let errorText = "";
            let warningText = "";
            if (isCC230) {
                const optionalCommand = async (commandName) => {
                    try {
                        return await executeDeviceCommand(target, commandName, {}, { timeoutMs: 12000 });
                    } catch (_error) {
                        return null;
                    }
                };
                processTempC = optionalNumber(await optionalCommand("get_process_temp"));
                bathTempC = optionalNumber(await optionalCommand("get_bath_temp"));
                errorText = asString(await optionalCommand("get_error"), "");
                warningText = asString(await optionalCommand("get_warning"), "");
            }
            if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
                return;
            }

            const setpointC = optionalNumber(setpointValue);
            const isOn = statusValue && typeof statusValue === "object"
                ? Boolean(statusValue.temperature_control_active)
                : null;
            node.control = {
                profile_id: node.control?.profile_id || "hc_system_temperature",
                config: {
                    ...(node.control?.config || {}),
                    target_temp: setpointC ?? asNumber(node.control?.config?.target_temp, 25),
                    is_on: isOn == null ? Boolean(node.control?.config?.is_on) : isOn,
                },
            };

            const telemetry = { kind: "huber", setpointC, processTempC, bathTempC, errorText, warningText, isOn };
            updateManualLiveMetrics(telemetry);
            updateManualDeviceStatus(target, telemetry);
            renderOperatorControls(node, target, { preserveInputs: shouldPreserveManualInputs(nodeId) });
            syncManualControlsEnabled(Boolean(target?.device_id) && isHuberThermostatTarget(node, target));
            if (!settings.skipStatus) {
                setManualStatus("Thermostat state loaded.", settings.quiet ? "muted" : "success");
            }
        } catch (error) {
            if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
                return;
            }
            if (!settings.skipStatus) {
                setManualStatus(error?.message || "Current thermostat state could not be loaded.", "error");
            }
        } finally {
            if (requestId === state.manualRequestId && state.selectedNodeId === nodeId) {
                state.isManualBusy = false;
            }
        }
    }

    function applyManualStateSnapshot(nodeId, target, snapshot, options) {
        const settings = options || {};
        const currentNode = getNodeById(nodeId);
        if (!currentNode) {
            return;
        }

        const telemetry = snapshotTelemetry(snapshot);
        const desired = snapshotDesiredState(snapshot);
        const currentSpeed = Math.max(0, Math.round(asNumber(currentNode.control?.config?.speed, 0)));
        const nextSpeed = desired.speed ?? telemetry.setpointRpm ?? currentSpeed;
        const runningFromTelemetry = telemetry.actualRpm == null ? null : telemetry.actualRpm > 0.5;
        const nextIsOn =
            desired.isOn == null
                ? runningFromTelemetry == null
                    ? Boolean(currentNode.control?.config?.is_on)
                    : runningFromTelemetry
                : desired.isOn;

        currentNode.control = {
            profile_id: currentNode.control?.profile_id || "motor_rpm",
            config: {
                ...(currentNode.control?.config || {}),
                speed: Math.max(0, Math.round(nextSpeed)),
                is_on: Boolean(nextIsOn),
            },
        };

        updateManualLiveMetrics(telemetry);
        updateManualDeviceStatus(target, telemetry);
        renderOperatorControls(currentNode, target, { preserveInputs: shouldPreserveManualInputs(nodeId) });
        syncManualControlsEnabled(Boolean(target?.device_id) && isIkaMotorTarget(currentNode, target));

        if (!settings.skipStatus) {
            setManualStatusFromSnapshot(snapshot, telemetry, settings);
        }
    }

    async function loadManualStateSnapshot(nodeId, options) {
        const settings = options || {};
        const node = getNodeById(nodeId);
        const target = manualTargets[nodeId] || null;
        if (isHuberThermostatTarget(node, target)) {
            await loadHuberStateSnapshot(nodeId, settings);
            return;
        }
        if (!canLoadIkaSettings(node, target)) {
            return;
        }

        const requestId = state.manualRequestId + 1;
        state.manualRequestId = requestId;
        state.isManualBusy = true;
        if (!settings.quiet) {
            setManualStatus("Loading current device state...", "muted");
        }

        const params = new URLSearchParams();
        params.set("watch", settings.watch === false ? "0" : "1");
        if (settings.refresh) {
            params.set("refresh", "1");
        }
        params.set("requested_by", "process_view");

        try {
            const payload = await fetchJson(`/api/devices/${target.device_id}/manual-state?${params.toString()}`, {
                timeoutMs: 12000,
                maxRetries: 1,
            });
            if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
                return;
            }
            applyManualStateSnapshot(nodeId, target, payload?.state || null, {
                quiet: Boolean(settings.quiet),
            });
        } catch (error) {
            if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
                return;
            }
            setManualStatus(error?.message || "Current device state could not be loaded.", "error");
        } finally {
            if (requestId === state.manualRequestId && state.selectedNodeId === nodeId) {
                state.isManualBusy = false;
            }
        }
    }

    function updateManualPanel() {
        if (state.nodes.length === 0) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Load a flowsheet first to use manual control.", "muted");
            return;
        }

        if (isRecipeMode()) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Manual control is disabled while recipe mode is selected.", "muted");
            return;
        }

        if (runningProgram()) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Manual control is locked while a recipe program is running.", "muted");
            return;
        }

        if (!state.manualMode) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Enable manual mode to operate actuators directly from the flowsheet.", "muted");
            return;
        }

        showManualCard();
        const node = getNodeById(state.selectedNodeId);
        if (!node) {
            clearManualInputsDirty();
            resetManualSummary();
            manualControls?.classList.add("is-hidden");
            syncManualControlsEnabled(false);
            setManualStatus("Click an actuator in the flowsheet to open its settings.", "muted");
            return;
        }

        const target = selectedTarget();
        manualTargetTitle.textContent = node.instance_id || node.label;
        manualTargetSubtitle.textContent = target?.device_display_name || node.symbol_id || "Actuator";
        manualPort.textContent = target?.connection_label || "-";
        manualServer.textContent = target?.server_code || "-";
        manualProtocol.textContent = protocolLabel(target?.protocol);
        updateManualDeviceStatus(target);

        if (!target || !target.is_resolved) {
            syncManualControlsEnabled(false);
            manualControls?.classList.add("is-hidden");
            const reason = target?.resolution_note || "No valid communication mapping is available for this actuator.";
            setManualStatus(reason, "error");
            return;
        }

        renderOperatorControls(node, target, { preserveInputs: shouldPreserveManualInputs(node.id) });
        syncManualControlsEnabled(Boolean(target.device_id) && isSupportedManualTarget(node, target));
        if (isIkaMotorTarget(node, target)) {
            setManualStatus("Set On/Off and RPM, then submit the change.", "muted");
            return;
        }
        if (isHuberThermostatTarget(node, target)) {
            setManualStatus("Set Status and Setpoint °C, then submit the change.", "muted");
            return;
        }
        setManualStatus("A simplified operator panel is not available for this actuator yet.", "muted");
    }

    function renderAll() {
        surface.style.width = `${state.canvasSize.width}px`;
        surface.style.height = `${state.canvasSize.height}px`;
        surface.classList.toggle("is-manual-mode", state.manualMode);
        renderEdges();
        renderNodes();
        updateEmptyState();
        updateManualPanel();
    }

    function setManualMode(enabled) {
        if (isRecipeMode()) {
            setManualStatus("Manual control is disabled while recipe mode is selected.", "muted");
            return;
        }
        if (runningProgram()) {
            setManualStatus("Manual control is locked while a recipe program is running.", "muted");
            return;
        }
        if (manualToggleButton.disabled || state.isSending) {
            if (state.isSending) {
                setManualStatus("Please wait until the current device request is finished.", "muted");
            }
            return;
        }
        state.manualMode = Boolean(enabled);
        if (!state.manualMode) {
            state.manualRequestId += 1;
            state.isManualBusy = false;
            state.selectedNodeId = null;
            clearManualInputsDirty();
        }
        if (state.manualMode && !getNodeById(state.selectedNodeId)) {
            state.selectedNodeId = null;
            clearManualInputsDirty();
        }

        manualToggleButton.setAttribute("aria-pressed", String(state.manualMode));
        manualToggleButton.classList.toggle("btn-primary", state.manualMode);
        manualToggleButton.textContent = "Manual";
        persistViewState();
        renderAll();

        if (state.manualMode && state.selectedNodeId) {
            void loadManualStateSnapshot(state.selectedNodeId, {
                quiet: true,
                refresh: true,
            });
        }
    }

    const buildData = parseJsonScript("process-build-data", null);
    const recipeData = parseJsonScript("process-recipes-data", []);
    const selectedRecipeData = parseJsonScript("process-selected-recipe", null);
    const selectionData = parseJsonScript("process-selection-data", {});
    const manualTargets = parseJsonScript("process-manual-targets", {});
    const plotTargetData = parseJsonScript("process-plot-targets", {});
    const metaData = parseJsonScript("process-meta-data", {});
    const definition = buildData && typeof buildData === "object" ? buildData.definition_json || {} : {};
    const nodes = Array.isArray(definition?.nodes) ? definition.nodes.map(normalizeNode) : [];
    const edges = Array.isArray(definition?.edges) ? definition.edges.map((edge) => normalizeEdge(edge, nodes)) : [];
    const recipeSummaryMap = new Map(
        (Array.isArray(recipeData) ? recipeData : [])
            .filter((item) => item && typeof item === "object")
            .map((item) => {
                const recipeId = Number(item.recipe_id);
                return Number.isInteger(recipeId) && recipeId > 0 ? [recipeId, item] : null;
            })
            .filter(Boolean),
    );
    const persistedViewState = readPersistedViewState();
    const requestedBuildId = queryBuildId();
    const requestedRecipeId = queryRecipeId();
    const activeBuildId = currentBuildId();
    const activeRecipeId = currentRecipeId();
    const initialSelectionMode = asString(selectionData?.mode, requestedRecipeId ? "recipe" : "build");

    if (!activeBuildId && !requestedBuildId && !activeRecipeId && !requestedRecipeId) {
        const persistedMode = asString(persistedViewState?.selectionMode, "build");
        const persistedRecipeId = Number(persistedViewState?.recipeId);
        const persistedBuildId = Number(persistedViewState?.buildId);
        const params = new URLSearchParams(window.location.search);
        if (persistedMode === "recipe" && Number.isInteger(persistedRecipeId) && persistedRecipeId > 0) {
            params.set("recipe_id", String(persistedRecipeId));
            window.location.replace(`${window.location.pathname}?${params.toString()}`);
            return;
        }
        if (Number.isInteger(persistedBuildId) && persistedBuildId > 0) {
            params.set("build_id", String(persistedBuildId));
            window.location.replace(`${window.location.pathname}?${params.toString()}`);
            return;
        }
    }

    const canRestorePersistedState =
        activeBuildId &&
        (
            (initialSelectionMode === "recipe" && Number(persistedViewState?.recipeId) === activeRecipeId) ||
            (initialSelectionMode !== "recipe" && Number(persistedViewState?.buildId) === activeBuildId)
        );
    const restoredSelectedNodeId = canRestorePersistedState
        ? asString(persistedViewState?.selectedNodeId, "").trim() || null
        : null;
    const plotSeriesOptions = buildPlotSeriesOptions(plotTargetData);
    const plotSeriesOptionMap = new Map(plotSeriesOptions.map((option) => [option.id, option]));
    const plotNodeGroups = buildPlotNodeGroups(plotTargetData, plotSeriesOptions);
    const liveDefaultPlotSeriesIds = defaultLivePlotSeriesIds();
    const persistedPlotSeriesIds = canRestorePersistedState && Array.isArray(persistedViewState?.selectedPlotSeriesIds)
        ? persistedViewState.selectedPlotSeriesIds
              .map((item) => asString(item, ""))
              .filter((item) => item && plotSeriesOptionMap.has(item))
        : [];
    const restoredPlotSeriesIds = Array.from(new Set([...liveDefaultPlotSeriesIds, ...persistedPlotSeriesIds]));
    const restoredPlotRangeId = canRestorePersistedState
        ? normalizePlotRangeId(persistedViewState?.selectedPlotRangeId)
        : DEFAULT_PROCESS_PLOT_RANGE_ID;
    const restoredPlotPanelOpen = Boolean(canRestorePersistedState && persistedViewState?.plotPanelOpen);

    const state = {
        nodes,
        edges,
        canvasSize: parseCanvasSize(definition, nodes),
        selectionMode: initialSelectionMode === "recipe" ? "recipe" : "build",
        selectedRecipeId: activeRecipeId,
        selectedBuildId: activeBuildId,
        manualMode: Boolean(initialSelectionMode !== "recipe" && canRestorePersistedState && persistedViewState?.manualMode),
        selectedNodeId: restoredSelectedNodeId,
        selectedPlotSeriesIds: restoredPlotSeriesIds,
        selectedPlotRangeId: restoredPlotRangeId,
        plotPanelOpen: restoredPlotPanelOpen,
        plotSeriesData: [],
        plotWindow: null,
        plotBackoffUntil: 0,
        isPlotBusy: false,
        plotRequestId: 0,
        inputsDirtyForNodeId: null,
        isSending: false,
        isManualBusy: false,
        manualRequestId: 0,
        programData: null,
        isProgramBusy: false,
    };

    if (state.selectedNodeId) {
        const restoredNode = getNodeById(state.selectedNodeId);
        if (!restoredNode || !isActuator(restoredNode)) {
            state.selectedNodeId = null;
        }
    }
    syncSelectedPlotSeriesIds();

    manualToggleButton?.addEventListener("click", () => {
        setManualMode(!state.manualMode);
    });

    manualStateInput?.addEventListener("change", () => {
        markManualInputsDirty();
    });

    manualSpeedInput?.addEventListener("input", () => {
        markManualInputsDirty();
    });

    plotPanel?.addEventListener("toggle", () => {
        state.plotPanelOpen = Boolean(plotPanel.open);
        persistViewState();
        if (plotPanel.open && state.selectedPlotSeriesIds.length > 0) {
            void loadPlotMeasurements({ quiet: true });
        }
    });

    if (plotRangeSelect) {
        plotRangeSelect.value = normalizePlotRangeId(state.selectedPlotRangeId);
        plotRangeSelect.addEventListener("change", () => {
            state.selectedPlotRangeId = normalizePlotRangeId(plotRangeSelect.value);
            plotRangeSelect.value = state.selectedPlotRangeId;
            persistViewState();
            if (plotPanel?.open && state.selectedPlotSeriesIds.length > 0) {
                void loadPlotMeasurements();
                return;
            }
            state.plotSeriesData = [];
            state.plotWindow = null;
            renderPlotCharts([]);
        });
    }

    window.addEventListener("reactor:themechange", () => {
        if (plotChartStack) {
            renderPlotCharts(state.plotSeriesData, state.plotWindow);
        }
    });

    manualSettingsForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        const node = getNodeById(state.selectedNodeId);
        const target = selectedTarget();
        const nextState = asString(manualStateInput?.value, "off").toLowerCase() === "on";

        if (!node || !target || !isSupportedManualTarget(node, target)) {
            setManualStatus("Select a mapped actuator first.", "error");
            return;
        }

        if (isHuberThermostatTarget(node, target)) {
            const limits = huberSetpointLimits(target);
            const setpointC = boundedNumberInputValue(manualSpeedInput, 25);
            if (manualSpeedInput) {
                manualSpeedInput.value = String(setpointC);
            }
            if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
                setManualStatus("No valid manual-control token is available for this page.", "error");
                return;
            }

            state.manualRequestId += 1;
            state.isManualBusy = false;
            state.isSending = true;
            syncManualControlsEnabled(true);
            setManualStatus("Submitting thermostat settings...", "muted");

            void (async () => {
                const sensorValue = asString(manualSensorInput?.value, "");
                if (isCC230ThermostatTarget(node, target) && sensorValue) {
                    await executeDeviceCommand(
                        target,
                        sensorValue === "external" ? "select_external_sensor" : "select_internal_sensor",
                        {},
                        { timeoutMs: 12000 },
                    );
                }
                const setpointMeta = await executeDeviceCommand(
                    target,
                    "set_setpoint",
                    { temp_c: setpointC, min_setpoint_c: limits.min, max_setpoint_c: limits.max },
                    { timeoutMs: 12000, returnMeta: true },
                );
                const confirmedSetpointC = optionalNumber(setpointMeta?.verified_setpoint) ?? setpointC;
                const syncStatus = String(setpointMeta?.setpoint_sync_status || "unknown");
                await executeDeviceCommand(target, nextState ? "start" : "stop", {}, { timeoutMs: 12000 });
                node.control = {
                    profile_id: node.control?.profile_id || "hc_system_temperature",
                    config: {
                        ...(node.control?.config || {}),
                        target_temp: confirmedSetpointC,
                        is_on: nextState,
                    },
                };
                clearManualInputsDirty(node.id);
                updateManualLiveMetrics({ kind: "huber", setpointC: confirmedSetpointC, isOn: nextState });
                updateManualDeviceStatus(target, { kind: "huber", setpointC: confirmedSetpointC, isOn: nextState });
                let statusMsg = `Thermostat ${nextState ? "started" : "stopped"} at ${confirmedSetpointC.toFixed(2)} °C setpoint`;
                if (syncStatus === "unverified") {
                    statusMsg += " (setpoint sent but readback timed out — command may not have been accepted)";
                }
                setManualStatus(statusMsg + ".", syncStatus === "unverified" ? "warning" : "success");
            })()
                .catch((error) => {
                    setManualStatus(error?.message || "Thermostat settings could not be applied.", "error");
                })
                .finally(() => {
                    state.isSending = false;
                    const currentNode = getNodeById(state.selectedNodeId);
                    const currentTarget = selectedTarget();
                    syncManualControlsEnabled(Boolean(currentTarget?.device_id) && isSupportedManualTarget(currentNode, currentTarget));
                });
            return;
        }

        const speed = boundedIntegerInputValue(manualSpeedInput, 0);

        if (manualSpeedInput) {
            manualSpeedInput.value = String(speed);
        }

        if (nextState && speed <= 0) {
            setManualStatus("Enter an RPM greater than 0 before switching the stirrer on.", "error");
            return;
        }

        if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
            setManualStatus("No valid manual-control token is available for this page.", "error");
            return;
        }

        state.manualRequestId += 1;
        state.isManualBusy = false;
        state.isSending = true;
        syncManualControlsEnabled(true);
        setManualStatus("Submitting desired device settings...", "muted");

        const headers = {
            "Content-Type": "application/json",
        };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }

        void (async () => {
            const payload = await fetchJson(`/api/devices/${target.device_id}/manual-state`, {
                method: "POST",
                headers,
                timeoutMs: 12000,
                body: JSON.stringify({
                    requested_by: "process_manual",
                    is_on: nextState,
                    speed,
                }),
            });

            const snapshot = payload?.state || null;
            applyManualStateSnapshot(node.id, target, snapshot, {
                quiet: true,
                skipStatus: true,
            });
            clearManualInputsDirty(node.id);

            const queueStatus = asString(snapshot?.queue_status, "idle").toLowerCase();
            const desiredVersion = Math.max(0, Math.round(optionalNumber(snapshot?.desired_version) ?? 0));
            const appliedVersion = Math.max(0, Math.round(optionalNumber(snapshot?.applied_version) ?? 0));
            const pending = desiredVersion > appliedVersion || queueStatus === "queued" || queueStatus === "running";
            if (pending) {
                if (nextState) {
                    setManualStatus(
                        `Change queued. ${speed} rpm will be applied as soon as the device worker confirms it.`,
                        "muted",
                    );
                } else {
                    setManualStatus("Stop queued. Waiting for device confirmation.", "muted");
                }
            } else {
                setManualStatusFromSnapshot(snapshot, snapshotTelemetry(snapshot), {
                    prefix: "Device updated.",
                    tone: "success",
                });
            }

            if (state.manualMode && state.selectedNodeId === node.id) {
                void loadManualStateSnapshot(node.id, { quiet: true });
            }
        })()
            .catch((error) => {
                setManualStatus(error?.message || "Desired device state could not be queued.", "error");
            })
            .finally(() => {
                state.isSending = false;
                const currentNode = getNodeById(state.selectedNodeId);
                const currentTarget = selectedTarget();
                syncManualControlsEnabled(Boolean(currentTarget?.device_id) && isSupportedManualTarget(currentNode, currentTarget));
            });
    });

    processSourceToggle?.addEventListener("click", (event) => {
        const button = event.target?.closest?.("[data-mode]");
        if (!button) {
            return;
        }
        const nextMode = button.getAttribute("data-mode") === "recipe" ? "recipe" : "build";
        setSelectionMode(nextMode);
        const selectedId = nextMode === "recipe"
            ? parseInt(processRecipeSelect?.value || state.selectedRecipeId || "", 10)
            : parseInt(processBuildSelect?.value || state.selectedBuildId || "", 10);
        navigateToProcessSelection(nextMode, Number.isInteger(selectedId) && selectedId > 0 ? selectedId : null);
    });

    processBuildSelect?.addEventListener("change", () => {
        if (processBuildSelect.disabled) {
            return;
        }
        const buildId = Number.parseInt(processBuildSelect.value || "", 10);
        if (!Number.isInteger(buildId) || buildId <= 0) {
            clearPersistedViewState();
            navigateToProcessSelection("build", null);
            return;
        }
        navigateToProcessSelection("build", buildId);
    });

    processRecipeSelect?.addEventListener("change", () => {
        if (processRecipeSelect.disabled) {
            return;
        }
        const recipeId = Number.parseInt(processRecipeSelect.value || "", 10);
        if (!Number.isInteger(recipeId) || recipeId <= 0) {
            clearPersistedViewState();
            navigateToProcessSelection("recipe", null);
            return;
        }
        navigateToProcessSelection("recipe", recipeId);
    });

    processClearSelectionLink?.addEventListener("click", () => {
        clearPersistedViewState();
    });

    programStartButton?.addEventListener("click", () => {
        void startSelectedRecipeProgram();
    });

    programStopButton?.addEventListener("click", () => {
        void stopActiveRecipeProgram();
    });

    window.setInterval(() => {
        if (!state.manualMode || state.isManualBusy || state.isSending || document.hidden) {
            return;
        }
        const nodeId = state.selectedNodeId;
        if (!nodeId) {
            return;
        }
        const node = getNodeById(nodeId);
        const target = selectedTarget();
        if (isHuberThermostatTarget(node, target) && target?.device_id) {
            void loadManualStateSnapshot(nodeId, { quiet: true, skipStatus: true });
            return;
        }
        if (!canLoadIkaSettings(node, target)) {
            return;
        }
        void loadManualStateSnapshot(nodeId, {
            quiet: true,
            watch: true,
        });
    }, MANUAL_LIVE_POLL_MS);

    window.setInterval(() => {
        if (
            document.hidden ||
            !plotPanel?.open ||
            state.isPlotBusy ||
            state.selectedPlotSeriesIds.length === 0 ||
            Date.now() < (state.plotBackoffUntil || 0)
        ) {
            return;
        }
        void loadPlotMeasurements({ quiet: true });
    }, PROCESS_PLOT_REFRESH_MS);

    window.setInterval(() => {
        if (document.hidden) {
            return;
        }
        void loadProcessProgram({ quiet: true });
    }, PROCESS_PROGRAM_POLL_MS);

    if (manualToggleButton) {
        manualToggleButton.setAttribute("aria-pressed", String(state.manualMode));
        manualToggleButton.classList.toggle("btn-primary", state.manualMode);
        manualToggleButton.textContent = "Manual";
    }

    syncProcessSelectionUi();
    if (plotPanel) {
        plotPanel.open = Boolean(state.plotPanelOpen);
    }
    renderPlotSelection();
    renderPlotCharts(state.plotSeriesData, state.plotWindow);
    if (plotPanel?.open && state.selectedPlotSeriesIds.length > 0) {
        void loadPlotMeasurements({ quiet: true });
    }
    void loadProcessProgram({ quiet: true });
    syncManualModeToggle();
    renderAll();
    updateProgramCard();
    if (state.manualMode && state.selectedNodeId) {
        const initialNode = getNodeById(state.selectedNodeId);
        const initialTarget = selectedTarget();
        if (canLoadIkaSettings(initialNode, initialTarget)) {
            void loadManualStateSnapshot(state.selectedNodeId, {
                quiet: true,
                refresh: true,
            });
        }
    }
    persistViewState();
})();
