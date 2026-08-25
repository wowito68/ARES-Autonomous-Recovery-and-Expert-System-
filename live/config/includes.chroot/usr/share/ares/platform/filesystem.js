(() => {
  "use strict";

  const api = "/api/v1/filesystems";
  const sessionId = `ui-${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
  let currentInspection = null;
  let currentPlan = null;
  let currentRepairId = null;
  let pollTimer = null;

  function text(value) {
    return value === null || value === undefined || value === "" ? "unknown" : String(value);
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function row(label, value) {
    const container = element("div", "filesystem-row");
    container.append(element("strong", "", `${label}: `), element("span", "", text(value)));
    return container;
  }

  function ensureSection() {
    let section = document.getElementById("filesystems-section");
    if (section) return section;

    section = element("section", "section-block auxiliary-section");
    section.id = "filesystems-section";
    section.dataset.aresSection = "filesystems";
    section.append(element("h2", "", "Filesystems"));
    section.append(
      element(
        "p",
        "muted",
        "Inspecta y planifica reparaciones. Una reparación de filesystem es de riesgo alto y requiere checkpoint verificado y consentimiento local independiente."
      )
    );

    const form = element("form", "filesystem-form");
    form.id = "filesystem-inspect-form";

    const resourceLabel = element("label", "", "Filesystem detectado por ARES");
    const resource = element("select", "");
    resource.id = "filesystem-resource";
    resourceLabel.append(resource);

    const targetLabel = element("label", "", "Dispositivo exacto");
    const target = element("input", "");
    target.id = "filesystem-device";
    target.required = true;
    target.autocomplete = "off";
    target.placeholder = "/dev/nvme0n1p3";
    targetLabel.append(target);

    const backupLabel = element("label", "", "Backup verificado del filesystem completo");
    const backup = element("input", "");
    backup.id = "filesystem-backup-id";
    backup.autocomplete = "off";
    backup.placeholder = "backup id — requerido para habilitar reparación";
    backupLabel.append(backup);

    const inspectButton = element("button", "", "Inspect");
    inspectButton.type = "submit";
    const planButton = element("button", "", "Generar Repair Plan");
    planButton.type = "button";
    planButton.id = "filesystem-plan-button";
    planButton.disabled = true;
    form.append(resourceLabel, targetLabel, backupLabel, inspectButton, planButton);

    const inspection = element("div", "filesystem-inspection");
    inspection.id = "filesystem-inspection";
    const plan = element("div", "filesystem-plan");
    plan.id = "filesystem-plan";
    plan.hidden = true;
    const execute = element("button", "", "Solicitar autorización de reparación");
    execute.type = "button";
    execute.id = "filesystem-execute";
    execute.disabled = true;
    const status = element("div", "filesystem-status");
    status.id = "filesystem-status";

    section.append(form, inspection, plan, execute, status);
    const main = document.querySelector("main") || document.body;
    main.append(section);

    const nav = document.querySelector("nav");
    if (nav && !document.querySelector('[href="#filesystems-section"]')) {
      const link = element("a", "nav-link", "Filesystems");
      link.href = "#filesystems-section";
      nav.append(link);
    }
    wireResourceSelector();
    return section;
  }

  async function wireResourceSelector() {
    if (!window.AresResources) return;
    try {
      await window.AresResources.fillSelect("filesystem-resource", {
        kinds: ["filesystem", "partition"],
        blankLabel: "Selecciona filesystem o usa dispositivo manual",
      });
      window.AresResources.bindPath("filesystem-resource", "filesystem-device");
    } catch (_) {
      // El campo técnico se conserva como respaldo offline/manual.
    }
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json", "X-ARES-Session-ID": sessionId, ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const problem = await response.json();
        detail = problem.code ? `${problem.code}: ${problem.detail || problem.title}` : detail;
      } catch (_) {
        // Keep HTTP status if Problem Details is unavailable.
      }
      throw new Error(detail);
    }
    return response.json();
  }

  function renderInspection(value) {
    const target = document.getElementById("filesystem-inspection");
    target.replaceChildren();
    target.append(element("h3", "", "Inspection"));
    target.append(row("Device", value.identity.canonical_path));
    target.append(row("Filesystem", value.filesystem));
    target.append(row("UUID", value.identity.filesystem_uuid));
    target.append(row("PARTUUID", value.identity.partuuid));
    target.append(row("Major/minor", value.identity.major_minor));
    target.append(row("Identity fingerprint", value.identity.fingerprint_sha256));
    target.append(row("Mounted", value.mount.mounted ? "YES" : "No"));
    target.append(
      row(
        "Mount point",
        value.mount.mounts && value.mount.mounts.length ? value.mount.mounts[0].mount_point : "none"
      )
    );
    target.append(row("Busy", value.mount.busy ? "YES" : "No"));
    target.append(row("Swap", value.mount.swap ? "YES" : "No"));
    target.append(row("Health", value.health));
    target.append(row("Repair supported", value.repair_supported ? "YES" : "No"));
    if (value.check) {
      target.append(row("Check tool", value.check.tool));
      target.append(row("Check exit code", value.check.exit_code));
      target.append(row("Detected problems", value.check.problems.join(", ") || "none"));
    }
    if (value.limitations && value.limitations.length) {
      const list = element("ul", "");
      for (const limitation of value.limitations) list.append(element("li", "", limitation));
      target.append(element("strong", "", "Limitations"), list);
    }
  }

  function renderPlan(value) {
    const target = document.getElementById("filesystem-plan");
    target.replaceChildren();
    target.append(element("h3", "", "FILESYSTEM REPAIR PLAN"));
    target.append(row("Plan ID", value.id));
    target.append(row("Repair ID", value.repair_id));
    target.append(row("Target", value.target.canonical_path));
    target.append(row("Target fingerprint", value.target.fingerprint_sha256));
    target.append(row("Filesystem", value.filesystem));
    target.append(row("Risk", value.risk));
    target.append(row("Mounted", value.mount.mounted ? "YES" : "No"));
    target.append(
      row(
        "Checkpoint",
        value.protection_checkpoint ? `${value.protection_checkpoint.id} / ${value.protection_checkpoint.status}` : "MISSING"
      )
    );
    target.append(row("Authorization", value.requires_authorization ? "Required" : "No"));
    target.append(row("Executable", value.executable ? "YES" : "BLOCKED"));
    target.append(row("Estimated duration", value.estimated_duration_seconds ?? "unknown"));
    target.append(row("Rollback", value.rollback_strategy));

    const actions = element("ol", "");
    for (const action of value.repair_actions || []) {
      actions.append(
        element(
          "li",
          "",
          `${action.id}: ${action.description}${action.mutates_target ? " [MUTATES]" : ""}`
        )
      );
    }
    target.append(element("strong", "", "Actions"), actions);

    const limitations = element("ul", "");
    for (const limitation of value.limitations || []) {
      limitations.append(element("li", "", limitation));
    }
    target.append(element("strong", "", "Limitations"), limitations);
    target.hidden = false;
    document.getElementById("filesystem-execute").disabled = value.executable !== true;
  }

  function renderRepair(record) {
    const target = document.getElementById("filesystem-status");
    target.replaceChildren();
    target.append(element("h3", "", `Repair ${record.id}`));
    target.append(row("Status", record.execution.status));
    target.append(row("Checkpoint", record.execution.checkpoint_id));
    target.append(row("Tool", record.execution.tool));
    target.append(row("Error", record.execution.error_code));
    if (record.execution.authorization_challenge_id) {
      target.append(
        row(
          "Authorization",
          `Use trusted local CLI: ares consent approve ${record.execution.authorization_challenge_id}`
        )
      );
    }
    if (record.verification) {
      const before = record.verification.before;
      const after = record.verification.after;
      target.append(element("h4", "", "Before"));
      target.append(row("Health", before ? before.health : "unknown"));
      target.append(
        row("Problems", before && before.problems ? before.problems.join(", ") || "none" : "unknown")
      );
      target.append(element("h4", "", "Action"));
      target.append(row("Repair tool", record.execution.tool));
      target.append(element("h4", "", "After"));
      target.append(row("Health", after ? after.health : "unknown"));
      target.append(row("Verification", record.verification.status));
      target.append(row("Remounted", record.verification.remounted));
      target.append(row("Result", record.verification.message));
    }
  }

  async function inspectFilesystem(event) {
    event.preventDefault();
    currentInspection = null;
    currentPlan = null;
    currentRepairId = null;
    document.getElementById("filesystem-plan-button").disabled = true;
    document.getElementById("filesystem-execute").disabled = true;
    document.getElementById("filesystem-plan").hidden = true;
    const status = document.getElementById("filesystem-status");
    status.textContent = "Inspecting target identity and filesystem health…";
    try {
      currentInspection = await requestJson(`${api}/inspect`, {
        method: "POST",
        body: JSON.stringify({ device: document.getElementById("filesystem-device").value }),
      });
      renderInspection(currentInspection);
      document.getElementById("filesystem-plan-button").disabled = false;
      status.textContent = "Inspection complete. Generate a Repair Plan before any mutation.";
    } catch (error) {
      status.textContent = `Inspection failed: ${error.message}`;
    }
  }

  async function generatePlan() {
    if (!currentInspection) return;
    const status = document.getElementById("filesystem-status");
    const backupId = document.getElementById("filesystem-backup-id").value.trim();
    document.getElementById("filesystem-execute").disabled = true;
    status.textContent = "Generating high-risk Repair Plan and validating protection…";
    try {
      currentPlan = await requestJson(`${api}/repair/plan`, {
        method: "POST",
        body: JSON.stringify({
          device: document.getElementById("filesystem-device").value,
          backup_id: backupId || null,
        }),
      });
      renderPlan(currentPlan);
      status.textContent = currentPlan.executable
        ? "Plan is protected and executable. Review every field before requesting authorization."
        : "Plan is BLOCKED. Resolve all listed safety limitations before repair.";
    } catch (error) {
      status.textContent = `Repair Plan failed: ${error.message}`;
    }
  }

  async function executePlan() {
    if (!currentPlan || currentPlan.executable !== true) return;
    const button = document.getElementById("filesystem-execute");
    button.disabled = true;
    const status = document.getElementById("filesystem-status");
    status.textContent = "Requesting independent high-risk authorization…";
    try {
      const record = await requestJson(`${api}/repair`, {
        method: "POST",
        body: JSON.stringify({ plan_id: currentPlan.id, request_authorization: true }),
      });
      currentRepairId = record.id;
      renderRepair(record);
      pollRepair();
    } catch (error) {
      status.textContent = `Repair was not started: ${error.message}`;
      button.disabled = false;
    }
  }

  async function pollRepair() {
    if (!currentRepairId) return;
    try {
      const record = await requestJson(
        `${api}/repairs/${encodeURIComponent(currentRepairId)}`
      );
      renderRepair(record);
      if (["COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "ABORTED"].includes(record.execution.status)) {
        currentRepairId = null;
        return;
      }
    } catch (error) {
      document.getElementById("filesystem-status").textContent =
        `Error reading repair state: ${error.message}`;
      return;
    }
    pollTimer = window.setTimeout(pollRepair, 1000);
  }

  function init() {
    ensureSection();
    document.getElementById("filesystem-inspect-form").addEventListener("submit", inspectFilesystem);
    document.getElementById("filesystem-plan-button").addEventListener("click", generatePlan);
    document.getElementById("filesystem-execute").addEventListener("click", executePlan);
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
