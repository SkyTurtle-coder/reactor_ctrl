(function () {
    const surface = document.getElementById("process-flowsheet-surface");
    if (!surface) {
        return;
    }

    const edgeLayer = document.getElementById("process-edge-layer");
    const nodeLayer = document.getElementById("process-node-layer");
    const emptyState = document.getElementById("process-flowsheet-empty");
    const processDisplayGrid = document.querySelector(".process-display-grid");
    const processPickerForm = document.getElementById("process-picker-form");
    const processBuildSelect = document.getElementById("process-build-select");
    const processClearSelectionLink = document.getElementById("process-clear-selection");
    const manualToggleButton = document.getElementById("process-manual-mode-toggle");
    const manualCard = document.getElementById("process-manual-card");
    const manualTargetTitle = document.getElementById("process-manual-target-title");
    const manualTargetSubtitle = document.getElementById("process-manual-target-subtitle");
    const manualControls = document.getElementById("process-manual-controls");
    const manualSettingsForm = document.getElementById("process-manual-settings-form");
    const manualStateInput = document.getElementById("process-manual-state-input");
    const manualSpeedInput = document.getElementById("process-manual-speed-input");
    const manualSubmitButton = document.getElementById("process-manual-submit-button");
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
    const plotChartStack = document.getElementById("process-plot-chart-stack");
    const plotStatus = document.getElementById("process-plot-status");
    const PROCESS_VIEW_STORAGE_KEY = "reactor_ctrl.processView";
    const manualToggleInitiallyDisabled = Boolean(manualToggleButton?.disabled);
    const MANUAL_LIVE_POLL_MS = 3000;
    const PROCESS_PLOT_REFRESH_MS = 5000;
    const PROCESS_PLOT_SERIES_LIMIT = 120;
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

    function parseIkaNumericResponse(value) {
        const text = asString(value, "");
        if (!text) {
            return null;
        }
        const [head] = text.split(/\s+/);
        const numeric = Number.parseFloat(head);
        return Number.isFinite(numeric) ? numeric : null;
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
        // Use actual rendered surface size for viewBox to prevent SVG coordinate scaling
        const svgW = surface.offsetWidth || state.canvasSize.width;
        const svgH = surface.offsetHeight || state.canvasSize.height;
        edgeLayer.setAttribute("viewBox", `0 0 ${svgW} ${svgH}`);

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
        processDisplayGrid?.classList.remove("has-manual-panel");
        if (manualCard) {
            manualCard.hidden = true;
        }
    }

    function showManualCard() {
        processDisplayGrid?.classList.add("has-manual-panel");
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
        if (!state.manualMode) {
            return;
        }
        if (state.isSending || state.isManualBusy) {
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
        void loadManualSettings(nodeId);
    }

    function renderNodes() {
        nodeLayer.innerHTML = "";

        for (const node of state.nodes) {
            const element = document.createElement("article");
            element.className = "builder-node process-node";
            if (state.selectedNodeId === node.id) {
                element.classList.add("is-selected");
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
        manualToggleButton.disabled = manualToggleInitiallyDisabled || state.isManualBusy || state.isSending;
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
                    deviceId,
                    deviceDisplayName: asString(target?.device_display_name, `Device ${deviceId}`),
                    channelCode,
                    channelLabel: asString(channel?.display_name, channelCode),
                    unit: asString(channel?.unit, ""),
                    valueType,
                    symbolId: asString(target?.symbol_id, ""),
                    dataSource: asString(channel?.data_source, "measurement"),
                    runtimeMetric: asString(channel?.runtime_metric, ""),
                });
            }
        }

        return options.sort((left, right) => {
            const byCategory = left.category.localeCompare(right.category);
            if (byCategory !== 0) {
                return byCategory;
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

    function syncSelectedPlotSeriesIds() {
        const unique = Array.from(new Set(Array.isArray(state.selectedPlotSeriesIds) ? state.selectedPlotSeriesIds : []));
        state.selectedPlotSeriesIds = unique.filter((item) => plotSeriesOptionMap.has(item));
    }

    function selectedPlotSeriesOptions() {
        syncSelectedPlotSeriesIds();
        return state.selectedPlotSeriesIds.map((item) => plotSeriesOptionMap.get(item)).filter(Boolean);
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
                        <span>${escapeHtml(option.channelCode)}${option.unit ? ` | ${escapeHtml(option.unit)}` : ""}${option.dataSource === "runtime_fallback" ? " | live" : ""}</span>
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

    function normalizePlotMeasurements(option, items) {
        const points = (Array.isArray(items) ? items : [])
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
            unit: asString((Array.isArray(items) && items[0]?.unit) || option.unit, option.unit),
            points,
        };
    }

    function runtimePlotPointsForSeries(option) {
        return Array.isArray(state.runtimePlotSeriesById[option.id]) ? state.runtimePlotSeriesById[option.id] : [];
    }

    function mergePlotPoints(primaryPoints, secondaryPoints) {
        return [...(Array.isArray(primaryPoints) ? primaryPoints : []), ...(Array.isArray(secondaryPoints) ? secondaryPoints : [])]
            .filter((point) => point && Number.isFinite(point.x) && Number.isFinite(point.y))
            .sort((left, right) => left.x - right.x)
            .filter((point, index, points) => {
                if (index === 0) {
                    return true;
                }
                const previous = points[index - 1];
                return previous.x !== point.x || previous.y !== point.y;
            })
            .slice(-PROCESS_PLOT_SERIES_LIMIT);
    }

    function appendRuntimePlotPoint(option, value, timestampMs) {
        if (!option || !Number.isFinite(value) || !Number.isFinite(timestampMs)) {
            return;
        }
        const nextPoint = { x: timestampMs, y: value };
        const existing = runtimePlotPointsForSeries(option);
        const mergedPoints = mergePlotPoints(existing, [nextPoint]);
        state.runtimePlotSeriesById[option.id] = mergedPoints;
    }

    function syncRuntimePlotTelemetry(nodeId, telemetry, timestampMs) {
        if (!nodeId || !telemetry || !Number.isFinite(timestampMs)) {
            return;
        }
        state.latestPlotTelemetryByNodeId[nodeId] = {
            telemetry,
            timestampMs,
        };
        const options = plotSeriesOptions.filter(
            (option) => option.nodeId === nodeId && option.dataSource === "runtime_fallback" && option.runtimeMetric,
        );
        for (const option of options) {
            const rawValue = telemetry[option.runtimeMetric];
            if (!Number.isFinite(rawValue)) {
                continue;
            }
            appendRuntimePlotPoint(option, rawValue, timestampMs);
        }
    }

    async function ensureRuntimePlotSamples(options) {
        const runtimeOptions = (Array.isArray(options) ? options : []).filter(
            (option) => option.dataSource === "runtime_fallback" && runtimePlotPointsForSeries(option).length === 0,
        );
        if (!runtimeOptions.length) {
            return;
        }

        const uniqueNodeIds = Array.from(new Set(runtimeOptions.map((option) => option.nodeId))).filter(Boolean);
        for (const nodeId of uniqueNodeIds) {
            const cachedTelemetry = state.latestPlotTelemetryByNodeId[nodeId];
            if (cachedTelemetry?.telemetry && Number.isFinite(cachedTelemetry.timestampMs)) {
                syncRuntimePlotTelemetry(nodeId, cachedTelemetry.telemetry, cachedTelemetry.timestampMs);
                continue;
            }
            if (!state.manualMode || state.selectedNodeId !== nodeId || state.isManualBusy || state.isSending) {
                continue;
            }
            try {
                await loadManualSettings(nodeId, { quiet: true });
            } catch (_error) {
                // Plot rendering falls back to the next successful live poll.
            }
        }
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

    function renderPlotChartCard(unitKey, seriesItems) {
        const card = document.createElement("article");
        card.className = "process-plot-chart-card";
        const unitLabel = unitKey || "unitless";
        const points = seriesItems.flatMap((series) => series.points);
        const hasRuntimeFallback = seriesItems.some((series) => series.dataSource === "runtime_fallback");

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
            const item = document.createElement("li");
            item.innerHTML = `
                <span class="process-plot-legend-swatch" style="background:${plotColor(index)}"></span>
                <span>${escapeHtml(series.nodeLabel)} | ${escapeHtml(series.channelLabel)}${latestPoint ? ` (${escapeHtml(formatPlotValue(latestPoint.y, series.unit))})` : " (no data)"}</span>
            `;
            legend.appendChild(item);
        });
        card.appendChild(legend);

        if (!points.length) {
            const empty = document.createElement("p");
            empty.className = "process-plot-chart-empty";
            empty.textContent = hasRuntimeFallback
                ? "No live samples are available for this unit group yet. Select the actuator in manual mode and let the device refresh run."
                : "No stored measurements are available for the selected series in this unit group yet.";
            card.appendChild(empty);
            return card;
        }

        let minX = Math.min(...points.map((point) => point.x));
        let maxX = Math.max(...points.map((point) => point.x));
        let minY = Math.min(...points.map((point) => point.y));
        let maxY = Math.max(...points.map((point) => point.y));
        if (minX === maxX) {
            minX -= 60000;
            maxX += 60000;
        }
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
        };

        const yTicks = Array.from({ length: 5 }, (_item, index) => {
            const ratio = index / 4;
            const value = maxY - (maxY - minY) * ratio;
            const y = bounds.top + bounds.height * ratio;
            return { value, y };
        });
        const xTicks = Array.from({ length: 5 }, (_item, index) => {
            const ratio = index / 4;
            const value = minX + (maxX - minX) * ratio;
            const x = bounds.left + bounds.width * ratio;
            return { value, x };
        });

        const gridLines = yTicks
            .map((tick) => `<line x1="${bounds.left}" y1="${tick.y.toFixed(2)}" x2="${(bounds.left + bounds.width).toFixed(2)}" y2="${tick.y.toFixed(2)}" stroke="rgba(0,0,0,0.10)" stroke-width="1"/>`)
            .join("");
        const xLines = xTicks
            .map((tick) => `<line x1="${tick.x.toFixed(2)}" y1="${bounds.top}" x2="${tick.x.toFixed(2)}" y2="${(bounds.top + bounds.height).toFixed(2)}" stroke="rgba(0,0,0,0.06)" stroke-width="1"/>`)
            .join("");
        const paths = seriesItems
            .map((series, index) => {
                if (!series.points.length) {
                    return "";
                }
                const latestPoint = series.points[series.points.length - 1];
                const lastX = bounds.left + ((latestPoint.x - bounds.minX) / (bounds.maxX - bounds.minX)) * bounds.width;
                const lastY = bounds.top + (1 - (latestPoint.y - bounds.minY) / (bounds.maxY - bounds.minY)) * bounds.height;
                const pointPath = buildPlotPath(series, bounds);
                return `
                    <path d="${pointPath}" fill="none" stroke="${plotColor(index)}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
                    <circle cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="4.5" fill="${plotColor(index)}" stroke="#ffffff" stroke-width="2"/>
                `;
            })
            .join("");
        const yLabels = yTicks
            .map((tick) => `<text x="${bounds.left - 10}" y="${(tick.y + 4).toFixed(2)}" text-anchor="end" fill="rgba(0,0,0,0.66)" font-size="11">${escapeHtml(formatPlotValue(tick.value, unitKey))}</text>`)
            .join("");
        const xLabels = xTicks
            .map((tick) => `<text x="${tick.x.toFixed(2)}" y="${viewBoxHeight - 12}" text-anchor="middle" fill="rgba(0,0,0,0.66)" font-size="11">${escapeHtml(formatPlotTimestamp(tick.value))}</text>`)
            .join("");

        const frame = document.createElement("div");
        frame.className = "process-plot-chart-frame";
        frame.innerHTML = `
            <svg class="process-plot-chart-svg" viewBox="0 0 ${viewBoxWidth} ${viewBoxHeight}" role="img" aria-label="Trend plot for ${escapeHtml(unitLabel)} values">
                <rect x="${bounds.left}" y="${bounds.top}" width="${bounds.width}" height="${bounds.height}" rx="12" fill="rgba(255,255,255,0.82)" stroke="rgba(0,0,0,0.08)"/>
                ${gridLines}
                ${xLines}
                <line x1="${bounds.left}" y1="${(bounds.top + bounds.height).toFixed(2)}" x2="${(bounds.left + bounds.width).toFixed(2)}" y2="${(bounds.top + bounds.height).toFixed(2)}" stroke="rgba(0,0,0,0.2)" stroke-width="1.2"/>
                ${paths}
                ${yLabels}
                ${xLabels}
            </svg>
        `;
        card.appendChild(frame);
        return card;
    }

    function renderPlotCharts(seriesItems) {
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
            fragment.appendChild(renderPlotChartCard(unitKey, group));
        }
        plotChartStack.appendChild(fragment);
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
            renderPlotCharts([]);
            setPlotStatus(
                activeBuildId
                    ? (plotSeriesOptions.length
                        ? "Select one or more values to display a live trend plot."
                        : "No plottable measurement channels are available for this flowsheet yet.")
                    : "Select a flowsheet with mapped sensors or actuators to display plots.",
                "muted",
            );
            return;
        }

        const requestId = state.plotRequestId + 1;
        state.plotRequestId = requestId;
        state.isPlotBusy = true;
        if (!settings.quiet) {
            setPlotStatus("Loading trend data for the selected values...", "muted");
        }

        try {
            const storedOptions = selectedOptions.filter((option) => option.dataSource !== "runtime_fallback");
            const runtimeOptions = selectedOptions.filter((option) => option.dataSource === "runtime_fallback");
            await ensureRuntimePlotSamples(runtimeOptions);

            const payloads = await Promise.all(
                storedOptions.map((option) =>
                    fetchJson(
                        `/api/devices/${option.deviceId}/measurements?channel_code=${encodeURIComponent(option.channelCode)}&limit=${PROCESS_PLOT_SERIES_LIMIT}`,
                        { timeoutMs: 16000, maxRetries: 1 },
                    ),
                ),
            );
            if (requestId !== state.plotRequestId) {
                return;
            }

            const storedSeries = storedOptions.map((option, index) => normalizePlotMeasurements(option, payloads[index]?.items));
            const runtimeSeries = runtimeOptions.map((option) => ({
                ...option,
                unit: option.unit,
                points: runtimePlotPointsForSeries(option),
            }));
            const mergedSeriesById = new Map();
            for (const series of [...storedSeries, ...runtimeSeries]) {
                const existing = mergedSeriesById.get(series.id);
                if (!existing) {
                    mergedSeriesById.set(series.id, series);
                    continue;
                }
                mergedSeriesById.set(series.id, {
                    ...existing,
                    unit: existing.unit || series.unit,
                    points: mergePlotPoints(existing.points, series.points),
                });
            }
            const seriesItems = selectedOptions.map((option) => {
                const mergedSeries = mergedSeriesById.get(option.id);
                return mergedSeries || { ...option, points: runtimePlotPointsForSeries(option) };
            });
            state.plotSeriesData = seriesItems;
            renderPlotCharts(seriesItems);

            const populatedSeries = seriesItems.filter((series) => series.points.length > 0).length;
            if (populatedSeries > 0) {
                setPlotStatus(
                    `Plot updated. ${populatedSeries} selected series currently contain trend data.`,
                    settings.quiet ? "muted" : "success",
                );
            } else {
                setPlotStatus(
                    runtimeOptions.length > 0
                        ? "No plot data is available yet. Live fallback values appear once the selected actuator is polled."
                        : "No plot data is available for the selected values yet.",
                    "muted",
                );
            }
        } catch (error) {
            if (requestId !== state.plotRequestId) {
                return;
            }
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

    function currentBuildId() {
        const buildId = Number(buildData?.reactor_build_id);
        return Number.isInteger(buildId) && buildId > 0 ? buildId : null;
    }

    function persistViewState() {
        const buildId = currentBuildId();
        if (!buildId) {
            clearPersistedViewState();
            return;
        }

        try {
            window.localStorage.setItem(
                PROCESS_VIEW_STORAGE_KEY,
                JSON.stringify({
                    buildId,
                    manualMode: state.manualMode,
                    selectedNodeId: state.selectedNodeId || null,
                    selectedPlotSeriesIds: Array.isArray(state.selectedPlotSeriesIds) ? state.selectedPlotSeriesIds : [],
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

    function isIkaMotorTarget(node, target) {
        const protocol = normalizedProtocolName(target?.protocol);
        const symbolId = asString(node?.symbol_id, "").trim().toLowerCase();
        return protocol === "ika_eurostar_60" && symbolId === "motor";
    }

    function syncManualControlsEnabled(enabled) {
        const allow = enabled && !state.isSending && !state.isManualBusy;
        if (manualStateInput) {
            manualStateInput.disabled = !allow;
        }
        if (manualSpeedInput) {
            manualSpeedInput.disabled = !allow;
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
        const enabled = isIkaMotorTarget(node, target);
        manualControls?.classList.toggle("is-hidden", !enabled);
        if (!enabled) {
            return;
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

    function buildManualCommandPayload(target, commandText) {
        const text = asString(commandText, "").trim();
        const protocol = normalizedProtocolName(target?.protocol);
        if (protocol === "ika_eurostar_60") {
            const normalizedText = text.toUpperCase();
            const expectResponse = normalizedText.startsWith("IN_");
            return {
                text: normalizedText,
                encoding: "ascii",
                line_ending: "space_crlf",
                response_terminator: "crlf",
                expect_response: expectResponse,
                strip_response: true,
            };
        }

        return {
            text,
            line_ending: "crlf",
            expect_response: true,
            strip_response: true,
        };
    }

    function updateManualLiveMetrics(telemetry) {
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

    function canLoadIkaSettings(node, target) {
        return Boolean(node && target && target.is_resolved && target.device_id && isIkaMotorTarget(node, target));
    }

    async function readCurrentIkaSettings(nodeId, requestId) {
        const setpointResult = await sendManualCommand("IN_SP_4", { quiet: true });
        if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
            return null;
        }

        const actualResult = await sendManualCommand("IN_PV_4", { quiet: true });
        if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
            return null;
        }

        const torqueResult = await sendManualCommand("IN_PV_5", { quiet: true });
        if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
            return null;
        }

        return {
            setpointRpm: parseIkaNumericResponse(setpointResult?.output),
            actualRpm: parseIkaNumericResponse(actualResult?.output),
            torqueNcm: parseIkaNumericResponse(torqueResult?.output),
        };
    }

    async function loadManualSettings(nodeId, options) {
        const settings = options || {};
        const node = getNodeById(nodeId);
        const target = manualTargets[nodeId] || null;
        if (!canLoadIkaSettings(node, target)) {
            return;
        }

        const requestId = state.manualRequestId + 1;
        state.manualRequestId = requestId;
        state.isManualBusy = true;
        syncManualControlsEnabled(true);
        if (!settings.quiet) {
            setManualStatus("Loading current device settings...", "muted");
        }

        try {
            const telemetry = await readCurrentIkaSettings(nodeId, requestId);
            if (!telemetry) {
                return;
            }
            syncRuntimePlotTelemetry(nodeId, telemetry, Date.now());

            const currentNode = getNodeById(nodeId);
            if (!currentNode) {
                return;
            }

            const nextSpeed = telemetry.setpointRpm == null
                ? Math.max(0, Math.round(asNumber(currentNode.control?.config?.speed, 0)))
                : Math.max(0, Math.round(telemetry.setpointRpm));
            const appearsRunning = telemetry.actualRpm != null && telemetry.actualRpm > 0.5;

            currentNode.control = {
                profile_id: currentNode.control?.profile_id || "motor_rpm",
                config: {
                    ...(currentNode.control?.config || {}),
                    speed: nextSpeed,
                    is_on: appearsRunning,
                },
            };

            updateManualLiveMetrics(telemetry);
            updateManualDeviceStatus(target, telemetry);
            renderOperatorControls(currentNode, target, { preserveInputs: shouldPreserveManualInputs(nodeId) });
            syncManualControlsEnabled(Boolean(target.device_id) && isIkaMotorTarget(currentNode, target));
            setManualStatusFromTelemetry(telemetry, {
                prefix: settings.quiet ? "Status refreshed." : "Device state loaded.",
                tone: settings.quiet ? "muted" : "success",
            });
        } catch (error) {
            if (requestId !== state.manualRequestId || state.selectedNodeId !== nodeId) {
                return;
            }
            setManualStatus(error?.message || "Current device settings could not be loaded.", "error");
        } finally {
            if (requestId === state.manualRequestId && state.selectedNodeId === nodeId) {
                state.isManualBusy = false;
                const currentNode = getNodeById(nodeId);
                syncManualControlsEnabled(Boolean(target?.device_id) && isIkaMotorTarget(currentNode, target));
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

        if (!state.manualMode) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Enable manual mode to operate actuators directly from the flowsheet.", "muted");
            return;
        }

        const node = getNodeById(state.selectedNodeId);
        if (!node) {
            clearManualInputsDirty();
            hideManualCard();
            resetManualSummary();
            syncManualControlsEnabled(false);
            setManualStatus("Click an actuator in the flowsheet to open its settings.", "muted");
            return;
        }

        const target = selectedTarget();
        showManualCard();
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
        syncManualControlsEnabled(Boolean(target.device_id) && isIkaMotorTarget(node, target));
        if (isIkaMotorTarget(node, target)) {
            setManualStatus("Set On/Off and RPM, then submit the change.", "muted");
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
        if (manualToggleButton.disabled || state.isManualBusy || state.isSending) {
            if (state.isManualBusy || state.isSending) {
                setManualStatus("Please wait until the current device request is finished.", "muted");
            }
            return;
        }
        state.manualMode = Boolean(enabled);
        if (!state.manualMode) {
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
    }

    async function sendManualCommand(commandText, options) {
        const settings = options || {};
        const target = selectedTarget();
        const text = String(commandText || "").trim();
        if (state.isSending) {
            setManualStatus("Please wait until the current command is finished.", "muted");
            return null;
        }
        if (!state.manualMode || !target || !target.is_resolved || !target.device_id) {
            setManualStatus("Select an actuator with a valid device mapping first.", "error");
            return null;
        }
        if (!text) {
            setManualStatus("A command is required.", "error");
            return null;
        }
        if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
            setManualStatus("No valid manual-control token is available for this page.", "error");
            return null;
        }

        state.isSending = true;
        syncManualControlsEnabled(true);
        if (!settings.quiet) {
            setManualStatus(`Sending ${text} to ${target.device_display_name} ...`, "muted");
        }

        const headers = {
            "Content-Type": "application/json",
        };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }

        const manualPayload = buildManualCommandPayload(target, text);

        try {
            const payload = await fetchJson(`/api/devices/${target.device_id}/commands`, {
                method: "POST",
                headers,
                timeoutMs: manualPayload.expect_response ? 20000 : 12000,
                body: JSON.stringify({
                    command_name: "manual_text",
                    requested_by: "process_manual",
                    payload: manualPayload,
                }),
            });

            const responseText = asString(payload?.result?.response_text, "");
            const metadata = payload?.result?.metadata || {};
            const output = responseText || (manualPayload.expect_response ? JSON.stringify(metadata, null, 2) : "OK");
            return {
                commandText: text,
                output,
                payload,
            };
        } catch (error) {
            throw new Error(error?.message || "Command could not be sent.");
        } finally {
            state.isSending = false;
            syncManualControlsEnabled(true);
        }
    }

    const buildData = parseJsonScript("process-build-data", null);
    const manualTargets = parseJsonScript("process-manual-targets", {});
    const plotTargetData = parseJsonScript("process-plot-targets", {});
    const metaData = parseJsonScript("process-meta-data", {});
    const definition = buildData && typeof buildData === "object" ? buildData.definition_json || {} : {};
    const nodes = Array.isArray(definition?.nodes) ? definition.nodes.map(normalizeNode) : [];
    const edges = Array.isArray(definition?.edges) ? definition.edges.map((edge) => normalizeEdge(edge, nodes)) : [];
    const persistedViewState = readPersistedViewState();
    const requestedBuildId = queryBuildId();
    const activeBuildId = currentBuildId();

    if (!activeBuildId && !requestedBuildId) {
        const persistedBuildId = Number(persistedViewState?.buildId);
        if (Number.isInteger(persistedBuildId) && persistedBuildId > 0) {
            const params = new URLSearchParams(window.location.search);
            params.set("build_id", String(persistedBuildId));
            window.location.replace(`${window.location.pathname}?${params.toString()}`);
            return;
        }
    }

    const canRestorePersistedState = activeBuildId && Number(persistedViewState?.buildId) === activeBuildId;
    const restoredSelectedNodeId = canRestorePersistedState
        ? asString(persistedViewState?.selectedNodeId, "").trim() || null
        : null;
    const plotSeriesOptions = buildPlotSeriesOptions(plotTargetData);
    const plotSeriesOptionMap = new Map(plotSeriesOptions.map((option) => [option.id, option]));
    const plotNodeGroups = buildPlotNodeGroups(plotTargetData, plotSeriesOptions);
    const restoredPlotSeriesIds = canRestorePersistedState && Array.isArray(persistedViewState?.selectedPlotSeriesIds)
        ? persistedViewState.selectedPlotSeriesIds
              .map((item) => asString(item, ""))
              .filter((item) => item && plotSeriesOptionMap.has(item))
        : [];

    const state = {
        nodes,
        edges,
        canvasSize: parseCanvasSize(definition, nodes),
        manualMode: Boolean(canRestorePersistedState && persistedViewState?.manualMode),
        selectedNodeId: restoredSelectedNodeId,
        selectedPlotSeriesIds: restoredPlotSeriesIds,
        plotSeriesData: [],
        runtimePlotSeriesById: {},
        latestPlotTelemetryByNodeId: {},
        isPlotBusy: false,
        plotRequestId: 0,
        inputsDirtyForNodeId: null,
        isSending: false,
        isManualBusy: false,
        manualRequestId: 0,
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
        if (plotPanel.open && state.selectedPlotSeriesIds.length > 0) {
            void loadPlotMeasurements({ quiet: true });
        }
    });

    manualSettingsForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        const node = getNodeById(state.selectedNodeId);
        const target = selectedTarget();
        const speed = boundedIntegerInputValue(manualSpeedInput, 0);
        const nextState = asString(manualStateInput?.value, "off").toLowerCase() === "on";

        if (!node || !target || !isIkaMotorTarget(node, target)) {
            setManualStatus("Select a mapped IKA stirrer first.", "error");
            return;
        }

        if (manualSpeedInput) {
            manualSpeedInput.value = String(speed);
        }

        if (nextState && speed <= 0) {
            setManualStatus("Enter an RPM greater than 0 before switching the stirrer on.", "error");
            return;
        }

        const requestId = state.manualRequestId + 1;
        state.manualRequestId = requestId;
        state.isManualBusy = true;
        syncManualControlsEnabled(true);
        setManualStatus("Submitting device settings...", "muted");

        void (async () => {
            if (nextState) {
                const startResult = await sendManualCommand("START_4", { quiet: true });
                if (!startResult) {
                    return;
                }
                await waitFor(180);

                const speedResult = await sendManualCommand(`OUT_SP_4 ${speed}`, { quiet: true });
                if (!speedResult) {
                    return;
                }
            } else {
                const stopResult = await sendManualCommand("STOP_4", { quiet: true });
                if (!stopResult) {
                    return;
                }
                await waitFor(180);
            }

            if (requestId !== state.manualRequestId || state.selectedNodeId !== node.id) {
                return;
            }

            const telemetry = await readCurrentIkaSettings(node.id, requestId);
            if (!telemetry) {
                return;
            }
            syncRuntimePlotTelemetry(node.id, telemetry, Date.now());

            const verifiedSpeed = telemetry.setpointRpm == null
                ? speed
                : Math.max(0, Math.round(telemetry.setpointRpm));
            const verifiedRunning = telemetry.actualRpm != null && telemetry.actualRpm > 0.5;

            node.control = {
                profile_id: node.control?.profile_id || "motor_rpm",
                config: {
                    ...(node.control?.config || {}),
                    speed: verifiedSpeed,
                    is_on: verifiedRunning,
                },
            };

            clearManualInputsDirty(node.id);
            renderOperatorControls(node, target);
            updateManualLiveMetrics(telemetry);
            updateManualDeviceStatus(target, telemetry);
            if (nextState) {
                const actualLabel = telemetry.actualRpm == null ? "unknown rpm" : `${Math.round(telemetry.actualRpm)} rpm`;
                setManualStatus(
                    `Device updated successfully. Setpoint ${verifiedSpeed} rpm, measured speed ${actualLabel}.`,
                    "success",
                );
            } else if (telemetry.actualRpm != null && telemetry.actualRpm <= 0.5) {
                setManualStatus("Device updated successfully. The stirrer is stopped.", "success");
            } else {
                const fallbackActual = telemetry.actualRpm == null ? "unknown rpm" : `${Math.round(telemetry.actualRpm)} rpm`;
                setManualStatus(
                    `Stop command sent. The device still reports ${fallbackActual}; please verify the hardware state.`,
                    "error",
                );
            }
        })()
            .catch((error) => {
                setManualStatus(error?.message || "Device settings could not be sent.", "error");
            })
            .finally(() => {
                if (requestId === state.manualRequestId && state.selectedNodeId === node?.id) {
                    state.isManualBusy = false;
                    syncManualControlsEnabled(Boolean(target?.device_id) && isIkaMotorTarget(node, target));
                }
            });
    });

    processBuildSelect?.addEventListener("change", () => {
        if (!processPickerForm || processBuildSelect.disabled) {
            return;
        }
        if (!String(processBuildSelect.value || "").trim()) {
            clearPersistedViewState();
            return;
        }
        if (typeof processPickerForm.requestSubmit === "function") {
            processPickerForm.requestSubmit();
            return;
        }
        processPickerForm.submit();
    });

    processClearSelectionLink?.addEventListener("click", () => {
        clearPersistedViewState();
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
        if (!canLoadIkaSettings(node, target)) {
            return;
        }
        void loadManualSettings(nodeId, { quiet: true });
    }, MANUAL_LIVE_POLL_MS);

    window.setInterval(() => {
        if (document.hidden || !plotPanel?.open || state.isPlotBusy || state.selectedPlotSeriesIds.length === 0) {
            return;
        }
        void loadPlotMeasurements({ quiet: true });
    }, PROCESS_PLOT_REFRESH_MS);

    if (manualToggleButton) {
        manualToggleButton.setAttribute("aria-pressed", String(state.manualMode));
        manualToggleButton.classList.toggle("btn-primary", state.manualMode);
        manualToggleButton.textContent = "Manual";
    }

    renderPlotSelection();
    renderPlotCharts(state.plotSeriesData);
    void loadPlotMeasurements({ quiet: true });
    syncManualModeToggle();
    renderAll();
    persistViewState();
})();
