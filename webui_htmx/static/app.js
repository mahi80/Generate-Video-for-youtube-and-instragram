// Browser glue for the HTMX WebUI.
//
// Every widget carries form="<form id>" and data-w. A change on a widget, or a
// click on a [data-action] button, triggers the custom "mpt" event on its form;
// the form's hx-post sends all of the form's widgets to the server, which
// reruns the page (or fragment) and returns HTML that idiomorph merges into the
// page without losing focus or typed text.
(function () {
  "use strict";

  // Keep <details> open/closed as the user left it when the server re-renders.
  Idiomorph.defaults.callbacks.beforeAttributeUpdated = function (attribute, node) {
    if (attribute === "open" && node.tagName === "DETAILS") return false;
    return true;
  };

  function formOf(element) {
    const id = element.getAttribute("form") || element.dataset.uploadForm;
    return id ? document.getElementById(id) : null;
  }

  function submit(form, action) {
    if (!form) return;
    htmx.trigger(form, "mpt", { action: action || "" });
  }

  // Each page load is its own server-side session, like a Streamlit tab.
  function sessionId() { return document.body.dataset.sid || ""; }

  // Add the session, the clicked action and a "user initiated" marker.
  document.addEventListener("htmx:configRequest", function (event) {
    event.detail.headers["X-MPT-Session"] = sessionId();
    const trigger = event.detail.triggeringEvent;
    if (trigger && trigger.type === "mpt") {
      event.detail.parameters._user = "1";
      event.detail.parameters._action = (trigger.detail && trigger.detail.action) || "";
    }
  });

  document.addEventListener("change", function (event) {
    const target = event.target;
    if (target.matches("input[type=file][data-upload]")) {
      upload(target);
      return;
    }
    if (!target.matches("[data-w]") || target.dataset.defer) return;
    submit(formOf(target));
  });

  // Live value labels for sliders and character counters for text areas.
  document.addEventListener("input", function (event) {
    const target = event.target;
    if (target.matches("input[type=range]")) {
      const output = target.parentElement.querySelector("output");
      if (output) output.textContent = formatNumber(target.dataset.format, Number(target.value));
    }
    if (target.matches("textarea[maxlength]")) {
      const counter = document.querySelector('[data-counter="' + target.id + '"]');
      if (counter) counter.textContent = target.value.length + "/" + target.maxLength;
    }
  });

  // Enter in a single-line input commits it, like Streamlit.
  document.addEventListener("keydown", function (event) {
    const target = event.target;
    if (event.key === "Enter" && target.matches("input[data-w]:not([type=checkbox]):not([type=radio])")) {
      event.preventDefault();
      // Fields inside an st.form only commit through the form's submit button.
      target.blur();
    }
    if (event.key === "Escape") {
      const dialogs = document.querySelectorAll(".modal-root[data-dismiss-url]");
      const top = dialogs[dialogs.length - 1];
      const closeButton = top && top.querySelector(".modal-close");
      if (closeButton) closeButton.click();
    }
  });

  document.addEventListener("click", function (event) {
    const target = event.target;

    const actionElement = target.closest("[data-action]");
    if (actionElement && !actionElement.disabled && !actionElement.classList.contains("is-disabled")) {
      if (!actionElement.dataset.link) event.preventDefault();
      // Commit a text field the user is still typing in before the action runs.
      if (document.activeElement && document.activeElement.matches("[data-w]")) {
        document.activeElement.blur();
      }
      actionElement.classList.add("is-busy");
      submit(formOf(actionElement), actionElement.dataset.action);
      return;
    }

    const tab = target.closest("[data-tab-index]");
    if (tab) {
      const input = document.getElementById(tab.dataset.tabInput);
      if (input) {
        input.value = tab.dataset.tabIndex;
        tab.closest(".tabs").querySelectorAll(".tab").forEach(function (other) {
          other.classList.toggle("is-active", other === tab);
        });
        submit(formOf(input));
      }
      return;
    }

    const reveal = target.closest("[data-reveal]");
    if (reveal) {
      const input = document.getElementById(reveal.dataset.reveal);
      if (input) input.type = input.type === "password" ? "text" : "password";
      return;
    }

    const copy = target.closest("[data-copy]");
    if (copy) {
      const code = copy.parentElement.querySelector("code");
      if (code && navigator.clipboard) {
        navigator.clipboard.writeText(code.textContent).then(function () {
          copy.classList.add("is-done");
          setTimeout(function () { copy.classList.remove("is-done"); }, 1200);
        });
      }
      return;
    }

    const remove = target.closest("[data-upload-remove]");
    if (remove) {
      const body = new FormData();
      body.append("key", remove.dataset.uploadKey);
      body.append("file_id", remove.dataset.uploadRemove);
      fetch("/upload/remove", { method: "POST", body: body, headers: { "X-MPT-Session": sessionId() } })
        .then(function () { submit(document.getElementById(remove.dataset.uploadForm)); });
      return;
    }

    // Close popovers when clicking elsewhere.
    document.querySelectorAll("details.popover[open]").forEach(function (popover) {
      if (!popover.contains(target)) popover.removeAttribute("open");
    });
  });

  // Drag and drop onto upload zones.
  ["dragenter", "dragover"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      const zone = event.target.closest && event.target.closest(".dropzone");
      if (zone) { event.preventDefault(); zone.classList.add("is-over"); }
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      const zone = event.target.closest && event.target.closest(".dropzone");
      if (!zone) return;
      zone.classList.remove("is-over");
      if (name === "drop") {
        event.preventDefault();
        const input = zone.querySelector("input[type=file]");
        if (input && !input.disabled && event.dataTransfer.files.length) {
          input.files = event.dataTransfer.files;
          upload(input);
        }
      }
    });
  });

  function upload(input) {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    const limit = Number(input.dataset.maxBytes || 0);
    const tooLarge = files.find(function (file) { return limit && file.size > limit; });
    if (tooLarge) {
      toast(tooLarge.name + ": file is too large", "error");
      input.value = "";
      return;
    }
    const zone = input.closest(".dropzone");
    const body = new FormData();
    body.append("key", input.dataset.upload);
    files.forEach(function (file) { body.append("files", file, file.name); });
    if (zone) zone.classList.add("is-uploading");
    document.getElementById("busy").classList.add("htmx-request");
    fetch("/upload", { method: "POST", body: body, headers: { "X-MPT-Session": sessionId() } })
      .then(function (response) {
        return response.text().then(function (text) {
          if (!response.ok) toast(text || "Upload failed", "error");
        });
      })
      .catch(function () { toast("Upload failed", "error"); })
      .finally(function () {
        input.value = "";
        if (zone) zone.classList.remove("is-uploading");
        document.getElementById("busy").classList.remove("htmx-request");
        submit(document.getElementById(input.dataset.uploadForm));
      });
  }

  function toast(text, level, symbol) {
    const container = document.getElementById("toasts");
    if (!container) return;
    const item = document.createElement("div");
    item.className = "toast" + (level ? " toast-" + level : "");
    item.setAttribute("role", "status");
    if (symbol) {
      const glyph = document.createElement("span");
      const material = /^:material\/([a-z0-9_]+):$/.exec(symbol);
      glyph.className = material ? "msym" : "emoji";
      glyph.textContent = material ? material[1] : symbol;
      item.appendChild(glyph);
    }
    const label = document.createElement("div");
    label.textContent = text;
    item.appendChild(label);
    container.appendChild(item);
    scheduleToastRemoval(item);
  }

  // Server-side st.toast() calls arrive in the HX-Trigger response header.
  document.body.addEventListener("mpt-toasts", function (event) {
    (event.detail.items || []).forEach(function (item) { toast(item.text, "", item.icon); });
  });

  function scheduleToastRemoval(item) {
    if (item.dataset.scheduled) return;
    item.dataset.scheduled = "1";
    setTimeout(function () { item.classList.add("is-leaving"); }, 4200);
    setTimeout(function () { item.remove(); }, 4600);
  }

  function formatNumber(format, value) {
    if (!format) return String(Math.round(value * 1000) / 1000);
    return format.replace(/%(\.(\d+))?([dfs])/, function (_, __, digits, type) {
      if (type === "d") return String(Math.round(value));
      if (type === "f") return value.toFixed(digits ? Number(digits) : 6);
      return String(value);
    }).replace(/%%/g, "%");
  }

  function startTours() {
    const app = document.getElementById("app");
    if (!app || !window.driver) return;
    let scripts = [];
    try { scripts = JSON.parse(app.dataset.scripts || "[]"); } catch (error) { scripts = []; }
    scripts.forEach(function (script) {
      if (script.type !== "tour") return;
      const storageKey = "mpt-tour:" + script.key;
      try {
        if (script.once && localStorage.getItem(storageKey)) return;
      } catch (error) { /* storage unavailable: show the tour */ }
      if (window.__mptTourStarted) return;
      const steps = script.steps.filter(function (step) { return document.querySelector(step.element); });
      if (!steps.length) return;
      window.__mptTourStarted = true;
      const tour = window.driver.js.driver({
        showProgress: script.showProgress,
        animate: script.animate,
        overlayOpacity: script.overlayOpacity,
        steps: steps,
        onDestroyed: function () {
          try { localStorage.setItem(storageKey, "1"); } catch (error) { /* ignore */ }
        },
      });
      setTimeout(function () { tour.drive(); }, 400);
    });
  }

  document.addEventListener("htmx:afterSettle", function () {
    document.querySelectorAll(".is-busy").forEach(function (element) { element.classList.remove("is-busy"); });
    document.querySelectorAll("#toasts .toast").forEach(scheduleToastRemoval);
    const app = document.getElementById("app");
    if (app && app.dataset.title) document.title = app.dataset.title;
    document.body.classList.toggle("has-modal", !!document.querySelector(".modal-root"));
    startTours();
  });

  document.addEventListener("htmx:responseError", function (event) {
    document.querySelectorAll(".is-busy").forEach(function (element) { element.classList.remove("is-busy"); });
    toast("Request failed (" + event.detail.xhr.status + "). Check the server log.", "error");
  });

  document.addEventListener("htmx:sendError", function () {
    document.querySelectorAll(".is-busy").forEach(function (element) { element.classList.remove("is-busy"); });
    toast("Cannot reach the WebUI server. Is it still running?", "error");
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("#toasts .toast").forEach(scheduleToastRemoval);
    document.body.classList.toggle("has-modal", !!document.querySelector(".modal-root"));
    startTours();
  });
})();
