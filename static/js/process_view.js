(function () {
    const surface = document.getElementById("process-flowsheet-surface");
    if (!surface) {
        return;
    }

    const edgeLayer = document.getElementById("process-edge-layer");
    const nodeLayer = document.getElementById("process-node-layer");
    const emptyState = document.getElementById("process-flowsheet-empty");
    const manualToggleButton = document.getElementById("process-manual-mode-toggle");
    const manualEmpty = document.getElementById("process-manual-empty");
    const manualEmptyText = document.getElementById("process-manual-empty-text");
    const manualPanel = document.getElementById("process-manual-panel");
    const manualTargetTitle = document.getElementById("process-manual-target-title");
    const manualTargetSubtitle = document.getElementById("process-manual-target-subtitle");
    const manualDevice = document.getElementById("process-manual-device");
    const manualConnection = document.getElementById("process-manual-connection");
    const manualProtocol = document.getElementById("process-manual-protocol");
    const manualDeviceStatus = document.getElementById("process-manual-device-status");
    const manualProtocolHint = document.getElementById("process-manual-protocol-hint");
    const manualProtocolNote = document.getElementById("process-manual-protocol-note");
    const manualReadout = document.getElementById("process-manual-readout");
    const manualRefreshButton = document.getElementById("process-manual-refresh-button");
    const manualReadoutName = document.getElementById("process-manual-readout-name");
    const manualReadoutMode = document.getElementById("process-manual-readout-mode");
    const manualReadoutSetpoint = document.getElementById("process-manual-readout-setpoint");
    const manualReadoutActual = document.getElementById("process-manual-readout-actual");
    const manualReadoutPv5 = document.getElementById("process-manual-readout-pv5");
    const manualQuickActions = document.getElementById("process-manual-quick-actions");
    const manualProfileForm = document.getElementById("process-manual-profile-form");
    const manualProfileGrid = document.getElementById("process-manual-profile-grid");
    const manualApplyButton = document.getElementById("process-manual-apply-button");
    const manualCommandInput = document.getElementById("process-manual-command-input");
    const manualCommandForm = document.getElementById("process-manual-command-form");
    const manualSendButton = document.getElementById("process-manual-send-button");
    const manualStatus = document.getElementById("process-manual-status");
    const manualResponse = document.getElementById("process-manual-response");

    function getManualActionButtons() {
        return Array.from(manualQuickActions?.querySelectorAll("button") || []);
    }

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

    function profileForNode(node) {
        if (!node) {
            return null;
        }
        const explicit = actuatorProfileById.get(asString(node.control?.profile_id, ""));
        if (explicit && explicit.allowed_symbols.includes(String(node.symbol_id || ""))) {
            return explicit;
        }
        return profileForSymbol(node.symbol_id);
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

    function buildAutoEdgePoints(sourcePoint, sourceSide, targetPoint, targetSide) {
        const stubDistance = 28;
        const sourceStub = offsetPoint(sourcePoint, sourceSide, stubDistance);
        const targetStub = offsetPoint(targetPoint, targetSide, stubDistance);
        const sourceHorizontal = sourceSide === "west" || sourceSide === "east";
        const targetHorizontal = targetSide === "west" || targetSide === "east";
        const points = [sourcePoint, sourceStub];

        if (sourceHorizontal && targetHorizontal) {
            const middleX = roundCanvasValue((sourceStub.x + targetStub.x) / 2);
            points.push({ x: middleX, y: sourceStub.y });
            points.push({ x: middleX, y: targetStub.y });
        } else if (!sourceHorizontal && !targetHorizontal) {
            const middleY = roundCanvasValue((sourceStub.y + targetStub.y) / 2);
            points.push({ x: sourceStub.x, y: middleY });
            points.push({ x: targetStub.x, y: middleY });
        } else if (sourceHorizontal) {
            points.push({ x: targetStub.x, y: sourceStub.y });
        } else {
            points.push({ x: sourceStub.x, y: targetStub.y });
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
        return buildAutoEdgePoints(sourcePoint, sourceSide, targetPoint, targetSide).slice(1, -1);
    }

    function edgePolylinePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide) {
        return [sourcePoint, ...edgeRoutePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide), targetPoint];
    }

    function edgePathFromPoints(points) {
        return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
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

        for (const edge of state.edges) {
            const sourceNode = getNodeById(edge.source_node_id);
            const targetNode = getNodeById(edge.target_node_id);
            if (!sourceNode || !targetNode) {
                continue;
            }

            const sourcePoint = anchorPoint(sourceNode, edge.source_anchor_id);
            const sourceSide = anchorSide(sourceNode, edge.source_anchor_id);
            const targetPoint = anchorPoint(targetNode, edge.target_anchor_id);
            const targetSide = anchorSide(targetNode, edge.target_anchor_id);
            const polylinePoints = edgePolylinePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide);
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("d", edgePathFromPoints(polylinePoints));
            path.setAttribute("class", "builder-edge");
            edgeLayer.appendChild(path);
        }
    }

    function isActuator(node) {
        return String(node?.category || "").trim().toLowerCase() === "actuators";
    }

    function isTargetResolved(nodeId) {
        return Boolean(manualTargets[nodeId]?.is_resolved);
    }

    function selectActuator(nodeId) {
        if (!state.manualMode) {
            return;
        }
        const node = getNodeById(nodeId);
        if (!node || !isActuator(node)) {
            return;
        }
        state.selectedNodeId = nodeId;
        renderNodes();
        updateManualPanel();
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
                    element.title = `${node.instance_id || node.label}: manueller Zugriff verfuegbar`;
                } else {
                    element.classList.add("is-manual-unresolved");
                    element.title = `${node.instance_id || node.label}: keine gueltige Kommunikationszuordnung`;
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

    function showManualState(message) {
        manualEmpty.classList.remove("is-hidden");
        manualPanel.classList.add("is-hidden");
        manualEmptyText.textContent = message;
    }

    function showManualPanel() {
        manualEmpty.classList.add("is-hidden");
        manualPanel.classList.remove("is-hidden");
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

    function setManualResponse(content) {
        const text = String(content || "").trim();
        manualResponse.textContent = text;
        manualResponse.classList.toggle("is-hidden", !text);
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
                    const error = new Error(responseMessage(payload, "Befehl konnte nicht gesendet werden."));
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
                    throw new Error("Request Timeout. Bitte Verbindung und Serverstatus pruefen.");
                }
                if (error?.payload) {
                    error.message = responseMessage(error.payload, error.message || "Befehl konnte nicht gesendet werden.");
                }
                throw error;
            } finally {
                window.clearTimeout(timer);
            }
        }

        throw new Error(lastError?.message || "Befehl konnte nicht gesendet werden.");
    }

    function selectedTarget() {
        return state.selectedNodeId ? manualTargets[state.selectedNodeId] || null : null;
    }

    function formatDeviceStatus(target) {
        const onlineText = target.is_online ? "online" : "offline";
        return target.quality_state ? `${onlineText} | ${target.quality_state}` : onlineText;
    }

    function syncManualControlsEnabled(enabled) {
        const allow = enabled && !state.isSending;
        if (manualProfileForm) {
            for (const element of manualProfileForm.elements) {
                element.disabled = !allow;
            }
        }
        manualCommandInput.disabled = !allow;
        manualSendButton.disabled = !allow;
        if (manualApplyButton) {
            manualApplyButton.disabled = !allow;
        }
        if (manualRefreshButton) {
            manualRefreshButton.disabled = !allow || Boolean(manualRefreshButton.hidden);
        }
        for (const button of getManualActionButtons()) {
            button.disabled = !allow;
        }
    }

    function renderManualProfile(node, profile) {
        if (!manualProfileGrid) {
            return;
        }

        manualProfileGrid.innerHTML = "";
        if (!node || !profile) {
            return;
        }

        const config = node.control?.config || {};
        for (const field of profile.fields) {
            if (field.type === "boolean") {
                const toggle = document.createElement("label");
                toggle.className = "process-manual-profile-toggle";
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.name = `manual-${field.key}`;
                checkbox.checked = Boolean(config[field.key]);
                const copy = document.createElement("span");
                copy.textContent = field.label;
                toggle.appendChild(checkbox);
                toggle.appendChild(copy);
                manualProfileGrid.appendChild(toggle);
                continue;
            }

            const fieldLabel = document.createElement("label");
            fieldLabel.className = "process-select-field process-manual-profile-field";
            const fieldText = document.createElement("span");
            fieldText.textContent = field.unit ? `${field.label} (${field.unit})` : field.label;
            const input = document.createElement("input");
            input.type = "number";
            input.name = `manual-${field.key}`;
            input.value = String(config[field.key] ?? field.default ?? "");
            if (field.min != null) {
                input.min = String(field.min);
            }
            if (field.max != null) {
                input.max = String(field.max);
            }
            if (field.step != null) {
                input.step = String(field.step);
            }
            fieldLabel.appendChild(fieldText);
            fieldLabel.appendChild(input);
            manualProfileGrid.appendChild(fieldLabel);
        }
    }

    function collectManualProfileValues(node, profile) {
        const current = node.control?.config || {};
        const nextValues = {};
        for (const field of profile.fields) {
            const input = manualProfileGrid?.querySelector(`[name="manual-${field.key}"]`);
            if (!input) {
                nextValues[field.key] = current[field.key] ?? field.default;
                continue;
            }
            if (field.type === "boolean") {
                nextValues[field.key] = Boolean(input.checked);
                continue;
            }
            let nextValue = asNumber(input.value, current[field.key] ?? field.default ?? 0);
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
            input.value = String(nextValue);
            nextValues[field.key] = nextValue;
        }
        return nextValues;
    }

    function buildManualCommandSequence(profile, values) {
        const commands = [];
        for (const item of profile.command_sequence || []) {
            if (item.kind === "choice") {
                commands.push(values[item.field] ? item.true : item.false);
                continue;
            }
            if (item.kind === "template") {
                commands.push(
                    item.template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_match, key) => String(values[key] ?? "")),
                );
            }
        }
        return commands.map((item) => String(item || "").trim()).filter(Boolean);
    }

    function normalizedProtocolName(value) {
        return asString(value, "").trim().toLowerCase();
    }

    function protocolLabel(value) {
        const id = asString(value, "");
        return protocolLabelMap.get(id) || id || "n/a";
    }

    function protocolUiConfig(target) {
        const protocol = normalizedProtocolName(target?.protocol);
        if (protocol === "ika_eurostar_60") {
            return {
                note: "Validierter IKA-Betrieb ueber Moxa mit 9600 / 7E1 / none. Echte Bewegung immer ueber IN_PV_4 pruefen; IN_SP_4 zeigt nur den Sollwert.",
                placeholder: "z. B. IN_NAME, IN_PV_4, START_4, OUT_SP_4 300",
                applyLabel: "Start/Stop und Sollwert senden",
                quickActions: [
                    { label: "Status lesen", action: "refresh-status" },
                    { label: "IN_NAME", command: "IN_NAME" },
                    { label: "IN_MODE", command: "IN_MODE" },
                    { label: "IN_SP_4", command: "IN_SP_4" },
                    { label: "IN_PV_4", command: "IN_PV_4" },
                    { label: "IN_PV_5", command: "IN_PV_5" },
                    { label: "START_4", command: "START_4" },
                    { label: "STOP_4", command: "STOP_4" },
                ],
            };
        }

        return {
            note: "",
            placeholder: "z. B. START, STOP, OPEN, CLOSE",
            applyLabel: "Aktor anwenden",
            quickActions: [],
        };
    }

    function selectedSnapshot(target) {
        const deviceId = asString(target?.device_id, "");
        if (!deviceId) {
            return {};
        }
        return state.protocolSnapshots[deviceId] || {};
    }

    function setReadoutValue(element, value) {
        if (!element) {
            return;
        }
        element.textContent = asString(value, "-");
    }

    function renderProtocolReadout(target) {
        const protocol = normalizedProtocolName(target?.protocol);
        if (protocol !== "ika_eurostar_60") {
            manualReadout?.classList.add("is-hidden");
            setReadoutValue(manualReadoutName, "-");
            setReadoutValue(manualReadoutMode, "-");
            setReadoutValue(manualReadoutSetpoint, "-");
            setReadoutValue(manualReadoutActual, "-");
            setReadoutValue(manualReadoutPv5, "-");
            return;
        }

        const snapshot = selectedSnapshot(target);
        manualReadout?.classList.remove("is-hidden");
        setReadoutValue(manualReadoutName, snapshot.name || "-");
        setReadoutValue(manualReadoutMode, snapshot.mode || "-");
        setReadoutValue(manualReadoutSetpoint, snapshot.setpoint_rpm || "-");
        setReadoutValue(manualReadoutActual, snapshot.actual_rpm || "-");
        setReadoutValue(manualReadoutPv5, snapshot.pv5 || "-");
    }

    function renderProtocolQuickActions(target) {
        if (!manualQuickActions) {
            return;
        }

        const config = protocolUiConfig(target);
        manualQuickActions.innerHTML = "";
        for (const action of config.quickActions) {
            const button = document.createElement("button");
            button.className = "btn";
            button.type = "button";
            button.textContent = action.label;
            if (action.command) {
                button.dataset.manualCommand = action.command;
            }
            if (action.action) {
                button.dataset.manualAction = action.action;
            }
            manualQuickActions.appendChild(button);
        }
        manualQuickActions.classList.toggle("is-hidden", config.quickActions.length === 0);
    }

    function renderProtocolSections(target) {
        const config = protocolUiConfig(target);
        manualCommandInput.placeholder = config.placeholder;
        manualApplyButton.textContent = config.applyLabel;

        if (config.note) {
            manualProtocolNote.textContent = config.note;
            manualProtocolHint?.classList.remove("is-hidden");
        } else {
            manualProtocolNote.textContent = "";
            manualProtocolHint?.classList.add("is-hidden");
        }

        if (manualRefreshButton) {
            manualRefreshButton.hidden = normalizedProtocolName(target?.protocol) !== "ika_eurostar_60";
        }
        renderProtocolQuickActions(target);
        renderProtocolReadout(target);
    }

    function extractIkaChannelValue(responseText, suffix) {
        const text = asString(responseText, "");
        if (!text) {
            return "";
        }
        const normalizedSuffix = String(suffix);
        if (text.endsWith(` ${normalizedSuffix}`)) {
            return text.slice(0, -(` ${normalizedSuffix}`.length)).trim();
        }
        return text;
    }

    function updateIkaSnapshot(target, commandText, responseText) {
        const deviceId = asString(target?.device_id, "");
        if (!deviceId) {
            return;
        }

        const normalizedCommand = asString(commandText, "").toUpperCase();
        const response = asString(responseText, "");
        const nextSnapshot = {
            ...selectedSnapshot(target),
        };

        if (normalizedCommand === "IN_NAME") {
            nextSnapshot.name = response;
        } else if (normalizedCommand === "IN_MODE") {
            nextSnapshot.mode = response;
        } else if (normalizedCommand === "IN_SP_4") {
            const value = extractIkaChannelValue(response, 4);
            nextSnapshot.setpoint_rpm = value ? `${value} rpm` : response;
        } else if (normalizedCommand === "IN_PV_4") {
            const value = extractIkaChannelValue(response, 4);
            nextSnapshot.actual_rpm = value ? `${value} rpm` : response;
        } else if (normalizedCommand === "IN_PV_5") {
            nextSnapshot.pv5 = extractIkaChannelValue(response, 5) || response;
        }

        state.protocolSnapshots[deviceId] = nextSnapshot;
        renderProtocolReadout(target);
    }

    async function refreshProtocolStatus(target) {
        const protocol = normalizedProtocolName(target?.protocol);
        if (protocol !== "ika_eurostar_60") {
            return;
        }

        const commands = ["IN_NAME", "IN_MODE", "IN_SP_4", "IN_PV_4", "IN_PV_5"];
        const outputs = [];
        for (const command of commands) {
            const result = await sendManualCommand(command, { quiet: true });
            outputs.push(`> ${result.commandText}\n${result.output || "OK"}`);
        }
        setManualResponse(outputs.join("\n\n"));
        setManualStatus("IKA-Status erfolgreich aktualisiert.", "success");
    }

    function buildProtocolAwareCommandSequence(node, target, profile, values) {
        const protocol = normalizedProtocolName(target?.protocol);
        const symbolId = asString(node?.symbol_id, "").trim().toLowerCase();
        if (protocol === "ika_eurostar_60" && symbolId === "motor") {
            if (!values.is_on) {
                return ["STOP_4"];
            }
            return [
                "START_4",
                `OUT_SP_4 ${Math.round(asNumber(values.speed, 0))}`,
            ];
        }
        return buildManualCommandSequence(profile, values);
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

    function updateManualPanel() {
        if (state.nodes.length === 0) {
            syncManualControlsEnabled(false);
            showManualState("Lade zuerst ein Flowsheet, um den manuellen Modus zu verwenden.");
            return;
        }

        if (!state.manualMode) {
            syncManualControlsEnabled(false);
            showManualState("Aktiviere den manuellen Modus, um Aktoren direkt im Flowsheet zu bedienen.");
            return;
        }

        const node = getNodeById(state.selectedNodeId);
        if (!node) {
            syncManualControlsEnabled(false);
            showManualState("Klicke im Flowsheet auf einen Aktor, um dessen Bedienfunktionen zu oeffnen.");
            return;
        }

        const target = selectedTarget();
        if (!target || !target.is_resolved) {
            syncManualControlsEnabled(false);
            const reason = target?.resolution_note || "Fuer diesen Aktor ist keine gueltige Kommunikationszuordnung vorhanden.";
            showManualState(reason);
            return;
        }

        const profile = profileForNode(node);
        if (!profile) {
            syncManualControlsEnabled(false);
            showManualState("Fuer diesen Aktor ist kein Bedienprofil hinterlegt.");
            return;
        }

        showManualPanel();
        manualTargetTitle.textContent = node.instance_id || node.label;
        manualTargetSubtitle.textContent = `${node.symbol_id} | ${profile.label}`;
        manualDevice.textContent = `${target.device_display_name} (${target.asset_serial})`;
        manualConnection.textContent = `${target.server_code} | ${target.connection_label}`;
        manualProtocol.textContent = protocolLabel(target.protocol);
        manualDeviceStatus.textContent = formatDeviceStatus(target);
        renderProtocolSections(target);
        renderManualProfile(node, profile);
        syncManualControlsEnabled(Boolean(target.device_id));
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
        if (manualToggleButton.disabled) {
            return;
        }
        state.manualMode = Boolean(enabled);
        if (!state.manualMode) {
            state.selectedNodeId = null;
            setManualResponse("");
        }

        manualToggleButton.setAttribute("aria-pressed", String(state.manualMode));
        manualToggleButton.classList.toggle("btn-primary", state.manualMode);
        manualToggleButton.textContent = state.manualMode ? "Aktiv" : "Aktivieren";
        renderAll();
    }

    async function sendManualCommand(commandText, options) {
        const settings = options || {};
        const target = selectedTarget();
        const text = String(commandText || "").trim();
        if (!state.manualMode || !target || !target.is_resolved || !target.device_id) {
            setManualStatus("Waehle zuerst einen gueltig zugeordneten Aktor aus.", "error");
            return;
        }
        if (!text) {
            setManualStatus("Ein Befehlstext ist erforderlich.", "error");
            return;
        }
        if (metaData.apiAuthRequired && !metaData.manualWriteToken) {
            setManualStatus("Fuer den manuellen Modus ist kein gueltiger Web-Token verfuegbar.", "error");
            return;
        }

        state.isSending = true;
        syncManualControlsEnabled(true);
        if (!settings.quiet) {
            setManualStatus(`Sende ${text} an ${target.device_display_name} ...`, "muted");
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
            if (responseText && normalizedProtocolName(target.protocol) === "ika_eurostar_60") {
                updateIkaSnapshot(target, manualPayload.text, responseText);
            }
            return {
                commandText: text,
                output,
                payload,
            };
        } catch (error) {
            if (error?.payload && typeof error.payload === "object" && Object.keys(error.payload).length > 0) {
                setManualResponse(JSON.stringify(error.payload, null, 2));
            }
            throw new Error(error?.message || "Befehl konnte nicht gesendet werden.");
        } finally {
            state.isSending = false;
            syncManualControlsEnabled(true);
        }
    }

    async function applyManualProfile(event) {
        event.preventDefault();
        const node = getNodeById(state.selectedNodeId);
        const target = selectedTarget();
        const profile = profileForNode(node);
        if (!node || !target || !profile) {
            setManualStatus("Waehle zuerst einen gueltig zugeordneten Aktor aus.", "error");
            return;
        }

        const values = collectManualProfileValues(node, profile);
        const commands = buildProtocolAwareCommandSequence(node, target, profile, values);
        if (commands.length === 0) {
            setManualStatus("Fuer dieses Profil sind keine ausfuehrbaren Befehle hinterlegt.", "error");
            return;
        }

        try {
            const outputs = [];
            for (const command of commands) {
                const result = await sendManualCommand(command, { quiet: true });
                outputs.push(`> ${result.commandText}\n${result.output || "OK"}`);
            }
            node.control = {
                profile_id: profile.id,
                config: values,
            };
            renderManualProfile(node, profile);
            if (normalizedProtocolName(target.protocol) === "ika_eurostar_60") {
                await refreshProtocolStatus(target);
            } else {
                setManualResponse(outputs.join("\n\n"));
            }
            setManualStatus(`Aktorwerte fuer ${node.instance_id || node.label} angewendet.`, "success");
        } catch (error) {
            setManualStatus(error?.message || "Aktorwerte konnten nicht angewendet werden.", "error");
        }
    }

    const buildData = parseJsonScript("process-build-data", null);
    const manualTargets = parseJsonScript("process-manual-targets", {});
    const metaData = parseJsonScript("process-meta-data", {});
    const definition = buildData && typeof buildData === "object" ? buildData.definition_json || {} : {};
    const nodes = Array.isArray(definition?.nodes) ? definition.nodes.map(normalizeNode) : [];
    const edges = Array.isArray(definition?.edges) ? definition.edges.map((edge) => normalizeEdge(edge, nodes)) : [];

    const state = {
        nodes,
        edges,
        canvasSize: parseCanvasSize(definition, nodes),
        manualMode: false,
        selectedNodeId: null,
        isSending: false,
        protocolSnapshots: {},
    };

    manualToggleButton?.addEventListener("click", () => {
        setManualMode(!state.manualMode);
    });

    manualProfileForm?.addEventListener("submit", (event) => {
        void applyManualProfile(event);
    });

    manualCommandForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        void sendManualCommand(manualCommandInput.value)
            .then((result) => {
                setManualResponse(`> ${result.commandText}\n${result.output || "OK"}`);
                setManualStatus(`Befehl ${result.commandText} erfolgreich gesendet.`, "success");
            })
            .catch((error) => {
                setManualStatus(error?.message || "Befehl konnte nicht gesendet werden.", "error");
            });
    });

    manualRefreshButton?.addEventListener("click", () => {
        const target = selectedTarget();
        if (!target) {
            setManualStatus("Waehle zuerst einen gueltig zugeordneten Aktor aus.", "error");
            return;
        }
        void refreshProtocolStatus(target).catch((error) => {
            setManualStatus(error?.message || "Status konnte nicht gelesen werden.", "error");
        });
    });

    manualQuickActions?.addEventListener("click", (event) => {
        const button =
            event.target instanceof Element
                ? event.target.closest("button[data-manual-command], button[data-manual-action]")
                : null;
        if (!button) {
            return;
        }

        const manualAction = String(button.dataset.manualAction || "").trim();
        if (manualAction === "refresh-status") {
            const target = selectedTarget();
            if (!target) {
                setManualStatus("Waehle zuerst einen gueltig zugeordneten Aktor aus.", "error");
                return;
            }
            void refreshProtocolStatus(target).catch((error) => {
                setManualStatus(error?.message || "Status konnte nicht gelesen werden.", "error");
            });
            return;
        }

        const command = String(button.dataset.manualCommand || "").trim();
        if (!command) {
            return;
        }
        manualCommandInput.value = command;
        void sendManualCommand(command)
            .then((result) => {
                setManualResponse(`> ${result.commandText}\n${result.output || "OK"}`);
                setManualStatus(`Befehl ${result.commandText} erfolgreich gesendet.`, "success");
            })
            .catch((error) => {
                setManualStatus(error?.message || "Befehl konnte nicht gesendet werden.", "error");
            });
    });

    renderAll();
})();
