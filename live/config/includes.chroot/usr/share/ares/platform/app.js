"use strict";

const API_ROOT = "/api/v1";
const history = [];
let chatBusy = false;
let capabilityBusy = false;
let plannerBusy = false;
let hardwareEvidenceReady = false;

const elements = {
  apiBadge: document.querySelector("#api-badge"),
  aiBadge: document.querySelector("#ai-badge"),
  banner: document.querySelector("#global-banner"),
  capabilityBadge: document.querySelector("#capability-badge"),
  capabilityResults: document.querySelector("#capability-results"),
  chatForm: document.querySelector("#chat-form"),
  chatHint: document.querySelector("#chat-hint"),
  chatInput: document.querySelector("#chat-input"),
  chatMessages: document.querySelector("#chat-messages"),
  diskAnalysisButton: document.querySelector("#disk-analysis-button"),
  hardwareGrid: document.querySelector("#hardware-grid"),
  integrityDetail: document.querySelector("#integrity-detail"),
  integrityValue: document.querySelector("#integrity-value"),
  lastUpdate: document.querySelector("#last-update"),
  modeDetail: document.querySelector("#mode-detail"),
  modeValue: document.querySelector("#mode-value"),
  modelName: document.querySelector("#model-name"),
  networkDetail: document.querySelector("#network-detail"),
  networkValue: document.querySelector("#network-value"),
  plannerBadge: document.querySelector("#planner-badge"),
  plannerButton: document.querySelector("#planner-button"),
  plannerForm: document.querySelector("#planner-form"),
  plannerGoal: document.querySelector("#planner-goal"),
  plannerResults: document.querySelector("#planner-results"),
  refreshButton: document.querySelector("#refresh-button"),
  retentionDetail: document.querySelector("#retention-detail"),
  retentionValue: document.querySelector("#retention-value"),
  sendButton: document.querySelector("#send-button"),
};

function setBadge(element, state, text) {
  element.className = `status-badge ${state}`;
  element.textContent = text;
}

function showBanner(message, warning = false) {
  elements.banner.className = warning ? "banner warning" : "banner";
  elements.banner.textContent = message;
}

function hideBanner() {
  elements.banner.className = "banner hidden";
  elements.banner.textContent = "";
}

async function requestJson(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeout || 5000);
  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(body?.detail || `Error HTTP ${response.status}`);
      error.code = body?.code || "HTTP_ERROR";
      error.detail = body?.detail || body?.title || "";
      throw error;
    }
    return body;
  } finally {
    window.clearTimeout(timeout);
  }
}

function valueOrFallback(value, fallback = "No disponible") {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

function updateSession(data) {
  const mode = data.mode || {};
  const retention = data.retention || {};
  const network = data.network || {};
  const integrity = data.integrity || {};

  elements.modeValue.textContent = valueOrFallback(mode.mode);
  elements.modeDetail.textContent = `Retención solicitada: ${valueOrFallback(
    mode.retention_requested,
    "live",
  )}`;
  elements.retentionValue.textContent = valueOrFallback(retention.effective, "efímera");
  elements.retentionDetail.textContent = valueOrFallback(retention.reason, "RAM overlay");
  elements.networkValue.textContent = valueOrFallback(network.effective, "offline");
  elements.networkDetail.textContent = valueOrFallback(network.reason, "Política local");
  elements.integrityValue.textContent = valueOrFallback(integrity.trust, "sin informe");
  elements.integrityDetail.textContent = valueOrFallback(
    integrity.reason,
    "Estado registrado durante el arranque",
  );
  elements.lastUpdate.textContent = `Actualizado ${new Date().toLocaleTimeString("es-MX")}`;
  hardwareEvidenceReady = Array.isArray(
    data.hardware?.probes?.storage?.data?.blockdevices,
  );
  renderHardware(data.hardware);
}

function lscpuValue(probe, field) {
  const entries = probe?.data?.lscpu;
  if (!Array.isArray(entries)) {
    return undefined;
  }
  const match = entries.find((entry) => entry?.field?.replace(":", "") === field);
  return match?.data;
}

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return undefined;
  }
  const gibibytes = value / 1024 ** 3;
  return `${gibibytes.toLocaleString("es-MX", { maximumFractionDigits: 1 })} GiB`;
}

function firstDisplayController(probe) {
  const lines = probe?.data;
  if (!Array.isArray(lines)) {
    return undefined;
  }
  return lines.find((line) => /VGA|3D controller|Display/i.test(line));
}

function batterySummary(probe) {
  const supplies = probe?.data;
  if (!Array.isArray(supplies)) {
    return undefined;
  }
  const battery = supplies.find((item) => item?.type === "Battery");
  if (!battery) {
    return undefined;
  }
  const capacity = battery.capacity ? `${battery.capacity}%` : "";
  return [battery.status, capacity].filter(Boolean).join(" · ");
}

function compactHardware(hardware) {
  if (!hardware || typeof hardware !== "object") {
    return [];
  }

  const probes = hardware.probes || {};
  const dmi = probes.dmi?.data || {};
  const memory = probes.memory?.data?.meminfo || {};
  const storage = probes.storage?.data?.blockdevices;
  const links = probes.network_links?.data;
  const candidates = [
    ["Equipo", dmi.product_name],
    ["Fabricante", dmi.sys_vendor],
    ["CPU", lscpuValue(probes.cpu, "Model name")],
    ["Hilos", lscpuValue(probes.cpu, "CPU(s)")],
    ["Memoria", formatBytes(memory.MemTotal)],
    ["GPU", firstDisplayController(probes.pci)],
    ["Discos", Array.isArray(storage) ? storage.length : undefined],
    ["Interfaces", Array.isArray(links) ? links.length : undefined],
    ["Firmware", hardware.boot?.firmware],
    ["Batería", batterySummary(probes.power)],
  ];
  return candidates.filter(([, value]) => value !== undefined && value !== null && value !== "");
}

function renderHardware(hardware) {
  const items = compactHardware(hardware);
  elements.hardwareGrid.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent =
      "El inventario todavía no está disponible o no contiene el resumen esperado.";
    elements.hardwareGrid.append(empty);
    return;
  }

  for (const [label, value] of items) {
    const card = document.createElement("article");
    const name = document.createElement("span");
    const detail = document.createElement("strong");
    card.className = "hardware-item";
    name.textContent = label;
    detail.textContent = valueOrFallback(value);
    card.append(name, detail);
    elements.hardwareGrid.append(card);
  }
}

function setChatAvailability(ready, hint) {
  const enabled = ready && !chatBusy;
  elements.chatInput.disabled = !enabled;
  elements.sendButton.disabled = !enabled;
  elements.chatHint.textContent = hint;
}

async function refreshAI() {
  try {
    const status = await requestJson("/ai/status", { timeout: 3000 });
    elements.modelName.textContent = valueOrFallback(status.model);
    if (status.status === "ready") {
      setBadge(elements.aiBadge, "ready", "IA local preparada");
      setChatAvailability(true, "El texto se procesa dentro de este equipo.");
      return;
    }
    if (status.status === "model_missing") {
      setBadge(elements.aiBadge, "warning", "Modelo no instalado");
      setChatAvailability(
        false,
        "El runtime funciona, pero la imagen aún no contiene el modelo configurado.",
      );
      return;
    }
    setBadge(elements.aiBadge, "degraded", "Runtime no instalado");
    setChatAvailability(
      false,
      "La interfaz está operativa; falta incorporar el runtime y su modelo offline.",
    );
  } catch {
    setBadge(elements.aiBadge, "error", "IA no disponible");
    elements.modelName.textContent = "Sin conexión local";
    setChatAvailability(false, "No se pudo consultar el servicio local de IA.");
  }
}

function renderDiskAnalysis(execution) {
  elements.capabilityResults.replaceChildren();
  if (execution.status !== "succeeded" || !execution.result) {
    const failed = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("p");
    failed.className = "capability-failure";
    title.textContent = "El análisis no se completó";
    detail.textContent = `Código: ${valueOrFallback(
      execution.error_code,
      "CAPABILITY_FAILED",
    )}`;
    failed.append(title, detail);
    elements.capabilityResults.append(failed);
    return;
  }

  const summary = execution.result.summary || {};
  const metrics = [
    ["Discos", summary.disk_count],
    ["Fijos", summary.fixed_disk_count],
    ["Removibles", summary.removable_disk_count],
    ["Capacidad", formatBytes(summary.total_capacity_bytes)],
    ["Grafo", `rev. ${execution.result.knowledge_graph?.revision || "—"}`],
  ];
  const grid = document.createElement("div");
  grid.className = "capability-metrics";
  for (const [label, value] of metrics) {
    const item = document.createElement("div");
    const name = document.createElement("span");
    const detail = document.createElement("strong");
    name.textContent = label;
    detail.textContent = valueOrFallback(value);
    item.append(name, detail);
    grid.append(item);
  }
  elements.capabilityResults.append(grid);

  const findings = execution.result.findings;
  if (Array.isArray(findings) && findings.length) {
    const list = document.createElement("ul");
    list.className = "finding-list";
    for (const finding of findings) {
      const item = document.createElement("li");
      const code = document.createElement("span");
      const message = document.createElement("p");
      code.textContent = valueOrFallback(finding.code, "OBSERVATION");
      message.textContent = valueOrFallback(finding.message);
      item.append(code, message);
      list.append(item);
    }
    elements.capabilityResults.append(list);
  }
}

function renderPlan(plan) {
  elements.plannerResults.replaceChildren();
  const summary = document.createElement("div");
  const title = document.createElement("strong");
  const detail = document.createElement("p");
  summary.className = `planner-summary ${plan.status || "stopped"}`;

  if (plan.status === "needs_evidence") {
    title.textContent = "Falta evidencia";
    const evidence = Array.isArray(plan.requested_evidence)
      ? plan.requested_evidence.join(", ")
      : "evidencia no especificada";
    detail.textContent = `El plan no se puede completar todavía: ${evidence}.`;
    summary.append(title, detail);
    elements.plannerResults.append(summary);
    return;
  }

  if (plan.status !== "ready") {
    title.textContent = "Plan detenido";
    detail.textContent = valueOrFallback(
      plan.stop_reason,
      "Ninguna Capability instalada coincide con el objetivo.",
    );
    summary.append(title, detail);
    elements.plannerResults.append(summary);
    return;
  }

  title.textContent = "Plan listo para revisión";
  detail.textContent = `Plan ${valueOrFallback(plan.id).slice(0, 12)} · No ejecutado`;
  summary.append(title, detail);
  elements.plannerResults.append(summary);

  const steps = document.createElement("ol");
  steps.className = "planner-steps";
  for (const step of plan.steps || []) {
    const item = document.createElement("li");
    const heading = document.createElement("strong");
    const metadata = document.createElement("span");
    const rationale = document.createElement("p");
    heading.textContent = `${valueOrFallback(step.capability_id)} @ ${valueOrFallback(
      step.version,
    )}`;
    metadata.textContent = `${valueOrFallback(
      step.operation_class,
    ).toUpperCase()} · RIESGO ${valueOrFallback(step.risk_level).toUpperCase()}`;
    rationale.textContent = valueOrFallback(step.rationale);
    item.append(heading, metadata, rationale);
    steps.append(item);
  }
  elements.plannerResults.append(steps);
}

async function refreshCapabilities() {
  try {
    const catalog = await requestJson("/capabilities?category=storage", {
      timeout: 3000,
    });
    const installed = catalog.capabilities?.some(
      (capability) => capability.id === "storage.disk-analysis",
    );
    if (installed) {
      setBadge(elements.capabilityBadge, "ready", "Disk Analysis instalada");
      elements.diskAnalysisButton.disabled = capabilityBusy;
      setBadge(elements.plannerBadge, "ready", "Planner disponible");
      elements.plannerButton.disabled = plannerBusy;
      return;
    }
    setBadge(elements.capabilityBadge, "warning", "Capability no instalada");
    elements.diskAnalysisButton.disabled = true;
    setBadge(elements.plannerBadge, "warning", "Sin Capabilities");
    elements.plannerButton.disabled = true;
  } catch {
    setBadge(elements.capabilityBadge, "error", "Catálogo no disponible");
    elements.diskAnalysisButton.disabled = true;
    setBadge(elements.plannerBadge, "error", "Planner no disponible");
    elements.plannerButton.disabled = true;
  }
}

async function buildPlan(event) {
  event.preventDefault();
  const goal = elements.plannerGoal.value.trim();
  if (!goal || plannerBusy || elements.plannerButton.disabled) {
    return;
  }

  plannerBusy = true;
  elements.plannerButton.disabled = true;
  elements.plannerButton.textContent = "Planificando…";
  setBadge(elements.plannerBadge, "pending", "Evaluando objetivo");
  const evidence = hardwareEvidenceReady
    ? [{ id: "hardware.block-devices", confidence: 1 }]
    : [];
  try {
    const plan = await requestJson("/planner/plan", {
      method: "POST",
      body: JSON.stringify({ goal, evidence }),
      timeout: 5000,
    });
    renderPlan(plan);
    const state = plan.status === "ready" ? "ready" : "warning";
    const label = plan.status === "ready" ? "Plan listo" : "Plan no ejecutable";
    setBadge(elements.plannerBadge, state, label);
  } catch {
    setBadge(elements.plannerBadge, "error", "Planner no disponible");
    showBanner("El Planner local no pudo evaluar el objetivo.", true);
  } finally {
    plannerBusy = false;
    elements.plannerButton.textContent = "Generar plan";
    await refreshCapabilities();
  }
}

async function runDiskAnalysis() {
  if (capabilityBusy || elements.diskAnalysisButton.disabled) {
    return;
  }
  capabilityBusy = true;
  elements.diskAnalysisButton.disabled = true;
  elements.diskAnalysisButton.textContent = "Analizando…";
  setBadge(elements.capabilityBadge, "pending", "Workflow en curso");
  try {
    const execution = await requestJson(
      "/capabilities/storage.disk-analysis/executions",
      {
        method: "POST",
        body: JSON.stringify({ scope: "all_detected" }),
        timeout: 15000,
      },
    );
    renderDiskAnalysis(execution);
    setBadge(
      elements.capabilityBadge,
      execution.status === "succeeded" ? "ready" : "warning",
      execution.status === "succeeded" ? "Análisis completado" : "Análisis incompleto",
    );
  } catch {
    setBadge(elements.capabilityBadge, "error", "Capability no disponible");
    showBanner(
      "Disk Analysis no pudo iniciar. El inventario de hardware puede no estar listo.",
      true,
    );
  } finally {
    capabilityBusy = false;
    elements.diskAnalysisButton.textContent = "Analizar discos";
    await refreshCapabilities();
  }
}

async function refreshSystem() {
  elements.refreshButton.disabled = true;
  hideBanner();
  try {
    await requestJson("/health/ready", { timeout: 3000 });
    setBadge(elements.apiBadge, "ready", "Sistema operativo");
    const overview = await requestJson("/system/overview", { timeout: 5000 });
    updateSession(overview);
  } catch {
    setBadge(elements.apiBadge, "error", "Backend no disponible");
    showBanner(
      "La interfaz gráfica está activa, pero el backend local no respondió. " +
        "El registro de inicio se encuentra en /run/user/1000/ares-kiosk.log.",
      true,
    );
  } finally {
    elements.refreshButton.disabled = false;
  }
  await Promise.all([refreshAI(), refreshCapabilities()]);
}

function appendMessage(author, content, type) {
  const wrapper = document.createElement("div");
  const label = document.createElement("span");
  const text = document.createElement("p");
  wrapper.className = `message ${type}-message`;
  label.className = "message-author";
  label.textContent = author;
  text.textContent = content;
  wrapper.append(label, text);
  elements.chatMessages.append(wrapper);
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
  return wrapper;
}

async function sendChat(event) {
  event.preventDefault();
  const content = elements.chatInput.value.trim();
  if (!content || elements.chatInput.disabled) {
    return;
  }

  appendMessage("TÚ", content, "user");
  history.push({ role: "user", content });
  elements.chatInput.value = "";
  chatBusy = true;
  setChatAvailability(false, "ARES está generando una respuesta local…");
  const pending = appendMessage("ARES", "Analizando…", "assistant");

  try {
    const response = await requestJson("/assistant/chat", {
      method: "POST",
      body: JSON.stringify({ messages: history.slice(-12) }),
      timeout: 130000,
    });
    pending.querySelector("p").textContent = response.content;
    history.push({ role: "assistant", content: response.content });
    chatBusy = false;
    setChatAvailability(true, "El texto se procesa dentro de este equipo.");
  } catch (error) {
    const messages = {
      AI_MODEL_MISSING: "El modelo configurado no está instalado en esta imagen.",
      AI_RUNTIME_UNAVAILABLE:
        "El runtime local no respondió. Espera unos segundos y pulsa Actualizar; si persiste, revisa /run/ares/api/ai-selftest.json.",
      AI_INVALID_RESPONSE:
        "El modelo respondió, pero ARES descartó la respuesta por seguridad.",
    };
    pending.querySelector("p").textContent =
      messages[error.code] ||
      `La IA local no pudo responder.${error.detail ? ` Detalle: ${error.detail}` : ""}`;
    chatBusy = false;
    await refreshAI();
  }
}

elements.refreshButton.addEventListener("click", refreshSystem);
elements.chatForm.addEventListener("submit", sendChat);
elements.plannerForm.addEventListener("submit", buildPlan);
elements.diskAnalysisButton.addEventListener("click", runDiskAnalysis);
window.addEventListener("DOMContentLoaded", refreshSystem);
window.setInterval(refreshSystem, 30000);
