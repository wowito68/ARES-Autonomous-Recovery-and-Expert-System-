(() => {
  "use strict";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function request(path, options = {}) {
    return requestJson(path, { timeout: 30000, ...options });
  }

  function badge(state, text) {
    const node = document.getElementById("session-badge");
    if (!node) return;
    node.className = `status-badge ${state}`;
    node.textContent = text;
  }

  function renderPreflight(preflight) {
    const target = document.getElementById("session-results");
    if (!target) return;
    target.replaceChildren();
    badge(preflight.allowed ? "ready" : "warning", preflight.allowed ? "Disponible" : "No verificado");
    const card = el("div", "exit-card");
    card.append(el("h3", "", preflight.message));
    card.append(el("p", "", `Firmware: ${preflight.firmware} · Bootloader: ${preflight.bootloader_detected ? preflight.bootloader_name : "no verificado"}`));
    if ((preflight.active_operations || []).length) {
      card.append(el("strong", "", "Operaciones activas o no terminales"));
      card.append(list(preflight.active_operations));
    }
    if ((preflight.prepared_terminals || []).length) {
      card.append(el("strong", "", "Terminales ARES pendientes"));
      card.append(list(preflight.prepared_terminals));
    }
    if ((preflight.mounted_resources || []).length) {
      card.append(el("strong", "", "Montajes gestionados por ARES"));
      card.append(list(preflight.mounted_resources));
    }
    if ((preflight.installed_systems || []).length) {
      const systems = el("ul", "finding-list");
      for (const system of preflight.installed_systems) systems.append(el("li", "", `${system.human_name} · ${system.technical_path || "sin ruta"}`));
      card.append(el("strong", "", "Sistemas instalados detectados"), systems);
    }
    const steps = el("ul", "finding-list");
    for (const step of preflight.steps_before_exit || []) steps.append(el("li", "", step));
    card.append(el("strong", "", "Antes de salir ARES hará"), steps);
    const details = el("details");
    details.append(el("summary", "", "Ver alternativas detectadas"));
    const caps = el("ul", "finding-list");
    for (const cap of preflight.capabilities || []) {
      caps.append(el("li", "", `${cap.label}: ${cap.available ? "disponible" : "no disponible"} · ${cap.explanation}`));
    }
    details.append(caps);
    card.append(details);
    if (preflight.allowed) {
      const warning = el("p", "form-hint", "ARES sincronizará escrituras y solicitará la transición a systemd. Si arrancaste desde USB, retíralo cuando el firmware vuelva a iniciar.");
      const execute = el("button", "", "Entiendo y ejecutar salida segura");
      execute.addEventListener("click", () => executeSession(preflight.requested_operation).catch(report));
      const cancel = el("button", "secondary-button", "Cancelar y permanecer en ARES");
      cancel.addEventListener("click", () => {
        target.replaceChildren(el("p", "empty-state", "Cancelado. ARES permanece activo."));
        badge("pending", "Sin acción pendiente");
      });
      const controls = el("div", "topbar-actions wrap-actions");
      controls.append(execute, cancel);
      card.append(warning, controls);
    }
    target.append(card);
  }

  function list(items) {
    const node = el("ul", "finding-list");
    for (const item of items || []) node.append(el("li", "", item));
    return node;
  }

  async function preflight(operation) {
    badge("pending", "Verificando salida");
    const result = await request(`/system/session/preflight/${encodeURIComponent(operation)}`);
    renderPreflight(result);
  }

  async function executeSession(operation) {
    const result = await request("/system/session/action", {
      method: "POST",
      body: JSON.stringify({ operation, confirm: true, understood: "ENTIENDO" }),
      timeout: 30000,
    });
    renderPreflight(result.preflight);
    const target = document.getElementById("session-results");
    target?.prepend(el("p", result.executed ? "banner" : "banner warning", result.message));
  }

  async function openTerminal() {
    badge("pending", "Preparando terminal");
    const select = document.getElementById("terminal-context");
    const resource = document.getElementById("terminal-resource");
    const context = select?.value || "ares_local";
    const resourceId = resource?.value || "";
    const plan = await request("/system/terminal/plans", {
      method: "POST",
      body: JSON.stringify({
        context_kind: context,
        ...(resourceId ? { resource_id: resourceId } : {}),
      }),
    });
    renderTerminalPlan(plan);
    if (!plan.plan.requires_authorization) {
      const started = await request(`/system/terminal/plans/${encodeURIComponent(plan.plan.id)}/authorize-open`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      renderTerminalSession(started);
    }
  }

  function renderTerminalPlan(result) {
    const target = document.getElementById("session-results");
    if (!target) return;
    const plan = result.plan;
    const card = el("div", "exit-card");
    card.append(el("h3", "", "Plan de terminal contextual"));
    card.append(el("p", "", `${contextLabel(plan.context_kind)} · Riesgo ${plan.risk}`));
    card.append(el("p", "", `Cambios esperados: ${plan.expected_changes}`));
    if (plan.target) {
      card.append(el("p", "", `Destino: ${plan.target.human_name} · ${plan.target.operating_system || "sistema detectado"}`));
    }
    card.append(el("strong", "", "ARES garantiza"));
    card.append(list(plan.expected_non_changes || []));
    if ((plan.limitations || []).length) {
      card.append(el("strong", "", "Limitaciones"));
      card.append(list(plan.limitations));
    }
    const fingerprint = el("p", "form-hint", `Huella del plan: ${plan.fingerprint_sha256}`);
    card.append(fingerprint);
    if (plan.requires_authorization) {
      const phrase = el("code", "", plan.confirmation_phrase);
      const input = el("input", "");
      input.type = "text";
      input.placeholder = plan.confirmation_phrase;
      input.autocomplete = "off";
      input.spellcheck = false;
      const authorize = el("button", "", "Autorizar y abrir terminal");
      authorize.type = "button";
      authorize.addEventListener("click", async () => {
        const started = await request(`/system/terminal/plans/${encodeURIComponent(plan.id)}/authorize-open`, {
          method: "POST",
          body: JSON.stringify({ confirm: true, understood: input.value }),
        });
        renderTerminalSession(started);
      });
      card.append(el("p", "form-hint", "Para abrirla escribe exactamente:"), phrase, input, authorize);
      badge("warning", "Autorización requerida");
    } else {
      badge("pending", "Abriendo terminal");
    }
    target.prepend(card);
  }

  function renderTerminalSession(result) {
    const target = document.getElementById("session-results");
    if (!target) return;
    const card = el("div", "exit-card");
    card.append(el("h3", "", result.accepted ? "Terminal iniciada" : "Terminal pendiente"));
    card.append(el("p", "", result.message || "La interacción ocurre fuera del navegador."));
    card.append(el("p", "form-hint", `Sesión: ${result.session.id} · Estado: ${result.session.status}`));
    const close = el("button", "secondary-button", "Cerrar y reconciliar sesión");
    close.type = "button";
    close.addEventListener("click", async () => {
      const closed = await request(`/system/terminal/sessions/${encodeURIComponent(result.session.id)}/close`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      renderTerminalSession(closed);
    });
    const cleanup = el("button", "secondary-button", "Ver evidencia de limpieza");
    cleanup.type = "button";
    cleanup.addEventListener("click", async () => {
      const evidence = await request(`/system/terminal/sessions/${encodeURIComponent(result.session.id)}/cleanup`);
      const box = el("pre", "evidence-box", JSON.stringify(evidence, null, 2));
      target.prepend(box);
    });
    const controls = el("div", "topbar-actions wrap-actions");
    controls.append(close, cleanup);
    card.append(controls);
    target.prepend(card);
    badge(result.session.status === "ACTIVE" ? "ready" : "pending", `Terminal ${result.session.status}`);
  }

  function contextLabel(value) {
    if (value === "ares_local") return "Terminal de ARES";
    if (value === "installed_system_read_only") return "Sistema instalado en solo lectura";
    return value || "Terminal";
  }

  async function loadTerminalContexts() {
    const select = document.getElementById("terminal-context");
    const detail = document.getElementById("terminal-context-detail");
    if (!select) return;
    const data = await request("/system/terminal/contexts");
    select.replaceChildren();
    for (const context of data.contexts || []) {
      const option = el(
        "option",
        "",
        `${context.label}${context.available ? "" : " · no disponible"}`,
      );
      option.value = context.id;
      option.disabled = !context.available;
      option.dataset.explanation = context.explanation || "";
      option.dataset.privilege = context.privilege || "";
      option.dataset.limitations = (context.limitations || []).join("; ");
      option.dataset.resourceId = context.resource_id || "";
      select.append(option);
    }
    const update = () => {
      const selected = select.options[select.selectedIndex];
      if (!selected || !detail) return;
      const resourceSelect = document.getElementById("terminal-resource");
      if (resourceSelect) {
        resourceSelect.disabled = selected.value !== "installed_system_read_only";
        if (selected.dataset.resourceId) resourceSelect.value = selected.dataset.resourceId;
      }
      detail.textContent = [
        selected.dataset.explanation,
        selected.dataset.privilege ? `Acceso: ${selected.dataset.privilege}` : "",
        selected.dataset.limitations ? `Limitaciones: ${selected.dataset.limitations}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
    };
    select.addEventListener("change", update);
    if (window.AresResources?.fillSelect) {
      await window.AresResources.fillSelect("terminal-resource", {
        kinds: ["operating_system"],
        includeBlank: true,
        blankLabel: "ARES elegirá si hay un único candidato",
        selectRecommended: true,
      });
    }
    update();
  }

  function report(error) {
    badge("error", error.code || "Error");
    const target = document.getElementById("session-results");
    target?.prepend(el("p", "banner warning", `${error.code || "ERROR"}: ${error.message}`));
  }

  function init() {
    for (const button of document.querySelectorAll(".session-preflight")) {
      button.addEventListener("click", () => preflight(button.dataset.operation).catch(report));
    }
    document.getElementById("terminal-open")?.addEventListener("click", () =>
      openTerminal().catch(report),
    );
    loadTerminalContexts().catch(report);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
