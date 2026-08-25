(() => {
  "use strict";

  const API = "/api/v1";
  let currentRun = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function request(path, options = {}) {
    return requestJson(path, { timeout: 30000, ...options });
  }

  function setAgentBadge(state, text) {
    const badge = document.getElementById("agent-badge");
    if (!badge) return;
    badge.className = `status-badge ${state}`;
    badge.textContent = text;
  }

  async function loadResources() {
    const select = document.getElementById("agent-resource");
    if (!select) return;
    if (window.AresResources) {
      await window.AresResources.fillSelect(select, {
        blankLabel: "ARES elegirá si no hay ambigüedad",
        selectRecommended: true,
      });
      return;
    }
    const catalog = await request("/resources");
    select.replaceChildren();
    select.append(el("option", "", "ARES elegirá si no hay ambigüedad"));
    select.firstChild.value = "";
    for (const resource of catalog.resources || []) {
      const option = el(
        "option",
        "",
        `${resource.human_name} · ${resource.kind} · confianza ${Math.round(resource.confidence * 100)}%`,
      );
      option.value = resource.resource_id;
      if (resource.recommended) option.selected = true;
      select.append(option);
    }
  }

  function renderRun(run) {
    currentRun = run;
    const target = document.getElementById("agent-results");
    if (!target) return;
    target.replaceChildren();
    setAgentBadge(run.state === "COMPLETED" || run.state === "PARTIAL" ? "ready" : "pending", run.state);

    const summary = el("div", "capability-card");
    summary.append(el("h3", "", "AgentRun"));
    summary.append(el("p", "", run.summary || "Sin resumen."));
    summary.append(el("p", "muted", `ID ${run.id} · estado ${run.state}`));
    if ((run.limitations || []).length) {
      const list = el("ul", "finding-list");
      for (const item of run.limitations) list.append(el("li", "", item));
      summary.append(el("strong", "", "Limitaciones reales"), list);
    }
    target.append(summary);

    for (const step of run.steps || []) {
      const block = el("div", "capability-card");
      block.append(el("h3", "", `${step.state} · ${step.objective}`));
      block.append(el("p", "", `Capability: ${step.capability_id || "ninguna"} · Riesgo: ${step.risk}`));
      block.append(el("p", "muted", `Autorización: ${step.requires_authorization ? "requerida" : "no"} · Protección: ${step.requires_protection ? "requerida" : "no"}`));
      if (step.action) {
        const details = el("details");
        details.append(el("summary", "", "Ver comandos y detalles técnicos"));
        const list = el("ul", "finding-list");
        for (const line of step.action.command_summary || []) list.append(el("li", "", line));
        list.append(el("li", "", `Clase: ${step.action.operation_class}`));
        list.append(el("li", "", `Cambios esperados: ${step.action.expected_changes}`));
        details.append(list);
        block.append(details);
      }
      if (step.result) {
        renderResult(block, step.result);
      }
      target.append(block);
    }

    const controls = el("div", "topbar-actions wrap-actions");
    if (run.state === "READ_ONLY_AUTHORIZATION_REQUIRED") {
      target.append(renderAuthorizationRequest(run));
    }
    if (run.state === "PLAN_READY") {
      const execute = el("button", "", "Ejecutar revisión autorizada");
      execute.addEventListener("click", executeRun);
      controls.append(execute);
    }
    if (!["COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "INVALIDATED"].includes(run.state)) {
      const cancel = el("button", "secondary-button", "Cancelar");
      cancel.addEventListener("click", cancelRun);
      controls.append(cancel);
    }
    if (controls.children.length) target.append(controls);
  }

  function renderAuthorizationRequest(run) {
    const selected = (run.resources || []).find(
      (resource) => resource.resource_id === run.selected_resource_id,
    );
    const card = el("div", "authorization-card");
    card.append(el("h3", "", "ARES solicita autorización"));
    card.append(el("p", "", "Plan propuesto. Todavía no se ha ejecutado."));

    const facts = el("dl", "authorization-facts");
    const entries = [
      ["Objetivo", run.objective],
      [
        "Destino detectado",
        selected
          ? `${selected.human_name} · ${selected.technical_path || "sin ruta técnica visible"}`
          : "ARES usará el entorno de recuperación si no hay ambigüedad.",
      ],
      ["Nivel de riesgo", "Solo lectura · bajo"],
      ["Cambios esperados", "Ninguno en discos, particiones, GRUB ni sistema instalado."],
      ["Duración de autorización", "5 minutos, un solo uso, ligada a este plan y destino."],
    ];
    for (const [key, value] of entries) {
      const wrapper = el("div");
      wrapper.append(el("dt", "", key), el("dd", "", value || "—"));
      facts.append(wrapper);
    }
    card.append(facts);

    const actions = el("ul", "finding-list");
    for (const step of run.steps || []) {
      actions.append(
        el(
          "li",
          "",
          `${step.objective} · ${step.capability_id || "sin capability"} · ${step.risk}`,
        ),
      );
    }
    card.append(el("strong", "", "Acciones que se realizarán"), actions);

    const details = el("details");
    details.append(el("summary", "", "Ver comandos y detalles técnicos"));
    const technical = el("ul", "finding-list");
    for (const step of run.steps || []) {
      for (const line of step.action?.command_summary || []) {
        technical.append(el("li", "", `${step.id}: ${line}`));
      }
      technical.append(el("li", "", `${step.id}: privilegios=${step.action?.privileged ? "sí" : "no"} · cambios=${step.action?.expected_changes || "Ninguno"}`));
    }
    technical.append(el("li", "", "La autorización de diagnóstico no autoriza reparaciones."));
    technical.append(el("li", "", "Si cambia el destino, fingerprint o plan, el backend invalida la autorización."));
    details.append(technical);
    card.append(details);

    const phrase = el("p", "form-hint", "Para autorizar, pulsa el botón. El backend enviará la frase contextual: AUTORIZO SOLO LECTURA.");
    const controls = el("div", "topbar-actions wrap-actions");
    const authorize = el("button", "", "Autorizar");
    authorize.addEventListener("click", authorizeRun);
    const cancel = el("button", "secondary-button", "Cancelar");
    cancel.addEventListener("click", cancelRun);
    controls.append(authorize, cancel);
    card.append(phrase, controls);
    return card;
  }

  function renderResult(block, result) {
    if (result.summary && Array.isArray(result.findings)) {
      block.append(el("p", "", result.summary));
      const findings = el("ul", "finding-list");
      for (const finding of result.findings) {
        findings.append(
          el(
            "li",
            "",
            `${finding.severity || "info"} · ${finding.title}: ${finding.detail}`,
          ),
        );
      }
      block.append(el("strong", "", "Hallazgos"), findings);
      if ((result.limitations || []).length) {
        const limitations = el("ul", "finding-list");
        for (const limitation of result.limitations) limitations.append(el("li", "", limitation));
        block.append(el("strong", "", "Limitaciones"), limitations);
      }
      if (result.repair_capability_available === false) {
        block.append(
          el(
            "p",
            "banner warning",
            "Reparación de arranque no disponible todavía: ARES no modificó GRUB.",
          ),
        );
      }
      return;
    }
    if (result.snapshot_id || result.diagnostic_id) {
      block.append(
        el(
          "p",
          "",
          `Snapshot: ${result.snapshot_id || "—"} · Diagnóstico: ${result.diagnostic_id || "—"}`,
        ),
      );
      if (result.message) block.append(el("p", "muted", result.message));
      return;
    }
    block.append(el("p", "", `Resultado: ${JSON.stringify(result)}`));
  }

  async function startRun(event) {
    event.preventDefault();
    const objective = document.getElementById("agent-objective")?.value.trim();
    const resource = document.getElementById("agent-resource")?.value || null;
    if (!objective) return;
    setAgentBadge("pending", "Creando AgentRun");
    const run = await request("/agent/runs", {
      method: "POST",
      body: JSON.stringify({ objective, resource_id: resource || null }),
    });
    renderRun(run);
  }

  async function authorizeRun() {
    if (!currentRun) return;
    const run = await request(`/agent/runs/${encodeURIComponent(currentRun.id)}/authorize-read-only`, {
      method: "POST",
      body: JSON.stringify({ confirm: true, understood: "AUTORIZO SOLO LECTURA" }),
    });
    renderRun(run);
  }

  async function executeRun() {
    if (!currentRun) return;
    setAgentBadge("pending", "Ejecutando backend");
    const run = await request(`/agent/runs/${encodeURIComponent(currentRun.id)}/execute`, {
      method: "POST",
      body: "{}",
      timeout: 120000,
    });
    renderRun(run);
  }

  async function cancelRun() {
    if (!currentRun) return;
    const run = await request(`/agent/runs/${encodeURIComponent(currentRun.id)}/cancel`, {
      method: "POST",
      body: "{}",
    });
    renderRun(run);
  }

  function report(error) {
    setAgentBadge("error", error.code || "Error");
    const target = document.getElementById("agent-results");
    target?.prepend(el("p", "banner warning", `${error.code || "ERROR"}: ${error.message}`));
  }

  function init() {
    document.getElementById("agent-form")?.addEventListener("submit", (event) =>
      startRun(event).catch(report),
    );
    loadResources().catch(() => {
      setAgentBadge("warning", "Recursos no disponibles");
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
