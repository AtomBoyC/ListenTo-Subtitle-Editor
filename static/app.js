/* ListenTo Studio v2.0 — Interactive Bilingual Subtitle Workbench */

(() => {
  "use strict";

  const state = {
    meta: null,
    settings: null,
    doc: null,
    activeCueId: null,
    filter: "all",
    searchQuery: "",
    searchScope: "target",
    previewSubview: "bilingual",
    renderLimit: 250,
    undoStack: [],
    redoStack: [],
    translating: false,
    abortController: null,
    openPopoverCueId: null,
    cueSuggestions: {}, // cue_id -> { best, alternatives, reason }
    simPlaying: false,
    simTimeMs: 0,
    simLastTick: 0,
    simRaf: null,
    hasMediaFile: false,
    waveformZoom: 1,
    waveformCollapsed: false,
    waveformPeaks: null,
    waveformDurationMs: 0,
    wfViewStartMs: 0,
    wfDrag: null,
    batchQueue: [],
    batchRunning: false,
    batchAbort: null,
    saveTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  // Inline SVG icon from sprite in index.html
  function icon(name, extraCls = "") {
    return `<svg class="ic${extraCls ? ` ${extraCls}` : ""}"><use href="#i-${name}"/></svg>`;
  }

  function setPlayIcon(playing) {
    const el = $("play-icon");
    if (el) el.innerHTML = icon(playing ? "pause" : "play");
  }

  // Swap a button's content to a loading label and return a restore function
  function setButtonBusy(btn, label) {
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span><span>${escapeHtml(label)}</span>`;
    return () => {
      btn.disabled = false;
      btn.innerHTML = original;
    };
  }

  // Format ms -> HH:MM:SS.mmm
  function msToDisplay(ms) {
    const v = Math.max(0, Math.round(ms || 0));
    const h = Math.floor(v / 3600000);
    const m = Math.floor((v % 3600000) / 60000);
    const s = Math.floor((v % 60000) / 1000);
    const f = v % 1000;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(f).padStart(3, "0")}`;
  }

  function parseDisplayToMs(str) {
    const cleaned = String(str || "").trim().replace(",", ".");
    const parts = cleaned.split(":");
    if (parts.length < 2 || parts.length > 3) return null;
    let h = 0, m = 0, secPart = "0";
    if (parts.length === 2) {
      m = parseInt(parts[0], 10);
      secPart = parts[1];
    } else {
      h = parseInt(parts[0], 10);
      m = parseInt(parts[1], 10);
      secPart = parts[2];
    }
    const [sStr, fStr = "0"] = secPart.split(".");
    const s = parseInt(sStr, 10);
    const f = parseInt((fStr + "000").slice(0, 3), 10);
    if ([h, m, s, f].some((n) => Number.isNaN(n) || n < 0)) return null;
    return (h * 3600 + m * 60 + s) * 1000 + f;
  }

  function computeCps(cue) {
    if (!cue || cue.is_special) return 0;
    const text = (cue.target_text || cue.source_text || "").replace(/\s+/g, "");
    if (!text) return 0;
    const durSec = Math.max(0.4, (cue.end_ms - cue.start_ms) / 1000);
    return text.length / durSec;
  }

  function isCueLongWarning(cue) {
    if (!cue || cue.is_special) return false;
    const txt = (cue.target_text || "").trim();
    if (!txt) return false;
    const cps = computeCps(cue);
    const maxLine = Math.max(...txt.split("\n").map((l) => l.trim().length));
    return cps > 16 || maxLine > 34;
  }

  function toast(msg, type = "info") {
    const stack = $("toast-stack");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 220);
    }, 3600);
  }

  function captureDocSnapshotString() {
    if (!state.doc) return null;
    return JSON.stringify({
      cues: state.doc.cues,
      glossary: state.doc.glossary,
      scene_summary: state.doc.scene_summary,
    });
  }

  function pushRawUndoSnapshot(snap) {
    if (!snap) return;
    if (state.undoStack.length && state.undoStack[state.undoStack.length - 1] === snap) {
      return;
    }
    state.undoStack.push(snap);
    if (state.undoStack.length > 40) state.undoStack.shift();
    state.redoStack = [];
    refreshUndoButtons();
  }

  function pushUndoSnapshot() {
    pushRawUndoSnapshot(captureDocSnapshotString());
  }

  function askStudioPrompt(title, defaultValue = "", placeholder = "") {
    return new Promise((resolve) => {
      const modal = $("modal-prompt");
      const titleEl = $("prompt-modal-title");
      const inputEl = $("prompt-modal-input");
      const okBtn = $("btn-prompt-ok");
      const cancelBtn = $("btn-prompt-cancel");
      const closeX = $("btn-prompt-cancel-x");

      titleEl.textContent = title;
      inputEl.value = defaultValue || "";
      inputEl.placeholder = placeholder || "";
      modal.hidden = false;
      requestAnimationFrame(() => {
        inputEl.focus();
        inputEl.select();
      });

      const cleanup = (val) => {
        modal.hidden = true;
        okBtn.onclick = null;
        cancelBtn.onclick = null;
        closeX.onclick = null;
        inputEl.onkeydown = null;
        resolve(val);
      };

      okBtn.onclick = () => cleanup(inputEl.value);
      cancelBtn.onclick = () => cleanup(null);
      closeX.onclick = () => cleanup(null);
      inputEl.onkeydown = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          cleanup(null);
        } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
          e.preventDefault();
          cleanup(inputEl.value);
        }
      };
    });
  }

  function undo() {
    if (!state.doc || !state.undoStack.length) return;
    const curSnap = captureDocSnapshotString();
    if (curSnap) state.redoStack.push(curSnap);
    const prev = JSON.parse(state.undoStack.pop());
    state.doc.cues = prev.cues;
    state.doc.glossary = prev.glossary;
    state.doc.scene_summary = prev.scene_summary;
    $("scene-summary-input").value = state.doc.scene_summary || "";
    refreshUndoButtons();
    renderAll();
    const activeCue = state.doc.cues.find((c) => c.id === state.activeCueId);
    if (activeCue) updateStageOverlayForCue(activeCue);
    scheduleAutoSave();
  }

  function redo() {
    if (!state.doc || !state.redoStack.length) return;
    const curSnap = captureDocSnapshotString();
    if (curSnap) state.undoStack.push(curSnap);
    const next = JSON.parse(state.redoStack.pop());
    state.doc.cues = next.cues;
    state.doc.glossary = next.glossary;
    state.doc.scene_summary = next.scene_summary;
    $("scene-summary-input").value = state.doc.scene_summary || "";
    refreshUndoButtons();
    renderAll();
    const activeCue = state.doc.cues.find((c) => c.id === state.activeCueId);
    if (activeCue) updateStageOverlayForCue(activeCue);
    scheduleAutoSave();
  }

  function refreshUndoButtons() {
    $("btn-undo").disabled = state.undoStack.length === 0;
    $("btn-redo").disabled = state.redoStack.length === 0;
  }

  function scheduleAutoSave() {
    if (!state.doc) return;
    $("doc-save-state").textContent = "保存中…";
    try {
      if (state.doc.doc_id) {
        localStorage.setItem("listento_last_doc_id", state.doc.doc_id);
      }
      const serialized = JSON.stringify(state.doc);
      if (serialized.length < 1800000) {
        localStorage.setItem("listento_studio_last_doc", serialized);
      } else {
        localStorage.removeItem("listento_studio_last_doc");
      }
    } catch (_) {}
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(async () => {
      if (!state.doc) return;
      try {
        const resp = await fetch("/api/projects/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state.doc),
        });
        if (resp.ok) {
          const saved = await resp.json();
          if (saved.doc_id && state.doc) {
            state.doc.doc_id = saved.doc_id;
            try {
              localStorage.setItem("listento_last_doc_id", saved.doc_id);
            } catch (_) {}
          }
          $("doc-save-state").textContent = "已自动保存";
        } else {
          $("doc-save-state").textContent = "本地已缓存";
        }
      } catch (_) {
        $("doc-save-state").textContent = "本地已缓存";
      }
    }, 700);
  }

  // Wrap native <select> in a skin so it renders identically on WebKit / WebView2 / Chromium
  function skinSelect(sel) {
    if (!sel || sel.parentElement?.classList.contains("select-skin")) return;
    const wrap = document.createElement("span");
    wrap.className = ["select-skin", sel.classList.contains("input-full") ? "input-full" : "", sel.classList.contains("select-compact") ? "select-compact" : ""]
      .filter(Boolean)
      .join(" ");
    if (sel.title) wrap.title = sel.title;
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const label = document.createElement("span");
    label.className = "select-skin-label";
    wrap.appendChild(label);
    const sync = () => {
      const opt = sel.options[sel.selectedIndex];
      label.textContent = opt ? opt.text : "";
    };
    sel.addEventListener("change", sync);
    sel.addEventListener("input", sync);
    const proto = HTMLSelectElement.prototype;
    ["value", "selectedIndex"].forEach((prop) => {
      const desc = Object.getOwnPropertyDescriptor(proto, prop);
      Object.defineProperty(sel, prop, {
        configurable: true,
        get() {
          return desc.get.call(sel);
        },
        set(v) {
          desc.set.call(sel, v);
          sync();
        },
      });
    });
    new MutationObserver(sync).observe(sel, { childList: true, subtree: true });
    sync();
  }

  function skinAllSelects() {
    document.querySelectorAll("select").forEach(skinSelect);
  }

  // Initialization
  async function init() {
    const savedTheme = localStorage.getItem("listento_theme") || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);
    state.waveformCollapsed = localStorage.getItem("listento_waveform_collapsed") === "1";

    skinAllSelects();
    bindEvents();
    bindWaveformEvents();

    try {
      const resp = await fetch("/api/meta");
      const meta = await resp.json();
      state.meta = meta;
      state.settings = meta.settings;
      populateLanguageSelectors(meta.languages);
      populateSampleMenus(meta.samples);
      populateProviderPresets(meta.presets);
      populateAsrPresets(meta.asr_presets || []);
      refreshApiStatusUI();
      if (meta.runtime === "desktop" && meta.exports_dir) {
        $("export-footer-hint").innerHTML = `导出文件将直接保存至：<code>${escapeHtml(meta.exports_dir)}</code>`;
      }
    } catch (err) {
      toast("无法连接本地后端服务，请检查服务是否已启动。", "error");
    }

    // Restore last project from server disk first (safe for >5MB ASS files), fallback to localStorage
    let restored = false;
    try {
      const lastDocId = localStorage.getItem("listento_last_doc_id");
      if (lastDocId) {
        const pResp = await fetch(`/api/projects/${encodeURIComponent(lastDocId)}`);
        if (pResp.ok) {
          const diskDoc = await pResp.json();
          if (diskDoc && Array.isArray(diskDoc.cues) && diskDoc.cues.length > 0) {
            loadDocumentIntoStudio(diskDoc, false);
            restored = true;
          }
        }
      }
    } catch (_) {}

    if (!restored) {
      try {
        const cached = localStorage.getItem("listento_studio_last_doc");
        if (cached) {
          const parsed = JSON.parse(cached);
          if (parsed && Array.isArray(parsed.cues) && parsed.cues.length > 0) {
            loadDocumentIntoStudio(parsed, false);
            restored = true;
          }
        }
      } catch (_) {}
    }

    if (!restored) {
      loadWelcomeRecent();
      updateStatusBar();
    }
  }

  async function loadWelcomeRecent() {
    const wrap = $("welcome-recent-wrap");
    const list = $("welcome-recent");
    if (!wrap || !list) return;
    try {
      const resp = await fetch("/api/projects");
      const data = await resp.json();
      const items = (data.projects || []).slice(0, 3);
      if (!items.length) {
        wrap.hidden = true;
        return;
      }
      list.innerHTML = "";
      items.forEach((p) => {
        const pct = p.cue_count ? Math.round((p.translated_count * 100) / p.cue_count) : 0;
        const dt = p.updated_at ? new Date(p.updated_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "recent-item";
        btn.innerHTML = `
          ${icon("file")}
          <div class="recent-main">
            <span class="recent-name">${escapeHtml(p.filename)}</span>
            <span class="recent-sub">${escapeHtml((p.source_format || "srt").toUpperCase())} · ${p.cue_count} 条 · 已译 ${pct}% · ${escapeHtml(dt)}</span>
          </div>
          <span class="recent-progress"><span style="width:${pct}%"></span></span>
        `;
        btn.addEventListener("click", async () => {
          const r = await fetch(`/api/projects/${encodeURIComponent(p.doc_id)}`);
          if (r.ok) {
            loadDocumentIntoStudio(await r.json(), true);
            toast(`已打开：${p.filename}`, "success");
          } else {
            toast("该工程已不存在", "error");
            loadWelcomeRecent();
          }
        });
        list.appendChild(btn);
      });
      wrap.hidden = false;
    } catch (_) {
      wrap.hidden = true;
    }
  }

  function updateStatusBar() {
    const stats = $("sb-stats");
    if (!stats) return;
    const progWrap = $("sb-progress-wrap");
    const warnEl = $("sb-warn");
    if (!state.doc) {
      stats.textContent = "未打开文档";
      progWrap.hidden = true;
      warnEl.hidden = true;
      return;
    }
    const cues = state.doc.cues.filter((c) => !c.is_special);
    const translated = cues.filter((c) => (c.target_text || "").trim()).length;
    const longWarn = cues.filter((c) => isCueLongWarning(c)).length;
    const pct = cues.length ? Math.round((translated * 100) / cues.length) : 0;
    const lastEnd = state.doc.cues.length ? Math.max(...state.doc.cues.map((c) => c.end_ms)) : 0;
    stats.textContent = `${cues.length} 条字幕 · 时长 ${msToDisplay(lastEnd).slice(0, 8)}`;
    progWrap.hidden = false;
    $("sb-progress-fill").style.width = `${pct}%`;
    $("sb-progress-text").textContent = `已译 ${translated}/${cues.length}（${pct}%）`;
    warnEl.hidden = longWarn === 0;
    warnEl.textContent = `${longWarn} 条语速预警`;
  }

  function populateLanguageSelectors(languages) {
    const srcSel = $("select-source-lang");
    const tgtSel = $("select-target-lang");
    srcSel.innerHTML = "";
    tgtSel.innerHTML = "";
    for (const lang of languages) {
      const shortLabel = lang.label.split(" ")[0];
      const opt1 = document.createElement("option");
      opt1.value = lang.code;
      opt1.textContent = lang.code === "auto" ? "自动检测" : shortLabel;
      srcSel.appendChild(opt1);

      if (lang.code !== "auto") {
        const opt2 = document.createElement("option");
        opt2.value = lang.code;
        opt2.textContent = shortLabel;
        tgtSel.appendChild(opt2);
      }
    }
    srcSel.value = "auto";
    tgtSel.value = state.settings?.default_target_lang || "zh";
  }

  function populateSampleMenus(samples) {
    const menu = $("sample-menu");
    const cards = $("welcome-sample-cards");
    menu.innerHTML = "";
    cards.innerHTML = "";

    for (const s of samples) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dropdown-item";
      btn.innerHTML = `
        <div class="dropdown-item-top">
          <span>${escapeHtml(s.title)}</span>
          <span class="doc-tag">${escapeHtml(s.badge)}</span>
        </div>
        <div class="dropdown-item-desc">${escapeHtml(s.desc)}</div>
      `;
      btn.addEventListener("click", () => {
        closeMenus();
        loadSampleById(s.id);
      });
      menu.appendChild(btn);

      const card = document.createElement("div");
      card.className = "sample-card";
      card.innerHTML = `
        <span class="sample-card-badge">${escapeHtml(s.badge)}</span>
        <div class="sample-card-title">${escapeHtml(s.title)}</div>
        <div class="sample-card-desc">${escapeHtml(s.desc)}</div>
      `;
      card.addEventListener("click", () => loadSampleById(s.id));
      cards.appendChild(card);
    }
  }

  function populateProviderPresets(presets) {
    const grid = $("provider-grid");
    grid.innerHTML = "";
    for (const p of presets) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "provider-btn";
      btn.dataset.providerId = p.id;
      btn.innerHTML = `
        <div class="provider-btn-name">${escapeHtml(p.name)}</div>
        <div class="provider-btn-badge">${escapeHtml(p.badge)}</div>
      `;
      btn.addEventListener("click", () => selectProviderPreset(p.id, true));
      grid.appendChild(btn);
    }
  }

  let gridCurrentProvider = "deepseek";
  let currentCandidateModels = [];
  let isLiveFetchedModels = false;

  function renderModelPickerChips() {
    const chipWrap = $("model-chip-list");
    const badge = $("model-list-badge");
    if (!chipWrap || !badge) return;

    const filterKw = ($("cfg-model-filter")?.value || "").trim().toLowerCase();
    const activeModel = ($("cfg-model")?.value || "").trim();

    const filtered = filterKw
      ? currentCandidateModels.filter((m) => m.toLowerCase().includes(filterKw))
      : currentCandidateModels;

    badge.className = `model-source-badge${isLiveFetchedModels ? " live" : ""}`;
    badge.textContent = isLiveFetchedModels
      ? `已从接口拉取 (${filtered.length}/${currentCandidateModels.length})`
      : `内置最新推荐模型 (${filtered.length})`;

    chipWrap.innerHTML = "";
    if (!filtered.length) {
      const tip = document.createElement("div");
      tip.className = "model-empty-tip";
      tip.textContent = currentCandidateModels.length
        ? "无匹配过滤词的模型，可直接上方输入框填写任意模型 ID。"
        : "填入地址与 API Key 后点击右上侧「拉取可用模型」，或直接在上方输入框手填模型名。";
      chipWrap.appendChild(tip);
      return;
    }

    const frag = document.createDocumentFragment();
    filtered.slice(0, 300).forEach((modelId) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `model-chip${modelId === activeModel ? " active" : ""}`;
      btn.textContent = modelId;
      btn.addEventListener("click", () => {
        $("cfg-model").value = modelId;
        renderModelPickerChips();
      });
      frag.appendChild(btn);
    });
    chipWrap.appendChild(frag);
  }

  function selectProviderPreset(providerId, overwriteFields = false) {
    const presets = state.meta?.presets || [];
    const preset = presets.find((p) => p.id === providerId) || presets[0];
    if (!preset) return;

    document.querySelectorAll(".provider-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.providerId === preset.id);
    });
    $("provider-hint").textContent = preset.doc_hint || "";

    gridCurrentProvider = preset.id;
    currentCandidateModels = Array.isArray(preset.models) ? [...preset.models] : [];
    isLiveFetchedModels = false;
    if ($("cfg-model-filter")) $("cfg-model-filter").value = "";

    if (overwriteFields) {
      $("cfg-base-url").value = preset.base_url;
      $("cfg-model").value = preset.default_model;
    }
    renderModelPickerChips();
  }

  let currentAsrProvider = "siliconflow";
  let selectedAsrFile = null;
  let selectedAsrDurationSec = 0;

  function populateAsrPresets(presets) {
    const grid = $("asr-provider-grid");
    if (!grid) return;
    grid.innerHTML = "";
    presets.forEach((p) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "provider-btn";
      btn.dataset.asrProviderId = p.id;
      btn.innerHTML = `
        <div class="provider-btn-name">${escapeHtml(p.name)}</div>
        <div class="provider-btn-badge">${escapeHtml(p.badge)}</div>
      `;
      btn.addEventListener("click", () => selectAsrPreset(p.id, true));
      grid.appendChild(btn);
    });
  }

  function selectAsrPreset(providerId, overwriteFields = false) {
    const presets = state.meta?.asr_presets || [];
    const preset = presets.find((p) => p.id === providerId) || presets[0];
    if (!preset) return;
    currentAsrProvider = preset.id;

    document.querySelectorAll("[data-asr-provider-id]").forEach((b) => {
      b.classList.toggle("active", b.dataset.asrProviderId === preset.id);
    });
    if ($("asr-provider-hint")) $("asr-provider-hint").textContent = preset.doc_hint || "";

    if (overwriteFields) {
      $("asr-base-url").value = preset.base_url;
      $("asr-model").value = preset.default_model;
    }

    const chipWrap = $("asr-model-chips");
    if (chipWrap) {
      chipWrap.innerHTML = "";
      (preset.models || []).forEach((m) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = `model-chip${$("asr-model").value.trim() === m ? " active" : ""}`;
        chip.textContent = m;
        chip.onclick = () => {
          $("asr-model").value = m;
          selectAsrPreset(currentAsrProvider, false);
        };
        chipWrap.appendChild(chip);
      });
    }
  }

  function prepareAsrMediaFile(file) {
    selectedAsrFile = file;
    selectedAsrDurationSec = 0;
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    $("asr-file-label").textContent = `已选：${file.name} (${sizeMb} MB · 正在读取时长…)`;

    const probe = document.createElement("video");
    probe.preload = "metadata";
    const objUrl = URL.createObjectURL(file);
    probe.onloadedmetadata = () => {
      if (Number.isFinite(probe.duration) && probe.duration > 0) {
        selectedAsrDurationSec = probe.duration;
        $("asr-file-label").textContent = `已选：${file.name} (${sizeMb} MB · 时长 ${Math.round(probe.duration)} 秒)`;
      } else {
        $("asr-file-label").textContent = `已选：${file.name} (${sizeMb} MB)`;
      }
      URL.revokeObjectURL(objUrl);
    };
    probe.onerror = () => {
      $("asr-file-label").textContent = `已选：${file.name} (${sizeMb} MB)`;
      URL.revokeObjectURL(objUrl);
    };
    probe.src = objUrl;
  }

  function refreshApiStatusUI() {
    if (!state.settings) return;
    const btn = $("btn-open-settings");
    const label = $("api-status-label");
    const baseUrl = (state.settings.base_url || "").toLowerCase();
    const isLocal =
      baseUrl.includes("localhost") ||
      baseUrl.includes("127.0.0.1") ||
      baseUrl.includes("host.docker.internal") ||
      baseUrl.includes("://ollama");
    const ready = state.settings.has_api_key || isLocal;

    btn.classList.toggle("ready", ready);
    label.textContent = ready ? `${state.settings.model}` : "配置 AI 引擎";
    $("select-trans-mode").value = state.settings.default_mode || "reflective";
    const sbModel = $("sb-model");
    if (sbModel) {
      sbModel.textContent = ready ? `模型 ${state.settings.model}` : "尚未配置 AI 引擎";
    }
  }

  async function loadSampleById(sampleId) {
    try {
      const resp = await fetch(`/api/samples/${encodeURIComponent(sampleId)}`);
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "加载示例失败");
      }
      const doc = await resp.json();
      loadDocumentIntoStudio(doc, true);
      toast(`已载入示例：${doc.filename}`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function uploadSubtitleFile(file) {
    const form = new FormData();
    form.append("file", file);
    form.append("encoding", "auto");
    try {
      const resp = await fetch("/api/subtitles/parse", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "字幕解析失败");
      loadDocumentIntoStudio(data, true);
      toast(`成功载入 ${data.filename}（共 ${data.cues.length} 条）`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function mountMediaFile(file) {
    const video = $("media-player");
    const url = URL.createObjectURL(file);
    stopSimPlayback();
    state.hasMediaFile = true;
    video.src = url;
    video.hidden = false;
    $("stage-placeholder").hidden = true;
    toast(`已挂载本地媒体：${file.name}`, "success");
    decodeMediaWaveform(file);
  }

  async function decodeMediaWaveform(file) {
    state.waveformPeaks = null;
    state.waveformDurationMs = 0;
    const statusEl = $("waveform-status");
    if (!file) return;

    if (file.size > 140 * 1024 * 1024) {
      if (statusEl) {
        statusEl.textContent = "大体积媒体 · 免解码模式（仍可拖动色块打轴）";
      }
      drawWaveformTimeline();
      return;
    }

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) {
      drawWaveformTimeline();
      return;
    }

    if (statusEl) statusEl.textContent = "正在解析音轨波形…";
    try {
      const arrayBuf = await file.arrayBuffer();
      const ctx = new AudioCtx();
      let audioBuf;
      try {
        audioBuf = await ctx.decodeAudioData(arrayBuf);
      } finally {
        try {
          await ctx.close();
        } catch (_) {}
      }

      const ch0 = audioBuf.getChannelData(0);
      const ch1 = audioBuf.numberOfChannels > 1 ? audioBuf.getChannelData(1) : null;
      const bucketCount = Math.min(12000, Math.max(1200, Math.ceil(audioBuf.duration * 40)));
      const samplesPerBucket = Math.max(1, Math.floor(ch0.length / bucketCount));
      const peaks = new Float32Array(bucketCount);
      const sortedSample = [];

      for (let i = 0; i < bucketCount; i++) {
        const start = i * samplesPerBucket;
        const end = Math.min(ch0.length, start + samplesPerBucket);
        const stride = Math.max(1, Math.floor((end - start) / 64));
        let maxAmp = 0;
        for (let j = start; j < end; j += stride) {
          const v = ch1 ? (Math.abs(ch0[j]) + Math.abs(ch1[j])) * 0.5 : Math.abs(ch0[j]);
          if (v > maxAmp) maxAmp = v;
        }
        peaks[i] = maxAmp;
        if (i % 4 === 0) sortedSample.push(maxAmp);
      }

      sortedSample.sort((a, b) => a - b);
      const normRef = Math.max(0.02, sortedSample[Math.floor(sortedSample.length * 0.98)] || 0.5);
      for (let i = 0; i < bucketCount; i++) {
        peaks[i] = Math.min(1, peaks[i] / normRef);
      }

      state.waveformPeaks = peaks;
      state.waveformDurationMs = Math.round(audioBuf.duration * 1000);
      if (statusEl) {
        statusEl.textContent = "波形已就绪 · 拖动色块平移 · 拖动边缘修剪";
      }
      drawWaveformTimeline();
    } catch (_) {
      if (statusEl) {
        statusEl.textContent = "无法解码音轨 · 仍可拖动色块打轴";
      }
      drawWaveformTimeline();
    }
  }

  function syncWaveformVisibilityUI() {
    const dock = $("timeline-dock");
    const btn = $("btn-toggle-waveform");
    if (!dock || !btn) return;
    const show = Boolean(state.doc) && !state.waveformCollapsed;
    dock.hidden = !show;
    btn.classList.toggle("active-toggle", show);
    if (show) {
      requestAnimationFrame(() => drawWaveformTimeline());
    }
  }

  function loadDocumentIntoStudio(doc, resetHistory = true) {
    state.doc = doc;
    state.cueSuggestions = {};
    state.renderLimit = 250;
    if (resetHistory) {
      state.undoStack = [];
      state.redoStack = [];
      refreshUndoButtons();
    }

    $("welcome-state").hidden = true;
    $("cue-table-wrap").hidden = false;
    $("doc-meta-pill").hidden = false;
    $("doc-filename").textContent = doc.filename;
    $("doc-format-badge").textContent = (doc.source_format || "srt").toUpperCase();
    $("doc-save-state").textContent = "已就绪";

    $("select-source-lang").value = doc.source_language || "auto";
    $("select-target-lang").value = doc.target_language || "zh";
    $("select-genre").value = doc.genre || "general";
    $("scene-summary-input").value = doc.scene_summary || "";
    $("custom-prompt-input").value = doc.custom_prompt || "";

    const hasDoc = true;
    $("btn-open-export").disabled = !hasDoc;
    $("btn-translate-all").disabled = !hasDoc;
    $("btn-translate-untranslated").disabled = !hasDoc;
    $("btn-toggle-waveform").disabled = !hasDoc;
    $("btn-open-qa").disabled = !hasDoc;
    $("btn-split-bilingual").disabled = !hasDoc;
    $("btn-time-shift").disabled = !hasDoc;
    $("btn-toggle-search").disabled = !hasDoc;
    $("btn-clear-translations").disabled = !hasDoc;
    $("btn-extract-glossary").disabled = !hasDoc;

    const firstDialogue = doc.cues.find((c) => !c.is_special) || doc.cues[0];
    state.activeCueId = firstDialogue ? firstDialogue.id : null;
    state.simTimeMs = firstDialogue ? firstDialogue.start_ms : 0;

    renderWarnings();
    syncWaveformVisibilityUI();
    renderAll(false);
    if (firstDialogue) updateStageOverlayForCue(firstDialogue);
    scheduleAutoSave();
  }

  function closeCurrentDocument() {
    if (!state.doc) return;
    if (state.abortController) state.abortController.abort();
    stopSimPlayback();
    clearTimeout(state.saveTimer);
    state.doc = null;
    state.activeCueId = null;
    state.cueSuggestions = {};
    state.undoStack = [];
    state.redoStack = [];
    refreshUndoButtons();

    try {
      localStorage.removeItem("listento_last_doc_id");
      localStorage.removeItem("listento_studio_last_doc");
    } catch (_) {}

    $("cue-table-wrap").hidden = true;
    $("doc-meta-pill").hidden = true;
    $("warnings-strip").hidden = true;
    $("timeline-dock").hidden = true;
    $("search-bar").hidden = true;
    $("shift-bar").hidden = true;
    $("welcome-state").hidden = false;

    [
      "btn-open-export",
      "btn-translate-all",
      "btn-translate-untranslated",
      "btn-toggle-waveform",
      "btn-open-qa",
      "btn-split-bilingual",
      "btn-time-shift",
      "btn-toggle-search",
      "btn-clear-translations",
      "btn-extract-glossary",
    ].forEach((id) => {
      if ($(id)) $(id).disabled = true;
    });
    $("btn-toggle-waveform").classList.remove("active-toggle");

    $("count-all").textContent = "0";
    $("count-untranslated").textContent = "0";
    $("count-translated").textContent = "0";
    $("count-long").textContent = "0";
    $("glossary-count").textContent = "0";
    $("glossary-list").innerHTML = `<div class="empty-mini">添加人名或专有名词后，AI 翻译会严格统一译法</div>`;
    $("overlay-primary").textContent = "";
    $("overlay-secondary").textContent = "";
    toast("已关闭当前字幕文档（历史进度仍可在「文件 → 历史工程」随时恢复）", "info");
    updateStatusBar();
    loadWelcomeRecent();
  }

  function clearAllTranslations() {
    if (!state.doc) return;
    const hasAny = state.doc.cues.some((c) => !c.is_special && (c.target_text || "").trim().length > 0);
    if (!hasAny) {
      toast("当前文档暂无译文可清空。", "info");
      return;
    }
    pushUndoSnapshot();
    state.doc.cues.forEach((c) => {
      if (!c.is_special) {
        c.target_text = "";
        c.status = "untranslated";
      }
    });
    state.cueSuggestions = {};
    renderAll(true);
    const cur = state.doc.cues.find((c) => c.id === state.activeCueId);
    if (cur) updateStageOverlayForCue(cur);
    scheduleAutoSave();
    toast("已清空当前文档全部译文（按 Cmd/Ctrl+Z 可一键撤销恢复）", "success");
  }

  function renderWarnings() {
    const strip = $("warnings-strip");
    const warnings = state.doc?.warnings || [];
    if (!warnings.length) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    strip.innerHTML = `
      <span style="display:inline-flex;align-items:center;gap:8px;">${icon("alert")}${escapeHtml(warnings.join(" ｜ "))}</span>
      <button class="btn btn-ghost btn-icon btn-sm" id="btn-dismiss-warn" type="button">${icon("x")}</button>
    `;
    $("btn-dismiss-warn").onclick = () => {
      state.doc.warnings = [];
      strip.hidden = true;
    };
  }

  function renderAll(preserveScroll = true) {
    if (!state.doc) return;
    updateCounts();
    renderGlossary();
    renderCueList(preserveScroll);
  }

  function updateCounts() {
    const cues = (state.doc?.cues || []).filter((c) => !c.is_special);
    const translated = cues.filter((c) => (c.target_text || "").trim().length > 0).length;
    const untranslated = cues.length - translated;
    const longWarn = cues.filter((c) => isCueLongWarning(c)).length;

    $("count-all").textContent = cues.length;
    $("count-untranslated").textContent = untranslated;
    $("count-translated").textContent = translated;
    $("count-long").textContent = longWarn;
    updateStatusBar();
  }

  function renderGlossary() {
    const list = $("glossary-list");
    const glossary = state.doc?.glossary || [];
    $("glossary-count").textContent = glossary.length;

    if (!glossary.length) {
      list.innerHTML = `<div class="empty-mini">添加人名或专有名词后，AI 翻译会严格统一译法</div>`;
      return;
    }

    list.innerHTML = "";
    glossary.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "glossary-item";
      row.innerHTML = `
        <div class="glossary-pair" title="${escapeHtml(item.note || "")}">
          <input type="checkbox" ${item.enabled !== false ? "checked" : ""} data-g-toggle="${idx}" />
          <span class="glossary-src">${escapeHtml(item.source)}</span>
          <span class="lang-arrow">→</span>
          <span class="glossary-tgt">${escapeHtml(item.target)}</span>
        </div>
        <button class="btn btn-ghost btn-icon btn-sm" data-g-del="${idx}" type="button" title="删除">${icon("x")}</button>
      `;
      list.appendChild(row);
    });
  }

  function getFilteredCues() {
    if (!state.doc) return [];
    const q = state.searchQuery.trim().toLowerCase();
    let matchCount = 0;

    const filtered = state.doc.cues.filter((cue) => {
      if (state.filter === "untranslated") {
        if (cue.is_special || (cue.target_text || "").trim()) return false;
      } else if (state.filter === "translated") {
        if (cue.is_special || !(cue.target_text || "").trim()) return false;
      } else if (state.filter === "long") {
        if (!isCueLongWarning(cue)) return false;
      }

      if (q) {
        const inSrc = (cue.source_text || "").toLowerCase().includes(q);
        const inTgt = (cue.target_text || "").toLowerCase().includes(q);
        const matched =
          state.searchScope === "source" ? inSrc : state.searchScope === "target" ? inTgt : inSrc || inTgt;
        if (matched) matchCount++;
        return matched;
      }
      return true;
    });

    if ($("search-bar") && !$("search-bar").hidden) {
      $("search-match-count").textContent = q ? `匹配 ${matchCount} 条` : "";
    }
    return filtered;
  }

  function estimateTextareaRows(text) {
    if (!text) return 1;
    const lines = String(text).split("\n");
    let rows = 0;
    for (const line of lines) {
      rows += Math.max(1, Math.ceil(line.length / 28));
    }
    return Math.min(8, Math.max(1, rows));
  }

  function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(32, el.scrollHeight + 2)}px`;
  }

  function createCueRowElement(cue) {
    const row = document.createElement("div");
    const durSec = ((cue.end_ms - cue.start_ms) / 1000).toFixed(1);
    const cps = computeCps(cue);
    const longWarn = isCueLongWarning(cue);
    const hasTgt = Boolean((cue.target_text || "").trim());

    row.className = [
      "cue-row",
      cue.id === state.activeCueId ? "active" : "",
      cue.is_special ? "special-row" : "",
      !cue.is_special && hasTgt && !longWarn ? "is-done" : "",
      longWarn ? "is-warn" : "",
    ]
      .filter(Boolean)
      .join(" ");
    row.dataset.cueId = cue.id;
    row.addEventListener("contextmenu", (e) => {
      if (e.target.closest("textarea, input") && window.getSelection()?.toString()) return;
      e.preventDefault();
      activateCue(cue.id, false);
      openCueContextMenu(cue, e.clientX, e.clientY);
    });

    // Column 1: Meta
    const metaCol = document.createElement("div");
    metaCol.className = "cue-meta";
    metaCol.innerHTML = `
      <div class="cue-id-line">
        <span class="cue-num">#${cue.id}</span>
        <span class="badge-mini">${durSec}s</span>
        ${cue.speaker ? `<span class="badge-mini badge-speaker">${escapeHtml(cue.speaker)}</span>` : ""}
        ${cue.style && cue.style !== "Default" ? `<span class="badge-mini">${escapeHtml(cue.style)}</span>` : ""}
        ${cue.is_special ? `<span class="badge-mini">特效保留</span>` : ""}
        ${!cue.is_special && hasTgt && !longWarn ? `<span class="badge-mini badge-done">已译</span>` : ""}
        ${longWarn ? `<span class="badge-mini badge-cps-warn" title="每秒约 ${cps.toFixed(1)} 字，建议右键 → 精简压缩字数">${cps.toFixed(0)}字/s</span>` : ""}
      </div>
      <div class="cue-times">
        <input class="time-input" data-time-field="start_ms" value="${msToDisplay(cue.start_ms)}" title="起始时间 (点击微调)" />
        <input class="time-input" data-time-field="end_ms" value="${msToDisplay(cue.end_ms)}" title="结束时间 (点击微调)" />
      </div>
    `;

    // Column 2: Source
    const srcCol = document.createElement("div");
    srcCol.className = "cue-source-box";
    srcCol.title = "点击定位预览；双击可微调原文";
    if (cue.ass_tags) {
      const tagChip = document.createElement("div");
      tagChip.className = "ass-tag-chip";
      tagChip.textContent = cue.ass_tags;
      srcCol.appendChild(tagChip);
    }
    const srcSpan = document.createElement("div");
    srcSpan.textContent = cue.source_text;
    srcCol.appendChild(srcSpan);

    srcCol.addEventListener("click", () => activateCue(cue.id, true));
    srcCol.addEventListener("dblclick", async () => {
      const edited = await askStudioPrompt(`编辑 #${cue.id} 原文`, cue.source_text, "输入修改后的原文字幕…");
      if (edited !== null && edited !== cue.source_text) {
        pushUndoSnapshot();
        cue.source_text = edited;
        renderAll(true);
        scheduleAutoSave();
      }
    });

    // Column 3: Target Editor + AI Suggestions
    const tgtWrap = document.createElement("div");
    tgtWrap.className = "cue-target-wrap";

    const textarea = document.createElement("textarea");
    textarea.className = "cue-target-input";
    textarea.rows = estimateTextareaRows(cue.target_text);
    textarea.disabled = cue.is_special;
    textarea.placeholder = cue.is_special ? "ASS 特效 / 注释行，导出时原样保留" : "输入译文…";
    textarea.value = cue.target_text || "";
    textarea.dataset.cueInput = cue.id;

    let focusSnap = null;
    let focusInitialVal = "";
    let pushedFocusUndo = false;

    textarea.addEventListener("focus", () => {
      focusSnap = captureDocSnapshotString();
      focusInitialVal = cue.target_text || "";
      pushedFocusUndo = false;
      autoResizeTextarea(textarea);
      activateCue(cue.id, false);
    });

    textarea.addEventListener("input", () => {
      if (!pushedFocusUndo && textarea.value !== focusInitialVal && focusSnap) {
        pushRawUndoSnapshot(focusSnap);
        pushedFocusUndo = true;
      }
      autoResizeTextarea(textarea);
      cue.target_text = textarea.value;
      cue.status = textarea.value.trim() ? "edited" : "untranslated";
      updateStageOverlayForCue(cue);
      updateCounts();
      drawWaveformTimeline();
      scheduleAutoSave();
    });

    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const allInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
        const idx = allInputs.indexOf(textarea);
        const nextIdx = e.shiftKey ? idx - 1 : idx + 1;
        if (!e.shiftKey && nextIdx >= allInputs.length) {
          appendMoreCueRows(250);
        }
        const refreshedInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
        if (refreshedInputs[nextIdx]) {
          refreshedInputs[nextIdx].focus();
        }
      } else if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        const allInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
        const idx = allInputs.indexOf(textarea);
        const nextIdx = e.key === "ArrowUp" ? idx - 1 : idx + 1;
        if (e.key === "ArrowDown" && nextIdx >= allInputs.length) {
          appendMoreCueRows(250);
        }
        const refreshedInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
        if (refreshedInputs[nextIdx]) {
          refreshedInputs[nextIdx].focus();
          const targetCueId = Number(refreshedInputs[nextIdx].dataset.cueInput);
          if (targetCueId) activateCue(targetCueId, true);
        }
      } else if (e.altKey && e.key === "Enter") {
        e.preventDefault();
        runSingleCueRewrite(cue, "retranslate", "");
      }
    });

    tgtWrap.appendChild(textarea);

    // Render AI single-cue suggestions if present
    const sug = state.cueSuggestions[cue.id];
    if (sug) {
      const sugBox = document.createElement("div");
      sugBox.className = "ai-suggestions";
      const allOptions = [sug.best, ...(sug.alternatives || [])].filter(Boolean);
      sugBox.innerHTML = `
        <div class="ai-sug-reason">${icon("sparkles")}<span>${escapeHtml(sug.reason || "AI 候选方案，点击即可采纳")}</span></div>
        <div class="ai-sug-chips"></div>
      `;
      const chipsWrap = sugBox.querySelector(".ai-sug-chips");
      allOptions.forEach((optText, i) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "sug-chip";
        chip.textContent = `${i === 0 ? "推荐：" : ""}${optText}`;
        chip.addEventListener("click", () => {
          pushUndoSnapshot();
          cue.target_text = optText;
          cue.status = "polished";
          delete state.cueSuggestions[cue.id];
          renderCueList(true);
          updateCounts();
          updateStageOverlayForCue(cue);
          scheduleAutoSave();
        });
        chipsWrap.appendChild(chip);
      });
      tgtWrap.appendChild(sugBox);
    }

    // Column 4: Row Actions
    const actCol = document.createElement("div");
    actCol.className = "cue-actions";
    actCol.innerHTML = `
      <button class="btn btn-icon btn-sm" data-act="play" type="button" title="定位预览">${icon("play")}</button>
      <button class="btn btn-icon btn-sm" data-act="ai" type="button" ${cue.is_special ? "disabled" : ""} title="AI 精修">${icon("sparkles")}</button>
      <button class="btn btn-icon btn-sm" data-act="more" type="button" title="更多操作（也可右键）">${icon("more")}</button>
    `;

    actCol.querySelector('[data-act="play"]').addEventListener("click", () => {
      activateCue(cue.id, true);
    });
    actCol.querySelector('[data-act="ai"]').addEventListener("click", (e) => {
      e.stopPropagation();
      toggleAiRowPopover(cue, actCol);
    });
    actCol.querySelector('[data-act="more"]').addEventListener("click", (e) => {
      e.stopPropagation();
      toggleRowManagePopover(cue, actCol);
    });

    // Bind timestamp inline edits
    metaCol.querySelectorAll(".time-input").forEach((inp) => {
      inp.addEventListener("change", () => {
        const field = inp.dataset.timeField;
        const parsedMs = parseDisplayToMs(inp.value);
        if (parsedMs === null) {
          inp.value = msToDisplay(cue[field]);
          toast("时间格式应为 HH:MM:SS.mmm", "error");
          return;
        }
        pushUndoSnapshot();
        cue[field] = parsedMs;
        if (cue.end_ms <= cue.start_ms) cue.end_ms = cue.start_ms + 300;
        renderCueList(true);
        scheduleAutoSave();
      });
    });

    row.appendChild(metaCol);
    row.appendChild(srcCol);
    row.appendChild(tgtWrap);
    row.appendChild(actCol);
    return row;
  }

  function createLoadMoreBanner(renderedCount, totalCount) {
    const banner = document.createElement("div");
    banner.className = "load-more-banner";
    banner.id = "cue-load-more-banner";
    const remain = totalCount - renderedCount;
    banner.innerHTML = `
      <span>已高效渲染前 <strong>${renderedCount}</strong> / ${totalCount} 条字幕</span>
      <button class="btn btn-subtle btn-sm" type="button">继续加载后续 ${Math.min(250, remain)} 条</button>
    `;
    banner.querySelector("button").addEventListener("click", () => appendMoreCueRows(250));
    return banner;
  }

  function appendMoreCueRows(step = 250) {
    const container = $("cue-list");
    const cues = getFilteredCues();
    if (state.renderLimit >= cues.length) return;

    const oldBanner = $("cue-load-more-banner");
    if (oldBanner) oldBanner.remove();

    const prevLimit = state.renderLimit;
    state.renderLimit = Math.min(cues.length, state.renderLimit + step);
    const slice = cues.slice(prevLimit, state.renderLimit);

    const frag = document.createDocumentFragment();
    slice.forEach((cue) => frag.appendChild(createCueRowElement(cue)));
    if (state.renderLimit < cues.length) {
      frag.appendChild(createLoadMoreBanner(state.renderLimit, cues.length));
    }
    container.appendChild(frag);
  }

  function ensureCueRendered(cueId) {
    const cues = getFilteredCues();
    const idx = cues.findIndex((c) => c.id === cueId);
    if (idx >= state.renderLimit) {
      state.renderLimit = Math.min(cues.length, idx + 120);
      renderCueList(true);
    }
  }

  function renderCueList(preserveScroll = true) {
    const container = $("cue-list");
    const prevScroll = preserveScroll ? container.scrollTop : 0;
    const cues = getFilteredCues();
    container.innerHTML = "";

    const visible = cues.slice(0, state.renderLimit);
    const frag = document.createDocumentFragment();
    visible.forEach((cue) => frag.appendChild(createCueRowElement(cue)));
    if (cues.length > visible.length) {
      frag.appendChild(createLoadMoreBanner(visible.length, cues.length));
    }
    container.appendChild(frag);

    if (preserveScroll) {
      container.scrollTop = prevScroll;
    }
    drawWaveformTimeline();
  }

  function closeAllPopovers() {
    document.querySelectorAll(".row-popover").forEach((el) => el.remove());
  }

  function getAiCueActions(cue) {
    const defs = [
      { key: "retranslate", icon: "refresh", label: "结合上下文重新翻译", hint: "Alt+↵" },
      { key: "natural", icon: "chat", label: "润色得更口语自然" },
      { key: "shorten", icon: "shrink", label: "精简压缩字数" },
      { key: "formal", icon: "briefcase", label: "改为书面专业表达" },
      { key: "custom", icon: "pen", label: "自定义修改指令…" },
    ];
    return defs.map((d) => ({
      icon: d.icon,
      label: d.label,
      hint: d.hint || "",
      disabled: cue.is_special,
      fn: async () => {
        let customInst = "";
        if (d.key === "custom") {
          const res = await askStudioPrompt(
            `#${cue.id} 自定义 AI 修改指令`,
            "",
            "例如：改成敬语口吻 / 保留英文专有名词 / 控制在 12 个字以内…"
          );
          if (!res || !res.trim()) return;
          customInst = res.trim();
        }
        await runSingleCueRewrite(cue, d.key, customInst);
      },
    }));
  }

  function getManageCueActions(cue) {
    return [
      {
        icon: "scissors",
        label: "按比例拆分为两条",
        fn: async () => {
          try {
            const resp = await fetch("/api/subtitles/split-cue", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ cues: state.doc.cues, cue_id: cue.id }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || "拆分失败");
            pushUndoSnapshot();
            state.doc.cues = data.cues;
            renderAll();
            scheduleAutoSave();
            toast(`已将 #${cue.id} 拆分为两条字幕`, "success");
          } catch (err) {
            toast(err.message, "error");
          }
        },
      },
      {
        icon: "plus",
        label: "在下方插入新行",
        fn: () => {
          pushUndoSnapshot();
          const idx = state.doc.cues.findIndex((c) => c.id === cue.id);
          const newStart = cue.end_ms + 100;
          state.doc.cues.splice(idx + 1, 0, {
            id: 0,
            start_ms: newStart,
            end_ms: newStart + 2200,
            source_text: "New subtitle line",
            target_text: "",
            speaker: cue.speaker || "",
            style: cue.style || "Default",
            ass_tags: "",
            raw_event_meta: {},
            is_special: false,
            status: "untranslated",
            note: "",
          });
          reindexCues();
          renderAll();
          scheduleAutoSave();
        },
      },
      {
        icon: "merge",
        label: "与下一条合并",
        fn: () => {
          const idx = state.doc.cues.findIndex((c) => c.id === cue.id);
          const next = state.doc.cues[idx + 1];
          if (!next) {
            toast("已经是最后一条字幕。", "info");
            return;
          }
          pushUndoSnapshot();
          cue.end_ms = Math.max(cue.end_ms, next.end_ms);
          cue.source_text = [cue.source_text, next.source_text].filter(Boolean).join("\n");
          cue.target_text = [cue.target_text, next.target_text].filter(Boolean).join("\n");
          state.doc.cues.splice(idx + 1, 1);
          reindexCues();
          renderAll();
          scheduleAutoSave();
        },
      },
      {
        icon: "trash",
        label: "删除此条",
        danger: true,
        fn: () => {
          if (state.doc.cues.length <= 1) {
            toast("至少需保留一条字幕。", "error");
            return;
          }
          pushUndoSnapshot();
          state.doc.cues = state.doc.cues.filter((c) => c.id !== cue.id);
          reindexCues();
          renderAll();
          scheduleAutoSave();
        },
      },
    ];
  }

  // Build menu buttons (shared by row popovers and the right-click context menu)
  function buildMenuButtons(container, groups, onPick) {
    groups.forEach((items, gi) => {
      if (gi > 0) {
        const sep = document.createElement("div");
        sep.className = "menu-sep";
        container.appendChild(sep);
      }
      items.forEach((it) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = `pop-btn${it.danger ? " danger" : ""}`;
        b.disabled = Boolean(it.disabled);
        b.innerHTML = `${icon(it.icon)}<span style="flex:1">${escapeHtml(it.label)}</span>${
          it.hint ? `<kbd style="background:transparent;border:none;color:var(--fg-3)">${escapeHtml(it.hint)}</kbd>` : ""
        }`;
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          onPick();
          it.fn();
        });
        container.appendChild(b);
      });
    });
  }

  function toggleAiRowPopover(cue, container) {
    const existing = container.querySelector(".row-popover");
    closeAllPopovers();
    if (existing) return;
    const pop = document.createElement("div");
    pop.className = "row-popover";
    buildMenuButtons(pop, [getAiCueActions(cue)], closeAllPopovers);
    container.appendChild(pop);
  }

  function toggleRowManagePopover(cue, container) {
    const existing = container.querySelector(".row-popover");
    closeAllPopovers();
    if (existing) return;
    const pop = document.createElement("div");
    pop.className = "row-popover";
    buildMenuButtons(pop, [getManageCueActions(cue)], closeAllPopovers);
    container.appendChild(pop);
  }

  function closeContextMenu() {
    const menu = $("ctx-menu");
    if (menu && !menu.hidden) {
      menu.hidden = true;
      menu.innerHTML = "";
    }
  }

  function openCueContextMenu(cue, x, y) {
    closeAllPopovers();
    closeMenus();
    const menu = $("ctx-menu");
    menu.innerHTML = `<div class="menu-label">#${cue.id} · ${escapeHtml(msToDisplay(cue.start_ms).slice(3))}</div>`;
    const playItem = {
      icon: "play",
      label: "定位并预览",
      fn: () => activateCue(cue.id, true),
    };
    buildMenuButtons(menu, [[playItem], getAiCueActions(cue), getManageCueActions(cue)], closeContextMenu);
    menu.hidden = false;
    const rect = menu.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - rect.width - 8);
    const top = Math.min(y, window.innerHeight - rect.height - 8);
    menu.style.left = `${Math.max(8, left)}px`;
    menu.style.top = `${Math.max(8, top)}px`;
  }

  function stepActiveCue(delta) {
    if (!state.doc) return;
    const list = getFilteredCues().filter((c) => !c.is_special);
    if (!list.length) return;
    let idx = list.findIndex((c) => c.id === state.activeCueId);
    idx = idx === -1 ? 0 : Math.min(list.length - 1, Math.max(0, idx + delta));
    const target = list[idx];
    activateCue(target.id, true);
    const rowEl = document.querySelector(`.cue-row[data-cue-id="${target.id}"]`);
    if (rowEl) rowEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function reindexCues() {
    state.doc.cues.forEach((c, i) => {
      c.id = i + 1;
    });
  }

  // Activate Cue & Sync Cinema Stage
  function activateCue(cueId, seekMedia = false) {
    state.activeCueId = cueId;
    ensureCueRendered(cueId);
    document.querySelectorAll(".cue-row").forEach((r) => {
      r.classList.toggle("active", Number(r.dataset.cueId) === cueId);
    });
    const cue = state.doc?.cues.find((c) => c.id === cueId);
    if (!cue) return;

    updateStageOverlayForCue(cue);
    if (seekMedia) {
      if (state.hasMediaFile) {
        const video = $("media-player");
        video.currentTime = cue.start_ms / 1000;
        drawWaveformTimeline();
      } else {
        state.simTimeMs = cue.start_ms;
        updatePlayerControlsUI(cue.start_ms);
      }
    } else {
      drawWaveformTimeline();
    }
  }

  function updateStageOverlayForCue(cue) {
    const pEl = $("overlay-primary");
    const sEl = $("overlay-secondary");
    if (!cue || cue.is_special) {
      pEl.textContent = "";
      sEl.textContent = "";
      return;
    }
    const src = (cue.source_text || "").trim();
    const tgt = (cue.target_text || "").trim();

    if (state.previewSubview === "source") {
      pEl.textContent = src;
      sEl.textContent = "";
    } else if (state.previewSubview === "target") {
      pEl.textContent = tgt || src;
      sEl.textContent = "";
    } else {
      // bilingual
      if (tgt) {
        pEl.textContent = tgt;
        sEl.textContent = src;
      } else {
        pEl.textContent = src;
        sEl.textContent = "（待翻译）";
      }
    }
    $("player-timecode").textContent = msToDisplay(cue.start_ms);
  }

  function getMaxDurationMs() {
    if (state.hasMediaFile) {
      const video = $("media-player");
      if (video.duration && Number.isFinite(video.duration)) {
        return video.duration * 1000;
      }
    }
    const cues = state.doc?.cues || [];
    return cues.length ? Math.max(...cues.map((c) => c.end_ms)) : 10000;
  }

  function updatePlayerControlsUI(currentMs) {
    $("player-timecode").textContent = msToDisplay(currentMs);
    const maxMs = Math.max(1, getMaxDurationMs());
    $("player-scrubber").value = Math.min(1000, Math.round((currentMs / maxMs) * 1000));

    if (!state.doc) return;
    const hit = state.doc.cues.find((c) => !c.is_special && currentMs >= c.start_ms && currentMs <= c.end_ms);
    if (hit) {
      if (state.activeCueId !== hit.id) {
        state.activeCueId = hit.id;
        ensureCueRendered(hit.id);
        document.querySelectorAll(".cue-row").forEach((r) => {
          const match = Number(r.dataset.cueId) === hit.id;
          r.classList.toggle("active", match);
          if (match) r.scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
      }
      updateStageOverlayForCue(hit);
    } else {
      $("overlay-primary").textContent = "";
      $("overlay-secondary").textContent = "";
    }
    drawWaveformTimeline();
  }

  function togglePlayback() {
    if (state.hasMediaFile) {
      const video = $("media-player");
      if (video.paused) {
        video.play();
        setPlayIcon(true);
      } else {
        video.pause();
        setPlayIcon(false);
      }
      return;
    }
    if (!state.doc) return;
    if (state.simPlaying) {
      stopSimPlayback();
    } else {
      const maxMs = getMaxDurationMs();
      if (state.simTimeMs >= maxMs) state.simTimeMs = 0;
      state.simPlaying = true;
      state.simLastTick = performance.now();
      setPlayIcon(true);
      const step = (now) => {
        if (!state.simPlaying) return;
        const dt = now - state.simLastTick;
        state.simLastTick = now;
        state.simTimeMs += dt;
        if (state.simTimeMs >= getMaxDurationMs()) {
          state.simTimeMs = getMaxDurationMs();
          updatePlayerControlsUI(state.simTimeMs);
          stopSimPlayback();
          return;
        }
        updatePlayerControlsUI(state.simTimeMs);
        state.simRaf = requestAnimationFrame(step);
      };
      state.simRaf = requestAnimationFrame(step);
    }
  }

  function stopSimPlayback() {
    state.simPlaying = false;
    if (state.simRaf) cancelAnimationFrame(state.simRaf);
    setPlayIcon(false);
  }

  // AI Batch Stream Translation
  async function startStreamTranslation(onlyUntranslated = false) {
    if (!state.doc || state.translating) return;

    const targetCues = state.doc.cues.filter(
      (c) => !c.is_special && c.source_text.trim() && (!onlyUntranslated || !(c.target_text || "").trim())
    );
    if (!targetCues.length) {
      toast("所有条目均已有译文！如需重翻请点击「AI 全文精翻」。", "info");
      return;
    }

    pushUndoSnapshot();
    syncDocContextFields();

    state.translating = true;
    state.abortController = new AbortController();
    $("btn-translate-all").hidden = true;
    $("btn-translate-untranslated").disabled = true;
    $("btn-stop-translate").hidden = false;
    $("progress-banner").hidden = false;
    $("progress-fill").style.width = "2%";
    $("progress-title").textContent = "正在连接 AI 引擎启动上下文精翻…";
    $("progress-reflection").textContent = "";
    $("progress-nums").textContent = `0 / ${targetCues.length}`;

    try {
      const resp = await fetch("/api/ai/translate-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: state.abortController.signal,
        body: JSON.stringify({
          cues: state.doc.cues,
          cue_ids: onlyUntranslated ? targetCues.map((c) => c.id) : null,
          source_language: state.doc.source_language,
          target_language: state.doc.target_language,
          mode: $("select-trans-mode").value,
          genre: state.doc.genre,
          scene_summary: state.doc.scene_summary,
          custom_prompt: state.doc.custom_prompt,
          glossary: state.doc.glossary,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "翻译请求失败");
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const chunk of parts) {
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const payload = JSON.parse(line.slice(5).trim());
          handleStreamEvent(payload);
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        toast("已停止后续翻译，已完成批次的译文已安全保留。", "info");
      } else {
        toast(err.message || "翻译出错", "error");
        if ((err.message || "").includes("API Key")) {
          openModal("modal-settings");
        }
      }
    } finally {
      state.translating = false;
      state.abortController = null;
      $("btn-translate-all").hidden = false;
      $("btn-translate-untranslated").disabled = false;
      $("btn-stop-translate").hidden = true;
      setTimeout(() => {
        if (!state.translating) $("progress-banner").hidden = true;
      }, 1600);
      renderAll();
      scheduleAutoSave();
    }
  }

  function handleStreamEvent(evt) {
    if (evt.event === "start") {
      $("progress-title").textContent = `正在使用 ${evt.model} 进行${evt.mode === "reflective" ? "反思精翻" : "极速上下文翻译"}…`;
    } else if (evt.event === "batch_start") {
      $("progress-title").textContent = `正在精翻第 ${evt.batch_index}/${evt.total_batches} 批（#${evt.from_id} ~ #${evt.to_id}）…`;
    } else if (evt.event === "batch_done") {
      const cueById = new Map(state.doc.cues.map((c) => [c.id, c]));
      for (const upd of evt.updates || []) {
        const target = cueById.get(upd.id);
        if (target && upd.target_text) {
          target.target_text = upd.target_text;
          target.status = upd.status;
          const domInput = document.querySelector(`.cue-target-input[data-cue-input="${upd.id}"]`);
          if (domInput) {
            domInput.value = upd.target_text;
            autoResizeTextarea(domInput);
          }
        }
      }
      $("progress-nums").textContent = `${evt.completed} / ${evt.total}`;
      $("progress-fill").style.width = `${evt.percent || 0}%`;
      if (evt.reflection) {
        $("progress-reflection").textContent = `审校笔记：${evt.reflection}`;
      }
      updateCounts();
    } else if (evt.event === "complete") {
      $("progress-fill").style.width = "100%";
      $("progress-title").textContent = "全文精翻完成";
      toast(evt.message || "翻译完成", "success");
    } else if (evt.event === "error") {
      throw new Error(evt.message || "翻译发生错误");
    }
  }

  async function runSingleCueRewrite(cue, action, instruction = "") {
    syncDocContextFields();
    toast(`正在调用 AI 精修第 #${cue.id} 条字幕…`, "info");
    try {
      const resp = await fetch("/api/ai/rewrite-cue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cue_id: cue.id,
          cues: state.doc.cues,
          source_language: state.doc.source_language,
          target_language: state.doc.target_language,
          action,
          instruction,
          scene_summary: state.doc.scene_summary,
          glossary: state.doc.glossary,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "单句润色失败");

      pushUndoSnapshot();
      cue.target_text = data.best;
      cue.status = "polished";
      state.cueSuggestions[cue.id] = {
        best: data.best,
        alternatives: data.alternatives || [],
        reason: data.reason || "",
      };
      renderCueList();
      updateCounts();
      updateStageOverlayForCue(cue);
      scheduleAutoSave();
      toast(`#${cue.id} 已生成精修方案与备选表达`, "success");
    } catch (err) {
      toast(err.message, "error");
      if ((err.message || "").includes("API Key")) openModal("modal-settings");
    }
  }

  async function runExtractGlossary() {
    if (!state.doc) return;
    const btn = $("btn-extract-glossary");
    const restoreBtn = setButtonBusy(btn, "正在扫描全片…");
    try {
      const resp = await fetch("/api/ai/extract-glossary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cues: state.doc.cues,
          source_language: state.doc.source_language,
          target_language: state.doc.target_language,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "术语提取失败");

      pushUndoSnapshot();
      if (data.scene_summary) {
        state.doc.scene_summary = data.scene_summary;
        $("scene-summary-input").value = data.scene_summary;
      }
      if (data.genre) {
        state.doc.genre = data.genre;
        $("select-genre").value = data.genre;
      }
      const existingSet = new Set(state.doc.glossary.map((g) => g.source.toLowerCase()));
      let added = 0;
      for (const item of data.glossary || []) {
        if (!existingSet.has(item.source.toLowerCase())) {
          state.doc.glossary.push(item);
          existingSet.add(item.source.toLowerCase());
          added++;
        }
      }
      renderGlossary();
      scheduleAutoSave();
      toast(`成功提取背景摘要及 ${added} 条关键术语！`, "success");
    } catch (err) {
      toast(err.message, "error");
      if ((err.message || "").includes("API Key")) openModal("modal-settings");
    } finally {
      restoreBtn();
    }
  }

  function syncDocContextFields() {
    if (!state.doc) return;
    state.doc.source_language = $("select-source-lang").value;
    state.doc.target_language = $("select-target-lang").value;
    state.doc.genre = $("select-genre").value;
    state.doc.scene_summary = $("scene-summary-input").value.trim();
    state.doc.custom_prompt = $("custom-prompt-input").value.trim();
  }

  // Modals
  function openModal(id) {
    if (id === "modal-settings" && state.settings) {
      selectProviderPreset(state.settings.provider_id || "deepseek", false);
      $("cfg-base-url").value = state.settings.base_url || "";
      $("cfg-model").value = state.settings.model || "";
      $("cfg-api-key").value = "";
      $("cfg-key-saved-badge").hidden = !state.settings.has_api_key;
      $("cfg-key-saved-badge").textContent = state.settings.has_api_key
        ? `已保存 (${state.settings.masked_api_key})`
        : "";
      $("cfg-batch-size").value = state.settings.batch_size || 12;
      $("cfg-context-win").value = state.settings.context_window || 4;
      $("cfg-concurrency").value = state.settings.concurrency || 2;
      $("cfg-test-result").hidden = true;
      renderModelPickerChips();
    }
    if (id === "modal-export") {
      $("export-result-box").hidden = true;
      $("export-code-preview").hidden = true;
    }
    if (id === "modal-asr" && state.settings) {
      $("asr-base-url").value = state.settings.asr_base_url || "https://api.siliconflow.cn/v1";
      $("asr-model").value = state.settings.asr_model || "FunAudioLLM/SenseVoiceSmall";
      $("asr-api-key").value = "";
      $("asr-key-saved-badge").hidden = !state.settings.has_asr_api_key;
      $("asr-key-saved-badge").textContent = state.settings.has_asr_api_key
        ? `已保存 (${state.settings.masked_asr_api_key})`
        : "";
      $("asr-status-box").hidden = true;
      selectAsrPreset(state.settings.asr_provider_id || "siliconflow", false);
    }
    if (id === "modal-projects") {
      loadAndRenderProjects();
    }
    if (id === "modal-qa") {
      runAndRenderQaCheck();
    }
    if (id === "modal-batch") {
      $("batch-export-result").hidden = true;
      renderBatchQueue();
    }
    $(id).hidden = false;
  }

  async function loadAndRenderProjects() {
    const wrap = $("project-list");
    wrap.innerHTML = `<div class="empty-mini">正在读取已保存工程…</div>`;
    try {
      const resp = await fetch("/api/projects");
      const data = await resp.json();
      const items = data.projects || [];
      if (!items.length) {
        wrap.innerHTML = `<div class="empty-mini">暂无历史工程记录。载入字幕后系统会自动存档你的所有精修进度。</div>`;
        return;
      }
      wrap.innerHTML = "";
      items.forEach((p) => {
        const pct = p.cue_count ? Math.round((p.translated_count * 100) / p.cue_count) : 0;
        const dt = p.updated_at ? new Date(p.updated_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
        const row = document.createElement("div");
        row.className = "project-item";
        row.innerHTML = `
          <div class="project-item-main">
            <div class="project-item-title">
              <span>${escapeHtml(p.filename)}</span>
              <span class="doc-tag">${escapeHtml((p.source_format || "srt").toUpperCase())}</span>
            </div>
            <div class="project-item-sub">进度：已译 ${p.translated_count}/${p.cue_count} 条 (${pct}%) · 保存于 ${escapeHtml(dt)}</div>
          </div>
          <div class="project-item-actions">
            <button class="btn btn-subtle btn-sm" data-open-proj="${escapeHtml(p.doc_id)}" type="button">打开</button>
            <button class="btn btn-ghost btn-icon btn-sm" data-del-proj="${escapeHtml(p.doc_id)}" type="button" title="删除存档">${icon("trash")}</button>
          </div>
        `;
        wrap.appendChild(row);
      });

      wrap.querySelectorAll("[data-open-proj]").forEach((btn) => {
        btn.onclick = async () => {
          const id = btn.dataset.openProj;
          const r = await fetch(`/api/projects/${encodeURIComponent(id)}`);
          if (r.ok) {
            const doc = await r.json();
            loadDocumentIntoStudio(doc, true);
            closeModal("modal-projects");
            toast(`已恢复工程：${doc.filename}`, "success");
          }
        };
      });

      wrap.querySelectorAll("[data-del-proj]").forEach((btn) => {
        btn.onclick = async () => {
          const id = btn.dataset.delProj;
          await fetch(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
          loadAndRenderProjects();
          toast("已删除该历史存档", "info");
        };
      });
    } catch (_) {
      wrap.innerHTML = `<div class="empty-mini">读取工程列表失败。</div>`;
    }
  }

  async function runAndRenderQaCheck() {
    if (!state.doc) return;
    const grid = $("qa-report-body");
    grid.innerHTML = `<div class="empty-mini">正在扫描时间轴与字幕质量…</div>`;
    try {
      const resp = await fetch("/api/subtitles/qa-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.doc.cues),
      });
      const report = await resp.json();
      renderQaReportCards(report);
    } catch (_) {
      grid.innerHTML = `<div class="empty-mini">质量体检执行失败。</div>`;
    }
  }

  function renderQaReportCards(report) {
    const grid = $("qa-report-body");
    const sections = [
      {
        title: "时间轴重叠",
        desc: "前一句结束时间晚于下一句起始时间，会导致播放器字幕闪烁跳动",
        ids: report.overlap_ids || [],
      },
      {
        title: "行尾句号 / 多余空格",
        desc: "符合中文影视规范可一键清理单句末尾多余句号与连续空格",
        ids: report.punctuation_ids || [],
      },
      {
        title: "语速过快（>16 字/秒）",
        desc: "定位后右键选择「精简压缩字数」",
        ids: report.high_cps_ids || [],
      },
      {
        title: "未翻译",
        desc: "尚未填写译文的对白条目（可点击工具栏「仅译未完成」补齐）",
        ids: report.untranslated_ids || [],
      },
    ];

    grid.innerHTML = "";
    sections.forEach((sec) => {
      const card = document.createElement("div");
      card.className = `qa-card ${sec.ids.length ? "warn" : "clean"}`;
      card.innerHTML = `
        <div class="qa-card-head">
          <span>${escapeHtml(sec.title)}</span>
          <span class="qa-count">${sec.ids.length ? `${sec.ids.length} 处` : "通过"}</span>
        </div>
        <div class="rc-desc">${escapeHtml(sec.desc)}</div>
        <div class="qa-chips"></div>
      `;
      const chips = card.querySelector(".qa-chips");
      sec.ids.slice(0, 60).forEach((cid) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "qa-id-chip";
        chip.textContent = `#${cid}`;
        chip.onclick = () => {
          state.filter = "all";
          document.querySelectorAll("#filter-pills .pill").forEach((p) => p.classList.toggle("active", p.dataset.filter === "all"));
          renderCueList();
          closeModal("modal-qa");
          activateCue(cid, true);
          const rowEl = document.querySelector(`.cue-row[data-cue-id="${cid}"]`);
          if (rowEl) rowEl.scrollIntoView({ block: "center", behavior: "smooth" });
        };
        chips.appendChild(chip);
      });
      grid.appendChild(card);
    });
  }

  function closeModal(id) {
    $(id).hidden = true;
  }

  // ---------- Menubar ----------
  function closeMenus() {
    document.querySelectorAll(".menu.open").forEach((m) => {
      m.classList.remove("open");
      const pop = m.querySelector(".menu-pop");
      if (pop) pop.hidden = true;
    });
  }

  function syncMenuProxyStates(menuEl) {
    menuEl.querySelectorAll("[data-click]").forEach((item) => {
      const target = $(item.dataset.click);
      let disabled = !target || target.disabled;
      if (item.dataset.click === "btn-close-doc") disabled = !state.doc;
      if (item.dataset.click === "btn-open-cmdk") disabled = false;
      item.disabled = disabled;
    });
  }

  function openMenu(menuEl) {
    closeMenus();
    closeContextMenu();
    syncMenuProxyStates(menuEl);
    menuEl.classList.add("open");
    const pop = menuEl.querySelector(".menu-pop");
    if (pop) pop.hidden = false;
  }

  function bindMenubar() {
    document.querySelectorAll("[data-menu]").forEach((menuEl) => {
      const trigger = menuEl.querySelector(".menu-trigger");
      const pop = menuEl.querySelector(".menu-pop");
      trigger.addEventListener("click", (e) => {
        e.stopPropagation();
        if (menuEl.classList.contains("open")) closeMenus();
        else openMenu(menuEl);
      });
      trigger.addEventListener("mouseenter", () => {
        if (document.querySelector(".menu.open") && !menuEl.classList.contains("open")) openMenu(menuEl);
      });
      pop.addEventListener("click", (e) => {
        const item = e.target.closest(".menu-item, .dropdown-item");
        if (!item || item.disabled) return;
        const proxyId = item.dataset.click;
        if (proxyId) {
          const target = $(proxyId);
          if (target && !target.disabled) target.click();
        }
        setTimeout(closeMenus, 0);
      });
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("[data-menu]")) closeMenus();
    });
  }

  // ---------- Command Palette (Cmd/Ctrl + K) ----------
  const cmdk = { items: [], filtered: [], active: 0 };

  function buildCommandList() {
    const hasDoc = Boolean(state.doc);
    const click = (id) => () => {
      const el = $(id);
      if (el) el.click();
    };
    const list = [
      { group: "文件", icon: "file", title: "打开字幕文件", kw: "open srt ass vtt dakai", hint: "⌘O", run: click("subtitle-file-input") },
      { group: "文件", icon: "film", title: "挂载本地音视频", kw: "video audio media guazai", run: click("media-file-input") },
      { group: "文件", icon: "mic", title: "音视频转字幕 (ASR)", kw: "asr whisper transcribe shibie", run: () => openModal("modal-asr") },
      { group: "文件", icon: "layers", title: "多集批量队列", kw: "batch zip queue piliang", run: () => openModal("modal-batch") },
      { group: "文件", icon: "history", title: "历史工程", kw: "projects history lishi", run: () => openModal("modal-projects") },
      { group: "文件", icon: "download", title: "导出字幕", kw: "export daochu", hint: "⌘E", enabled: hasDoc, run: () => openModal("modal-export") },
      { group: "文件", icon: "x", title: "关闭当前文档", kw: "close guanbi", enabled: hasDoc, run: closeCurrentDocument },
      { group: "翻译", icon: "sparkles", title: "AI 全文精翻", kw: "translate ai fanyi", enabled: hasDoc && !state.translating, run: click("btn-translate-all") },
      { group: "翻译", icon: "languages", title: "仅翻译未完成条目", kw: "translate untranslated", enabled: hasDoc && !state.translating, run: click("btn-translate-untranslated") },
      { group: "翻译", icon: "book", title: "AI 提取术语与背景", kw: "glossary term shuyu", enabled: hasDoc, run: click("btn-extract-glossary") },
      { group: "翻译", icon: "sliders", title: "AI 引擎设置", kw: "settings api key model shezhi", run: () => openModal("modal-settings") },
      { group: "编辑", icon: "undo", title: "撤销", kw: "undo chexiao", hint: "⌘Z", enabled: state.undoStack.length > 0, run: undo },
      { group: "编辑", icon: "redo", title: "重做", kw: "redo chongzuo", hint: "⇧⌘Z", enabled: state.redoStack.length > 0, run: redo },
      { group: "编辑", icon: "search", title: "查找与替换", kw: "find replace chazhao", hint: "⌘F", enabled: hasDoc, run: click("btn-toggle-search") },
      { group: "编辑", icon: "clock", title: "时间轴整体平移", kw: "shift offset pingyi", enabled: hasDoc, run: click("btn-time-shift") },
      { group: "编辑", icon: "shield", title: "字幕质量体检", kw: "qa check tijian", enabled: hasDoc, run: () => openModal("modal-qa") },
      { group: "编辑", icon: "split", title: "双语字幕拆分为双轨", kw: "split bilingual fengui", enabled: hasDoc, run: click("btn-split-bilingual") },
      { group: "编辑", icon: "eraser", title: "清空全部译文", kw: "clear qingkong", enabled: hasDoc, run: clearAllTranslations },
      { group: "视图", icon: "contrast", title: "切换深色 / 浅色主题", kw: "theme dark light zhuti", run: click("btn-theme-toggle") },
      { group: "视图", icon: "panel-right", title: "显示 / 隐藏右侧面板", kw: "sidebar panel", hint: "⌘\\", run: click("btn-toggle-sidebar") },
      { group: "视图", icon: "wave", title: "显示 / 隐藏波形时间轴", kw: "waveform timeline boxing", enabled: hasDoc, run: click("btn-toggle-waveform") },
      { group: "视图", icon: "keyboard", title: "键盘快捷键", kw: "shortcuts kuaijiejian", hint: "?", run: () => openModal("modal-shortcuts") },
    ];
    if (hasDoc) {
      [
        ["all", "显示全部字幕"],
        ["untranslated", "只看未翻译"],
        ["translated", "只看已翻译"],
        ["long", "只看语速预警"],
      ].forEach(([f, t]) => {
        list.push({
          group: "筛选",
          icon: "search",
          title: t,
          kw: `filter ${f}`,
          run: () => document.querySelector(`#filter-pills .pill[data-filter="${f}"]`)?.click(),
        });
      });
    }
    (state.meta?.samples || []).forEach((sm) => {
      list.push({ group: "示例", icon: "star", title: `示例：${sm.title}`, kw: `sample ${sm.badge}`, run: () => loadSampleById(sm.id) });
    });
    return list;
  }

  function scoreCommand(item, q) {
    if (!q) return 1;
    const hay = `${item.title} ${item.kw || ""} ${item.group}`.toLowerCase();
    if (item.title.toLowerCase().startsWith(q)) return 4;
    if (hay.includes(q)) return 3;
    // subsequence match
    let i = 0;
    for (const ch of hay) {
      if (ch === q[i]) i++;
      if (i === q.length) return 1;
    }
    return 0;
  }

  function highlightTitle(title, q) {
    if (!q) return escapeHtml(title);
    const idx = title.toLowerCase().indexOf(q);
    if (idx === -1) return escapeHtml(title);
    return `${escapeHtml(title.slice(0, idx))}<mark>${escapeHtml(title.slice(idx, idx + q.length))}</mark>${escapeHtml(
      title.slice(idx + q.length)
    )}`;
  }

  function renderCommandPalette() {
    const q = $("cmdk-input").value.trim().toLowerCase();
    const listEl = $("cmdk-list");
    let results = cmdk.items
      .map((it) => ({ it, s: scoreCommand(it, q) }))
      .filter((r) => r.s > 0);
    if (q) results.sort((a, b) => b.s - a.s);
    cmdk.filtered = results.map((r) => r.it);

    const jump = q.match(/^#?(\d{1,5})$/);
    if (jump && state.doc) {
      const id = Number(jump[1]);
      if (state.doc.cues.some((c) => c.id === id)) {
        cmdk.filtered.unshift({
          group: "跳转",
          icon: "arrow-right",
          title: `跳转到第 #${id} 条字幕`,
          run: () => {
            state.filter = "all";
            document.querySelectorAll("#filter-pills .pill").forEach((p) => p.classList.toggle("active", p.dataset.filter === "all"));
            renderCueList(true);
            activateCue(id, true);
            document.querySelector(`.cue-row[data-cue-id="${id}"]`)?.scrollIntoView({ block: "center" });
          },
        });
      }
    }

    cmdk.active = Math.min(cmdk.active, Math.max(0, cmdk.filtered.length - 1));
    if (!cmdk.filtered.length) {
      listEl.innerHTML = `<div class="cmdk-empty">没有匹配的命令</div>`;
      return;
    }

    listEl.innerHTML = "";
    let lastGroup = "";
    cmdk.filtered.forEach((it, i) => {
      if (!q && it.group !== lastGroup) {
        const g = document.createElement("div");
        g.className = "cmdk-group";
        g.textContent = it.group;
        listEl.appendChild(g);
        lastGroup = it.group;
      }
      const b = document.createElement("button");
      b.type = "button";
      const disabled = it.enabled === false;
      b.className = `cmdk-item${i === cmdk.active ? " active" : ""}${disabled ? " disabled" : ""}`;
      b.innerHTML = `${icon(it.icon)}<span class="cmdk-title">${highlightTitle(it.title, q)}</span>${
        q ? `<span class="cmdk-hint">${escapeHtml(it.group)}</span>` : ""
      }${it.hint ? `<kbd>${escapeHtml(it.hint)}</kbd>` : ""}`;
      b.addEventListener("mousemove", () => {
        if (cmdk.active !== i) {
          cmdk.active = i;
          listEl.querySelectorAll(".cmdk-item").forEach((el, j) => el.classList.toggle("active", j === i));
        }
      });
      b.addEventListener("click", () => runCommandAt(i));
      listEl.appendChild(b);
    });
  }

  function runCommandAt(i) {
    const it = cmdk.filtered[i];
    if (!it) return;
    if (it.enabled === false) {
      toast("当前状态下不可用（请先打开字幕文档）", "info");
      return;
    }
    closeCommandPalette();
    setTimeout(() => it.run(), 0);
  }

  function openCommandPalette() {
    closeMenus();
    closeContextMenu();
    cmdk.items = buildCommandList();
    cmdk.active = 0;
    $("cmdk-input").value = "";
    $("cmdk").hidden = false;
    renderCommandPalette();
    requestAnimationFrame(() => $("cmdk-input").focus());
  }

  function closeCommandPalette() {
    $("cmdk").hidden = true;
  }

  function bindCommandPalette() {
    $("btn-open-cmdk").addEventListener("click", openCommandPalette);
    $("cmdk").addEventListener("mousedown", (e) => {
      if (e.target.id === "cmdk") closeCommandPalette();
    });
    $("cmdk-input").addEventListener("input", () => {
      cmdk.active = 0;
      renderCommandPalette();
    });
    $("cmdk-input").addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = cmdk.filtered.length;
        if (!n) return;
        cmdk.active = (cmdk.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
        const items = $("cmdk-list").querySelectorAll(".cmdk-item");
        items.forEach((el, j) => el.classList.toggle("active", j === cmdk.active));
        items[cmdk.active]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        e.preventDefault();
        runCommandAt(cmdk.active);
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeCommandPalette();
      }
    });
  }

  function collectSettingsForm() {
    const payload = {
      provider_id: gridCurrentProvider,
      base_url: $("cfg-base-url").value.trim(),
      model: $("cfg-model").value.trim(),
      batch_size: parseInt($("cfg-batch-size").value, 10) || 12,
      context_window: parseInt($("cfg-context-win").value, 10) || 4,
      concurrency: parseInt($("cfg-concurrency").value, 10) || 2,
      default_mode: $("select-trans-mode").value,
      default_target_lang: $("select-target-lang").value,
    };
    const keyVal = $("cfg-api-key").value.trim();
    if (keyVal) payload.api_key = keyVal;
    return payload;
  }

  // Bind UI Events
  function bindEvents() {
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".cue-actions")) closeAllPopovers();
      if (!e.target.closest("#ctx-menu")) closeContextMenu();
    });

    bindMenubar();
    bindCommandPalette();

    $("btn-prev-cue").addEventListener("click", () => stepActiveCue(-1));
    $("btn-next-cue").addEventListener("click", () => stepActiveCue(1));

    $("subtitle-file-input").addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (file) uploadSubtitleFile(file);
      e.target.value = "";
    });

    $("media-file-input").addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (file) mountMediaFile(file);
      e.target.value = "";
    });

    // Drag & drop anywhere
    let dragDepth = 0;
    window.addEventListener("dragenter", (e) => {
      e.preventDefault();
      dragDepth++;
      $("drop-overlay").hidden = false;
    });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) $("drop-overlay").hidden = true;
    });
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      dragDepth = 0;
      $("drop-overlay").hidden = true;
      const files = Array.from(e.dataTransfer?.files || []);
      const subFiles = [];
      const mediaFiles = [];
      for (const f of files) {
        const ext = f.name.split(".").pop().toLowerCase();
        if (["srt", "ass", "ssa", "vtt"].includes(ext)) {
          subFiles.push(f);
        } else if (
          f.type.startsWith("video/") ||
          f.type.startsWith("audio/") ||
          ["mp4", "mkv", "mov", "webm", "mp3", "wav", "m4a", "flac"].includes(ext)
        ) {
          mediaFiles.push(f);
        } else {
          toast(`不支持的文件类型：${f.name}`, "error");
        }
      }

      if (subFiles.length > 1 || (subFiles.length === 1 && !$("modal-batch").hidden)) {
        openModal("modal-batch");
        enqueueBatchSubtitleFiles(subFiles);
      } else if (subFiles.length === 1) {
        uploadSubtitleFile(subFiles[0]);
      }

      for (const mf of mediaFiles) {
        mountMediaFile(mf);
        if (!state.doc && !subFiles.length) {
          prepareAsrMediaFile(mf);
          openModal("modal-asr");
        }
      }
    });

    // Progressive infinite scroll on #cue-list
    $("cue-list").addEventListener("scroll", (e) => {
      const el = e.currentTarget;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 280) {
        appendMoreCueRows(250);
      }
    });

    // Theme & Sidebar toggles
    $("btn-theme-toggle").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("listento_theme", next);
      drawWaveformTimeline();
    });

    $("btn-toggle-sidebar").addEventListener("click", () => {
      $("sidebar").classList.toggle("collapsed");
      requestAnimationFrame(() => drawWaveformTimeline());
    });

    $("btn-close-doc").addEventListener("click", closeCurrentDocument);
    $("btn-clear-translations").addEventListener("click", clearAllTranslations);

    $("btn-toggle-waveform").addEventListener("click", () => {
      state.waveformCollapsed = !state.waveformCollapsed;
      localStorage.setItem("listento_waveform_collapsed", state.waveformCollapsed ? "1" : "0");
      syncWaveformVisibilityUI();
    });
    $("btn-close-waveform").addEventListener("click", () => {
      state.waveformCollapsed = true;
      localStorage.setItem("listento_waveform_collapsed", "1");
      syncWaveformVisibilityUI();
    });
    document.querySelectorAll("[data-wf-zoom]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.waveformZoom = Number(btn.dataset.wfZoom) || 1;
        document.querySelectorAll("[data-wf-zoom]").forEach((b) => b.classList.toggle("active", b === btn));
        drawWaveformTimeline();
      });
    });

    // Undo / Redo + Shortcuts
    $("btn-undo").addEventListener("click", undo);
    $("btn-redo").addEventListener("click", redo);

    window.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = (e.key || "").toLowerCase();
      const typing = ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement?.tagName);

      if (e.key === "Escape") {
        if (!$("cmdk").hidden) return; // handled by palette
        if (!$("ctx-menu").hidden) {
          closeContextMenu();
          return;
        }
        if (document.querySelector(".menu.open")) {
          closeMenus();
          return;
        }
        const openModalEl = Array.from(document.querySelectorAll(".modal-backdrop")).find((m) => !m.hidden);
        if (openModalEl && openModalEl.id !== "modal-prompt") {
          closeModal(openModalEl.id);
          return;
        }
        if (!$("search-bar").hidden) {
          $("btn-close-search").click();
          return;
        }
        if (!$("shift-bar").hidden) {
          $("shift-bar").hidden = true;
          return;
        }
        closeAllPopovers();
        return;
      }

      if (mod && key === "k") {
        e.preventDefault();
        if ($("cmdk").hidden) openCommandPalette();
        else closeCommandPalette();
      } else if (mod && key === "z") {
        if (typing) return;
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (mod && key === "f" && state.doc) {
        e.preventDefault();
        $("search-bar").hidden = false;
        $("search-input").focus();
      } else if (mod && key === "s" && state.doc) {
        e.preventDefault();
        syncDocContextFields();
        scheduleAutoSave();
        toast("当前字幕工程已手动保存", "success");
      } else if (mod && key === "o") {
        e.preventDefault();
        $("subtitle-file-input").click();
      } else if (mod && key === "e" && state.doc) {
        e.preventDefault();
        openModal("modal-export");
      } else if (mod && e.key === "\\") {
        e.preventDefault();
        $("btn-toggle-sidebar").click();
      } else if (e.ctrlKey && e.code === "Space") {
        e.preventDefault();
        togglePlayback();
      } else if (!typing && !mod && e.key === "?") {
        e.preventDefault();
        openModal("modal-shortcuts");
      }
    });

    // Player controls
    $("btn-play-toggle").addEventListener("click", togglePlayback);
    $("media-player").addEventListener("timeupdate", () => {
      if (state.hasMediaFile) {
        updatePlayerControlsUI($("media-player").currentTime * 1000);
      }
    });
    $("media-player").addEventListener("ended", () => {
      setPlayIcon(false);
    });
    $("player-scrubber").addEventListener("input", (e) => {
      const ratio = Number(e.target.value) / 1000;
      const targetMs = ratio * getMaxDurationMs();
      if (state.hasMediaFile) {
        $("media-player").currentTime = targetMs / 1000;
      } else {
        state.simTimeMs = targetMs;
        updatePlayerControlsUI(targetMs);
      }
    });

    document.querySelectorAll("[data-subview]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.previewSubview = btn.dataset.subview;
        document.querySelectorAll("[data-subview]").forEach((b) => b.classList.toggle("active", b === btn));
        const cue = state.doc?.cues.find((c) => c.id === state.activeCueId);
        if (cue) updateStageOverlayForCue(cue);
      });
    });

    // Sidebar Tabs
    document.querySelectorAll(".panel-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const target = tab.dataset.tab;
        document.querySelectorAll(".panel-tab").forEach((t) => t.classList.toggle("active", t === tab));
        $("tab-glossary").hidden = target !== "glossary";
        $("tab-context").hidden = target !== "context";
      });
    });

    // Glossary handlers
    $("btn-add-glossary").addEventListener("click", () => {
      if (!state.doc) {
        toast("请先载入一份字幕文件。", "info");
        return;
      }
      const src = $("glossary-src-input").value.trim();
      const tgt = $("glossary-tgt-input").value.trim();
      if (!src || !tgt) return;
      pushUndoSnapshot();
      state.doc.glossary.push({ source: src, target: tgt, note: "", enabled: true });
      $("glossary-src-input").value = "";
      $("glossary-tgt-input").value = "";
      renderGlossary();
      scheduleAutoSave();
    });

    $("glossary-list").addEventListener("click", (e) => {
      const delIdx = e.target.closest?.("[data-g-del]")?.dataset.gDel;
      if (delIdx !== undefined) {
        pushUndoSnapshot();
        state.doc.glossary.splice(Number(delIdx), 1);
        renderGlossary();
        scheduleAutoSave();
      }
    });

    $("glossary-list").addEventListener("change", (e) => {
      const togIdx = e.target.dataset?.gToggle;
      if (togIdx !== undefined) {
        state.doc.glossary[Number(togIdx)].enabled = e.target.checked;
        scheduleAutoSave();
      }
    });

    $("btn-extract-glossary").addEventListener("click", runExtractGlossary);

    // Language & Context changes
    ["select-source-lang", "select-target-lang", "select-genre", "scene-summary-input", "custom-prompt-input"].forEach((id) => {
      $(id).addEventListener("change", () => {
        syncDocContextFields();
        scheduleAutoSave();
      });
    });

    // Filter pills
    document.querySelectorAll("#filter-pills .pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        state.filter = pill.dataset.filter;
        state.renderLimit = 250;
        document.querySelectorAll("#filter-pills .pill").forEach((p) => p.classList.toggle("active", p === pill));
        renderCueList(false);
      });
    });

    // Search & Replace
    $("btn-toggle-search").addEventListener("click", () => {
      const bar = $("search-bar");
      bar.hidden = !bar.hidden;
      if (!bar.hidden) $("search-input").focus();
      else {
        state.searchQuery = "";
        state.renderLimit = 250;
        $("search-input").value = "";
        renderCueList(false);
      }
    });
    $("btn-close-search").addEventListener("click", () => {
      $("search-bar").hidden = true;
      state.searchQuery = "";
      state.renderLimit = 250;
      $("search-input").value = "";
      renderCueList(false);
    });
    $("search-input").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      state.renderLimit = 250;
      renderCueList(false);
    });
    $("search-scope").addEventListener("change", (e) => {
      state.searchScope = e.target.value;
      state.renderLimit = 250;
      renderCueList(false);
    });
    $("btn-replace-all").addEventListener("click", () => {
      const findStr = $("search-input").value;
      const repStr = $("replace-input").value;
      if (!findStr || !state.doc) return;
      pushUndoSnapshot();
      let count = 0;
      state.doc.cues.forEach((c) => {
        if (!c.is_special && c.target_text && c.target_text.includes(findStr)) {
          c.target_text = c.target_text.split(findStr).join(repStr);
          count++;
        }
      });
      renderAll();
      scheduleAutoSave();
      toast(`已在 ${count} 条译文中完成替换`, "success");
    });

    // Time Shift
    $("btn-time-shift").addEventListener("click", () => {
      $("shift-bar").hidden = !$("shift-bar").hidden;
    });
    $("btn-close-shift").addEventListener("click", () => {
      $("shift-bar").hidden = true;
    });
    $("btn-apply-shift").addEventListener("click", () => {
      const ms = parseInt($("shift-ms-input").value, 10) || 0;
      if (ms !== 0) applyTimeShift(ms);
    });
    document.querySelectorAll("[data-shift]").forEach((btn) => {
      btn.addEventListener("click", () => applyTimeShift(parseInt(btn.dataset.shift, 10)));
    });

    // Split Bilingual
    $("btn-split-bilingual").addEventListener("click", async () => {
      if (!state.doc) return;
      try {
        const resp = await fetch("/api/subtitles/split-bilingual", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state.doc.cues),
        });
        const data = await resp.json();
        if (!data.split_count) {
          toast("未检测到可拆分的双行未译字幕。", "info");
          return;
        }
        pushUndoSnapshot();
        state.doc.cues = data.cues;
        renderAll();
        scheduleAutoSave();
        toast(`已将 ${data.split_count} 条双语字幕拆分为原文轨 + 译文轨！`, "success");
      } catch (err) {
        toast("双语分轨失败", "error");
      }
    });

    // AI Translation buttons
    $("btn-translate-all").addEventListener("click", () => startStreamTranslation(false));
    $("btn-translate-untranslated").addEventListener("click", () => startStreamTranslation(true));
    $("btn-stop-translate").addEventListener("click", () => {
      if (state.abortController) state.abortController.abort();
    });

    // Settings, ASR, Batch, Projects, QA, Shortcuts, Export Modals
    $("btn-open-settings").addEventListener("click", () => openModal("modal-settings"));
    $("btn-open-export").addEventListener("click", () => openModal("modal-export"));
    $("btn-open-asr").addEventListener("click", () => openModal("modal-asr"));
    $("btn-welcome-asr").addEventListener("click", () => openModal("modal-asr"));
    $("btn-open-batch").addEventListener("click", () => openModal("modal-batch"));
    $("btn-welcome-batch").addEventListener("click", () => openModal("modal-batch"));
    $("btn-open-projects").addEventListener("click", () => openModal("modal-projects"));
    $("btn-open-qa").addEventListener("click", () => openModal("modal-qa"));
    $("btn-open-shortcuts").addEventListener("click", () => openModal("modal-shortcuts"));

    $("batch-files-input").addEventListener("change", (e) => {
      const files = Array.from(e.target.files || []);
      if (files.length) enqueueBatchSubtitleFiles(files);
      e.target.value = "";
    });
    $("btn-batch-add-current").addEventListener("click", () => {
      if (!state.doc) {
        toast("当前编辑台尚未打开字幕文档。", "info");
        return;
      }
      syncDocContextFields();
      const exists = state.batchQueue.some((it) => it.doc.doc_id === state.doc.doc_id);
      if (exists) {
        toast(`「${state.doc.filename}」已在队列中。`, "info");
        return;
      }
      const cloned = JSON.parse(JSON.stringify(state.doc));
      const allDone = cloned.cues.filter((c) => !c.is_special).every((c) => (c.target_text || "").trim());
      state.batchQueue.push({
        queueId: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        doc: cloned,
        status: allDone ? "done" : "pending",
        progressPct: allDone ? 100 : 0,
        errorMsg: "",
      });
      renderBatchQueue();
      toast(`已将「${cloned.filename}」加入批量队列`, "success");
    });
    $("btn-batch-clear").addEventListener("click", () => {
      if (state.batchRunning) return;
      state.batchQueue = [];
      $("batch-export-result").hidden = true;
      renderBatchQueue();
    });
    $("btn-start-batch").addEventListener("click", runBatchQueueTranslation);
    $("btn-stop-batch").addEventListener("click", () => {
      if (state.batchAbort) state.batchAbort.abort();
    });
    $("btn-export-batch-zip").addEventListener("click", exportBatchQueueZip);

    $("asr-file-input").addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (f) prepareAsrMediaFile(f);
      e.target.value = "";
    });

    $("btn-start-asr").addEventListener("click", async () => {
      if (!selectedAsrFile) {
        toast("请先选择需要识别的视频或音频文件。", "error");
        return;
      }
      const btn = $("btn-start-asr");
      const box = $("asr-status-box");
      const restoreBtn = setButtonBusy(btn, "正在识别…");
      box.hidden = false;
      box.className = "test-result-box";
      box.textContent = "正在上传并识别，长音频可能需要一两分钟…";

      const form = new FormData();
      form.append("file", selectedAsrFile);
      form.append("language", $("asr-language").value);
      form.append("duration_sec", String(selectedAsrDurationSec || 0));
      form.append("asr_provider_id", currentAsrProvider);
      form.append("asr_base_url", $("asr-base-url").value.trim());
      form.append("asr_api_key", $("asr-api-key").value.trim());
      form.append("asr_model", $("asr-model").value.trim());

      try {
        const resp = await fetch("/api/asr/transcribe", { method: "POST", body: form });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "语音识别失败");

        mountMediaFile(selectedAsrFile);
        loadDocumentIntoStudio(data, true);
        const sResp = await fetch("/api/settings");
        if (sResp.ok) state.settings = await sResp.json();
        closeModal("modal-asr");
        toast(`语音识别成功！共生成 ${data.cues.length} 条时间轴字幕`, "success");
      } catch (err) {
        box.className = "test-result-box err";
        box.textContent = err.message;
      } finally {
        restoreBtn();
      }
    });

    $("btn-run-qa-fix").addEventListener("click", async () => {
      if (!state.doc) return;
      try {
        const resp = await fetch("/api/subtitles/qa-fix", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cues: state.doc.cues,
            fix_overlaps: true,
            clean_punctuation: true,
            target_language: state.doc.target_language || "zh",
          }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "修复失败");
        pushUndoSnapshot();
        state.doc.cues = data.cues;
        renderAll();
        scheduleAutoSave();
        renderQaReportCards(data.report);
        toast(
          `已完成自动修复：修正时间重叠 ${data.summary.fixed_overlaps} 处，规范标点 ${data.summary.cleaned_punctuation} 处`,
          "success"
        );
      } catch (err) {
        toast(err.message, "error");
      }
    });

    $("btn-preview-export").addEventListener("click", async () => {
      if (!state.doc) return;
      syncDocContextFields();
      const formats = Array.from(document.querySelectorAll("#modal-export .format-checks input:checked")).map(
        (el) => el.value
      );
      const fmt = formats[0] || "srt";
      const trackMode = document.querySelector('input[name="track_mode"]:checked')?.value || "bilingual_tgt_top";
      const scale = parseInt($("export-ass-scale").value, 10) || 72;
      const assPreset = $("export-ass-preset").value || "cinema_outline";
      try {
        const resp = await fetch("/api/subtitles/preview-export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            document: state.doc,
            formats: [fmt],
            track_mode: trackMode,
            ass_secondary_scale: scale,
            ass_style_preset: assPreset,
            fallback_to_source: true,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "预览失败");
        const pre = $("export-code-preview");
        pre.hidden = false;
        pre.textContent = data.preview;
      } catch (err) {
        toast(err.message, "error");
      }
    });

    document.querySelectorAll("[data-close-modal]").forEach((btn) => {
      btn.addEventListener("click", () => closeModal(btn.dataset.closeModal));
    });

    $("cfg-model").addEventListener("input", () => renderModelPickerChips());
    $("cfg-model-filter").addEventListener("input", () => renderModelPickerChips());

    $("btn-fetch-models").addEventListener("click", async () => {
      const btn = $("btn-fetch-models");
      const restoreBtn = setButtonBusy(btn, "正在拉取…");
      try {
        const resp = await fetch("/api/settings/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSettingsForm()),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "拉取模型失败");
        currentCandidateModels = data.models || [];
        isLiveFetchedModels = true;
        if ($("cfg-model-filter")) $("cfg-model-filter").value = "";
        if (currentCandidateModels.length && !$("cfg-model").value.trim()) {
          $("cfg-model").value = currentCandidateModels[0];
        }
        renderModelPickerChips();
        toast(`成功从接口拉取 ${data.count} 个可用模型！`, "success");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        restoreBtn();
      }
    });

    $("btn-test-api").addEventListener("click", async () => {
      const box = $("cfg-test-result");
      box.hidden = false;
      box.className = "test-result-box";
      box.textContent = "正在测试连通性…";
      try {
        const resp = await fetch("/api/settings/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSettingsForm()),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "连接失败");
        box.className = "test-result-box ok";
        box.textContent = `连接成功 · ${data.model} · 往返 ${data.latency_ms} ms`;
      } catch (err) {
        box.className = "test-result-box err";
        box.textContent = err.message;
      }
    });

    $("btn-save-settings").addEventListener("click", async () => {
      try {
        const resp = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSettingsForm()),
        });
        const saved = await resp.json();
        if (!resp.ok) throw new Error(saved.detail || "保存失败");
        state.settings = saved;
        refreshApiStatusUI();
        closeModal("modal-settings");
        toast("AI 引擎设置已持久化保存", "success");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    // Export Modal
    document.querySelectorAll("#export-track-cards .radio-card").forEach((card) => {
      card.addEventListener("click", () => {
        document.querySelectorAll("#export-track-cards .radio-card").forEach((c) => c.classList.remove("selected"));
        card.classList.add("selected");
        card.querySelector("input").checked = true;
      });
    });

    $("export-ass-scale").addEventListener("input", (e) => {
      $("ass-scale-val").textContent = `${e.target.value}%`;
    });

    $("btn-confirm-export").addEventListener("click", async () => {
      if (!state.doc) return;
      syncDocContextFields();
      const formats = Array.from(document.querySelectorAll("#modal-export .format-checks input:checked")).map(
        (el) => el.value
      );
      if (!formats.length) {
        toast("请至少勾选一种导出格式。", "error");
        return;
      }
      const trackMode = document.querySelector('input[name="track_mode"]:checked')?.value || "bilingual_tgt_top";
      const scale = parseInt($("export-ass-scale").value, 10) || 72;
      const assPreset = $("export-ass-preset")?.value || "cinema_outline";

      try {
        const resp = await fetch("/api/subtitles/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            document: state.doc,
            formats,
            track_mode: trackMode,
            ass_secondary_scale: scale,
            ass_style_preset: assPreset,
            fallback_to_source: true,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "导出失败");

        if (state.meta?.runtime !== "desktop") {
          for (const f of data.files) {
            triggerDownload(f.filename, f.content);
          }
        }
        const box = $("export-result-box");
        box.hidden = false;
        const canOpenDir = state.meta?.runtime && state.meta.runtime !== "docker";
        box.innerHTML = `
          <div>已生成 <strong>${data.files.map((x) => escapeHtml(x.filename)).join(", ")}</strong></div>
          <div style="margin-top:4px;"><small>保存路径：<code>${escapeHtml(data.exports_dir)}</code></small></div>
          ${canOpenDir ? `<div style="margin-top:8px;"><button class="btn btn-subtle btn-sm" id="btn-reveal-exports" type="button">${icon("folder")}<span>打开导出文件夹</span></button></div>` : ""}
        `;
        const revealBtn = $("btn-reveal-exports");
        if (revealBtn) {
          revealBtn.onclick = async () => {
            try {
              const r = await fetch("/api/desktop/open-exports", { method: "POST" });
              if (!r.ok) {
                const ej = await r.json();
                throw new Error(ej.detail || "打开失败");
              }
            } catch (err) {
              toast(err.message, "error");
            }
          };
        }
        toast(`成功导出 ${data.files.length} 个字幕文件`, "success");
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  // Interactive Canvas Audio Waveform & Draggable Cue Timeline
  function getCurrentPlayheadMs() {
    if (state.hasMediaFile) {
      const video = $("media-player");
      return Math.max(0, (video.currentTime || 0) * 1000);
    }
    return Math.max(0, state.simTimeMs || 0);
  }

  function seekStudioToMs(targetMs) {
    const clamped = Math.max(0, Math.round(targetMs));
    if (state.hasMediaFile) {
      const video = $("media-player");
      video.currentTime = clamped / 1000;
      updatePlayerControlsUI(clamped);
    } else {
      state.simTimeMs = clamped;
      updatePlayerControlsUI(clamped);
    }
  }

  function getWaveformWindow() {
    const spanMs = Math.round(20000 / (state.waveformZoom || 1));
    if (state.wfDrag && state.wfDrag.mode !== "seek") {
      return { winStart: state.wfDrag.winStart, spanMs };
    }
    const curMs = getCurrentPlayheadMs();
    const winStart = Math.max(0, Math.round(curMs - spanMs * 0.32));
    state.wfViewStartMs = winStart;
    return { winStart, spanMs };
  }

  function formatShortSec(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function drawWaveformTimeline() {
    const dock = $("timeline-dock");
    const canvas = $("waveform-canvas");
    if (!dock || dock.hidden || !canvas || !state.doc) return;

    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(200, canvas.clientWidth || canvas.parentElement?.clientWidth || 800);
    const height = 74;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }

    const ctx = canvas.getContext("2d");
    const light = document.documentElement.getAttribute("data-theme") === "light";
    const P = light
      ? {
          bg: "#fafafb", ruler: "#f1f1f4", line: "rgba(15,15,25,0.10)", tickMajor: "rgba(15,15,25,0.35)",
          tickMinor: "rgba(15,15,25,0.14)", tickText: "rgba(15,15,25,0.55)", wave: "rgba(8,145,178,0.35)",
          base: "rgba(91,80,230,0.18)", cueFill: "rgba(15,15,25,0.06)", cueLine: "rgba(15,15,25,0.18)",
          activeFill: "rgba(91,80,230,0.16)", activeLine: "#5b50e6", warnFill: "rgba(217,119,6,0.12)",
          warnLine: "rgba(217,119,6,0.6)", grip: "rgba(15,15,25,0.25)", gripActive: "#5b50e6",
          idText: "rgba(15,15,25,0.5)", idTextActive: "#4c41d6", text: "rgba(15,15,25,0.75)", textActive: "#18181b",
          playhead: "#0891b2",
        }
      : {
          bg: "#0d0e11", ruler: "#121317", line: "rgba(255,255,255,0.06)", tickMajor: "rgba(255,255,255,0.32)",
          tickMinor: "rgba(255,255,255,0.12)", tickText: "rgba(255,255,255,0.5)", wave: "rgba(34,211,238,0.32)",
          base: "rgba(114,104,242,0.2)", cueFill: "rgba(255,255,255,0.05)", cueLine: "rgba(255,255,255,0.12)",
          activeFill: "rgba(114,104,242,0.28)", activeLine: "#8b82f8", warnFill: "rgba(251,191,36,0.12)",
          warnLine: "rgba(251,191,36,0.55)", grip: "rgba(255,255,255,0.22)", gripActive: "#a5a0fb",
          idText: "rgba(255,255,255,0.45)", idTextActive: "#ffffff", text: "rgba(255,255,255,0.72)", textActive: "#ffffff",
          playhead: "#22d3ee",
        };
    ctx.save();
    ctx.scale(dpr, dpr);

    // 1. Background + Ruler header
    ctx.fillStyle = P.bg;
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = P.ruler;
    ctx.fillRect(0, 0, width, 18);
    ctx.lineWidth = 1;
    ctx.strokeStyle = P.line;
    ctx.beginPath();
    ctx.moveTo(0, 18.5);
    ctx.lineTo(width, 18.5);
    ctx.stroke();

    const { winStart, spanMs } = getWaveformWindow();
    const winEnd = winStart + spanMs;

    // 2. Time Ruler Ticks
    const majorStep = spanMs <= 6000 ? 1000 : spanMs <= 12000 ? 2000 : 5000;
    const minorStep = majorStep / 5;
    const firstTick = Math.floor(winStart / minorStep) * minorStep;
    ctx.font = '10px "SF Mono", Menlo, monospace';
    ctx.textBaseline = "top";

    for (let t = firstTick; t <= winEnd; t += minorStep) {
      if (t < 0) continue;
      const x = ((t - winStart) / spanMs) * width;
      const isMajor = t % majorStep === 0;
      ctx.strokeStyle = isMajor ? P.tickMajor : P.tickMinor;
      ctx.beginPath();
      ctx.moveTo(x, isMajor ? 2 : 12);
      ctx.lineTo(x, 18);
      ctx.stroke();
      if (isMajor) {
        ctx.fillStyle = P.tickText;
        ctx.fillText(formatShortSec(t), x + 4, 3);
      }
    }

    // 3. Audio Waveform Peaks or Baseline Grid
    const midY = 46;
    if (state.waveformPeaks && state.waveformDurationMs > 0) {
      const peaks = state.waveformPeaks;
      const totalBuckets = peaks.length;
      const durMs = state.waveformDurationMs;
      const barStep = 3;
      ctx.fillStyle = P.wave;
      for (let px = 0; px < width; px += barStep) {
        const tMs = winStart + (px / width) * spanMs;
        if (tMs < 0 || tMs > durMs) continue;
        const bIdx = Math.min(totalBuckets - 1, Math.floor((tMs / durMs) * totalBuckets));
        const amp = peaks[bIdx] || 0;
        const barH = Math.max(2, amp * 44);
        ctx.fillRect(px, midY - barH * 0.5, 2, barH);
      }
    } else {
      ctx.strokeStyle = P.base;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, midY);
      ctx.lineTo(width, midY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // 4. Draggable Subtitle Cue Blocks
    const cues = state.doc.cues || [];
    for (let i = 0; i < cues.length; i++) {
      const cue = cues[i];
      if (cue.is_special || cue.end_ms < winStart || cue.start_ms > winEnd) continue;

      const x1 = ((cue.start_ms - winStart) / spanMs) * width;
      const x2 = ((cue.end_ms - winStart) / spanMs) * width;
      const bw = Math.max(8, x2 - x1);
      const isActive = cue.id === state.activeCueId;
      const isLong = isCueLongWarning(cue);

      ctx.fillStyle = isActive ? P.activeFill : isLong ? P.warnFill : P.cueFill;
      ctx.strokeStyle = isActive ? P.activeLine : isLong ? P.warnLine : P.cueLine;
      ctx.lineWidth = isActive ? 1.5 : 1;

      const topY = 22;
      const boxH = 46;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(x1 + 0.5, topY + 0.5, bw - 1, boxH - 1, 6);
      } else {
        ctx.rect(x1, topY, bw, boxH);
      }
      ctx.fill();
      ctx.stroke();

      // Left & right trim grip accents
      ctx.fillStyle = isActive ? P.gripActive : P.grip;
      ctx.fillRect(x1 + 2, topY + 14, 2, boxH - 28);
      ctx.fillRect(x1 + bw - 4, topY + 14, 2, boxH - 28);

      if (bw > 28) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x1 + 5, topY + 2, bw - 10, boxH - 4);
        ctx.clip();
        ctx.fillStyle = isActive ? P.idTextActive : P.idText;
        ctx.font = '600 10px "SF Mono", Menlo, monospace';
        ctx.fillText(`#${cue.id}`, x1 + 6, topY + 5);
        const label = (cue.target_text || cue.source_text || "").replace(/\s+/g, " ").trim();
        ctx.fillStyle = isActive ? P.textActive : P.text;
        ctx.font = '11.5px -apple-system, "PingFang SC", "Microsoft YaHei UI", sans-serif';
        ctx.fillText(label, x1 + 6, topY + 22);
        ctx.restore();
      }
    }

    // 5. Playhead Needle
    const curMs = getCurrentPlayheadMs();
    if (curMs >= winStart && curMs <= winEnd) {
      const px = ((curMs - winStart) / spanMs) * width;
      ctx.strokeStyle = P.playhead;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, height);
      ctx.stroke();

      ctx.fillStyle = P.playhead;
      ctx.beginPath();
      ctx.moveTo(px - 5, 0);
      ctx.lineTo(px + 5, 0);
      ctx.lineTo(px, 7);
      ctx.closePath();
      ctx.fill();
    }

    ctx.restore();
  }

  function hitTestWaveform(canvas, clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    const width = Math.max(1, rect.width);
    const { winStart, spanMs } = getWaveformWindow();
    const timeMs = Math.max(0, winStart + (x / width) * spanMs);

    if (y >= 20 && state.doc) {
      const cues = state.doc.cues || [];
      for (let i = cues.length - 1; i >= 0; i--) {
        const cue = cues[i];
        if (cue.is_special) continue;
        const x1 = ((cue.start_ms - winStart) / spanMs) * width;
        const x2 = Math.max(x1 + 8, ((cue.end_ms - winStart) / spanMs) * width);
        if (x >= x1 - 5 && x <= x2 + 5) {
          if (Math.abs(x - x1) <= 6) return { mode: "trim-start", cue, timeMs, x, width, winStart, spanMs };
          if (Math.abs(x - x2) <= 6) return { mode: "trim-end", cue, timeMs, x, width, winStart, spanMs };
          return { mode: "move", cue, timeMs, x, width, winStart, spanMs };
        }
      }
    }
    return { mode: "seek", cue: null, timeMs, x, width, winStart, spanMs };
  }

  function bindWaveformEvents() {
    const canvas = $("waveform-canvas");
    if (!canvas) return;

    window.addEventListener("resize", () => drawWaveformTimeline());

    canvas.addEventListener("mousemove", (e) => {
      if (state.wfDrag || !state.doc) return;
      const hit = hitTestWaveform(canvas, e.clientX, e.clientY);
      if (hit.mode === "trim-start" || hit.mode === "trim-end") {
        canvas.style.cursor = "ew-resize";
      } else if (hit.mode === "move") {
        canvas.style.cursor = "grab";
      } else {
        canvas.style.cursor = "pointer";
      }
    });

    canvas.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !state.doc) return;
      const hit = hitTestWaveform(canvas, e.clientX, e.clientY);
      if (hit.mode === "seek") {
        state.wfDrag = { mode: "seek", winStart: hit.winStart, spanMs: hit.spanMs, width: hit.width };
        seekStudioToMs(hit.timeMs);
        return;
      }

      const cue = hit.cue;
      activateCue(cue.id, false);
      canvas.style.cursor = hit.mode === "move" ? "grabbing" : "ew-resize";
      state.wfDrag = {
        mode: hit.mode,
        cue,
        origStart: cue.start_ms,
        origEnd: cue.end_ms,
        startClientX: e.clientX,
        winStart: hit.winStart,
        spanMs: hit.spanMs,
        width: hit.width,
        moved: false,
        preSnap: captureDocSnapshotString(),
      };
    });

    window.addEventListener("mousemove", (e) => {
      const drag = state.wfDrag;
      if (!drag) return;

      if (drag.mode === "seek") {
        const rect = canvas.getBoundingClientRect();
        const x = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
        const tMs = drag.winStart + (x / Math.max(1, rect.width)) * drag.spanMs;
        seekStudioToMs(tMs);
        return;
      }

      const dx = e.clientX - drag.startClientX;
      if (Math.abs(dx) > 2) drag.moved = true;
      const deltaMs = Math.round((dx / drag.width) * drag.spanMs);
      const cue = drag.cue;

      if (drag.mode === "move") {
        const dur = drag.origEnd - drag.origStart;
        const nextStart = Math.max(0, drag.origStart + deltaMs);
        cue.start_ms = nextStart;
        cue.end_ms = nextStart + dur;
      } else if (drag.mode === "trim-start") {
        cue.start_ms = Math.max(0, Math.min(drag.origEnd - 300, drag.origStart + deltaMs));
      } else if (drag.mode === "trim-end") {
        cue.end_ms = Math.max(drag.origStart + 300, drag.origEnd + deltaMs);
      }

      const rowEl = document.querySelector(`.cue-row[data-cue-id="${cue.id}"]`);
      if (rowEl) {
        const sInp = rowEl.querySelector('[data-time-field="start_ms"]');
        const eInp = rowEl.querySelector('[data-time-field="end_ms"]');
        if (sInp) sInp.value = msToDisplay(cue.start_ms);
        if (eInp) eInp.value = msToDisplay(cue.end_ms);
      }
      $("player-timecode").textContent = msToDisplay(cue.start_ms);
      drawWaveformTimeline();
    });

    window.addEventListener("mouseup", () => {
      const drag = state.wfDrag;
      if (!drag) return;
      state.wfDrag = null;
      canvas.style.cursor = "pointer";

      if (drag.mode === "seek") return;

      if (drag.moved) {
        pushRawUndoSnapshot(drag.preSnap);
        renderCueList(true);
        updateCounts();
        scheduleAutoSave();
      } else if (drag.cue) {
        activateCue(drag.cue.id, true);
        const rowEl = document.querySelector(`.cue-row[data-cue-id="${drag.cue.id}"]`);
        if (rowEl) rowEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }

  // Multi-File Batch Subtitle Queue & One-Click ZIP Export
  async function enqueueBatchSubtitleFiles(files) {
    let added = 0;
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      form.append("encoding", "auto");
      try {
        const resp = await fetch("/api/subtitles/parse", { method: "POST", body: form });
        const doc = await resp.json();
        if (!resp.ok) throw new Error(doc.detail || "解析失败");
        state.batchQueue.push({
          queueId: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
          doc,
          status: "pending",
          progressPct: 0,
          errorMsg: "",
        });
        added++;
      } catch (err) {
        toast(`${file.name} 解析失败：${err.message}`, "error");
      }
    }
    renderBatchQueue();
    if (added > 0) {
      toast(`已向批量队列添加 ${added} 份字幕文件`, "success");
    }
  }

  function renderBatchQueue() {
    const listEl = $("batch-queue-list");
    const summaryEl = $("batch-summary-text");
    if (!listEl || !summaryEl) return;

    const total = state.batchQueue.length;
    const doneCount = state.batchQueue.filter((it) => it.status === "done").length;
    summaryEl.textContent = `队列共 ${total} 个文件 · 已完成 ${doneCount}/${total}`;

    $("btn-start-batch").disabled = total === 0 || state.batchRunning;
    $("btn-export-batch-zip").disabled = total === 0 || state.batchRunning;
    $("btn-batch-clear").disabled = total === 0 || state.batchRunning;
    $("btn-stop-batch").hidden = !state.batchRunning;

    if (!total) {
      listEl.innerHTML = `<div class="empty-mini">添加多份字幕或直接拖入窗口，即可开始队列精翻或批量转格式。</div>`;
      return;
    }

    listEl.innerHTML = "";
    state.batchQueue.forEach((item, idx) => {
      const dialogueCount = item.doc.cues.filter((c) => !c.is_special).length;
      const translatedCount = item.doc.cues.filter((c) => !c.is_special && (c.target_text || "").trim()).length;
      let badgeCls = "";
      let badgeTxt = `待翻译 ${translatedCount}/${dialogueCount}`;
      if (item.status === "translating") {
        badgeCls = "running";
        badgeTxt = `精翻中 ${item.progressPct}%`;
      } else if (item.status === "done") {
        badgeCls = "done";
        badgeTxt = `已完成 ${translatedCount}/${dialogueCount}`;
      } else if (item.status === "error") {
        badgeCls = "err";
        badgeTxt = `${item.errorMsg || "失败"}`;
      }

      const row = document.createElement("div");
      row.className = "batch-item";
      row.innerHTML = `
        <div class="batch-item-info">
          <div class="batch-item-head">
            <span class="batch-item-name" title="${escapeHtml(item.doc.filename)}">${idx + 1}. ${escapeHtml(item.doc.filename)}</span>
            <span class="doc-tag">${escapeHtml((item.doc.source_format || "srt").toUpperCase())}</span>
            <span class="batch-status-tag ${badgeCls}">${escapeHtml(badgeTxt)}</span>
          </div>
          <div class="batch-progress-bar">
            <div class="batch-progress-fill" style="width: ${item.progressPct || 0}%"></div>
          </div>
        </div>
        <div class="batch-item-actions">
          <button class="btn btn-ghost btn-sm" data-batch-open="${idx}" type="button" ${state.batchRunning ? "disabled" : ""}>打开</button>
          <button class="btn btn-ghost btn-icon btn-sm" data-batch-del="${idx}" type="button" title="从队列移除" ${state.batchRunning ? "disabled" : ""}>${icon("x")}</button>
        </div>
      `;
      listEl.appendChild(row);
    });

    listEl.querySelectorAll("[data-batch-open]").forEach((btn) => {
      btn.onclick = () => {
        const item = state.batchQueue[Number(btn.dataset.batchOpen)];
        if (!item) return;
        loadDocumentIntoStudio(item.doc, true);
        closeModal("modal-batch");
        toast(`已在编辑台打开：${item.doc.filename}`, "info");
      };
    });

    listEl.querySelectorAll("[data-batch-del]").forEach((btn) => {
      btn.onclick = () => {
        if (state.batchRunning) return;
        state.batchQueue.splice(Number(btn.dataset.batchDel), 1);
        renderBatchQueue();
      };
    });
  }

  async function runBatchQueueTranslation() {
    if (state.batchRunning || !state.batchQueue.length) return;
    syncDocContextFields();

    const sharedGlossary = state.doc?.glossary || [];
    const sharedPrompt = state.doc?.custom_prompt || $("custom-prompt-input").value.trim();
    const sharedSummary = state.doc?.scene_summary || $("scene-summary-input").value.trim();
    const srcLang = $("select-source-lang").value || "auto";
    const tgtLang = $("select-target-lang").value || "zh";
    const mode = $("select-trans-mode").value || "reflective";
    const genre = $("select-genre").value || "general";

    const pendingItems = state.batchQueue.filter((it) => it.status !== "done");
    const targets = pendingItems.length ? pendingItems : state.batchQueue;

    state.batchRunning = true;
    state.batchAbort = new AbortController();
    renderBatchQueue();

    let finishedFiles = 0;
    try {
      for (const item of targets) {
        item.status = "translating";
        item.progressPct = 3;
        item.errorMsg = "";
        item.doc.source_language = srcLang === "auto" ? item.doc.source_language || "auto" : srcLang;
        item.doc.target_language = tgtLang;
        item.doc.genre = genre;
        if (sharedGlossary.length && (!item.doc.glossary || !item.doc.glossary.length)) {
          item.doc.glossary = JSON.parse(JSON.stringify(sharedGlossary));
        }
        renderBatchQueue();

        const resp = await fetch("/api/ai/translate-stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: state.batchAbort.signal,
          body: JSON.stringify({
            cues: item.doc.cues,
            source_language: item.doc.source_language,
            target_language: tgtLang,
            mode,
            genre,
            scene_summary: item.doc.scene_summary || sharedSummary,
            custom_prompt: item.doc.custom_prompt || sharedPrompt,
            glossary: item.doc.glossary || sharedGlossary,
          }),
        });

        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.detail || "请求失败");
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        const cueMap = new Map(item.doc.cues.map((c) => [c.id, c]));

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const chunks = buf.split("\n\n");
          buf = chunks.pop() || "";

          for (const raw of chunks) {
            const line = raw.trim();
            if (!line.startsWith("data:")) continue;
            const evt = JSON.parse(line.slice(5).trim());
            if (evt.event === "batch_done") {
              for (const upd of evt.updates || []) {
                const c = cueMap.get(upd.id);
                if (c && upd.target_text) {
                  c.target_text = upd.target_text;
                  c.status = upd.status;
                }
              }
              item.progressPct = evt.percent || item.progressPct;
              renderBatchQueue();
            } else if (evt.event === "error") {
              throw new Error(evt.message || "翻译中断");
            }
          }
        }

        item.status = "done";
        item.progressPct = 100;
        finishedFiles++;
        renderBatchQueue();

        // Persist each completed episode in project archive
        try {
          await fetch("/api/projects/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(item.doc),
          });
        } catch (_) {}

        if (state.doc && state.doc.doc_id === item.doc.doc_id) {
          state.doc.cues = JSON.parse(JSON.stringify(item.doc.cues));
          renderAll(true);
        }
      }
      toast(`批量队列精翻完成（共完成 ${finishedFiles} 份字幕），可一键打包导出 ZIP！`, "success");
    } catch (err) {
      if (err.name === "AbortError") {
        state.batchQueue.forEach((it) => {
          if (it.status === "translating") it.status = "pending";
        });
        toast("已停止批量队列，已完成集数的译文已保留。", "info");
      } else {
        const runningItem = state.batchQueue.find((it) => it.status === "translating");
        if (runningItem) {
          runningItem.status = "error";
          runningItem.errorMsg = err.message || "翻译失败";
        }
        toast(err.message || "批量队列发生错误", "error");
        if ((err.message || "").includes("API Key")) {
          closeModal("modal-batch");
          openModal("modal-settings");
        }
      }
    } finally {
      state.batchRunning = false;
      state.batchAbort = null;
      renderBatchQueue();
    }
  }

  async function exportBatchQueueZip() {
    if (!state.batchQueue.length) return;
    const formats = Array.from(document.querySelectorAll("#batch-format-checks input:checked")).map(
      (el) => el.value
    );
    if (!formats.length) {
      toast("请至少勾选一种打包导出格式。", "error");
      return;
    }
    const trackMode = $("batch-track-mode").value || "bilingual_tgt_top";

    try {
      const resp = await fetch("/api/subtitles/export-batch-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          documents: state.batchQueue.map((it) => it.doc),
          formats,
          track_mode: trackMode,
          ass_secondary_scale: 72,
          ass_style_preset: "cinema_outline",
          fallback_to_source: true,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "ZIP 打包失败");

      if (state.meta?.runtime !== "desktop" && data.download_url) {
        const a = document.createElement("a");
        a.href = data.download_url;
        a.download = data.zip_filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => a.remove(), 300);
      }

      const box = $("batch-export-result");
      box.hidden = false;
      const canOpenDir = state.meta?.runtime && state.meta.runtime !== "docker";
      box.innerHTML = `
        <div>已打包生成 <strong>${escapeHtml(data.zip_filename)}</strong>（共包含 ${data.file_count} 个字幕文件）</div>
        <div style="margin-top:4px;"><small>保存路径：<code>${escapeHtml(data.saved_path)}</code></small></div>
        <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
          <a class="btn btn-subtle btn-sm" href="${escapeHtml(data.download_url)}" download="${escapeHtml(data.zip_filename)}" style="text-decoration:none;">${icon("download")}<span>下载 ZIP</span></a>
          ${canOpenDir ? `<button class="btn btn-subtle btn-sm" id="btn-batch-reveal-dir" type="button">${icon("folder")}<span>打开导出文件夹</span></button>` : ""}
        </div>
      `;
      const revBtn = $("btn-batch-reveal-dir");
      if (revBtn) {
        revBtn.onclick = async () => {
          try {
            await fetch("/api/desktop/open-exports", { method: "POST" });
          } catch (_) {}
        };
      }
      toast(`成功打包 ${data.file_count} 份字幕至 ${data.zip_filename}`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function applyTimeShift(offsetMs) {
    if (!state.doc) return;
    try {
      const resp = await fetch("/api/subtitles/shift", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cues: state.doc.cues, offset_ms: offsetMs }),
      });
      const data = await resp.json();
      pushUndoSnapshot();
      state.doc.cues = data.cues;
      renderCueList();
      scheduleAutoSave();
      toast(`时间轴已整体平移 ${offsetMs > 0 ? "+" : ""}${offsetMs} ms`, "success");
    } catch (_) {
      toast("时间轴平移失败", "error");
    }
  }

  function triggerDownload(filename, content) {
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      a.remove();
      URL.revokeObjectURL(url);
    }, 300);
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  init();
})();
