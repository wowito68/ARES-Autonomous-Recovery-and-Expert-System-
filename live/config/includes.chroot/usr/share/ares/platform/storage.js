"use strict";

const STORAGE_API_ROOT = "/api/v1";
let storageBusy = false;

const storageElements = {
  badge: document.querySelector("#capability-badge"),
  button: document.querySelector("#disk-analysis-button"),
  results: document.querySelector("#capability-results"),
};

async function storageRequestJson(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeout || 20000);
  try {
    const response = await fetch(`${STORAGE_API_ROOT}${path}`, {
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
      throw error;
    }
    return body;
  } finally {
    window.clearTimeout(timeout);
  }
}

function storageFormatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "No disponible";
  }
  const gibibytes = value / 1024 ** 3;
  return `${gibibytes.toLocaleString("es-MX", { maximumFractionDigits: 1 })} GiB`;
}

function storageText(value, fallback = "No disponible") {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

function storageSetBadge(state, text) {
  if (!storageElements.badge) {
    return;
  }
  storageElements.badge.className = `status-badge ${state}`;
  storageElements.badge.textContent = text;
}

function storageMetricGrid(items) {
  const grid = document.createElement("div");
  grid.className = "capability-metrics";
  for (const [label, value] of items) {
    const item = document.createElement("div");
    const name = document.createElement("span");
    const detail = document.createElement("strong");
    name.textContent = label;
    detail.textContent = storageText(value);
    item.append(name, detail);
    grid.append(item);
  }
  return grid;
}

function storageSection(titleText, entries) {
  const section = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = titleText;
  section.append(title);
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Sin datos reportados.";
    section.append(empty);
    return section;
  }
  const list = document.createElement("ul");
  list.className = "finding-list";
  for (const [codeText, messageText] of entries) {
    const item = document.createElement("li");
    const code = document.createElement("span");
    const message = document.createElement("p");
    code.textContent = codeText;
    message.textContent = messageText;
    item.append(code, message);
    list.append(item);
  }
  section.append(list);
  return section;
}

function storageRenderPending() {
  storageElements.results?.replaceChildren();
  if (!storageElements.results) {
    return;
  }
  const pending = document.createElement("p");
  pending.className = "empty-state";
  pending.textContent = "Analizando almacenamiento…";
  storageElements.results.append(pending);
}

function storageRenderFailure(error) {
  storageElements.results?.replaceChildren();
  if (!storageElements.results) {
    return;
  }
  const failed = document.createElement("div");
  failed.className = "capability-failure";
  const title = document.createElement("strong");
  const detail = document.createElement("p");
  title.textContent = "El análisis de almacenamiento no se completó";
  detail.textContent = `Código: ${storageText(error.code, "STORAGE_ANALYSIS_FAILED")}`;
  failed.append(title, detail);
  storageElements.results.append(failed);
}

function storageDiskEntries(snapshot) {
  const partitions = Array.isArray(snapshot.partitions) ? snapshot.partitions : [];
  const filesystems = Array.isArray(snapshot.filesystems) ? snapshot.filesystems : [];
  const mounts = Array.isArray(snapshot.mounts) ? snapshot.mounts : [];
  const smart = Array.isArray(snapshot.smart) ? snapshot.smart : [];
  return (snapshot.disks || []).map((disk) => {
    const diskPartitions = partitions.filter((partition) => partition.disk_id === disk.id);
    const smartState = smart.find((item) => item.disk_id === disk.id);
    const partitionDetails = diskPartitions.map((partition) => {
      const filesystem = filesystems.find((item) => item.id === partition.filesystem_id);
      const partitionMounts = mounts.filter((item) =>
        (partition.mount_point_ids || []).includes(item.id),
      );
      const mountText = partitionMounts.length
        ? partitionMounts
            .map((mount) => {
              const usage =
                typeof mount.used_percent === "number" ? ` · ${mount.used_percent}% usado` : "";
              return `${mount.path}${usage}`;
            })
            .join(", ")
        : "sin montaje reportado";
      return `${partition.path} · ${storageFormatBytes(partition.size_bytes)} · ${storageText(
        filesystem?.filesystem_type,
        "filesystem desconocido",
      )} · ${mountText}`;
    });
    const model = [disk.vendor, disk.model].filter(Boolean).join(" ") || "modelo no reportado";
    const smartText = smartState
      ? `${storageText(smartState.status)}${smartState.reason ? ` (${smartState.reason})` : ""}`
      : "no reportado";
    const details = [
      `${model} · ${storageFormatBytes(disk.size_bytes)} · SMART: ${smartText}`,
      ...partitionDetails,
    ].join("\n");
    return [storageText(disk.name, disk.path), details];
  });
}

function storageRender(analysis, snapshot) {
  storageElements.results?.replaceChildren();
  if (!storageElements.results) {
    return;
  }
  const diagnostic = analysis.diagnostic || {};
  const summary = snapshot.summary || {};
  storageElements.results.append(
    storageMetricGrid([
      ["Estado", diagnostic.severity],
      [
        "Confianza",
        typeof diagnostic.confidence === "number"
          ? `${Math.round(diagnostic.confidence * 100)}%`
          : undefined,
      ],
      ["Discos", summary.disk_count],
      ["Particiones", summary.partition_count],
      ["Filesystems", summary.filesystem_count],
      ["Capacidad", storageFormatBytes(summary.total_capacity_bytes)],
    ]),
  );

  storageElements.results.append(storageSection("Discos", storageDiskEntries(snapshot)));

  const osEntries = (snapshot.operating_systems || []).map((item) => [
    storageText(item.name, "Sistema operativo"),
    `${storageText(item.version, "versión no reportada")} · ${storageText(
      item.mountpoint,
      "montaje no reportado",
    )}`,
  ]);
  storageElements.results.append(storageSection("Sistemas operativos detectados", osEntries));

  const findings = (diagnostic.findings || []).map((item) => [
    storageText(item.type, "finding"),
    storageText(item.message),
  ]);
  storageElements.results.append(storageSection("Hallazgos", findings));

  const evidence = (diagnostic.evidence || []).map((item) => [
    storageText(item.resource, "resource"),
    storageText(item.observation),
  ]);
  storageElements.results.append(storageSection("Evidencia", evidence));

  const recommendations = (diagnostic.recommendations || []).map((item) => [
    `Prioridad ${storageText(item.priority)}`,
    storageText(item.message),
  ]);
  storageElements.results.append(storageSection("Recomendaciones", recommendations));

  const warnings = [
    ...(snapshot.warnings || []),
    ...(diagnostic.warnings || []),
    ...(diagnostic.limitations || []),
  ].map((message, index) => [`W${index + 1}`, storageText(message)]);
  storageElements.results.append(storageSection("Warnings y limitaciones", warnings));
}

async function storageAnalyze(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
  if (storageBusy || !storageElements.button || storageElements.button.disabled) {
    return;
  }
  storageBusy = true;
  storageElements.button.disabled = true;
  storageElements.button.textContent = "Analizando…";
  storageSetBadge("pending", "Analizando almacenamiento");
  storageRenderPending();
  try {
    const analysis = await storageRequestJson("/storage/analyze", {
      method: "POST",
      body: JSON.stringify({}),
      timeout: 20000,
    });
    const snapshot = await storageRequestJson(
      `/storage/snapshots/${encodeURIComponent(analysis.snapshot_id)}`,
      { timeout: 5000 },
    );
    storageRender(analysis, snapshot);
    storageSetBadge(
      analysis.diagnostic?.severity === "error" || analysis.diagnostic?.severity === "critical"
        ? "warning"
        : "ready",
      "Análisis completado",
    );
  } catch (error) {
    storageRenderFailure(error);
    storageSetBadge("error", "Storage no disponible");
  } finally {
    storageBusy = false;
    storageElements.button.disabled = false;
    storageElements.button.textContent = "Analizar almacenamiento";
  }
}

if (storageElements.button) {
  storageElements.button.addEventListener("click", storageAnalyze, { capture: true });
}
