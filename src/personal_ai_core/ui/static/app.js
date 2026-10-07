(() => {
  "use strict";

  // Prototype-only state. The HTTP API adapter will replace this layer later.
  const sessions = [
    { title: "ميزانية المشروع", time: "اليوم", active: true },
    { title: "ملاحظات اجتماع الفريق", time: "أمس", active: false },
    { title: "خطة السفر الصيفية", time: "٢ أكتوبر", active: false },
    { title: "أفكار المنتج", time: "٢٨ سبتمبر", active: false },
  ];

  const $ = (selector) => document.querySelector(selector);
  const sessionList = $("#session-list");
  const drawer = $("#evidence-drawer");
  const toast = $("#toast");
  const fileInput = $("#file-input");
  const attachmentPreview = $("#attachment-preview");
  let toastTimer;

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
  }

  function renderSessions() {
    sessionList.innerHTML = sessions.map((session, index) => `
      <button class="session-item${session.active ? " active" : ""}" type="button" data-session="${index}">
        <span>${session.title}</span><small>${session.time}</small>
      </button>
    `).join("");
    sessionList.querySelectorAll("[data-session]").forEach((button) => {
      button.addEventListener("click", () => {
        sessions.forEach((session) => { session.active = false; });
        sessions[Number(button.dataset.session)].active = true;
        $("#active-session-title").textContent = sessions[Number(button.dataset.session)].title;
        renderSessions();
        showToast("تم فتح الجلسة");
      });
    });
  }

  function openEvidence() {
    drawer.setAttribute("aria-hidden", "false");
    $("#evidence-trigger")?.focus();
  }

  function closeEvidence() {
    drawer.setAttribute("aria-hidden", "true");
  }

  $("#evidence-trigger").addEventListener("click", openEvidence);
  $("#close-evidence").addEventListener("click", closeEvidence);
  $("#new-chat").addEventListener("click", () => showToast("بدأت محادثة جديدة — الربط بالحفظ سيضاف مع API"));
  $("#settings-link").addEventListener("click", () => showToast("الإعدادات الأساسية ستظهر هنا في النسخة التالية"));
  $("#mode-toggle").addEventListener("click", () => showToast("الوضع البسيط مفعّل — وضع المطور مؤجل"));

  $("#attach-button").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const allowed = /\.(md|txt|pdf)$/i.test(file.name);
    if (!allowed) {
      fileInput.value = "";
      showToast("يسمح فقط بملفات MD وTXT وPDF");
      return;
    }
    $("#attachment-name").textContent = file.name;
    attachmentPreview.hidden = false;
    uploadDocument(file);
  });

  async function uploadDocument(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    showToast("جارٍ فحص المستند وقراءته...");
    try {
      const response = await fetch("/api/documents", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken() },
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || payload.error || "تعذر رفع المستند");
      }
      showToast(`تمت إضافة ${payload.document.display_name} (${payload.document.chunk_count} مقاطع)`);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "تعذر رفع المستند");
      attachmentPreview.hidden = true;
      fileInput.value = "";
    }
  }
  $("#remove-attachment").addEventListener("click", () => {
    fileInput.value = "";
    attachmentPreview.hidden = true;
  });

  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#message-input");
    if (!input.value.trim() && attachmentPreview.hidden) {
      showToast("اكتب سؤالًا أو أرفق مستندًا أولًا");
      return;
    }
    showToast("تم استلام السؤال — مسار المحادثة سيُربط بالخدمة التالية");
  });

  $("#message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });

  renderSessions();
})();
