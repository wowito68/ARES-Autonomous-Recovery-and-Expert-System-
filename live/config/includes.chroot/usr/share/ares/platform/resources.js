(() => {
  "use strict";

  let cachedCatalog = null;

  function optionLabel(resource) {
    const confidence = Number.isFinite(resource.confidence)
      ? `${Math.round(resource.confidence * 100)}%`
      : "n/a";
    return [
      resource.human_name || resource.resource_id,
      resource.kind || "resource",
      resource.technical_path || "",
      `confianza ${confidence}`,
    ]
      .filter(Boolean)
      .join(" · ");
  }

  async function api(path, options = {}) {
    if (typeof requestJson === "function") {
      return requestJson(path, { timeout: 15000, ...options });
    }
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(body?.detail || body?.title || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  async function catalog({ refresh = false } = {}) {
    if (refresh) {
      cachedCatalog = await api("/resources/refresh", { method: "POST" });
      return cachedCatalog;
    }
    if (!cachedCatalog) {
      cachedCatalog = await api("/resources");
    }
    return cachedCatalog;
  }

  function selectNode(selectOrId) {
    return typeof selectOrId === "string" ? document.getElementById(selectOrId) : selectOrId;
  }

  async function fillSelect(selectOrId, options = {}) {
    const select = selectNode(selectOrId);
    if (!select) return [];
    const {
      kinds = [],
      includeBlank = true,
      blankLabel = "Selecciona recurso detectado",
      refresh = false,
      selectRecommended = false,
    } = options;
    const data = await catalog({ refresh });
    const resources = (data.resources || [])
      .filter((resource) => kinds.length === 0 || kinds.includes(resource.kind))
      .sort((left, right) => {
        if (left.recommended !== right.recommended) return left.recommended ? -1 : 1;
        return (right.confidence || 0) - (left.confidence || 0);
      });

    select.replaceChildren();
    if (includeBlank) {
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = blankLabel;
      select.append(blank);
    }
    for (const resource of resources) {
      const option = document.createElement("option");
      option.value = resource.resource_id;
      option.textContent = optionLabel(resource);
      option.dataset.path = resource.technical_path || "";
      option.dataset.kind = resource.kind || "";
      option.dataset.name = resource.human_name || "";
      option.dataset.confidence = String(resource.confidence ?? "");
      if (selectRecommended && resource.recommended && includeBlank) option.selected = true;
      select.append(option);
    }
    return resources;
  }

  function selectedOption(selectOrId) {
    const select = selectNode(selectOrId);
    if (!select || select.selectedIndex < 0) return null;
    return select.options[select.selectedIndex] || null;
  }

  function technicalPath(selectOrId) {
    return selectedOption(selectOrId)?.dataset.path || "";
  }

  function bindPath(selectOrId, inputOrId) {
    const select = selectNode(selectOrId);
    const input = selectNode(inputOrId);
    if (!select || !input) return;
    const apply = () => {
      const path = technicalPath(select);
      if (path) input.value = path;
    };
    select.addEventListener("change", apply);
    apply();
  }

  window.AresResources = {
    bindPath,
    catalog,
    fillSelect,
    formatLabel: optionLabel,
    technicalPath,
  };
})();
