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
    saveTimer: null,
  };

  const $ = (id) => document.getElementById(id);

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

  function pushUndoSnapshot() {
    if (!state.doc) return;
    const snap = JSON.stringify({
      cues: state.doc.cues,
      glossary: state.doc.glossary,
      scene_summary: state.doc.scene_summary,
    });
    if (state.undoStack.length && state.undoStack[state.undoStack.length - 1] === snap) {
      return;
    }
    state.undoStack.push(snap);
    if (state.undoStack.length > 40) state.undoStack.shift();
    state.redoStack = [];
    refreshUndoButtons();
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
    state.redoStack.push(
      JSON.stringify({
        cues: state.doc.cues,
        glossary: state.doc.glossary,
        scene_summary: state.doc.scene_summary,
      })
    );
    const prev = JSON.parse(state.undoStack.pop());
    state.doc.cues = prev.cues;
    state.doc.glossary = prev.glossary;
    state.doc.scene_summary = prev.scene_summary;
    refreshUndoButtons();
    renderAll();
    scheduleAutoSave();
  }

  function redo() {
    if (!state.doc || !state.redoStack.length) return;
    state.undoStack.push(
      JSON.stringify({
        cues: state.doc.cues,
        glossary: state.doc.glossary,
        scene_summary: state.doc.scene_summary,
      })
    );
    const next = JSON.parse(state.redoStack.pop());
    state.doc.cues = next.cues;
    state.doc.glossary = next.glossary;
    state.doc.scene_summary = next.scene_summary;
    refreshUndoButtons();
    renderAll();
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
      localStorage.setItem("listento_studio_last_doc", JSON.stringify(state.doc));
    } catch (_) {}
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(async () => {
      try {
        await fetch("/api/projects/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state.doc),
        });
        $("doc-save-state").textContent = "已自动保存";
      } catch (_) {
        $("doc-save-state").textContent = "本地已缓存";
      }
    }, 700);
  }

  // Initialization
  async function init() {
    const savedTheme = localStorage.getItem("listento_theme") || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);

    bindEvents();

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

    // Restore cached session if available
    try {
      const cached = localStorage.getItem("listento_studio_last_doc");
      if (cached) {
        const parsed = JSON.parse(cached);
        if (parsed && Array.isArray(parsed.cues) && parsed.cues.length > 0) {
          loadDocumentIntoStudio(parsed, false);
        }
      }
    } catch (_) {}
  }

  function populateLanguageSelectors(languages) {
    const srcSel = $("select-source-lang");
    const tgtSel = $("select-target-lang");
    srcSel.innerHTML = "";
    tgtSel.innerHTML = "";
    for (const lang of languages) {
      const opt1 = document.createElement("option");
      opt1.value = lang.code;
      opt1.textContent = `原: ${lang.label.split(" ")[0]}`;
      srcSel.appendChild(opt1);

      if (lang.code !== "auto") {
        const opt2 = document.createElement("option");
        opt2.value = lang.code;
        opt2.textContent = `译: ${lang.label.split(" ")[0]}`;
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
        menu.hidden = true;
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
      ? `✅ 已从接口实时拉取 (${filtered.length}/${currentCandidateModels.length})`
      : `内置最新推荐模型 (${filtered.length})`;

    chipWrap.innerHTML = "";
    if (!filtered.length) {
      const tip = document.createElement("div");
      tip.className = "model-empty-tip";
      tip.textContent = currentCandidateModels.length
        ? "无匹配过滤词的模型，可直接上方输入框填写任意模型 ID。"
        : "填入地址与 API Key 后点击右上侧「🔄 实时拉取可用模型」，或直接在上方输入框手填模型名。";
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
    label.textContent = ready ? `${state.settings.model}` : "配置 API Key";
    $("select-trans-mode").value = state.settings.default_mode || "reflective";
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
  }

  function loadDocumentIntoStudio(doc, resetHistory = true) {
    state.doc = doc;
    state.cueSuggestions = {};
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
    $("btn-open-qa").disabled = !hasDoc;
    $("btn-split-bilingual").disabled = !hasDoc;
    $("btn-time-shift").disabled = !hasDoc;
    $("btn-toggle-search").disabled = !hasDoc;
    $("btn-extract-glossary").disabled = !hasDoc;

    const firstDialogue = doc.cues.find((c) => !c.is_special) || doc.cues[0];
    state.activeCueId = firstDialogue ? firstDialogue.id : null;
    state.simTimeMs = firstDialogue ? firstDialogue.start_ms : 0;

    renderWarnings();
    renderAll();
    if (firstDialogue) updateStageOverlayForCue(firstDialogue);
    scheduleAutoSave();
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
      <span>💡 ${escapeHtml(warnings.join(" ｜ "))}</span>
      <button class="btn btn-icon btn-sm" id="btn-dismiss-warn" type="button">✕</button>
    `;
    $("btn-dismiss-warn").onclick = () => {
      state.doc.warnings = [];
      strip.hidden = true;
    };
  }

  function renderAll() {
    if (!state.doc) return;
    updateCounts();
    renderGlossary();
    renderCueList();
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
  }

  function renderGlossary() {
    const list = $("glossary-list");
    const glossary = state.doc?.glossary || [];
    $("glossary-count").textContent = glossary.length;

    if (!glossary.length) {
      list.innerHTML = `<div class="empty-mini">添加专有名词或人名后，AI 全文精翻与单句润色将严格统一译法。</div>`;
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
        <button class="btn btn-icon btn-sm" data-g-del="${idx}" type="button" title="删除">✕</button>
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

  function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(38, el.scrollHeight)}px`;
  }

  function renderCueList() {
    const container = $("cue-list");
    const cues = getFilteredCues();
    container.innerHTML = "";

    const frag = document.createDocumentFragment();
    cues.forEach((cue) => {
      const row = document.createElement("div");
      row.className = `cue-row${cue.id === state.activeCueId ? " active" : ""}${cue.is_special ? " special-row" : ""}`;
      row.dataset.cueId = cue.id;

      const durSec = ((cue.end_ms - cue.start_ms) / 1000).toFixed(1);
      const cps = computeCps(cue);
      const longWarn = isCueLongWarning(cue);
      const hasTgt = Boolean((cue.target_text || "").trim());

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
          ${longWarn ? `<span class="badge-mini badge-cps-warn" title="每秒约 ${cps.toFixed(1)} 字，建议点击 ✨ 精简缩字">${cps.toFixed(0)}字/s</span>` : ""}
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
          renderAll();
          scheduleAutoSave();
        }
      });

      // Column 3: Target Editor + AI Suggestions
      const tgtWrap = document.createElement("div");
      tgtWrap.className = "cue-target-wrap";

      const textarea = document.createElement("textarea");
      textarea.className = "cue-target-input";
      textarea.rows = 1;
      textarea.disabled = cue.is_special;
      textarea.placeholder = cue.is_special ? "（ASS 注释/特效绘图行，导出自动原样保留）" : "输入或精修译文… (Tab 跳至下一行)";
      textarea.value = cue.target_text || "";
      textarea.dataset.cueInput = cue.id;

      textarea.addEventListener("focus", () => activateCue(cue.id, false));
      textarea.addEventListener("input", () => {
        autoResizeTextarea(textarea);
        cue.target_text = textarea.value;
        cue.status = textarea.value.trim() ? "edited" : "untranslated";
        updateStageOverlayForCue(cue);
        updateCounts();
        scheduleAutoSave();
      });
      textarea.addEventListener("keydown", (e) => {
        if (e.key === "Tab") {
          e.preventDefault();
          const allInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
          const idx = allInputs.indexOf(textarea);
          const nextIdx = e.shiftKey ? idx - 1 : idx + 1;
          if (allInputs[nextIdx]) {
            allInputs[nextIdx].focus();
          }
        } else if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
          e.preventDefault();
          const allInputs = Array.from(document.querySelectorAll(".cue-target-input:not(:disabled)"));
          const idx = allInputs.indexOf(textarea);
          const nextIdx = e.key === "ArrowUp" ? idx - 1 : idx + 1;
          if (allInputs[nextIdx]) {
            allInputs[nextIdx].focus();
            const targetCueId = Number(allInputs[nextIdx].dataset.cueInput);
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
          <div class="ai-sug-reason">✨ ${escapeHtml(sug.reason || "AI 候选方案（点击即可采纳）：")}</div>
          <div class="ai-sug-chips"></div>
        `;
        const chipsWrap = sugBox.querySelector(".ai-sug-chips");
        allOptions.forEach((optText, i) => {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "sug-chip";
          chip.textContent = `${i === 0 ? "★ 推荐：" : "备选："}${optText}`;
          chip.addEventListener("click", () => {
            pushUndoSnapshot();
            cue.target_text = optText;
            cue.status = "polished";
            delete state.cueSuggestions[cue.id];
            renderCueList();
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
        <button class="btn btn-icon btn-sm" data-act="play" type="button" title="定位并预览此句">▶</button>
        <button class="btn btn-icon btn-sm" data-act="ai" type="button" ${cue.is_special ? "disabled" : ""} title="AI 单句精修 / 重翻 / 缩字">✨</button>
        <button class="btn btn-icon btn-sm" data-act="more" type="button" title="插入 / 合并 / 删除行">⋯</button>
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
          renderCueList();
          scheduleAutoSave();
        });
      });

      row.appendChild(metaCol);
      row.appendChild(srcCol);
      row.appendChild(tgtWrap);
      row.appendChild(actCol);
      frag.appendChild(row);

      requestAnimationFrame(() => autoResizeTextarea(textarea));
    });

    container.appendChild(frag);
  }

  function closeAllPopovers() {
    document.querySelectorAll(".row-popover").forEach((el) => el.remove());
  }

  function toggleAiRowPopover(cue, container) {
    const existing = container.querySelector(".row-popover");
    closeAllPopovers();
    if (existing) return;

    const pop = document.createElement("div");
    pop.className = "row-popover";
    const actions = [
      { key: "retranslate", label: "🔄 结合上下文重新精翻" },
      { key: "natural", label: "💬 润色得更地道口语化" },
      { key: "shorten", label: "✂️ 精简压缩字数 (降语速)" },
      { key: "formal", label: "👔 调整为书面专业表达" },
      { key: "custom", label: "✍️ 输入自定义修改指令…" },
    ];
    actions.forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pop-btn";
      b.textContent = a.label;
      b.addEventListener("click", async () => {
        closeAllPopovers();
        let customInst = "";
        if (a.key === "custom") {
          const res = await askStudioPrompt(
            `#${cue.id} 自定义 AI 精修指令`,
            "",
            "例如：改成敬语口吻 / 保留英文专有名词不翻译 / 控制在12个字以内…"
          );
          if (!res || !res.trim()) return;
          customInst = res.trim();
        }
        await runSingleCueRewrite(cue, a.key, customInst);
      });
      pop.appendChild(b);
    });
    container.appendChild(pop);
  }

  function toggleRowManagePopover(cue, container) {
    const existing = container.querySelector(".row-popover");
    closeAllPopovers();
    if (existing) return;

    const pop = document.createElement("div");
    pop.className = "row-popover";
    const items = [
      {
        label: "✂️ 智能拆分为两条字幕",
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
            toast(`已将 #${cue.id} 按时间比例拆分为两条字幕`, "success");
          } catch (err) {
            toast(err.message, "error");
          }
        },
      },
      {
        label: "➕ 在下方插入新字幕行",
        fn: () => {
          pushUndoSnapshot();
          const idx = state.doc.cues.findIndex((c) => c.id === cue.id);
          const newStart = cue.end_ms + 100;
          const newCue = {
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
          };
          state.doc.cues.splice(idx + 1, 0, newCue);
          reindexCues();
          renderAll();
          scheduleAutoSave();
        },
      },
      {
        label: "🔗 与下一条字幕合并",
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
        label: "🗑 删除此条字幕",
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
    items.forEach((it) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pop-btn";
      b.textContent = it.label;
      b.addEventListener("click", () => {
        closeAllPopovers();
        it.fn();
      });
      pop.appendChild(b);
    });
    container.appendChild(pop);
  }

  function reindexCues() {
    state.doc.cues.forEach((c, i) => {
      c.id = i + 1;
    });
  }

  // Activate Cue & Sync Cinema Stage
  function activateCue(cueId, seekMedia = false) {
    state.activeCueId = cueId;
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
      } else {
        state.simTimeMs = cue.start_ms;
        updatePlayerControlsUI(cue.start_ms);
      }
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
  }

  function togglePlayback() {
    if (state.hasMediaFile) {
      const video = $("media-player");
      if (video.paused) {
        video.play();
        $("play-icon").textContent = "⏸";
      } else {
        video.pause();
        $("play-icon").textContent = "▶";
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
      $("play-icon").textContent = "⏸";
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
    $("play-icon").textContent = "▶";
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
      $("progress-title").textContent = "✅ 全文精翻完成！";
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
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ 正在扫描全片提取术语与背景…";
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
      btn.disabled = false;
      btn.textContent = origText;
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
            <button class="btn btn-primary btn-sm" data-open-proj="${escapeHtml(p.doc_id)}" type="button">打开恢复</button>
            <button class="btn btn-icon btn-sm" data-del-proj="${escapeHtml(p.doc_id)}" type="button" title="删除存档">🗑</button>
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
        title: "⏱ 时间轴重叠行",
        desc: "前一句结束时间晚于下一句起始时间，会导致播放器字幕闪烁跳动",
        ids: report.overlap_ids || [],
      },
      {
        title: "🧹 行尾冗余句号 / 多余空格",
        desc: "符合中文影视规范可一键清理单句末尾多余句号与连续空格",
        ids: report.punctuation_ids || [],
      },
      {
        title: "⚡ 阅读语速过快 (>16字/s)",
        desc: "点击行号定位后可用 ✨「精简压缩字数」优化观看体验",
        ids: report.high_cps_ids || [],
      },
      {
        title: "📝 空白未翻译行",
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
          <span class="qa-count">${sec.ids.length ? `${sec.ids.length} 处` : "✅ 通过"}</span>
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
      if (!e.target.closest("#sample-dropdown")) $("sample-menu").hidden = true;
    });

    $("btn-samples").addEventListener("click", (e) => {
      e.stopPropagation();
      $("sample-menu").hidden = !$("sample-menu").hidden;
    });

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
      for (const f of files) {
        const ext = f.name.split(".").pop().toLowerCase();
        if (["srt", "ass", "ssa", "vtt"].includes(ext)) {
          uploadSubtitleFile(f);
        } else if (f.type.startsWith("video/") || f.type.startsWith("audio/") || ["mp4", "mkv", "mov", "webm", "mp3", "wav", "m4a", "flac"].includes(ext)) {
          mountMediaFile(f);
          if (!state.doc) {
            prepareAsrMediaFile(f);
            openModal("modal-asr");
          }
        } else {
          toast(`不支持的文件类型：${f.name}`, "error");
        }
      }
    });

    // Theme & Sidebar toggles
    $("btn-theme-toggle").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("listento_theme", next);
    });

    $("btn-toggle-sidebar").addEventListener("click", () => {
      $("sidebar").classList.toggle("collapsed");
    });

    // Undo / Redo + Shortcuts
    $("btn-undo").addEventListener("click", undo);
    $("btn-redo").addEventListener("click", redo);

    window.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "z") {
        if (document.activeElement?.tagName === "TEXTAREA" || document.activeElement?.tagName === "INPUT") {
          return;
        }
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (mod && e.key.toLowerCase() === "f" && state.doc) {
        e.preventDefault();
        $("search-bar").hidden = false;
        $("search-input").focus();
      } else if (mod && e.key.toLowerCase() === "s" && state.doc) {
        e.preventDefault();
        syncDocContextFields();
        scheduleAutoSave();
        toast("当前字幕工程已手动保存", "success");
      } else if (e.ctrlKey && e.code === "Space") {
        e.preventDefault();
        togglePlayback();
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
      $("play-icon").textContent = "▶";
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
      const delIdx = e.target.dataset?.gDel;
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
        document.querySelectorAll("#filter-pills .pill").forEach((p) => p.classList.toggle("active", p === pill));
        renderCueList();
      });
    });

    // Search & Replace
    $("btn-toggle-search").addEventListener("click", () => {
      const bar = $("search-bar");
      bar.hidden = !bar.hidden;
      if (!bar.hidden) $("search-input").focus();
      else {
        state.searchQuery = "";
        $("search-input").value = "";
        renderCueList();
      }
    });
    $("btn-close-search").addEventListener("click", () => {
      $("search-bar").hidden = true;
      state.searchQuery = "";
      $("search-input").value = "";
      renderCueList();
    });
    $("search-input").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderCueList();
    });
    $("search-scope").addEventListener("change", (e) => {
      state.searchScope = e.target.value;
      renderCueList();
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

    // Settings, ASR, Projects, QA, Shortcuts, Export Modals
    $("btn-open-settings").addEventListener("click", () => openModal("modal-settings"));
    $("btn-open-export").addEventListener("click", () => openModal("modal-export"));
    $("btn-open-asr").addEventListener("click", () => openModal("modal-asr"));
    $("btn-welcome-asr").addEventListener("click", () => openModal("modal-asr"));
    $("btn-open-projects").addEventListener("click", () => openModal("modal-projects"));
    $("btn-open-qa").addEventListener("click", () => openModal("modal-qa"));
    $("btn-open-shortcuts").addEventListener("click", () => openModal("modal-shortcuts"));

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
      btn.disabled = true;
      const origTxt = btn.textContent;
      btn.textContent = "⏳ 正在识别语音生成时间轴…";
      box.hidden = false;
      box.className = "test-result-box";
      box.textContent = "正在上传音频流并提取时间轴字幕，请稍候…";

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
        box.textContent = `❌ ${err.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = origTxt;
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
      const formats = Array.from(document.querySelectorAll(".format-checks input:checked")).map((el) => el.value);
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
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "⏳ 正在拉取…";
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
        btn.disabled = false;
        btn.textContent = origText;
      }
    });

    $("btn-test-api").addEventListener("click", async () => {
      const box = $("cfg-test-result");
      box.hidden = false;
      box.className = "test-result-box";
      box.textContent = "⏳ 正在测试 API 连通性与响应延迟…";
      try {
        const resp = await fetch("/api/settings/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSettingsForm()),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "连接失败");
        box.className = "test-result-box ok";
        box.textContent = `✅ 连接成功！模型 ${data.model} 响应正常，往返延迟 ${data.latency_ms} ms`;
      } catch (err) {
        box.className = "test-result-box err";
        box.textContent = `❌ ${err.message}`;
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
      const formats = Array.from(document.querySelectorAll(".format-checks input:checked")).map((el) => el.value);
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
          <div>✅ 已成功生成 <strong>${data.files.map((x) => escapeHtml(x.filename)).join(", ")}</strong></div>
          <div style="margin-top:4px;"><small>保存路径：<code>${escapeHtml(data.exports_dir)}</code></small></div>
          ${canOpenDir ? `<div style="margin-top:8px;"><button class="btn btn-subtle btn-sm" id="btn-reveal-exports" type="button">📂 打开导出文件夹</button></div>` : ""}
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
