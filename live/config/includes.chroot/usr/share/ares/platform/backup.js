(() => {
  "use strict";

  const api = "/api/v1/backups";
  let currentPlan = null;
  let currentBackupId = null;
  let pollTimer = null;

  function text(value) {
    return value === null || value === undefined || value === "" ? "unknown" : String(value);
  }

  function bytes(value) {
    if (!Number.isFinite(value)) return "unknown";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let amount = Number(value);
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function eta(value) {
    if (!Number.isFinite(value)) return "unknown";
    const seconds = Math.max(0, Math.round(value));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function ensureSection() {
    let section = document.getElementById("backups-section");
    if (section) return section;

    section = element("section", "panel");
    section.id = "backups-section";
    section.dataset.aresSection = "backups";
    section.append(element("h2", "", "Backups"));
    section.append(
      element(
        "p",
        "muted",
        "Planifica, autoriza y verifica respaldos locales. ARES nunca inicia una copia antes de mostrar el plan."
      )
    );

    const form = element("form", "backup-form");
    form.id = "backup-plan-form";
    const sourceLabel = element("label", "", "Origen");
    const source = element("input", "");
    source.id = "backup-source";
    source.name = "source";
    source.required = true;
    source.placeholder = "/mnt/windows/Users/.../Documents";
    source.autocomplete = "off";
    sourceLabel.append(source);

    const destinationLabel = element("label", "", "Destino montado");
    const destination = element("input", "");
    destination.id = "backup-destination";
    destination.name = "destination";
    destination.required = true;
    destination.placeholder = "/mnt/backup";
    destination.autocomplete = "off";
    destinationLabel.append(destination);

    const analyze = element("button", "", "Analizar y generar plan");
    analyze.type = "submit";
    analyze.id = "backup-analyze";
    form.append(sourceLabel, destinationLabel, analyze);

    const plan = element("div", "backup-plan");
    plan.id = "backup-plan";
    plan.hidden = true;
    const execute = element("button", "", "Solicitar autorización y ejecutar");
    execute.type = "button";
    execute.id = "backup-execute";
    execute.disabled = true;
    const status = element("div", "backup-status");
    status.id = "backup-status";
    const list = element("div", "backup-list");
    list.id = "backup-list";

    section.append(form, plan, execute, status, element("h3", "", "Backups existentes"), list);

    const main = document.querySelector("main") || document.body;
    main.append(section);

    const nav = document.querySelector("nav");
    if (nav && !document.querySelector('[href="#backups-section"]')) {
      const link = element("a", "", "Backups");
      link.href = "#backups-section";
      nav.append(link);
    }
    return section;
  }

  function row(label, value) {
    const container = element("div", "backup-row");
    container.append(element("strong", "", `${label}: `), element("span", "", value));
    return container;
  }

  function renderPlan(plan) {
    const target = document.getElementById("backup-plan");
    target.replaceChildren();
    target.append(element("h3", "", "BACKUP PLAN"));
    target.append(row("Source", plan.source.path));
    target.append(row("Destination", plan.destination.backup_path));
    target.append(row("Estimated size", bytes(plan.source.estimated_size_bytes)));
    target.append(row("Available", bytes(plan.destination.available_bytes)));
    target.append(row("Required", bytes(plan.required_bytes)));
    target.append(row("Files", text(plan.included_file_count)));
    target.append(row("Directories", text(plan.included_directory_count)));
    target.append(row("Estimated duration", "unknown"));
    target.append(row("Risk", text(plan.risk)));
    target.append(row("Overwrite", plan.policy.overwrite ? "YES" : "No"));
    target.append(row("Verification", text(plan.policy.checksum_algorithm).toUpperCase()));
    target.append(row("Authorization required", plan.authorization_required ? "YES" : "No"));
    target.append(row("Plan fingerprint", text(plan.fingerprint_sha256)));

    const exclusions = element("div", "backup-exclusions");
    exclusions.append(element("strong", "", "Excluded data:"));
    if (plan.exclusions.length === 0) {
      exclusions.append(element("p", "", "None detected by the current policy."));
    } else {
      const ul = element("ul", "");
      for (const item of plan.exclusions) {
        ul.append(element("li", "", `${item.relative_path} — ${item.reason}`));
      }
      exclusions.append(ul);
    }
    target.append(exclusions);
    target.hidden = false;
    document.getElementById("backup-execute").disabled = false;
  }

  function renderProgress(backup) {
    const target = document.getElementById("backup-status");
    target.replaceChildren();
    const progress = backup.execution?.progress || {};
    target.append(element("h3", "", `Backup ${backup.id}`));
    target.append(row("Status", backup.status));
    target.append(
      row(
        "Files",
        `${text(progress.files_completed)} / ${text(progress.files_total)}`
      )
    );
    target.append(
      row(
        "Data",
        `${bytes(progress.bytes_completed)} / ${bytes(progress.bytes_total)}`
      )
    );
    target.append(
      row(
        "Progress",
        Number.isFinite(progress.percent) ? `${Number(progress.percent).toFixed(1)}%` : "unknown"
      )
    );
    target.append(row("Speed", Number.isFinite(progress.speed_bytes_per_second) ? `${bytes(progress.speed_bytes_per_second)}/s` : "unknown"));
    target.append(row("ETA", eta(progress.eta_seconds)));
    target.append(row("Verification", text(backup.verification_status)));
    target.append(row("Integrity", backup.verification_status === "VERIFIED" ? "verified" : "not verified"));
    if (backup.execution?.authorization_challenge_id) {
      target.append(
        row(
          "Authorization",
          `Pending local consent: ares consent approve ${backup.execution.authorization_challenge_id}`
        )
      );
    }
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const problem = await response.json();
        detail = problem.code ? `${problem.code}: ${problem.detail || problem.title}` : detail;
      } catch (_) {
        // Keep the HTTP status when Problem Details is unavailable.
      }
      throw new Error(detail);
    }
    return response.json();
  }

  async function generatePlan(event) {
    event.preventDefault();
    const planTarget = document.getElementById("backup-plan");
    const statusTarget = document.getElementById("backup-status");
    currentPlan = null;
    currentBackupId = null;
    document.getElementById("backup-execute").disabled = true;
    planTarget.hidden = true;
    statusTarget.textContent = "Analizando origen, destino y espacio disponible…";
    try {
      const plan = await requestJson(`${api}/plan`, {
        method: "POST",
        body: JSON.stringify({
          source: document.getElementById("backup-source").value,
          destination: document.getElementById("backup-destination").value,
        }),
      });
      currentPlan = plan;
      statusTarget.textContent = "Plan listo. Revísalo antes de solicitar autorización.";
      renderPlan(plan);
    } catch (error) {
      statusTarget.textContent = `No se pudo generar el plan: ${error.message}`;
    }
  }

  async function executePlan() {
    if (!currentPlan) return;
    const button = document.getElementById("backup-execute");
    button.disabled = true;
    const statusTarget = document.getElementById("backup-status");
    statusTarget.textContent = "Solicitando autorización independiente…";
    try {
      const accepted = await requestJson(api, {
        method: "POST",
        body: JSON.stringify({
          plan_id: currentPlan.id,
          request_authorization: true,
        }),
      });
      currentBackupId = accepted.backup.id;
      renderProgress(accepted.backup);
      pollBackup();
    } catch (error) {
      statusTarget.textContent = `No se inició el backup: ${error.message}`;
      button.disabled = false;
    }
  }

  async function pollBackup() {
    if (!currentBackupId) return;
    try {
      const backup = await requestJson(`${api}/${encodeURIComponent(currentBackupId)}`);
      renderProgress(backup);
      if (["COMPLETED", "FAILED", "CANCELLED", "CORRUPTED"].includes(backup.status)) {
        currentBackupId = null;
        await loadBackups();
        return;
      }
    } catch (error) {
      document.getElementById("backup-status").textContent = `Error consultando progreso: ${error.message}`;
      return;
    }
    pollTimer = window.setTimeout(pollBackup, 1000);
  }

  async function loadBackups() {
    const target = document.getElementById("backup-list");
    if (!target) return;
    target.textContent = "Cargando…";
    try {
      const data = await requestJson(api);
      target.replaceChildren();
      if (!data.backups.length) {
        target.textContent = "No hay backups registrados.";
        return;
      }
      for (const backup of data.backups) {
        const card = element("article", "backup-card");
        card.append(element("strong", "", backup.id));
        card.append(row("Source", backup.source.path));
        card.append(row("Destination", backup.destination.backup_path));
        card.append(row("Size", bytes(backup.size)));
        card.append(row("Created", text(backup.created_at)));
        card.append(row("Status", backup.status));
        card.append(row("Verification", backup.verification_status));
        card.append(row("Integrity", backup.verification_status === "VERIFIED" ? "verified" : "not verified"));
        target.append(card);
      }
    } catch (error) {
      target.textContent = `No se pudieron listar los backups: ${error.message}`;
    }
  }

  function init() {
    ensureSection();
    document.getElementById("backup-plan-form").addEventListener("submit", generatePlan);
    document.getElementById("backup-execute").addEventListener("click", executePlan);
    loadBackups();
  }

  window.addEventListener("beforeunload", () => {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
