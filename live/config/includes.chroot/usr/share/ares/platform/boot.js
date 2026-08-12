(() => {
  "use strict";

  const api = "/api/v1/boot";
  const sessionId = `ui-${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
  let diagnostic = null;
  let plan = null;
  let repairId = null;
  let pollTimer = null;

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function text(value) {
    return value === null || value === undefined || value === "" ? "unknown" : String(value);
  }

  function row(label, value) {
    const node = element("div", "boot-row");
    node.append(element("strong", "", `${label}: `), element("span", "", text(value)));
    return node;
  }

  function ensureSection() {
    let section = document.getElementById("boot-recovery-section");
    if (section) return section;
    section = element("section", "panel");
    section.id = "boot-recovery-section";
    section.dataset.aresSection = "boot-recovery";
    section.append(element("h2", "", "Boot Recovery"));
    section.append(
      element(
        "p",
        "muted",
        "Diagnóstico primero. Las reparaciones GRUB son de riesgo alto, requieren checkpoint, autorización local independiente y verificación posterior."
      )
    );

    const form = element("form", "boot-form");
    form.id = "boot-diagnose-form";
    const diskLabel = element("label", "", "Disco objetivo (opcional si el root identifica el sistema)");
    const disk = element("input");
    disk.id = "boot-target-disk";
    disk.autocomplete = "off";
    disk.placeholder = "/dev/nvme0n1";
    diskLabel.append(disk);
    const rootLabel = element("label", "", "Root Linux montado (opcional)");
    const root = element("input");
    root.id = "boot-root-path";
    root.autocomplete = "off";
    root.placeholder = "/mnt/linux-root";
    rootLabel.append(root);
    const diagnose = element("button", "", "Diagnosticar arranque");
    diagnose.type = "submit";
    const makePlan = element("button", "", "Generar Boot Repair Plan");
    makePlan.type = "button";
    makePlan.id = "boot-plan-button";
    makePlan.disabled = true;
    form.append(diskLabel, rootLabel, diagnose, makePlan);

    const diagnosisTarget = element("div", "boot-diagnosis");
    diagnosisTarget.id = "boot-diagnosis";
    const planTarget = element("div", "boot-plan");
    planTarget.id = "boot-plan";
    planTarget.hidden = true;
    const execute = element("button", "", "Proteger y solicitar autorización");
    execute.type = "button";
    execute.id = "boot-execute-button";
    execute.disabled = true;
    const cancel = element("button", "secondary-button", "Cancelar reparación");
    cancel.type = "button";
    cancel.id = "boot-cancel-button";
    cancel.disabled = true;
    const status = element("div", "boot-status");
    status.id = "boot-status";
    section.append(form, diagnosisTarget, planTarget, execute, cancel, status);

    (document.querySelector("main") || document.body).append(section);
    const nav = document.querySelector("nav");
    if (nav && !document.querySelector('[href="#boot-recovery-section"]')) {
      const link = element("a", "nav-link", "Boot Recovery");
      link.href = "#boot-recovery-section";
      nav.append(link);
    }
    return section;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        "X-ARES-Session-ID": sessionId,
        ...(options.headers || {}),
      },
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const problem = await response.json();
        detail = problem.code ? `${problem.code}: ${problem.detail || problem.title}` : detail;
      } catch (_) {
        // Preserve the HTTP status if Problem Details is unavailable.
      }
      throw new Error(detail);
    }
    return response.json();
  }

  function renderDiagnosis(value) {
    const target = document.getElementById("boot-diagnosis");
    target.replaceChildren();
    const env = value.environment;
    target.append(element("h3", "", "Boot diagnosis"));
    target.append(row("Diagnostic ID", value.id));
    target.append(row("Firmware", env.firmware.mode));
    target.append(row("Bootloader", env.bootloader.kind));
    target.append(row("Distribution", env.bootloader.distribution_family));
    target.append(row("Automatic GRUB repair supported", env.bootloader.repair_supported ? "YES" : "No"));
    target.append(row("Confidence", `${Math.round(value.confidence * 100)}%`));
    target.append(row("Severity", value.severity));
    if (env.target_disk) {
      target.append(row("Target disk", env.target_disk.canonical_path));
      target.append(row("Disk fingerprint", env.target_disk.fingerprint_sha256));
    }
    target.append(row("Root partition", env.root_partition?.device_path));
    target.append(row("ESP", env.esp?.device_path));
    target.append(row("Kernels", env.configuration.kernels?.join(", ") || "none"));
    target.append(row("Initramfs", env.configuration.initramfs?.join(", ") || "none"));
    if (env.operating_systems?.length) {
      const systems = element("ul");
      for (const os of env.operating_systems) {
        systems.append(element("li", "", `${os.id}: ${os.name} ${os.version || ""} [${os.family}]`));
      }
      target.append(element("strong", "", "Operating systems"), systems);
    }
    if (value.issues?.length) {
      const issues = element("ul");
      for (const issue of value.issues) {
        issues.append(
          element(
            "li",
            "",
            `${issue.severity} · ${issue.code} · ${issue.summary} · confidence ${Math.round(issue.confidence * 100)}%`
          )
        );
      }
      target.append(element("strong", "", "Issues"), issues);
    }
    target.append(row("Recommended action", value.recommended_action));
  }

  function renderPlan(value) {
    const target = document.getElementById("boot-plan");
    target.replaceChildren();
    target.append(element("h3", "", "BOOT REPAIR PLAN"));
    target.append(row("Plan ID", value.id));
    target.append(row("Repair ID", value.repair_id));
    target.append(row("Target OS", `${value.target_os.name} [${value.target_os.family}]`));
    target.append(row("Target disk", value.target_disk.canonical_path));
    target.append(row("Disk fingerprint", value.target_disk.fingerprint_sha256));
    target.append(row("Bootloader", value.bootloader.kind));
    target.append(row("Risk", value.risk));
    target.append(row("Executable", value.executable ? "YES" : "BLOCKED"));
    target.append(row("Authorization", value.authorization_required ? "Required" : "No"));
    target.append(
      row(
        "Checkpoint",
        value.protection_checkpoint ? `${value.protection_checkpoint.id} / ${value.protection_checkpoint.status}` : "created immediately before authorization"
      )
    );
    const operations = element("ol");
    for (const operation of value.operations || []) {
      operations.append(
        element(
          "li",
          "",
          `${operation.kind}: ${operation.description}${operation.mutates_system ? " [MUTATES]" : ""}`
        )
      );
    }
    target.append(element("strong", "", "Operations"), operations);
    const limits = element("ul");
    for (const limitation of value.limitations || []) limits.append(element("li", "", limitation));
    limits.append(
      element(
        "li",
        "",
        "Offline verification cannot prove a successful reboot; ARES must report PARTIAL/LIMITED when reboot evidence is unavailable."
      )
    );
    target.append(element("strong", "", "Limitations"), limits);
    target.hidden = false;
    document.getElementById("boot-execute-button").disabled = value.executable !== true;
  }

  function renderRecord(record) {
    const target = document.getElementById("boot-status");
    target.replaceChildren();
    target.append(element("h3", "", `Boot repair ${record.execution.repair_id}`));
    target.append(row("Status", record.execution.status));
    target.append(row("Checkpoint", record.execution.checkpoint_id));
    target.append(row("Stage", record.execution.last_known_stage));
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
      target.append(element("h4", "", "Verification"));
      target.append(row("Result", record.verification.status));
      target.append(row("Confidence", record.verification.confidence));
      target.append(row("Firmware", record.verification.firmware_verified));
      target.append(row("Bootloader", record.verification.bootloader_verified));
      target.append(row("Kernel", record.verification.kernel_verified));
      target.append(row("Initramfs", record.verification.initramfs_verified));
      target.append(row("Root filesystem", record.verification.root_filesystem_verified));
      target.append(row("Message", record.verification.message));
      if (record.verification.limitations?.length) {
        const limits = element("ul");
        for (const limitation of record.verification.limitations) {
          limits.append(element("li", "", limitation));
        }
        target.append(element("strong", "", "Verification limitations"), limits);
      }
    }
  }

  async function diagnoseBoot(event) {
    event.preventDefault();
    diagnostic = null;
    plan = null;
    repairId = null;
    document.getElementById("boot-plan-button").disabled = true;
    document.getElementById("boot-execute-button").disabled = true;
    document.getElementById("boot-plan").hidden = true;
    const status = document.getElementById("boot-status");
    status.textContent = "Collecting read-only boot evidence…";
    const disk = document.getElementById("boot-target-disk").value.trim();
    const root = document.getElementById("boot-root-path").value.trim();
    try {
      diagnostic = await requestJson(`${api}/diagnose`, {
        method: "POST",
        body: JSON.stringify({ target_disk: disk || null, root_path: root || null }),
      });
      renderDiagnosis(diagnostic);
      document.getElementById("boot-plan-button").disabled = false;
      status.textContent = "Diagnosis complete. No boot state has been modified.";
    } catch (error) {
      status.textContent = `Boot diagnosis failed: ${error.message}`;
    }
  }

  async function generatePlan() {
    if (!diagnostic) return;
    const status = document.getElementById("boot-status");
    status.textContent = "Building minimal evidence-bound repair plan…";
    document.getElementById("boot-execute-button").disabled = true;
    try {
      plan = await requestJson(`${api}/repair/plan`, {
        method: "POST",
        body: JSON.stringify({ diagnostic_id: diagnostic.id, target_os_id: null }),
      });
      renderPlan(plan);
      status.textContent = plan.executable
        ? "Review the exact plan before requesting protection and independent authorization."
        : "Plan is BLOCKED by current evidence or safety limitations.";
    } catch (error) {
      status.textContent = `Boot repair plan failed: ${error.message}`;
    }
  }

  async function startRepair() {
    if (!plan || plan.executable !== true) return;
    const button = document.getElementById("boot-execute-button");
    button.disabled = true;
    const status = document.getElementById("boot-status");
    status.textContent = "Creating boot-state checkpoint and requesting independent authorization…";
    try {
      const accepted = await requestJson(`${api}/repair`, {
        method: "POST",
        body: JSON.stringify({ plan_id: plan.id, request_authorization: true }),
      });
      repairId = accepted.repair.execution.repair_id;
      renderRecord(accepted.repair);
      document.getElementById("boot-cancel-button").disabled = false;
      pollRepair();
    } catch (error) {
      status.textContent = `Boot repair was not started: ${error.message}`;
      button.disabled = false;
    }
  }

  async function cancelRepair() {
    if (!repairId) return;
    try {
      const record = await requestJson(`${api}/repairs/${encodeURIComponent(repairId)}/cancel`, {
        method: "POST",
        body: "{}",
      });
      renderRecord(record);
    } catch (error) {
      document.getElementById("boot-status").textContent = `Cancellation failed: ${error.message}`;
    }
  }

  async function pollRepair() {
    if (!repairId) return;
    try {
      const record = await requestJson(`${api}/repairs/${encodeURIComponent(repairId)}`);
      renderRecord(record);
      if (["COMPLETED", "REPAIR_FAILED", "ABORTED", "UNKNOWN"].includes(record.execution.status)) {
        document.getElementById("boot-cancel-button").disabled = true;
        repairId = null;
        return;
      }
    } catch (error) {
      document.getElementById("boot-status").textContent = `Error reading Boot repair state: ${error.message}`;
      return;
    }
    pollTimer = window.setTimeout(pollRepair, 1000);
  }

  function init() {
    ensureSection();
    document.getElementById("boot-diagnose-form").addEventListener("submit", diagnoseBoot);
    document.getElementById("boot-plan-button").addEventListener("click", generatePlan);
    document.getElementById("boot-execute-button").addEventListener("click", startRepair);
    document.getElementById("boot-cancel-button").addEventListener("click", cancelRepair);
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
