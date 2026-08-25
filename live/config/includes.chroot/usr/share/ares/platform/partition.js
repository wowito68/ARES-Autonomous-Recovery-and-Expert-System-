(() => {
  "use strict";

  const API = "/api/v1/storage";
  const SESSION_KEY = "ares-storage-operation-session";
  const sessionId = (() => {
    const existing = window.sessionStorage.getItem(SESSION_KEY);
    if (existing && /^[A-Za-z0-9._-]{8,64}$/.test(existing)) return existing;
    const value = `web-${crypto.randomUUID().replaceAll("-", "")}`;
    window.sessionStorage.setItem(SESSION_KEY, value);
    return value;
  })();
  let currentPlan = null;
  let currentOperationId = null;
  let pollTimer = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(value) {
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

  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-ARES-Session-ID": sessionId,
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(body?.detail || `HTTP ${response.status}`);
      error.code = body?.code || "HTTP_ERROR";
      throw error;
    }
    return body;
  }

  function ensureSection() {
    let section = document.getElementById("partition-management");
    if (section) return section;
    section = el("section", "section-block");
    section.id = "partition-management";

    const heading = el("div", "section-heading");
    const titleBox = el("div");
    titleBox.append(el("p", "section-kicker", "STORAGE · DECLARATIVE TRANSACTIONS"));
    titleBox.append(el("h2", "", "Storage & Partition Management"));
    const badge = el("span", "status-badge pending", "Write gate: physical disks disabled");
    badge.id = "partition-write-gate";
    heading.append(titleBox, badge);

    const intro = el(
      "p",
      "muted",
      "Create/Delete solo se habilitan para imágenes o loop controlados. Resize/Move se muestran para planificación, pero están deshabilitados. LVM, RAID, cifrado y dependencias de boot se detectan y bloquean."
    );

    const form = el("form", "planner-form");
    form.id = "partition-plan-form";
    const resource = el("select");
    resource.id = "partition-target-resource";
    resource.setAttribute("aria-label", "Disco detectado por ARES");
    resource.title = "Disco detectado por ARES";
    const disk = el("input");
    disk.id = "partition-target";
    disk.placeholder = "/var/lib/ares/storage-images/test.img";
    disk.required = true;
    disk.autocomplete = "off";
    const operation = el("select");
    operation.id = "partition-operation";
    for (const value of ["create", "delete", "resize", "move"]) {
      const option = el("option", "", value);
      option.value = value;
      operation.append(option);
    }
    const number = el("input");
    number.id = "partition-number";
    number.type = "number";
    number.min = "1";
    number.placeholder = "Partition number (delete/resize/move)";
    const size = el("input");
    size.id = "partition-size";
    size.type = "number";
    size.min = "1048576";
    size.placeholder = "Size bytes (create)";
    const newSize = el("input");
    newSize.id = "partition-new-size";
    newSize.type = "number";
    newSize.min = "1048576";
    newSize.placeholder = "New size bytes (resize)";
    const newStart = el("input");
    newStart.id = "partition-new-start";
    newStart.type = "number";
    newStart.min = "0";
    newStart.placeholder = "New start sector (move)";
    const tableType = el("select");
    tableType.id = "partition-table-type";
    const auto = el("option", "", "Keep detected table / choose for empty image");
    auto.value = "";
    tableType.append(auto);
    for (const value of ["GPT", "MBR"]) {
      const option = el("option", "", value);
      option.value = value;
      tableType.append(option);
    }
    const inspect = el("button", "secondary-button", "Inspect layout");
    inspect.type = "button";
    inspect.id = "partition-inspect";
    const plan = el("button", "", "Generate plan");
    plan.type = "submit";
    form.append(resource, disk, operation, number, size, newSize, newStart, tableType, inspect, plan);

    const controls = el("div", "topbar-actions");
    const validate = el("button", "secondary-button", "Validate + Dry Run + Protect");
    validate.id = "partition-validate";
    validate.disabled = true;
    const authorize = el("button", "secondary-button", "Request authorization");
    authorize.id = "partition-authorize";
    authorize.disabled = true;
    const execute = el("button", "", "Execute authorized plan");
    execute.id = "partition-execute";
    execute.disabled = true;
    const reconcile = el("button", "secondary-button", "Reinspect UNKNOWN");
    reconcile.id = "partition-reconcile";
    reconcile.disabled = true;
    controls.append(validate, authorize, execute, reconcile);

    const result = el("div", "capability-results");
    result.id = "partition-results";
    result.append(el("p", "empty-state", "Inspecta un target exacto o genera un plan declarativo."));
    section.append(heading, intro, form, controls, result);

    const hardware = document.getElementById("hardware");
    if (hardware?.parentNode) hardware.parentNode.insertBefore(section, hardware);
    else document.querySelector("main")?.append(section);

    const nav = document.querySelector("aside nav");
    if (nav && !nav.querySelector('a[href="#partition-management"]')) {
      const link = el("a", "nav-link", "Partitions");
      link.href = "#partition-management";
      nav.append(link);
    }
    return section;
  }

  function partitionSummary(layout, partition) {
    const fs = (layout.filesystems || []).find((item) => item.id === partition.filesystem_id);
    const mounts = (layout.mount_points || []).filter((item) =>
      (partition.mount_point_ids || []).includes(item.id),
    );
    const systems = (layout.operating_systems || []).filter((item) =>
      (partition.operating_system_ids || []).includes(item.id),
    );
    const volumes = (layout.volumes || []).filter((item) =>
      (partition.volume_ids || []).includes(item.id),
    );
    const boot = (layout.boot_dependencies || []).filter(
      (item) => item.partition_number === partition.number,
    );
    return [
      `#${partition.number} ${partition.path}`,
      formatBytes(partition.size_bytes),
      fs?.filesystem_type || "no filesystem",
      fs?.uuid ? `UUID ${fs.uuid}` : "UUID unknown",
      mounts.length ? `mount ${mounts.map((item) => item.path).join(", ")}` : "unmounted",
      systems.length ? `OS ${systems.map((item) => item.name).join(", ")}` : "OS none",
      `encryption ${partition.encryption_status}`,
      volumes.length ? `volume ${volumes.map((item) => item.kind).join(", ")}` : "volume none",
      boot.length ? `boot ${boot.map((item) => item.kind).join(", ")}` : `role ${partition.role}`,
      "health: not modified by this view",
    ].join(" · ");
  }

  function renderLayout(title, layout) {
    const block = el("div", "capability-card");
    block.append(el("h3", "", title));
    const table = layout.partition_table || {};
    block.append(
      el(
        "p",
        "muted",
        `${layout.disk?.identity?.canonical_path || "unknown"} · ${table.type || "UNKNOWN"} · sector ${table.sector_size || "?"} · fingerprint ${table.fingerprint_sha256 || "?"}`,
      ),
    );
    const list = el("ul", "finding-list");
    for (const partition of table.partitions || []) {
      const item = el("li");
      item.append(el("p", "", partitionSummary(layout, partition)));
      list.append(item);
    }
    for (const free of table.free_regions || []) {
      const item = el("li");
      item.append(
        el(
          "p",
          "",
          `FREE · ${formatBytes(free.size_bytes)} · sectors ${free.start_sector}-${free.end_sector}`,
        ),
      );
      list.append(item);
    }
    if (!list.children.length) list.append(el("li", "", "No partitions/free regions reported."));
    block.append(list);
    return block;
  }

  function renderPlan(plan) {
    const result = document.getElementById("partition-results");
    if (!result) return;
    result.replaceChildren();
    const metrics = el("div", "capability-metrics");
    const values = [
      ["Operation", plan.operation],
      ["Risk", plan.risk],
      ["Data impact", plan.data_impact?.level],
      ["Boot impact", plan.boot_impact?.level],
      ["Write gate", plan.write_gate?.allowed ? "allowed" : "BLOCKED"],
      ["Executable", plan.executable ? "yes" : "no"],
      ["Dry run", plan.dry_run?.valid === true ? "valid" : "pending/not valid"],
      ["Checkpoint", plan.protection_checkpoint?.id || "pending"],
    ];
    for (const [label, value] of values) {
      const item = el("div");
      item.append(el("span", "", label), el("strong", "", String(value ?? "unknown")));
      metrics.append(item);
    }
    result.append(metrics, renderLayout("CURRENT", plan.original_layout), renderLayout("PROPOSED", plan.proposed_layout));
    const impact = el("div", "capability-card");
    impact.append(el("h3", "", "Proposed Changes / Impact"));
    impact.append(
      el(
        "p",
        "",
        `Affected: ${(plan.affected_resources || []).join(", ") || "none"}\nLimitations: ${(plan.limitations || []).join("; ") || "none"}`,
      ),
    );
    result.append(impact);
    document.getElementById("partition-validate").disabled = false;
    document.getElementById("partition-authorize").disabled = !(
      plan.executable && plan.dry_run?.valid && plan.protection_checkpoint
    );
    document.getElementById("partition-execute").disabled = true;
  }

  function renderRecord(record) {
    currentPlan = record.plan;
    renderPlan(record.plan);
    const result = document.getElementById("partition-results");
    const status = el("div", "capability-card");
    status.append(el("h3", "", "StorageTransaction"));
    status.append(
      el(
        "p",
        "",
        `Status: ${record.transaction.status} · stage: ${record.transaction.last_known_stage || "unknown"} · error: ${record.transaction.error_code || "none"} · challenge: ${record.transaction.authorization_challenge_id || "none"}`,
      ),
    );
    if (record.verification) {
      status.append(
        el(
          "p",
          "",
          `Verification: ${record.verification.status} · ${record.verification.message}`,
        ),
      );
    }
    result?.prepend(status);
    document.getElementById("partition-authorize").disabled =
      record.transaction.status !== "PROTECTED";
    document.getElementById("partition-execute").disabled =
      record.transaction.status !== "AUTHORIZED";
    document.getElementById("partition-reconcile").disabled =
      record.transaction.status !== "UNKNOWN";
  }

  async function inspectLayout() {
    const target = document.getElementById("partition-target")?.value.trim();
    if (!target) return;
    const layout = await request(`/layout?target_disk=${encodeURIComponent(target)}`);
    const result = document.getElementById("partition-results");
    result?.replaceChildren(renderLayout("PHYSICAL DISK / IMAGE", layout));
  }

  async function planOperation(event) {
    event.preventDefault();
    const value = (id) => document.getElementById(id)?.value;
    const number = (id) => {
      const raw = value(id);
      return raw === "" || raw === undefined ? null : Number(raw);
    };
    const payload = {
      operation: value("partition-operation"),
      target_disk: value("partition-target")?.trim(),
      partition_number: number("partition-number"),
      size_bytes: number("partition-size"),
      new_size_bytes: number("partition-new-size"),
      new_start_sector: number("partition-new-start"),
      table_type: value("partition-table-type") || null,
    };
    const plan = await request("/operations/plan", { method: "POST", body: JSON.stringify(payload) });
    currentPlan = plan;
    currentOperationId = plan.operation_id;
    renderPlan(plan);
  }

  async function validateOperation() {
    if (!currentOperationId) return;
    currentPlan = await request(`/operations/${encodeURIComponent(currentOperationId)}/validate`, {
      method: "POST",
      body: "{}",
    });
    renderPlan(currentPlan);
  }

  async function authorizeOperation() {
    if (!currentOperationId) return;
    await request(`/operations/${encodeURIComponent(currentOperationId)}/authorize`, {
      method: "POST",
      body: JSON.stringify({ request_authorization: true }),
    });
    startPolling();
  }

  async function executeOperation() {
    if (!currentOperationId) return;
    await request(`/operations/${encodeURIComponent(currentOperationId)}/execute`, {
      method: "POST",
      body: "{}",
    });
    startPolling();
  }

  async function reconcileOperation() {
    if (!currentOperationId) return;
    await request(`/operations/${encodeURIComponent(currentOperationId)}/reconcile`, {
      method: "POST",
      body: "{}",
    });
    startPolling();
  }

  async function poll() {
    if (!currentOperationId) return;
    try {
      const record = await request(`/operations/${encodeURIComponent(currentOperationId)}`);
      renderRecord(record);
      if (["COMMITTED", "FAILED", "ABORTED"].includes(record.transaction.status)) stopPolling();
    } catch (error) {
      stopPolling();
      const result = document.getElementById("partition-results");
      result?.prepend(el("p", "banner warning", `Polling failed: ${error.code || error.message}`));
    }
  }

  function startPolling() {
    stopPolling();
    poll();
    pollTimer = window.setInterval(poll, 750);
  }

  function stopPolling() {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    pollTimer = null;
  }

  function report(error) {
    const result = document.getElementById("partition-results");
    result?.prepend(el("p", "banner warning", `${error.code || "ERROR"}: ${error.message}`));
  }

  async function wireResourceSelector() {
    if (!window.AresResources) return;
    try {
      await window.AresResources.fillSelect("partition-target-resource", {
        kinds: ["disk"],
        blankLabel: "Selecciona disco detectado o usa imagen manual",
      });
      window.AresResources.bindPath("partition-target-resource", "partition-target");
    } catch (_) {
      // Las imágenes/rutas manuales siguen siendo el flujo seguro por defecto.
    }
  }

  ensureSection();
  wireResourceSelector();
  document.getElementById("partition-inspect")?.addEventListener("click", () => inspectLayout().catch(report));
  document.getElementById("partition-plan-form")?.addEventListener("submit", (event) => planOperation(event).catch(report));
  document.getElementById("partition-validate")?.addEventListener("click", () => validateOperation().catch(report));
  document.getElementById("partition-authorize")?.addEventListener("click", () => authorizeOperation().catch(report));
  document.getElementById("partition-execute")?.addEventListener("click", () => executeOperation().catch(report));
  document.getElementById("partition-reconcile")?.addEventListener("click", () => reconcileOperation().catch(report));
})();
