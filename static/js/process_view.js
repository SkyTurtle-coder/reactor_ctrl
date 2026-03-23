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
    const manualCommandInput = document.getElementById("process-manual-command-input");
    const manualCommandForm = document.getElementById("process-manual-command-form");
    const manualSendButton = document.getElementById("process-manual-send-button");
    const manualStatus = document.getElementById("process-manual-status");
    const manualResponse = document.getElementById("process-manual-response");
    const manualCommandButtons = Array.from(document.querySelectorAll("[data-manual-command]"));

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

    function selectedTarget() {
        return state.selectedNodeId ? manualTargets[state.selectedNodeId] || null : null;
    }

    function formatDeviceStatus(target) {
        const onlineText = target.is_online ? "online" : "offline";
        return target.quality_state ? `${onlineText} | ${target.quality_state}` : onlineText;
    }

    function syncManualControlsEnabled(enabled) {
        const allow = enabled && !state.isSending;
        manualCommandInput.disabled = !allow;
        manualSendButton.disabled = !allow;
        for (const button of manualCommandButtons) {
            button.disabled = !allow;
        }
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

        showManualPanel();
        manualTargetTitle.textContent = node.instance_id || node.label;
        manualTargetSubtitle.textContent = `${node.symbol_id} | ${target.device_type || "device"}`;
        manualDevice.textContent = `${target.device_display_name} (${target.asset_serial})`;
        manualConnection.textContent = `${target.server_code} | ${target.connection_label}`;
        manualProtocol.textContent = target.protocol || "n/a";
        manualDeviceStatus.textContent = formatDeviceStatus(target);
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

    async function sendManualCommand(commandText) {
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
        setManualStatus(`Sende ${text} an ${target.device_display_name} ...`, "muted");

        const headers = {
            "Content-Type": "application/json",
        };
        if (metaData.manualWriteToken) {
            headers["X-Process-Manual-Token"] = metaData.manualWriteToken;
        }

        try {
            const response = await fetch(`/api/devices/${target.device_id}/commands`, {
                method: "POST",
                headers,
                body: JSON.stringify({
                    command_name: "manual_text",
                    requested_by: "process_manual",
                    payload: {
                        text,
                        line_ending: "crlf",
                        expect_response: true,
                        strip_response: true,
                    },
                }),
            });
            const payload = await response.json().catch(() => ({}));

            if (!response.ok) {
                const errorMessage = [payload.error || "Befehl konnte nicht gesendet werden.", payload.details || ""]
                    .filter(Boolean)
                    .join(" ");
                setManualResponse(JSON.stringify(payload, null, 2));
                throw new Error(errorMessage);
            }

            const responseText = asString(payload?.result?.response_text, "");
            const metadata = payload?.result?.metadata || {};
            const output = responseText || JSON.stringify(metadata, null, 2);
            setManualResponse(output);
            setManualStatus(`Befehl ${text} erfolgreich gesendet.`, "success");
        } catch (error) {
            setManualStatus(error?.message || "Befehl konnte nicht gesendet werden.", "error");
        } finally {
            state.isSending = false;
            syncManualControlsEnabled(true);
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
    };

    manualToggleButton?.addEventListener("click", () => {
        setManualMode(!state.manualMode);
    });

    manualCommandForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        sendManualCommand(manualCommandInput.value);
    });

    for (const button of manualCommandButtons) {
        const command = String(button.dataset.manualCommand || "").trim();
        button.addEventListener("click", () => {
            manualCommandInput.value = command;
            sendManualCommand(command);
        });
    }

    renderAll();
})();
