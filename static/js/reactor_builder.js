(function () {
    const canvas = document.getElementById("builder-canvas");
    if (!canvas) {
        return;
    }

    const edgeLayer = document.getElementById("builder-edge-layer");
    const nodeLayer = document.getElementById("builder-node-layer");
    const emptyState = document.getElementById("builder-canvas-empty");
    const nameInput = document.getElementById("builder-name-input");
    const dateInput = document.getElementById("builder-date-input");
    const userInput = document.getElementById("builder-user-input");
    const buildSelect = document.getElementById("builder-build-select");
    const newBuildButton = document.getElementById("builder-new-build-button");
    const saveButton = document.getElementById("builder-save-button");
    const saveAsButton = document.getElementById("builder-save-as-button");
    const deleteNodeButton = document.getElementById("builder-delete-node-button");
    const selectToolButton = document.getElementById("builder-select-tool");
    const anchorToolButton = document.getElementById("builder-anchor-tool");
    const connectToolButton = document.getElementById("builder-connect-tool");
    const layoutViewTabButton = document.getElementById("builder-layout-view-tab");
    const communicationViewTabButton = document.getElementById("builder-communication-view-tab");
    const layoutView = document.getElementById("builder-layout-view");
    const communicationView = document.getElementById("builder-communication-view");
    const communicationBody = document.getElementById("builder-communication-body");
    const instanceModal = document.getElementById("builder-instance-modal");
    const instanceModalCopy = document.getElementById("builder-instance-modal-copy");
    const instanceIdInput = document.getElementById("builder-instance-id-input");
    const instanceCancelButton = document.getElementById("builder-instance-cancel-button");
    const instanceConfirmButton = document.getElementById("builder-instance-confirm-button");
    const statusElement = document.getElementById("builder-status");
    const libraryItems = Array.from(document.querySelectorAll(".builder-symbol-item"));
    const librarySearchInput = document.getElementById("builder-library-search-input");
    const librarySearchClearButton = document.getElementById("builder-library-search-clear");
    const libraryEmptyState = document.getElementById("builder-library-empty");
    const symbolAddButtons = Array.from(document.querySelectorAll("[data-symbol-add]"));
    const libraryCategories = Array.from(document.querySelectorAll(".builder-category"));
    const currentBuildElement = document.getElementById("builder-current-build");
    const nodeCountElement = document.getElementById("builder-node-count");
    const edgeCountElement = document.getElementById("builder-edge-count");
    const saveStateElement = document.getElementById("builder-save-state");

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

    function parseBuildId(value) {
        const numeric = Number.parseInt(String(value ?? ""), 10);
        return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
    }

    function generateId(prefix) {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return `${prefix}-${window.crypto.randomUUID()}`;
        }
        return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
    }

    function normalizeInstanceIdInput(value) {
        return String(value ?? "")
            .trim()
            .replace(/\s+/g, "-");
    }

    function validateInstanceIdInput(value) {
        const normalized = normalizeInstanceIdInput(value);
        if (!normalized) {
            throw new Error("Element ID darf nicht leer sein.");
        }
        if (!/^[A-Za-z0-9._-]+$/.test(normalized)) {
            throw new Error("Element IDs duerfen nur Buchstaben, Zahlen, Punkt, Unterstrich oder Bindestrich enthalten.");
        }
        return normalized;
    }

    const libraryCategoryData = parseJsonScript("builder-library-data", []);
    const buildData = parseJsonScript("builder-build-data", null);
    const metaData = parseJsonScript("builder-meta-data", {});

    const libraryById = new Map();
    for (const category of Array.isArray(libraryCategoryData) ? libraryCategoryData : []) {
        const symbols = Array.isArray(category?.symbols) ? category.symbols : [];
        for (const rawSymbol of symbols) {
            const width = Math.max(40, asNumber(rawSymbol.default_width ?? rawSymbol.width, 120));
            const height = Math.max(40, asNumber(rawSymbol.default_height ?? rawSymbol.height, 80));
            const ports = Array.isArray(rawSymbol.ports)
                ? rawSymbol.ports
                      .filter((port) => port && typeof port === "object")
                      .map((port, index) => {
                          const xRatio = clamp(asNumber(port.x, width / 2) / width, 0, 1);
                          const yRatio = clamp(asNumber(port.y, height / 2) / height, 0, 1);
                          return {
                              id: asString(port.id, `port-${index + 1}`),
                              x_ratio: xRatio,
                              y_ratio: yRatio,
                              side: directionToSide(port.direction, xRatio, yRatio),
                          };
                      })
                : [];

            libraryById.set(String(rawSymbol.id || ""), {
                id: String(rawSymbol.id || ""),
                label: asString(rawSymbol.label, rawSymbol.id || "Symbol"),
                category: asString(rawSymbol.category, "uncategorized"),
                svg_url: asString(rawSymbol.svg_url, ""),
                width,
                height,
                ports,
            });
        }
    }

    for (const item of libraryItems) {
        const symbolId = String(item.dataset.symbolId || "");
        item.addEventListener("dragstart", (event) => {
            if (!libraryById.has(symbolId)) {
                return;
            }
            event.dataTransfer.effectAllowed = "copy";
            event.dataTransfer.setData("text/reactor-symbol-id", symbolId);
        });
    }

    for (const button of symbolAddButtons) {
        const symbolId = String(button.dataset.symbolAdd || "");
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            openInstanceModal(symbolId, canvas.clientWidth / 2, canvas.clientHeight / 2);
        });
    }

    const state = {
        currentBuildId: parseBuildId(metaData.currentBuildId),
        currentView: "layout",
        mode: "select",
        nodes: [],
        edges: [],
        persistedSnapshot: "",
        isDirty: false,
        isSaving: false,
        isLoading: false,
        selectedNodeId: null,
        selectedEdgeId: null,
        pendingAnchor: null,
        pendingPlacement: null,
        dragMove: null,
        anchorMove: null,
        edgeSegmentMove: null,
        undoStack: [],
        modalReturnFocus: null,
    };

    function buildDefinitionPayload() {
        return {
            canvas: {
                width: canvas.clientWidth,
                height: canvas.clientHeight,
            },
            nodes: state.nodes.map((node) => ({
                id: node.id,
                symbol_id: node.symbol_id,
                instance_id: node.instance_id,
                label: node.label,
                category: node.category,
                svg_url: node.svg_url,
                x: node.x,
                y: node.y,
                width: node.width,
                height: node.height,
                communication: {
                    device_server_code: node.communication.device_server_code || null,
                    connection_label: node.communication.connection_label || null,
                    protocol: node.communication.protocol || null,
                    notes: node.communication.notes || null,
                },
                anchors: node.anchors.map((anchor) => ({
                    id: anchor.id,
                    x_ratio: anchor.x_ratio,
                    y_ratio: anchor.y_ratio,
                    side: anchor.side,
                })),
            })),
            edges: state.edges.map((edge) => ({
                id: edge.id,
                source_node_id: edge.source_node_id,
                source_anchor_id: edge.source_anchor_id,
                target_node_id: edge.target_node_id,
                target_anchor_id: edge.target_anchor_id,
                route_points: (edge.route_points || []).map((point) => ({
                    x: roundCanvasValue(point.x),
                    y: roundCanvasValue(point.y),
                })),
            })),
        };
    }

    function comparableState() {
        const definition = buildDefinitionPayload();
        return {
            build_name: nameInput.value.trim(),
            build_date: dateInput.value.trim(),
            build_user: userInput.value.trim(),
            definition_json: {
                nodes: definition.nodes,
                edges: definition.edges,
            },
        };
    }

    function capturePersistedSnapshot() {
        state.persistedSnapshot = JSON.stringify(comparableState());
        state.isDirty = false;
        syncUiState();
    }

    function syncDirtyState() {
        state.isDirty = JSON.stringify(comparableState()) !== state.persistedSnapshot;
        syncUiState();
    }

    function syncUiState() {
        const busy = state.isSaving || state.isLoading;
        const saveBlocked = busy || (metaData.apiAuthRequired && !metaData.builderWriteToken);
        const selectionExists = Boolean(state.selectedNodeId || state.selectedEdgeId);

        if (currentBuildElement) {
            currentBuildElement.textContent = state.currentBuildId ? `#${state.currentBuildId}` : "Aktueller Draft";
        }
        if (nodeCountElement) {
            nodeCountElement.textContent = String(state.nodes.length);
        }
        if (edgeCountElement) {
            edgeCountElement.textContent = String(state.edges.length);
        }
        if (saveStateElement) {
            saveStateElement.className = "badge";
            if (state.isSaving) {
                saveStateElement.classList.add("badge-warning");
                saveStateElement.textContent = "Speichert ...";
            } else if (state.isLoading) {
                saveStateElement.classList.add("badge-info");
                saveStateElement.textContent = "Laedt ...";
            } else if (state.isDirty) {
                saveStateElement.classList.add("badge-warning");
                saveStateElement.textContent = "Ungespeichert";
            } else {
                saveStateElement.classList.add("badge-success");
                saveStateElement.textContent = "Gespeichert";
            }
        }

        deleteNodeButton.disabled = busy || !selectionExists;
        saveButton.disabled = saveBlocked;
        saveAsButton.disabled = saveBlocked;
        if (buildSelect) {
            buildSelect.disabled = busy;
        }
        if (newBuildButton) {
            newBuildButton.disabled = busy;
        }

        selectToolButton.setAttribute("aria-pressed", String(state.mode === "select"));
        anchorToolButton.setAttribute("aria-pressed", String(state.mode === "anchor"));
        connectToolButton.setAttribute("aria-pressed", String(state.mode === "connect"));

        document.title = `${state.isDirty ? "* " : ""}Reactor Builder | reactor_ctrl`;
    }

    function confirmDiscardDirtyChanges(actionLabel) {
        if (!state.isDirty) {
            return true;
        }
        return window.confirm(
            `${actionLabel}?\n\nEs gibt ungespeicherte Aenderungen im aktuellen Build. Diese Aenderungen gehen verloren.`,
        );
    }

    function cloneSnapshot() {
        return JSON.parse(
            JSON.stringify({
                nodes: state.nodes,
                edges: state.edges,
            }),
        );
    }

    function pushUndoSnapshot() {
        state.undoStack.push(cloneSnapshot());
        if (state.undoStack.length > 80) {
            state.undoStack.shift();
        }
    }

    function restoreSnapshot(snapshot) {
        if (!snapshot) {
            return;
        }
        state.nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes.map(normalizeNode) : [];
        state.edges = Array.isArray(snapshot.edges) ? snapshot.edges.map((edge) => normalizeEdge(edge, state.nodes)) : [];
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        state.pendingAnchor = null;
        state.anchorMove = null;
        state.edgeSegmentMove = null;
        renderAll();
        setStatus("Undo ausgefuehrt.", "muted");
    }

    function setStatus(message, tone) {
        statusElement.textContent = message;
        statusElement.classList.remove("muted", "error-text", "builder-status-success");
        if (tone === "error") {
            statusElement.classList.add("error-text");
            return;
        }
        if (tone === "success") {
            statusElement.classList.add("builder-status-success");
            return;
        }
        statusElement.classList.add("muted");
    }

    function currentDraftMessage() {
        return state.currentBuildId
            ? `Build #${state.currentBuildId} geladen.`
            : "Neuer Build. Ziehe Elemente aus der Library in die Fliessbildflaeche.";
    }

    function applyLibraryFilter() {
        const query = String(librarySearchInput?.value || "")
            .trim()
            .toLowerCase();
        let visibleItemCount = 0;

        for (const item of libraryItems) {
            const haystack = [
                item.dataset.symbolId || "",
                item.dataset.symbolLabel || "",
                item.dataset.symbolCategory || "",
            ]
                .join(" ")
                .toLowerCase();
            const matches = !query || haystack.includes(query);
            item.hidden = !matches;
            if (matches) {
                visibleItemCount += 1;
            }
        }

        for (const category of libraryCategories) {
            const visibleInCategory = Array.from(category.querySelectorAll(".builder-symbol-item")).some((item) => !item.hidden);
            category.hidden = !visibleInCategory;
            if (query && visibleInCategory) {
                category.open = true;
            }
        }

        if (libraryEmptyState) {
            libraryEmptyState.classList.toggle("is-hidden", !query || visibleItemCount > 0);
        }
        if (librarySearchClearButton) {
            librarySearchClearButton.disabled = !query;
        }
    }

    function fallbackInstanceId(symbolId, nodeId) {
        const base = String(symbolId || "item")
            .replace(/[^a-z0-9]+/gi, "-")
            .replace(/^-+|-+$/g, "")
            .toUpperCase() || "ITEM";
        const tail = String(nodeId || "").replace(/[^a-z0-9]/gi, "").slice(-4).toUpperCase() || "0001";
        return `${base}-${tail}`;
    }

    function normalizeCommunication(communication) {
        const payload = communication && typeof communication === "object" ? communication : {};
        return {
            device_server_code: asString(payload.device_server_code, ""),
            connection_label: asString(payload.connection_label, ""),
            protocol: asString(payload.protocol, ""),
            notes: asString(payload.notes, ""),
        };
    }

    function hasDuplicateInstanceId(candidate, excludeNodeId) {
        const normalized = String(candidate || "").trim().toLowerCase();
        if (!normalized) {
            return false;
        }
        return state.nodes.some(
            (node) => node.id !== excludeNodeId && String(node.instance_id || "").trim().toLowerCase() === normalized,
        );
    }

    function suggestionForSymbol(symbolId) {
        const symbol = libraryById.get(symbolId);
        const base = String(symbol?.id || symbolId || "item")
            .replace(/[^a-z0-9]+/gi, "-")
            .replace(/^-+|-+$/g, "")
            .toUpperCase() || "ITEM";
        let index = 1;
        let candidate = `${base}-${String(index).padStart(2, "0")}`;
        while (hasDuplicateInstanceId(candidate, null)) {
            index += 1;
            candidate = `${base}-${String(index).padStart(2, "0")}`;
        }
        return candidate;
    }

    function symbolAnchors(symbol) {
        if (!symbol || !Array.isArray(symbol.ports)) {
            return [];
        }
        return symbol.ports.map((port) => ({
            id: port.id,
            x_ratio: clamp(asNumber(port.x_ratio, 0.5), 0, 1),
            y_ratio: clamp(asNumber(port.y_ratio, 0.5), 0, 1),
            side: port.side || directionToSide("", port.x_ratio, port.y_ratio),
        }));
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
        const symbol = libraryById.get(String(node?.symbol_id || ""));
        const width = Math.max(40, asNumber(node?.width, symbol?.width || 120));
        const height = Math.max(40, asNumber(node?.height, symbol?.height || 80));
        const anchors = Array.isArray(node?.anchors) && node.anchors.length > 0
            ? node.anchors.map(normalizeAnchor)
            : symbolAnchors(symbol);
        const nodeId = asString(node?.id, generateId("node"));
        const instanceId = asString(
            node?.instance_id,
            node?.label && node?.label !== symbol?.label ? node.label : fallbackInstanceId(symbol?.id, nodeId),
        );

        return {
            id: nodeId,
            symbol_id: asString(node?.symbol_id, symbol?.id || ""),
            instance_id: instanceId,
            label: asString(node?.label, symbol?.label || node?.symbol_id || "Symbol"),
            category: asString(node?.category, symbol?.category || ""),
            svg_url: asString(node?.svg_url, symbol?.svg_url || ""),
            x: asNumber(node?.x, 0),
            y: asNumber(node?.y, 0),
            width,
            height,
            communication: normalizeCommunication(node?.communication),
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
            id: asString(edge?.id, generateId("edge")),
            source_node_id: asString(edge?.source_node_id, ""),
            source_anchor_id: sourceAnchor ? sourceAnchor.id : null,
            target_node_id: asString(edge?.target_node_id, ""),
            target_anchor_id: targetAnchor ? targetAnchor.id : null,
            route_points: Array.isArray(edge?.route_points)
                ? edge.route_points
                      .filter((point) => point && typeof point === "object")
                      .map((point) => ({
                          x: Math.round(asNumber(point.x, 0) * 100) / 100,
                          y: Math.round(asNumber(point.y, 0) * 100) / 100,
                      }))
                : [],
        };
    }

    function loadDefinition(definition) {
        const rawNodes = Array.isArray(definition?.nodes) ? definition.nodes : [];
        state.nodes = rawNodes.map(normalizeNode);
        const rawEdges = Array.isArray(definition?.edges) ? definition.edges : [];
        state.edges = rawEdges.map((edge) => normalizeEdge(edge, state.nodes));
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        state.pendingAnchor = null;
        state.anchorMove = null;
        state.edgeSegmentMove = null;
    }

    function updateHistory(buildId) {
        const nextUrl = new URL(window.location.href);
        if (buildId) {
            nextUrl.searchParams.set("build_id", String(buildId));
        } else {
            nextUrl.searchParams.delete("build_id");
        }
        window.history.replaceState({}, "", nextUrl.toString());
    }

    function setBuildSelection(buildId) {
        if (!buildSelect) {
            return;
        }
        buildSelect.value = buildId ? String(buildId) : "";
    }

    function formatBuildOption(build) {
        return `${build.build_name} | ${build.build_date || ""} | ${build.updated_by || build.created_by || ""}`;
    }

    function upsertBuildOption(build) {
        if (!buildSelect || !build?.reactor_build_id) {
            return;
        }

        let option = Array.from(buildSelect.options).find(
            (candidate) => candidate.value === String(build.reactor_build_id),
        );
        if (!option) {
            option = document.createElement("option");
            option.value = String(build.reactor_build_id);
            const placeholder = buildSelect.querySelector('option[value=""]');
            if (placeholder && placeholder.nextSibling) {
                buildSelect.insertBefore(option, placeholder.nextSibling);
            } else {
                buildSelect.appendChild(option);
            }
        }
        option.textContent = formatBuildOption(build);
        setBuildSelection(build.reactor_build_id);
    }

    function applyBuildRecord(build, options) {
        const clearUndo = Boolean(options?.clearUndo);
        state.currentBuildId = build?.reactor_build_id || null;
        nameInput.value = asString(build?.build_name, "Untitled Reactor Build");
        dateInput.value = asString(build?.build_date, new Date().toISOString().slice(0, 10));
        userInput.value = asString(build?.updated_by || build?.created_by, userInput.value || "operator");
        loadDefinition(build?.definition_json || {});
        if (clearUndo) {
            state.undoStack = [];
        }
        if (state.currentBuildId) {
            upsertBuildOption(build);
        } else {
            setBuildSelection(null);
        }
        updateHistory(state.currentBuildId);
        renderAll();
        capturePersistedSnapshot();
    }

    function resetDraft() {
        state.currentBuildId = null;
        state.nodes = [];
        state.edges = [];
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        state.pendingAnchor = null;
        state.anchorMove = null;
        state.edgeSegmentMove = null;
        state.undoStack = [];
        nameInput.value = "Untitled Reactor Build";
        if (!dateInput.value) {
            dateInput.value = new Date().toISOString().slice(0, 10);
        }
        if (!userInput.value.trim()) {
            userInput.value = "operator";
        }
        setBuildSelection(null);
        updateHistory(null);
        renderAll();
        capturePersistedSnapshot();
        setStatus("Neuer Draft aktiv. Bestehende Builds bleiben in der Datenbank gespeichert.", "muted");
    }

    if (buildData && typeof buildData === "object") {
        applyBuildRecord(buildData, { clearUndo: true });
    }

    function pointerToCanvas(event) {
        const rect = canvas.getBoundingClientRect();
        return {
            x: event.clientX - rect.left,
            y: event.clientY - rect.top,
        };
    }

    function roundRatio(value) {
        return Math.round(clamp(value, 0, 1) * 1000000) / 1000000;
    }

    function roundCanvasValue(value) {
        return Math.round(value * 100) / 100;
    }

    function clampNode(node) {
        const padding = 14;
        const maxX = Math.max(padding, canvas.clientWidth - node.width - padding);
        const maxY = Math.max(padding, canvas.clientHeight - node.height - padding);
        node.x = clamp(node.x, padding, maxX);
        node.y = clamp(node.y, padding, maxY);
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
                x: Math.round(point.x * 100) / 100,
                y: Math.round(point.y * 100) / 100,
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
            const middleX = Math.round(((sourceStub.x + targetStub.x) / 2) * 100) / 100;
            points.push({ x: middleX, y: sourceStub.y });
            points.push({ x: middleX, y: targetStub.y });
        } else if (!sourceHorizontal && !targetHorizontal) {
            const middleY = Math.round(((sourceStub.y + targetStub.y) / 2) * 100) / 100;
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
                x: roundCanvasValue(asNumber(point.x, 0)),
                y: roundCanvasValue(asNumber(point.y, 0)),
            }));
        }

        const autoPoints = buildAutoEdgePoints(sourcePoint, sourceSide, targetPoint, targetSide);
        return autoPoints.slice(1, -1);
    }

    function edgePolylinePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide) {
        return [sourcePoint, ...edgeRoutePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide), targetPoint];
    }

    function edgePathFromPoints(points) {
        return points
            .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
            .join(" ");
    }

    function edgeRoutePointsFromPolyline(points) {
        return compressOrthogonalPoints(points)
            .slice(1, -1)
            .map((point) => ({
                x: roundCanvasValue(point.x),
                y: roundCanvasValue(point.y),
            }));
    }

    function edgeExists(connection) {
        return state.edges.some((edge) => {
            const forward =
                edge.source_node_id === connection.source_node_id &&
                edge.source_anchor_id === connection.source_anchor_id &&
                edge.target_node_id === connection.target_node_id &&
                edge.target_anchor_id === connection.target_anchor_id;
            const reverse =
                edge.source_node_id === connection.target_node_id &&
                edge.source_anchor_id === connection.target_anchor_id &&
                edge.target_node_id === connection.source_node_id &&
                edge.target_anchor_id === connection.source_anchor_id;
            return forward || reverse;
        });
    }

    function updateEmptyState() {
        emptyState.classList.toggle("is-hidden", state.nodes.length > 0);
    }

    function renderCommunicationTable() {
        if (!communicationBody) {
            return;
        }

        communicationBody.innerHTML = "";
        if (state.nodes.length === 0) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");
            cell.colSpan = 6;
            cell.className = "muted";
            cell.textContent = "Noch keine Elemente im aktuellen Build.";
            row.appendChild(cell);
            communicationBody.appendChild(row);
            return;
        }

        const orderedNodes = [...state.nodes].sort((left, right) =>
            String(left.instance_id || "").localeCompare(String(right.instance_id || "")),
        );

        for (const node of orderedNodes) {
            const row = document.createElement("tr");

            const instanceCell = document.createElement("td");
            const instanceInput = document.createElement("input");
            instanceInput.type = "text";
            instanceInput.value = node.instance_id;
            instanceInput.maxLength = 80;
            instanceInput.addEventListener("change", () => {
                let nextValue;
                try {
                    nextValue = validateInstanceIdInput(instanceInput.value);
                } catch (error) {
                    instanceInput.value = node.instance_id;
                    setStatus(error.message || "Element ID ist ungueltig.", "error");
                    return;
                }
                if (hasDuplicateInstanceId(nextValue, node.id)) {
                    instanceInput.value = node.instance_id;
                    setStatus(`Element ID ${nextValue} existiert bereits im Build.`, "error");
                    return;
                }
                if (nextValue === node.instance_id) {
                    instanceInput.value = nextValue;
                    return;
                }
                pushUndoSnapshot();
                node.instance_id = nextValue;
                instanceInput.value = nextValue;
                renderAll();
                setStatus("Element ID aktualisiert. Build noch nicht gespeichert.", "muted");
            });
            instanceCell.appendChild(instanceInput);
            row.appendChild(instanceCell);

            const typeCell = document.createElement("td");
            typeCell.textContent = node.symbol_id;
            row.appendChild(typeCell);

            const communicationFields = [
                { key: "device_server_code", placeholder: "MOXA-01" },
                { key: "connection_label", placeholder: "Port 3 / COM" },
                { key: "protocol", placeholder: "RS-232 / ASCII" },
                { key: "notes", placeholder: "optional" },
            ];

            for (const field of communicationFields) {
                const cell = document.createElement("td");
                const input = document.createElement("input");
                input.type = "text";
                input.value = node.communication[field.key] || "";
                input.placeholder = field.placeholder;
                input.addEventListener("change", () => {
                    const nextValue = input.value.trim();
                    if (nextValue === (node.communication[field.key] || "")) {
                        return;
                    }
                    pushUndoSnapshot();
                    node.communication[field.key] = nextValue;
                    input.value = nextValue;
                    syncDirtyState();
                    setStatus("Communication Mapping aktualisiert. Build noch nicht gespeichert.", "muted");
                });
                cell.appendChild(input);
                row.appendChild(cell);
            }

            communicationBody.appendChild(row);
        }
    }

    function renderEdges() {
        while (edgeLayer.firstChild) {
            edgeLayer.removeChild(edgeLayer.firstChild);
        }

        edgeLayer.setAttribute("viewBox", `0 0 ${canvas.clientWidth} ${canvas.clientHeight}`);

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
            path.setAttribute("class", `builder-edge${state.selectedEdgeId === edge.id ? " is-selected" : ""}`);
            path.dataset.edgeId = edge.id;
            path.addEventListener("click", (event) => {
                event.stopPropagation();
                state.selectedEdgeId = edge.id;
                state.selectedNodeId = null;
                renderAll();
            });
            edgeLayer.appendChild(path);

            if (state.mode === "connect" && state.selectedEdgeId === edge.id) {
                for (let segmentIndex = 1; segmentIndex < polylinePoints.length - 2; segmentIndex += 1) {
                    const segmentStart = polylinePoints[segmentIndex];
                    const segmentEnd = polylinePoints[segmentIndex + 1];
                    const segment = document.createElementNS("http://www.w3.org/2000/svg", "line");
                    const isVertical = Math.abs(segmentStart.x - segmentEnd.x) < 0.01;
                    const active =
                        state.edgeSegmentMove?.edgeId === edge.id && state.edgeSegmentMove?.segmentIndex === segmentIndex;
                    segment.setAttribute("x1", String(segmentStart.x));
                    segment.setAttribute("y1", String(segmentStart.y));
                    segment.setAttribute("x2", String(segmentEnd.x));
                    segment.setAttribute("y2", String(segmentEnd.y));
                    segment.setAttribute(
                        "class",
                        `builder-edge-segment-hit${active ? " is-active" : ""}${isVertical ? " is-vertical" : " is-horizontal"}`,
                    );
                    segment.dataset.edgeId = edge.id;
                    segment.dataset.segmentIndex = String(segmentIndex);
                    segment.addEventListener("pointerdown", (event) => {
                        startEdgeSegmentMove(edge.id, segmentIndex, event);
                    });
                    edgeLayer.appendChild(segment);
                }
            }
        }

        if (state.pendingAnchor) {
            const sourceNode = getNodeById(state.pendingAnchor.nodeId);
            if (sourceNode) {
                const point = anchorPoint(sourceNode, state.pendingAnchor.anchorId);
                const marker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
                marker.setAttribute("cx", String(point.x));
                marker.setAttribute("cy", String(point.y));
                marker.setAttribute("r", "7");
                marker.setAttribute("class", "builder-edge is-pending");
                edgeLayer.appendChild(marker);
            }
        }
    }

    function shouldShowAnchors(node) {
        return (
            Array.isArray(node.anchors) &&
            node.anchors.length > 0 &&
            (state.mode === "anchor" || state.mode === "connect" || state.selectedNodeId === node.id)
        );
    }

    function snapAnchorToOuterSide(localX, localY, width, height) {
        const x = clamp(localX, 0, width);
        const y = clamp(localY, 0, height);
        const distances = [
            { side: "west", distance: Math.abs(x), x_ratio: 0, y_ratio: clamp(y / height, 0, 1) },
            { side: "east", distance: Math.abs(width - x), x_ratio: 1, y_ratio: clamp(y / height, 0, 1) },
            { side: "north", distance: Math.abs(y), x_ratio: clamp(x / width, 0, 1), y_ratio: 0 },
            { side: "south", distance: Math.abs(height - y), x_ratio: clamp(x / width, 0, 1), y_ratio: 1 },
        ];

        distances.sort((left, right) => left.distance - right.distance);
        return distances[0];
    }

    function updateAnchorButtonPosition(button, anchor) {
        if (!button || !anchor) {
            return;
        }
        button.style.left = `${anchor.x_ratio * 100}%`;
        button.style.top = `${anchor.y_ratio * 100}%`;
    }

    function updateAnchorPosition(nodeId, anchorId, clientX, clientY) {
        const node = getNodeById(nodeId);
        const nodeElement =
            nodeLayer.querySelector(`.builder-node[data-node-id="${nodeId}"]`);
        if (!node || !nodeElement) {
            return null;
        }

        const rect = nodeElement.getBoundingClientRect();
        const snapped = snapAnchorToOuterSide(clientX - rect.left, clientY - rect.top, node.width, node.height);
        const anchor = getAnchorById(node, anchorId);
        if (!anchor) {
            return null;
        }

        anchor.x_ratio = roundRatio(snapped.x_ratio);
        anchor.y_ratio = roundRatio(snapped.y_ratio);
        anchor.side = snapped.side;
        return anchor;
    }

    function stopAnchorPointerMove() {
        window.removeEventListener("pointermove", handleAnchorPointerMove);
        window.removeEventListener("pointerup", stopAnchorPointerMove);
        window.removeEventListener("pointercancel", stopAnchorPointerMove);

        const activeMove = state.anchorMove;
        if (!activeMove) {
            return;
        }

        if (activeMove.button && activeMove.button.isConnected) {
            activeMove.button.classList.remove("is-moving");
        }

        const didMove = activeMove.moved;
        state.anchorMove = null;
        renderAll();
        if (didMove) {
            setStatus("Anchor versetzt. Verbundene Leitungen wurden aktualisiert.", "success");
        }
    }

    function handleAnchorPointerMove(event) {
        const activeMove = state.anchorMove;
        if (!activeMove) {
            return;
        }

        if (!activeMove.snapshotTaken) {
            pushUndoSnapshot();
            activeMove.snapshotTaken = true;
        }

        const anchor = updateAnchorPosition(activeMove.nodeId, activeMove.anchorId, event.clientX, event.clientY);
        if (!anchor) {
            return;
        }

        activeMove.moved = true;
        const liveButton =
            nodeLayer.querySelector(
                `.builder-node[data-node-id="${activeMove.nodeId}"] .builder-anchor[data-anchor-id="${activeMove.anchorId}"]`,
            ) || activeMove.button;
        if (liveButton) {
            liveButton.classList.add("is-moving");
            updateAnchorButtonPosition(liveButton, anchor);
            activeMove.button = liveButton;
        }
        renderEdges();
    }

    function ensureEdgeRoutePoints(edgeId) {
        const edge = state.edges.find((item) => item.id === edgeId) || null;
        if (!edge) {
            return null;
        }
        if (Array.isArray(edge.route_points) && edge.route_points.length > 0) {
            return edge;
        }

        const sourceNode = getNodeById(edge.source_node_id);
        const targetNode = getNodeById(edge.target_node_id);
        if (!sourceNode || !targetNode) {
            return edge;
        }

        const sourcePoint = anchorPoint(sourceNode, edge.source_anchor_id);
        const sourceSide = anchorSide(sourceNode, edge.source_anchor_id);
        const targetPoint = anchorPoint(targetNode, edge.target_anchor_id);
        const targetSide = anchorSide(targetNode, edge.target_anchor_id);
        edge.route_points = edgeRoutePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide).map((point) => ({
            x: roundCanvasValue(point.x),
            y: roundCanvasValue(point.y),
        }));
        return edge;
    }

    function stopEdgeSegmentMove() {
        window.removeEventListener("pointermove", handleEdgeSegmentMove);
        window.removeEventListener("pointerup", stopEdgeSegmentMove);
        window.removeEventListener("pointercancel", stopEdgeSegmentMove);

        const activeMove = state.edgeSegmentMove;
        if (!activeMove) {
            return;
        }

        const didMove = activeMove.moved;
        state.edgeSegmentMove = null;
        renderAll();
        if (didMove) {
            setStatus("Leitungsverlauf aktualisiert. Build noch nicht gespeichert.", "success");
        }
    }

    function handleEdgeSegmentMove(event) {
        const activeMove = state.edgeSegmentMove;
        if (!activeMove) {
            return;
        }

        if (!activeMove.snapshotTaken) {
            pushUndoSnapshot();
            activeMove.snapshotTaken = true;
        }

        const edge = ensureEdgeRoutePoints(activeMove.edgeId);
        if (!edge) {
            return;
        }

        const baseStart = activeMove.basePoints[activeMove.segmentIndex];
        const baseEnd = activeMove.basePoints[activeMove.segmentIndex + 1];
        if (!baseStart || !baseEnd) {
            return;
        }

        const pointer = pointerToCanvas(event);
        const nextPoints = activeMove.basePoints.map((point) => ({ x: point.x, y: point.y }));
        if (activeMove.isVertical) {
            const nextX = roundCanvasValue(activeMove.baseCoordinate + (pointer.x - activeMove.startPointer.x));
            nextPoints[activeMove.segmentIndex].x = nextX;
            nextPoints[activeMove.segmentIndex + 1].x = nextX;
        } else {
            const nextY = roundCanvasValue(activeMove.baseCoordinate + (pointer.y - activeMove.startPointer.y));
            nextPoints[activeMove.segmentIndex].y = nextY;
            nextPoints[activeMove.segmentIndex + 1].y = nextY;
        }

        edge.route_points = edgeRoutePointsFromPolyline(nextPoints);
        activeMove.moved = true;
        renderEdges();
    }

    function startEdgeSegmentMove(edgeId, segmentIndex, event) {
        if (state.mode !== "connect" || event.button !== 0) {
            return;
        }

        const edge = ensureEdgeRoutePoints(edgeId);
        if (!edge) {
            return;
        }

        const sourceNode = getNodeById(edge.source_node_id);
        const targetNode = getNodeById(edge.target_node_id);
        if (!sourceNode || !targetNode) {
            return;
        }

        const sourcePoint = anchorPoint(sourceNode, edge.source_anchor_id);
        const sourceSide = anchorSide(sourceNode, edge.source_anchor_id);
        const targetPoint = anchorPoint(targetNode, edge.target_anchor_id);
        const targetSide = anchorSide(targetNode, edge.target_anchor_id);
        const polylinePoints = edgePolylinePoints(edge, sourcePoint, sourceSide, targetPoint, targetSide);
        const segmentStart = polylinePoints[segmentIndex];
        const segmentEnd = polylinePoints[segmentIndex + 1];
        if (!segmentStart || !segmentEnd) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        const pointer = pointerToCanvas(event);
        const isVertical = Math.abs(segmentStart.x - segmentEnd.x) < 0.01;
        state.selectedEdgeId = edgeId;
        state.selectedNodeId = null;
        state.edgeSegmentMove = {
            edgeId,
            segmentIndex,
            basePoints: polylinePoints.map((point) => ({ x: point.x, y: point.y })),
            baseCoordinate: isVertical ? segmentStart.x : segmentStart.y,
            startPointer: pointer,
            isVertical,
            moved: false,
            snapshotTaken: false,
        };
        renderEdges();

        window.addEventListener("pointermove", handleEdgeSegmentMove);
        window.addEventListener("pointerup", stopEdgeSegmentMove);
        window.addEventListener("pointercancel", stopEdgeSegmentMove);
    }

    function startAnchorMove(nodeId, anchorId, event) {
        if (event.button !== 0) {
            return;
        }

        const anchor = getAnchorById(getNodeById(nodeId), anchorId);
        if (!anchor) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        state.selectedNodeId = nodeId;
        state.selectedEdgeId = null;
        state.pendingAnchor = null;

        state.anchorMove = {
            nodeId,
            anchorId,
            button: event.currentTarget,
            moved: false,
            snapshotTaken: false,
        };
        event.currentTarget.classList.add("is-moving");
        renderEdges();

        window.addEventListener("pointermove", handleAnchorPointerMove);
        window.addEventListener("pointerup", stopAnchorPointerMove);
        window.addEventListener("pointercancel", stopAnchorPointerMove);
    }

    function selectNode(nodeId) {
        state.selectedNodeId = nodeId;
        state.selectedEdgeId = null;
        renderAll();
    }

    function addManualAnchor(nodeId, event) {
        const node = getNodeById(nodeId);
        const body = event.currentTarget;
        if (!node || !body) {
            return;
        }

        const rect = body.getBoundingClientRect();
        const localX = event.clientX - rect.left;
        const localY = event.clientY - rect.top;
        const snapped = snapAnchorToOuterSide(localX, localY, node.width, node.height);
        const duplicate = node.anchors.find(
            (anchor) =>
                Math.abs(anchor.x_ratio - snapped.x_ratio) < 0.015 &&
                Math.abs(anchor.y_ratio - snapped.y_ratio) < 0.015,
        );
        if (duplicate) {
            state.selectedNodeId = nodeId;
            state.selectedEdgeId = null;
            renderAll();
            setStatus("An dieser Position existiert bereits ein Anchor.", "muted");
            return;
        }

        pushUndoSnapshot();
        node.anchors.push({
            id: generateId("anchor"),
            x_ratio: roundRatio(snapped.x_ratio),
            y_ratio: roundRatio(snapped.y_ratio),
            side: snapped.side,
        });
        state.selectedNodeId = nodeId;
        state.selectedEdgeId = null;
        renderAll();
        setStatus("Anchor angelegt. Das Connection Tool snappt auf diese Punkte.", "success");
    }

    function renderNodes() {
        nodeLayer.innerHTML = "";

        for (const node of state.nodes) {
            clampNode(node);

            const element = document.createElement("article");
            element.className = "builder-node";
            if (state.selectedNodeId === node.id) {
                element.classList.add("is-selected");
            }
            if (state.pendingAnchor && state.pendingAnchor.nodeId === node.id) {
                element.classList.add("is-connect-source");
            }
            element.dataset.nodeId = node.id;
            element.style.left = `${node.x}px`;
            element.style.top = `${node.y}px`;
            element.style.width = `${node.width}px`;
            element.style.height = `${node.height}px`;

            const body = document.createElement("div");
            body.className = "builder-node-body";
            body.addEventListener("click", (event) => {
                event.stopPropagation();
                if (state.mode === "anchor") {
                    addManualAnchor(node.id, event);
                    return;
                }
                if (state.mode === "connect") {
                    selectNode(node.id);
                    if (node.anchors.length === 0) {
                        setStatus("Dieses Element hat noch keine Anchorpunkte. Nutze zuerst das Anchor Tool.", "error");
                    } else {
                        setStatus("Klicke einen sichtbaren Anchorpunkt, um eine Verbindung zu starten oder zu beenden.", "muted");
                    }
                    return;
                }
                selectNode(node.id);
            });

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
            instance.textContent = node.instance_id;
            const type = document.createElement("span");
            type.className = "builder-node-type";
            type.textContent = node.symbol_id;
            label.appendChild(instance);
            label.appendChild(type);

            body.appendChild(graphic);
            body.appendChild(label);
            element.appendChild(body);

            if (shouldShowAnchors(node)) {
                const anchorLayer = document.createElement("div");
                anchorLayer.className = "builder-node-anchor-layer";
                for (const anchor of node.anchors) {
                    const anchorButton = document.createElement("button");
                    anchorButton.type = "button";
                    anchorButton.className = "builder-anchor";
                    if (state.mode === "anchor") {
                        anchorButton.classList.add("is-editable");
                    }
                    if (
                        state.pendingAnchor &&
                        state.pendingAnchor.nodeId === node.id &&
                        state.pendingAnchor.anchorId === anchor.id
                    ) {
                        anchorButton.classList.add("is-pending");
                    }
                    anchorButton.dataset.anchorId = anchor.id;
                    anchorButton.style.left = `${anchor.x_ratio * 100}%`;
                    anchorButton.style.top = `${anchor.y_ratio * 100}%`;
                    anchorButton.setAttribute("aria-label", `${node.label} ${anchor.id}`);
                    anchorButton.addEventListener("pointerdown", (event) => {
                        if (state.mode !== "anchor") {
                            return;
                        }
                        startAnchorMove(node.id, anchor.id, event);
                    });
                    anchorButton.addEventListener("click", (event) => {
                        event.stopPropagation();
                        if (state.mode === "anchor") {
                            selectNode(node.id);
                            return;
                        }
                        if (state.mode !== "connect") {
                            selectNode(node.id);
                            return;
                        }

                        if (!state.pendingAnchor) {
                            state.pendingAnchor = { nodeId: node.id, anchorId: anchor.id };
                            state.selectedNodeId = node.id;
                            state.selectedEdgeId = null;
                            renderAll();
                            setStatus("Quell-Anchor gewaehlt. Jetzt den Ziel-Anchor anklicken.", "muted");
                            return;
                        }

                        if (state.pendingAnchor.nodeId === node.id && state.pendingAnchor.anchorId === anchor.id) {
                            state.pendingAnchor = null;
                            renderAll();
                            setStatus("Connection Tool zurueckgesetzt.", "muted");
                            return;
                        }

                        if (state.pendingAnchor.nodeId === node.id) {
                            state.pendingAnchor = { nodeId: node.id, anchorId: anchor.id };
                            renderAll();
                            setStatus("Quell-Anchor geaendert. Jetzt Ziel-Anchor anklicken.", "muted");
                            return;
                        }

                        const nextEdge = {
                            id: generateId("edge"),
                            source_node_id: state.pendingAnchor.nodeId,
                            source_anchor_id: state.pendingAnchor.anchorId,
                            target_node_id: node.id,
                            target_anchor_id: anchor.id,
                        };

                        if (edgeExists(nextEdge)) {
                            state.pendingAnchor = null;
                            renderAll();
                            setStatus("Zwischen diesen Anchors existiert bereits eine Verbindung.", "error");
                            return;
                        }

                        pushUndoSnapshot();
                        state.edges.push(normalizeEdge(nextEdge, state.nodes));
                        state.pendingAnchor = null;
                        state.selectedEdgeId = nextEdge.id;
                        state.selectedNodeId = null;
                        renderAll();
                        setStatus("Verbindung erstellt und auf Anchorpunkte gesnappt.", "success");
                    });
                    anchorLayer.appendChild(anchorButton);
                }
                element.appendChild(anchorLayer);
            }

            element.addEventListener("pointerdown", (event) => {
                if (state.mode !== "select" || event.button !== 0) {
                    return;
                }
                if (event.target instanceof Element && event.target.closest(".builder-anchor")) {
                    return;
                }

                const point = pointerToCanvas(event);
                state.selectedNodeId = node.id;
                state.selectedEdgeId = null;
                renderAll();
                const liveElement = nodeLayer.querySelector(`[data-node-id="${node.id}"]`) || element;
                state.dragMove = {
                    nodeId: node.id,
                    offsetX: point.x - node.x,
                    offsetY: point.y - node.y,
                    element: liveElement,
                    moved: false,
                    snapshotTaken: false,
                };
                liveElement.classList.add("is-dragging");
                window.addEventListener("pointermove", handlePointerMove);
                window.addEventListener("pointerup", stopPointerMove, { once: true });
            });

            nodeLayer.appendChild(element);
        }
    }

    function renderAll() {
        state.nodes.forEach(clampNode);
        renderEdges();
        renderNodes();
        updateEmptyState();
        renderCommunicationTable();
        syncDirtyState();
    }

    function handlePointerMove(event) {
        if (!state.dragMove) {
            return;
        }
        if (!state.dragMove.snapshotTaken) {
            pushUndoSnapshot();
            state.dragMove.snapshotTaken = true;
        }
        const node = getNodeById(state.dragMove.nodeId);
        if (!node) {
            return;
        }
        const point = pointerToCanvas(event);
        node.x = point.x - state.dragMove.offsetX;
        node.y = point.y - state.dragMove.offsetY;
        clampNode(node);

        const liveElement = nodeLayer.querySelector(`[data-node-id="${node.id}"]`) || state.dragMove.element;
        liveElement.style.left = `${node.x}px`;
        liveElement.style.top = `${node.y}px`;
        state.dragMove.element = liveElement;
        state.dragMove.moved = true;
        renderEdges();
    }

    function stopPointerMove() {
        window.removeEventListener("pointermove", handlePointerMove);
        if (state.dragMove?.element) {
            state.dragMove.element.classList.remove("is-dragging");
        }
        const didMove = Boolean(state.dragMove?.moved);
        state.dragMove = null;
        renderAll();
        if (didMove) {
            setStatus("Element verschoben. Build noch nicht gespeichert.", "muted");
        }
    }

    function removeNode(nodeId) {
        pushUndoSnapshot();
        state.nodes = state.nodes.filter((node) => node.id !== nodeId);
        state.edges = state.edges.filter(
            (edge) => edge.source_node_id !== nodeId && edge.target_node_id !== nodeId,
        );
        state.selectedNodeId = null;
        if (state.pendingAnchor?.nodeId === nodeId) {
            state.pendingAnchor = null;
        }
        renderAll();
        setStatus("Element entfernt. Build noch nicht gespeichert.", "muted");
    }

    function removeEdge(edgeId) {
        pushUndoSnapshot();
        state.edges = state.edges.filter((edge) => edge.id !== edgeId);
        state.selectedEdgeId = null;
        renderAll();
        setStatus("Verbindung entfernt. Build noch nicht gespeichert.", "muted");
    }

    function setMode(mode) {
        state.mode = mode === "anchor" || mode === "connect" ? mode : "select";
        if (state.mode !== "connect") {
            state.pendingAnchor = null;
        }
        if (state.mode !== "anchor" && state.anchorMove) {
            stopAnchorPointerMove();
        }
        if (state.mode !== "connect" && state.edgeSegmentMove) {
            stopEdgeSegmentMove();
        }
        selectToolButton.classList.toggle("is-active", state.mode === "select");
        anchorToolButton.classList.toggle("is-active", state.mode === "anchor");
        connectToolButton.classList.toggle("is-active", state.mode === "connect");
        renderAll();

        if (state.mode === "anchor") {
            setStatus("Anchor Tool aktiv. Klicke fuer neue Anchors oder ziehe bestehende Anchors am Rand entlang.", "muted");
            return;
        }
        if (state.mode === "connect") {
            setStatus("Connection Tool aktiv. Verbindungen an Anchors erstellen und Leitungssegmente direkt verschieben.", "muted");
            return;
        }
        setStatus(currentDraftMessage(), "muted");
    }

    function setView(viewName) {
        state.currentView = viewName === "communication" ? "communication" : "layout";
        layoutView.classList.toggle("is-hidden", state.currentView !== "layout");
        communicationView.classList.toggle("is-hidden", state.currentView !== "communication");
        layoutViewTabButton.classList.toggle("btn-primary", state.currentView === "layout");
        communicationViewTabButton.classList.toggle("btn-primary", state.currentView === "communication");
    }

    function closeInstanceModal() {
        state.pendingPlacement = null;
        instanceIdInput.value = "";
        instanceModal.classList.add("is-hidden");
        if (state.modalReturnFocus && typeof state.modalReturnFocus.focus === "function") {
            state.modalReturnFocus.focus();
        }
        state.modalReturnFocus = null;
    }

    function openInstanceModal(symbolId, x, y) {
        const symbol = libraryById.get(symbolId);
        if (!symbol) {
            setStatus(`Symbol ${symbolId} ist nicht in der Library registriert.`, "error");
            return;
        }
        state.modalReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        state.pendingPlacement = { symbolId, x, y };
        instanceModalCopy.textContent = `Vergib eine eindeutige ID für ${symbol.label}. Diese ID wird im Canvas angezeigt und für die Kommunikationszuordnung verwendet.`;
        instanceIdInput.value = suggestionForSymbol(symbolId);
        instanceModal.classList.remove("is-hidden");
        window.setTimeout(() => {
            instanceIdInput.focus();
            instanceIdInput.select();
        }, 0);
    }

    function confirmInstancePlacement() {
        if (!state.pendingPlacement) {
            closeInstanceModal();
            return;
        }

        let instanceId;
        try {
            instanceId = validateInstanceIdInput(instanceIdInput.value);
        } catch (error) {
            setStatus(error.message || "Element ID ist ungueltig.", "error");
            instanceIdInput.focus();
            instanceIdInput.select();
            return;
        }
        if (hasDuplicateInstanceId(instanceId, null)) {
            setStatus(`Element ID ${instanceId} existiert bereits im Build.`, "error");
            instanceIdInput.focus();
            instanceIdInput.select();
            return;
        }

        const placement = state.pendingPlacement;
        closeInstanceModal();
        addNodeFromSymbol(placement.symbolId, placement.x, placement.y, instanceId);
    }

    function addNodeFromSymbol(symbolId, x, y, instanceId) {
        const symbol = libraryById.get(symbolId);
        if (!symbol) {
            setStatus(`Symbol ${symbolId} ist nicht in der Library registriert.`, "error");
            return;
        }
        let safeInstanceId;
        try {
            safeInstanceId = validateInstanceIdInput(instanceId);
        } catch (error) {
            setStatus(error.message || "Element ID ist ungueltig.", "error");
            return;
        }
        if (hasDuplicateInstanceId(safeInstanceId, null)) {
            setStatus(`Element ID ${safeInstanceId} existiert bereits im Build.`, "error");
            return;
        }

        pushUndoSnapshot();
        const node = normalizeNode({
            id: generateId("node"),
            symbol_id: symbol.id,
            instance_id: safeInstanceId,
            label: symbol.label,
            category: symbol.category,
            svg_url: symbol.svg_url,
            width: symbol.width,
            height: symbol.height,
            x: x - symbol.width / 2,
            y: y - symbol.height / 2,
            communication: {},
            anchors: symbolAnchors(symbol),
        });
        clampNode(node);
        state.nodes.push(node);
        state.selectedNodeId = node.id;
        state.selectedEdgeId = null;
        renderAll();
        setStatus(`${symbol.label} als ${safeInstanceId} auf dem Canvas platziert.`, "muted");
    }

    function buildPayload(isCreate) {
        const buildName = nameInput.value.trim();
        const buildDate = dateInput.value.trim();
        const buildUser = userInput.value.trim();

        if (!buildName) {
            throw new Error("Build Name darf nicht leer sein.");
        }
        if (!buildDate) {
            throw new Error("Date muss gesetzt sein.");
        }
        if (!buildUser) {
            throw new Error("User darf nicht leer sein.");
        }

        const payload = {
            build_name: buildName,
            build_date: buildDate,
            updated_by: buildUser,
            definition_json: buildDefinitionPayload(),
        };
        if (isCreate) {
            payload.created_by = buildUser;
        }
        return payload;
    }

    async function fetchJson(url, options) {
        const timeoutMs = asNumber(options?.timeoutMs, 20000);
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeoutMs);

        try {
            const response = await window.fetch(url, {
                ...(options || {}),
                headers: {
                    Accept: "application/json",
                    ...((options && options.headers) || {}),
                },
                cache: "no-store",
                credentials: "same-origin",
                signal: controller.signal,
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                const message = [payload.error || "Request fehlgeschlagen.", payload.details || ""]
                    .filter(Boolean)
                    .join(" ");
                throw new Error(message);
            }
            return payload;
        } catch (error) {
            if (error && error.name === "AbortError") {
                throw new Error("Request Timeout. Bitte Verbindung und Serverstatus pruefen.");
            }
            throw error;
        } finally {
            window.clearTimeout(timer);
        }
    }

    async function saveBuild(forceCreate) {
        if (state.isSaving || state.isLoading) {
            return;
        }
        if (metaData.apiAuthRequired && !metaData.builderWriteToken) {
            setStatus("Der Server liefert keinen Builder-Token. Speichern ist derzeit nicht verfuegbar.", "error");
            return;
        }

        const isCreate = forceCreate || !state.currentBuildId;
        let payload;
        try {
            payload = buildPayload(isCreate);
        } catch (error) {
            setStatus(error.message || "Build-Daten sind ungueltig.", "error");
            return;
        }

        const url = isCreate ? "/api/reactor-builds" : `/api/reactor-builds/${state.currentBuildId}`;
        const method = isCreate ? "POST" : "PATCH";
        const headers = {
            "Content-Type": "application/json",
        };

        if (metaData.builderWriteToken) {
            headers["X-Reactor-Builder-Token"] = metaData.builderWriteToken;
        }

        state.isSaving = true;
        syncUiState();
        setStatus("Build wird gespeichert ...", "muted");

        try {
            const savedBuild = await fetchJson(url, {
                method,
                headers,
                body: JSON.stringify(payload),
            });

            applyBuildRecord(savedBuild, { clearUndo: false });
            setStatus("Build gespeichert. SQL-Stand und Builder sind synchron.", "success");
        } catch (error) {
            setStatus(error.message || "Speichern fehlgeschlagen.", "error");
        } finally {
            state.isSaving = false;
            syncUiState();
        }
    }

    async function loadBuild(buildId) {
        if (state.isSaving || state.isLoading) {
            return;
        }
        if (!buildId) {
            resetDraft();
            return;
        }

        state.isLoading = true;
        syncUiState();
        setStatus("Build wird geladen ...", "muted");
        try {
            const build = await fetchJson(`/api/reactor-builds/${buildId}`, {
                method: "GET",
            });
            applyBuildRecord(build, { clearUndo: true });
            setStatus(`Build #${buildId} geladen.`, "success");
        } catch (error) {
            setBuildSelection(state.currentBuildId);
            setStatus(error.message || "Build konnte nicht geladen werden.", "error");
        } finally {
            state.isLoading = false;
            syncUiState();
        }
    }

    canvas.addEventListener("dragover", (event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
    });

    canvas.addEventListener("drop", (event) => {
        event.preventDefault();
        const symbolId = event.dataTransfer.getData("text/reactor-symbol-id");
        if (!symbolId) {
            return;
        }
        const point = pointerToCanvas(event);
        openInstanceModal(symbolId, point.x, point.y);
    });

    canvas.addEventListener("click", (event) => {
        if (event.target === canvas || event.target === nodeLayer || event.target === edgeLayer) {
            state.selectedNodeId = null;
            state.selectedEdgeId = null;
            if (state.mode !== "connect") {
                state.pendingAnchor = null;
            }
            renderAll();
        }
    });

    deleteNodeButton.addEventListener("click", () => {
        if (state.selectedNodeId) {
            removeNode(state.selectedNodeId);
            return;
        }
        if (state.selectedEdgeId) {
            removeEdge(state.selectedEdgeId);
            return;
        }
        setStatus("Kein Element oder keine Verbindung ausgewaehlt.", "error");
    });

    selectToolButton.addEventListener("click", () => {
        setMode("select");
    });

    anchorToolButton.addEventListener("click", () => {
        setMode("anchor");
    });

    connectToolButton.addEventListener("click", () => {
        setMode("connect");
    });

    saveButton.addEventListener("click", () => {
        void saveBuild(false);
    });

    saveAsButton.addEventListener("click", () => {
        void saveBuild(true);
    });

    nameInput.addEventListener("input", () => {
        syncDirtyState();
    });
    dateInput.addEventListener("change", () => {
        syncDirtyState();
    });
    userInput.addEventListener("input", () => {
        syncDirtyState();
    });

    if (newBuildButton) {
        newBuildButton.addEventListener("click", () => {
            if (!confirmDiscardDirtyChanges("Neuen Draft starten")) {
                return;
            }
            resetDraft();
        });
    }

    if (buildSelect) {
        buildSelect.addEventListener("change", () => {
            const nextBuildId = parseBuildId(buildSelect.value);
            if (!confirmDiscardDirtyChanges(nextBuildId ? `Build #${nextBuildId} laden` : "Zum Draft wechseln")) {
                setBuildSelection(state.currentBuildId);
                return;
            }
            void loadBuild(nextBuildId);
        });
    }

    if (librarySearchInput) {
        librarySearchInput.addEventListener("input", () => {
            applyLibraryFilter();
        });
    }
    if (librarySearchClearButton) {
        librarySearchClearButton.addEventListener("click", () => {
            if (!librarySearchInput) {
                return;
            }
            librarySearchInput.value = "";
            applyLibraryFilter();
            librarySearchInput.focus();
        });
    }

    layoutViewTabButton.addEventListener("click", () => {
        setView("layout");
    });

    communicationViewTabButton.addEventListener("click", () => {
        setView("communication");
    });

    instanceCancelButton.addEventListener("click", () => {
        closeInstanceModal();
        setStatus("Platzierung abgebrochen.", "muted");
    });

    instanceConfirmButton.addEventListener("click", () => {
        confirmInstancePlacement();
    });

    instanceIdInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            confirmInstancePlacement();
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            closeInstanceModal();
        }
    });

    instanceModal.addEventListener("click", (event) => {
        if (event.target === instanceModal) {
            closeInstanceModal();
        }
    });

    document.addEventListener("keydown", (event) => {
        const activeTag = document.activeElement ? document.activeElement.tagName : "";
        const isFormField = activeTag === "INPUT" || activeTag === "TEXTAREA" || activeTag === "SELECT";

        if (event.key === "Escape" && !instanceModal.classList.contains("is-hidden")) {
            event.preventDefault();
            closeInstanceModal();
            return;
        }

        if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "s") {
            event.preventDefault();
            if (!instanceModal.classList.contains("is-hidden")) {
                confirmInstancePlacement();
                return;
            }
            void saveBuild(false);
            return;
        }

        if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "z" && !isFormField) {
            event.preventDefault();
            const snapshot = state.undoStack.pop();
            restoreSnapshot(snapshot);
            return;
        }

        if (!isFormField && !event.ctrlKey && !event.metaKey && !event.altKey) {
            if (event.key === "1") {
                event.preventDefault();
                setMode("select");
                return;
            }
            if (event.key === "2") {
                event.preventDefault();
                setMode("anchor");
                return;
            }
            if (event.key === "3") {
                event.preventDefault();
                setMode("connect");
                return;
            }
        }

        if ((event.key === "Delete" || event.key === "Backspace") && !isFormField) {
            event.preventDefault();
            if (state.selectedNodeId) {
                removeNode(state.selectedNodeId);
                return;
            }
            if (state.selectedEdgeId) {
                removeEdge(state.selectedEdgeId);
            }
        }
    });

    window.addEventListener("beforeunload", (event) => {
        if (!state.isDirty) {
            return;
        }
        event.preventDefault();
        event.returnValue = "";
    });

    setBuildSelection(state.currentBuildId);
    setMode("select");
    setView("layout");
    applyLibraryFilter();
    renderAll();
    capturePersistedSnapshot();
})();
