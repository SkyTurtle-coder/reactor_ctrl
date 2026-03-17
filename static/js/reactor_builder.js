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
    const saveButton = document.getElementById("builder-save-button");
    const saveAsButton = document.getElementById("builder-save-as-button");
    const deleteNodeButton = document.getElementById("builder-delete-node-button");
    const selectToolButton = document.getElementById("builder-select-tool");
    const connectToolButton = document.getElementById("builder-connect-tool");
    const statusElement = document.getElementById("builder-status");

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

    const buildData = parseJsonScript("builder-build-data", null);
    const metaData = parseJsonScript("builder-meta-data", {});
    const libraryItems = Array.from(document.querySelectorAll(".builder-symbol-item"));
    const libraryById = new Map();

    for (const item of libraryItems) {
        const symbol = {
            id: item.dataset.symbolId || "",
            label: item.dataset.symbolLabel || item.dataset.symbolId || "Symbol",
            category: item.dataset.symbolCategory || "",
            svg_url: item.dataset.symbolSvgUrl || "",
            width: parseFloat(item.dataset.symbolWidth || "120"),
            height: parseFloat(item.dataset.symbolHeight || "80"),
        };
        libraryById.set(symbol.id, symbol);
        item.addEventListener("dragstart", (event) => {
            event.dataTransfer.effectAllowed = "copy";
            event.dataTransfer.setData("text/reactor-symbol-id", symbol.id);
        });
    }

    const state = {
        currentBuildId: metaData.currentBuildId || null,
        mode: "select",
        nodes: [],
        edges: [],
        selectedNodeId: null,
        selectedEdgeId: null,
        pendingConnectionSourceId: null,
        dragMove: null,
        undoStack: [],
    };

    function cloneSnapshot() {
        return JSON.parse(JSON.stringify({ nodes: state.nodes, edges: state.edges }));
    }

    function pushUndoSnapshot() {
        state.undoStack.push(cloneSnapshot());
        if (state.undoStack.length > 50) {
            state.undoStack.shift();
        }
    }

    function restoreSnapshot(snapshot) {
        if (!snapshot) {
            return;
        }
        state.nodes = snapshot.nodes || [];
        state.edges = snapshot.edges || [];
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        state.pendingConnectionSourceId = null;
        renderAll();
        setStatus("Undo ausgefuehrt.", "muted");
    }

    function generateNodeId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return `node-${window.crypto.randomUUID()}`;
        }
        return `node-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
    }

    function generateEdgeId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return `edge-${window.crypto.randomUUID()}`;
        }
        return `edge-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
    }

    function normalizeNode(node) {
        const symbol = libraryById.get(String(node.symbol_id || ""));
        return {
            id: String(node.id || generateNodeId()),
            symbol_id: String(node.symbol_id || symbol?.id || ""),
            label: String(node.label || symbol?.label || node.symbol_id || "Symbol"),
            category: String(node.category || symbol?.category || ""),
            svg_url: String(node.svg_url || symbol?.svg_url || ""),
            x: Number(node.x || 0),
            y: Number(node.y || 0),
            width: Number(node.width || symbol?.width || 120),
            height: Number(node.height || symbol?.height || 80),
        };
    }

    function normalizeEdge(edge) {
        return {
            id: String(edge.id || generateEdgeId()),
            source_node_id: String(edge.source_node_id || ""),
            target_node_id: String(edge.target_node_id || ""),
        };
    }

    if (buildData && buildData.definition_json) {
        if (Array.isArray(buildData.definition_json.nodes)) {
            state.nodes = buildData.definition_json.nodes.map(normalizeNode);
        }
        if (Array.isArray(buildData.definition_json.edges)) {
            state.edges = buildData.definition_json.edges.map(normalizeEdge);
        }
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

    function setMode(mode) {
        state.mode = mode === "connect" ? "connect" : "select";
        if (state.mode !== "connect") {
            state.pendingConnectionSourceId = null;
        }
        selectToolButton.classList.toggle("is-active", state.mode === "select");
        connectToolButton.classList.toggle("is-active", state.mode === "connect");
        renderAll();
        if (state.mode === "connect") {
            setStatus("Connection Tool aktiv. Klicke Quelle und danach Ziel.", "muted");
            return;
        }
        setStatus(
            state.currentBuildId
                ? `Build #${state.currentBuildId} geladen.`
                : "Neuer Build. Ziehe Elemente aus der Library in die Fliessbildflaeche.",
            "muted",
        );
    }

    function clampNode(node) {
        const padding = 14;
        const maxX = Math.max(padding, canvas.clientWidth - node.width - padding);
        const maxY = Math.max(padding, canvas.clientHeight - node.height - padding);
        node.x = Math.max(padding, Math.min(node.x, maxX));
        node.y = Math.max(padding, Math.min(node.y, maxY));
    }

    function updateEmptyState() {
        emptyState.classList.toggle("is-hidden", state.nodes.length > 0);
    }

    function pointerToCanvas(event) {
        const rect = canvas.getBoundingClientRect();
        return {
            x: event.clientX - rect.left,
            y: event.clientY - rect.top,
        };
    }

    function selectedNode() {
        return state.nodes.find((node) => node.id === state.selectedNodeId) || null;
    }

    function getNodeById(nodeId) {
        return state.nodes.find((node) => node.id === nodeId) || null;
    }

    function nodeCenter(node) {
        return {
            x: node.x + node.width / 2,
            y: node.y + node.height / 2,
        };
    }

    function selectNode(nodeId) {
        state.selectedNodeId = nodeId;
        state.selectedEdgeId = null;
        renderAll();
    }

    function selectEdge(edgeId) {
        state.selectedEdgeId = edgeId;
        state.selectedNodeId = null;
        renderAll();
    }

    function renderEdges() {
        while (edgeLayer.firstChild) {
            edgeLayer.removeChild(edgeLayer.firstChild);
        }
        edgeLayer.setAttribute("viewBox", `0 0 ${canvas.clientWidth} ${canvas.clientHeight}`);

        for (const edge of state.edges) {
            const source = getNodeById(edge.source_node_id);
            const target = getNodeById(edge.target_node_id);
            if (!source || !target) {
                continue;
            }
            const sourcePoint = nodeCenter(source);
            const targetPoint = nodeCenter(target);
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", String(sourcePoint.x));
            line.setAttribute("y1", String(sourcePoint.y));
            line.setAttribute("x2", String(targetPoint.x));
            line.setAttribute("y2", String(targetPoint.y));
            line.setAttribute("class", `builder-edge${state.selectedEdgeId === edge.id ? " is-selected" : ""}`);
            line.dataset.edgeId = edge.id;
            line.addEventListener("click", (event) => {
                event.stopPropagation();
                selectEdge(edge.id);
            });
            edgeLayer.appendChild(line);
        }

        if (state.mode === "connect" && state.pendingConnectionSourceId) {
            const source = getNodeById(state.pendingConnectionSourceId);
            if (source) {
                const sourcePoint = nodeCenter(source);
                const marker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
                marker.setAttribute("cx", String(sourcePoint.x));
                marker.setAttribute("cy", String(sourcePoint.y));
                marker.setAttribute("r", "8");
                marker.setAttribute("class", "builder-edge is-pending");
                edgeLayer.appendChild(marker);
            }
        }
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
            element.dataset.nodeId = node.id;
            element.style.left = `${node.x}px`;
            element.style.top = `${node.y}px`;
            element.style.width = `${node.width}px`;
            element.style.minHeight = `${node.height}px`;

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
            label.textContent = node.label;

            body.appendChild(graphic);
            body.appendChild(label);
            element.appendChild(body);

            element.addEventListener("click", (event) => {
                event.stopPropagation();
                if (state.mode === "connect") {
                    handleConnectionClick(node.id);
                    return;
                }
                selectNode(node.id);
            });

            element.addEventListener("pointerdown", (event) => {
                if (state.mode !== "select") {
                    return;
                }
                pushUndoSnapshot();
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
                };
                liveElement.classList.add("is-dragging");
                window.addEventListener("pointermove", handlePointerMove);
                window.addEventListener("pointerup", stopPointerMove, { once: true });
            });

            nodeLayer.appendChild(element);
        }
    }

    function renderAll() {
        renderEdges();
        renderNodes();
        updateEmptyState();
    }

    function handlePointerMove(event) {
        if (!state.dragMove) {
            return;
        }
        const node = selectedNode();
        if (!node || node.id !== state.dragMove.nodeId) {
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
        renderEdges();
    }

    function stopPointerMove() {
        window.removeEventListener("pointermove", handlePointerMove);
        if (state.dragMove && state.dragMove.element) {
            state.dragMove.element.classList.remove("is-dragging");
        }
        state.dragMove = null;
        renderAll();
    }

    function removeNode(nodeId) {
        pushUndoSnapshot();
        state.nodes = state.nodes.filter((node) => node.id !== nodeId);
        state.edges = state.edges.filter(
            (edge) => edge.source_node_id !== nodeId && edge.target_node_id !== nodeId,
        );
        if (state.selectedNodeId === nodeId) {
            state.selectedNodeId = null;
        }
        if (state.pendingConnectionSourceId === nodeId) {
            state.pendingConnectionSourceId = null;
        }
        renderAll();
        setStatus("Element entfernt. Build noch nicht gespeichert.", "muted");
    }

    function removeEdge(edgeId) {
        pushUndoSnapshot();
        state.edges = state.edges.filter((edge) => edge.id !== edgeId);
        if (state.selectedEdgeId === edgeId) {
            state.selectedEdgeId = null;
        }
        renderAll();
        setStatus("Verbindung entfernt. Build noch nicht gespeichert.", "muted");
    }

    function edgeExists(sourceNodeId, targetNodeId) {
        return state.edges.some(
            (edge) =>
                (edge.source_node_id === sourceNodeId && edge.target_node_id === targetNodeId) ||
                (edge.source_node_id === targetNodeId && edge.target_node_id === sourceNodeId),
        );
    }

    function handleConnectionClick(nodeId) {
        if (!state.pendingConnectionSourceId) {
            state.pendingConnectionSourceId = nodeId;
            state.selectedNodeId = nodeId;
            state.selectedEdgeId = null;
            renderAll();
            setStatus("Quelle gewaehlt. Jetzt Ziel-Element anklicken.", "muted");
            return;
        }

        if (state.pendingConnectionSourceId === nodeId) {
            state.pendingConnectionSourceId = null;
            state.selectedNodeId = nodeId;
            renderAll();
            setStatus("Connection Tool zurueckgesetzt.", "muted");
            return;
        }

        if (edgeExists(state.pendingConnectionSourceId, nodeId)) {
            state.pendingConnectionSourceId = null;
            renderAll();
            setStatus("Zwischen diesen Elementen existiert bereits eine Verbindung.", "error");
            return;
        }

        pushUndoSnapshot();
        const edge = normalizeEdge({
            id: generateEdgeId(),
            source_node_id: state.pendingConnectionSourceId,
            target_node_id: nodeId,
        });
        state.edges.push(edge);
        state.selectedEdgeId = edge.id;
        state.selectedNodeId = null;
        state.pendingConnectionSourceId = null;
        renderAll();
        setStatus("Verbindung erstellt. Weitere Verbindung oder Select-Tool waehlen.", "success");
    }

    function addNodeFromSymbol(symbolId, x, y) {
        const symbol = libraryById.get(symbolId);
        if (!symbol) {
            setStatus(`Symbol ${symbolId} ist nicht in der Library registriert.`, "error");
            return;
        }

        pushUndoSnapshot();
        const node = normalizeNode({
            id: generateNodeId(),
            symbol_id: symbol.id,
            label: symbol.label,
            category: symbol.category,
            svg_url: symbol.svg_url,
            width: symbol.width,
            height: symbol.height,
            x: x - symbol.width / 2,
            y: y - symbol.height / 2,
        });
        clampNode(node);
        state.nodes.push(node);
        state.selectedNodeId = node.id;
        state.selectedEdgeId = null;
        renderAll();
        setStatus(`${symbol.label} auf dem Canvas platziert.`, "muted");
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
        addNodeFromSymbol(symbolId, point.x, point.y);
    });

    canvas.addEventListener("click", (event) => {
        if (event.target === canvas || event.target === nodeLayer || event.target === edgeLayer) {
            state.selectedNodeId = null;
            state.selectedEdgeId = null;
            if (state.mode !== "connect") {
                state.pendingConnectionSourceId = null;
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

    connectToolButton.addEventListener("click", () => {
        setMode("connect");
    });

    document.addEventListener("keydown", (event) => {
        const activeTag = document.activeElement ? document.activeElement.tagName : "";
        const isFormField = activeTag === "INPUT" || activeTag === "TEXTAREA" || activeTag === "SELECT";

        if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "z" && !isFormField) {
            event.preventDefault();
            const snapshot = state.undoStack.pop();
            restoreSnapshot(snapshot);
            return;
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

    function buildPayload() {
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

        return {
            build_name: buildName,
            build_date: buildDate,
            created_by: buildUser,
            updated_by: buildUser,
            definition_json: {
                canvas: {
                    width: canvas.clientWidth,
                    height: canvas.clientHeight,
                },
                nodes: state.nodes.map((node) => ({
                    id: node.id,
                    symbol_id: node.symbol_id,
                    label: node.label,
                    category: node.category,
                    svg_url: node.svg_url,
                    x: node.x,
                    y: node.y,
                    width: node.width,
                    height: node.height,
                })),
                edges: state.edges.map((edge) => ({
                    id: edge.id,
                    source_node_id: edge.source_node_id,
                    target_node_id: edge.target_node_id,
                })),
            },
        };
    }

    async function saveBuild(forceCreate) {
        if (metaData.apiAuthRequired && !metaData.builderApiToken) {
            setStatus("Der Server liefert keinen Builder-Token. Speichern ist derzeit nicht verfuegbar.", "error");
            return;
        }

        let payload;
        try {
            payload = buildPayload();
        } catch (error) {
            setStatus(error.message || "Build-Daten sind ungueltig.", "error");
            return;
        }

        const isCreate = forceCreate || !state.currentBuildId;
        const url = isCreate ? "/api/reactor-builds" : `/api/reactor-builds/${state.currentBuildId}`;
        const method = isCreate ? "POST" : "PATCH";

        setStatus("Build wird gespeichert ...", "muted");

        try {
            const headers = {
                "Content-Type": "application/json",
            };
            if (metaData.builderApiToken) {
                headers.Authorization = `Bearer ${metaData.builderApiToken}`;
            }

            const response = await window.fetch(url, {
                method,
                headers,
                body: JSON.stringify(payload),
            });
            const responsePayload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(responsePayload.error || "Speichern fehlgeschlagen.");
            }

            const nextBuildId = responsePayload.reactor_build_id;
            if (!nextBuildId) {
                throw new Error("Build gespeichert, aber es wurde keine Build-ID zurueckgegeben.");
            }

            setStatus("Build gespeichert. Seite wird neu geladen ...", "success");
            const nextUrl = new URL(window.location.href);
            nextUrl.searchParams.set("build_id", String(nextBuildId));
            window.location.assign(nextUrl.toString());
        } catch (error) {
            setStatus(error.message || "Speichern fehlgeschlagen.", "error");
        }
    }

    saveButton.addEventListener("click", () => {
        void saveBuild(false);
    });

    saveAsButton.addEventListener("click", () => {
        void saveBuild(true);
    });

    setMode("select");
    renderAll();
})();
