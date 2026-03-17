(function () {
    const canvas = document.getElementById("builder-canvas");
    if (!canvas) {
        return;
    }

    const nodeLayer = document.getElementById("builder-node-layer");
    const emptyState = document.getElementById("builder-canvas-empty");
    const nameInput = document.getElementById("builder-name-input");
    const dateInput = document.getElementById("builder-date-input");
    const userInput = document.getElementById("builder-user-input");
    const tokenInput = document.getElementById("builder-token-input");
    const saveButton = document.getElementById("builder-save-button");
    const saveAsButton = document.getElementById("builder-save-as-button");
    const deleteNodeButton = document.getElementById("builder-delete-node-button");
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

    const categoryData = parseJsonScript("builder-library-data", []);
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
        nodes: [],
        selectedNodeId: null,
        dragMove: null,
    };

    function generateNodeId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return `node-${window.crypto.randomUUID()}`;
        }
        return `node-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
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

    if (buildData && buildData.definition_json && Array.isArray(buildData.definition_json.nodes)) {
        state.nodes = buildData.definition_json.nodes.map(normalizeNode);
    }

    const storedToken = window.localStorage.getItem("reactorBuilderApiToken");
    if (storedToken) {
        tokenInput.value = storedToken;
    }
    tokenInput.addEventListener("input", () => {
        window.localStorage.setItem("reactorBuilderApiToken", tokenInput.value.trim());
    });

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

    function selectNode(nodeId) {
        state.selectedNodeId = nodeId;
        renderNodes();
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

            const deleteButton = document.createElement("button");
            deleteButton.type = "button";
            deleteButton.className = "builder-node-delete";
            deleteButton.textContent = "×";
            deleteButton.setAttribute("aria-label", `${node.label} entfernen`);
            deleteButton.addEventListener("click", (event) => {
                event.stopPropagation();
                removeNode(node.id);
            });

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
            element.appendChild(deleteButton);
            element.appendChild(body);

            element.addEventListener("click", () => {
                selectNode(node.id);
            });

            element.addEventListener("pointerdown", (event) => {
                if (event.target instanceof HTMLElement && event.target.closest(".builder-node-delete")) {
                    return;
                }
                const point = pointerToCanvas(event);
                state.dragMove = {
                    nodeId: node.id,
                    offsetX: point.x - node.x,
                    offsetY: point.y - node.y,
                    element,
                };
                element.classList.add("is-dragging");
                selectNode(node.id);
                window.addEventListener("pointermove", handlePointerMove);
                window.addEventListener("pointerup", stopPointerMove, { once: true });
            });

            nodeLayer.appendChild(element);
        }

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
        state.dragMove.element.style.left = `${node.x}px`;
        state.dragMove.element.style.top = `${node.y}px`;
    }

    function stopPointerMove() {
        window.removeEventListener("pointermove", handlePointerMove);
        if (state.dragMove && state.dragMove.element) {
            state.dragMove.element.classList.remove("is-dragging");
        }
        state.dragMove = null;
    }

    function removeNode(nodeId) {
        state.nodes = state.nodes.filter((node) => node.id !== nodeId);
        if (state.selectedNodeId === nodeId) {
            state.selectedNodeId = null;
        }
        renderNodes();
        setStatus("Element entfernt. Build noch nicht gespeichert.", "muted");
    }

    function addNodeFromSymbol(symbolId, x, y) {
        const symbol = libraryById.get(symbolId);
        if (!symbol) {
            setStatus(`Symbol ${symbolId} ist nicht in der Library registriert.`, "error");
            return;
        }

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
        renderNodes();
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
        if (event.target === canvas || event.target === nodeLayer) {
            state.selectedNodeId = null;
            renderNodes();
        }
    });

    deleteNodeButton.addEventListener("click", () => {
        if (!state.selectedNodeId) {
            setStatus("Kein Element ausgewaehlt.", "error");
            return;
        }
        removeNode(state.selectedNodeId);
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Delete") {
            return;
        }
        const activeTag = document.activeElement ? document.activeElement.tagName : "";
        if (activeTag === "INPUT" || activeTag === "TEXTAREA") {
            return;
        }
        if (state.selectedNodeId) {
            removeNode(state.selectedNodeId);
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
            },
        };
    }

    async function saveBuild(forceCreate) {
        const token = tokenInput.value.trim();
        if (!token) {
            setStatus("Zum Speichern wird ein API Token benoetigt.", "error");
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
            const response = await window.fetch(url, {
                method,
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`,
                },
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

    setStatus(
        state.currentBuildId
            ? `Build #${state.currentBuildId} geladen.`
            : "Neuer Build. Ziehe Elemente aus der Library in die Fliessbildflaeche.",
        "muted",
    );

    if (!Array.isArray(categoryData)) {
        console.warn("Builder category data is missing or invalid.");
    }

    renderNodes();
})();
