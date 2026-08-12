(() => {
  "use strict";

  const api = "/api/v1/recovery";
  const session = `recovery-ui-${crypto.randomUUID().replaceAll("-", "")}`;
  let currentCase = null;
  let pollTimer = null;

  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  async function request(path, options = {}) {
    const response = await fetch(`${api}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-ARES-Session-ID": session,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const problem = await response.json();
        message = `${problem.code || "RECOVERY_ERROR"}: ${problem.detail || problem.title}`;
      } catch (_) {
        // Keep HTTP status if Problem Details cannot be decoded.
      }
      throw new Error(message);
    }
    return response.json();
  }

  function ensureSection() {
    if (document.getElementById("system-recovery-section")) return;
    const section = el("section", undefined, "panel");
    section.id = "system-recovery-section";
    section.append(el("h2", "System Recovery"));
    section.append(
      el(
        "p",
        "Evidence → Root Cause → Recovery Plan → Safety Gate → Child Capabilities → Verification.",
        "muted"
      )
    );
    const form = el("form");
    form.id = "recovery-diagnose-form";
    const root = el("input");
    root.id = "recovery-root";
    root.placeholder = "/mnt/linux-root";
    root.required = true;
    const disk = el("input");
    disk.id = "recovery-disk";
    disk.placeholder = "/dev/nvme0n1 (optional)";
    const diagnose = el("button", "Diagnose");
    diagnose.type = "submit";
    const plan = el("button", "Build recovery plan");
    plan.type = "button";
    plan.id = "recovery-plan-button";
    plan.disabled = true;
    const authorize = el("button", "Protect and request authorization");
    authorize.type = "button";
    authorize.id = "recovery-authorize-button";
    authorize.disabled = true;
    const execute = el("button", "Execute authorized operations");
    execute.type = "button";
    execute.id = "recovery-execute-button";
    execute.disabled = true;
    const abort = el("button", "Abort recovery", "secondary-button");
    abort.type = "button";
    abort.id = "recovery-abort-button";
    abort.disabled = true;
    form.append(root, disk, diagnose, plan, authorize, execute, abort);
    const view = el("div");
    view.id = "recovery-view";
    section.append(form, view);
    (document.querySelector("main") || document.body).append(section);
    const nav = document.querySelector("nav");
    if (nav && !document.querySelector('[href="#system-recovery-section"]')) {
      const link = el("a", "System Recovery", "nav-link");
      link.href = "#system-recovery-section";
      nav.append(link);
    }
  }

  function row(label, value) {
    const node = el("div", undefined, "recovery-row");
    node.append(el("strong", `${label}: `), el("span", value ?? "unknown"));
    return node;
  }

  function render(caseValue) {
    currentCase = caseValue;
    const view = document.getElementById("recovery-view");
    view.replaceChildren();
    view.append(el("h3", `Recovery Case ${caseValue.case_id}`));
    view.append(row("Status", caseValue.status));
    view.append(row("Mode", caseValue.mode));
    view.append(row("Initial state", caseValue.initial_state));
    view.append(row("Target root", caseValue.target_root));

    const layers = new Map([
      ["hardware", "✓"],
      ["storage", "✓"],
      ["filesystem", "✓"],
      ["boot", "✓"],
      ["kernel", "✓"],
      ["initramfs", "✓"],
      ["systemd", "✓"],
      ["services", "✓"],
      ["packages", "✓"],
      ["configuration", "✓"],
    ]);
    for (const issue of caseValue.detected_issues || []) layers.set(issue.layer, "⚠");
    const statusList = el("ul");
    for (const [layer, state] of layers) statusList.append(el("li", `${layer}: ${state}`));
    view.append(el("h4", "SYSTEM STATUS"), statusList);

    if (caseValue.hypotheses?.length) {
      const hypotheses = el("ol");
      for (const hypothesis of caseValue.hypotheses) {
        hypotheses.append(
          el(
            "li",
            `${hypothesis.hypothesis} — confidence ${Math.round(hypothesis.confidence * 100)}%`
          )
        );
      }
      view.append(el("h4", "Root-cause hypotheses"), hypotheses);
    }

    if (caseValue.recovery_plan) {
      const plan = caseValue.recovery_plan;
      view.append(el("h4", "Recovery Plan"));
      view.append(row("Fingerprint", plan.fingerprint_sha256));
      view.append(row("Minimum change", plan.minimum_change_rationale));
      const operations = el("ol");
      for (const operation of plan.operations) {
        const checkpoint = operation.protection_checkpoint?.id || "pending";
        const challenge = operation.authorization_challenge_id || "pending";
        operations.append(
          el(
            "li",
            `${operation.capability_id} · ${operation.description} · ${operation.status} · risk=${operation.risk} · checkpoint=${checkpoint} · challenge=${challenge}`
          )
        );
      }
      view.append(operations);
      if (plan.blocked_reasons?.length) {
        const blocked = el("ul");
        for (const reason of plan.blocked_reasons) blocked.append(el("li", reason));
        view.append(el("h4", "Blocked / independent child recovery"), blocked);
      }
    }

    if (caseValue.verification) {
      view.append(el("h4", "Verification"));
      view.append(row("Result", caseValue.verification.status));
      view.append(row("Message", caseValue.verification.message));
      const limitations = el("ul");
      for (const limitation of caseValue.verification.limitations || []) {
        limitations.append(el("li", limitation));
      }
      view.append(limitations);
    }
    if (caseValue.report) view.append(row("User summary", caseValue.report.user_summary));

    document.getElementById("recovery-plan-button").disabled = caseValue.status !== "DISCOVERY";
    document.getElementById("recovery-authorize-button").disabled = caseValue.status !== "PLANNED";
    document.getElementById("recovery-execute-button").disabled = caseValue.status !== "AUTHORIZED";
    document.getElementById("recovery-abort-button").disabled = [
      "RECOVERED",
      "PARTIAL",
      "FAILED",
      "UNKNOWN",
      "ABORTED",
    ].includes(caseValue.status);
  }

  async function diagnose(event) {
    event.preventDefault();
    const root = document.getElementById("recovery-root").value.trim();
    const disk = document.getElementById("recovery-disk").value.trim();
    try {
      render(
        await request("/diagnose", {
          method: "POST",
          body: JSON.stringify({ target_root: root, target_disk: disk || null, mode: "READ_ONLY" }),
        })
      );
    } catch (error) {
      document.getElementById("recovery-view").textContent = `Diagnosis failed: ${error.message}`;
    }
  }

  async function buildPlan() {
    if (!currentCase) return;
    const plan = await request("/plan", {
      method: "POST",
      body: JSON.stringify({ case_id: currentCase.case_id, mode: "ASSISTED" }),
    });
    currentCase.recovery_plan = plan;
    currentCase.status = "PLANNED";
    render(currentCase);
  }

  async function authorize() {
    if (!currentCase) return;
    const accepted = await request(`/${encodeURIComponent(currentCase.case_id)}/authorize`, {
      method: "POST",
      body: "{}",
    });
    render(accepted.case);
    poll();
  }

  async function execute() {
    if (!currentCase) return;
    const accepted = await request(`/${encodeURIComponent(currentCase.case_id)}/execute`, {
      method: "POST",
      body: "{}",
    });
    render(accepted.case);
    poll();
  }

  async function abortRecovery() {
    if (!currentCase) return;
    render(
      await request(`/${encodeURIComponent(currentCase.case_id)}/abort`, {
        method: "POST",
        body: "{}",
      })
    );
  }

  async function poll() {
    if (!currentCase) return;
    try {
      const value = await request(`/${encodeURIComponent(currentCase.case_id)}`);
      render(value);
      if (["RECOVERED", "PARTIAL", "FAILED", "UNKNOWN", "ABORTED"].includes(value.status)) return;
    } catch (_) {
      return;
    }
    pollTimer = setTimeout(poll, 1000);
  }

  function init() {
    ensureSection();
    document.getElementById("recovery-diagnose-form").addEventListener("submit", diagnose);
    document.getElementById("recovery-plan-button").addEventListener("click", buildPlan);
    document.getElementById("recovery-authorize-button").addEventListener("click", authorize);
    document.getElementById("recovery-execute-button").addEventListener("click", execute);
    document.getElementById("recovery-abort-button").addEventListener("click", abortRecovery);
  }

  addEventListener("beforeunload", () => pollTimer && clearTimeout(pollTimer));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
