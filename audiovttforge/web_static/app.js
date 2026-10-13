const state = {
  sourceDir: "",
  scan: null,
  assignments: {},
  previewImageIndex: 0,
  autoOutputName: "result.mp4",
  outputNameTouched: false,
  jobId: null,
  after: 0,
  pollTimer: null,
  polling: false,
  pollFailures: 0,
  serviceOnline: false,
  cancelPending: false,
  desktop: null,
  restoring: true,
  scanSequence: 0,
  scanning: false,
  submitting: false,
  recoveringJob: false,
  outputFollowsSource: true,
  recentSources: [],
  restoredAssignments: null,
  imagePaths: [],
  picking: false,
  flushing: false,
};

const MAX_POLL_FAILURES = 10;

const $ = (id) => document.getElementById(id);
const sourceInput = $("source-dir");

// Survives a page reload so a running render can be picked up again (the job
// registry itself only lives in the service process).
const JOB_STORAGE_KEY = "audiovttforge.jobId";
const SETTINGS_STORAGE_KEY = "audiovttforge.outputSettings";
const WORKSPACE_STORAGE_KEY = "audiovttforge.workspace";
let workspaceWrites = null;
let pendingWorkspace = null;
let lastSavedWorkspace = "";
let workspaceSaveError = null;
let currentScan = Promise.resolve();
const PERSISTED_SETTING_IDS = [
  "subtitle-mode",
  "merge-subtitles",
  "fps",
  "width",
  "workers",
  "font-name",
  "font-size",
  "font-color",
  "font-outline-color",
  "font-outline-width",
  "font-shadow-color",
  "font-shadow",
  "subtitle-margin",
  "audio-codec",
  "subtitle-preview-text",
];

// Labels stay in the UI; every value, default and limit comes from the
// /api/v1/capabilities resource so nothing is declared twice.
const SUBTITLE_LABELS = {
  burnin: "烧录到画面",
  embedded: "嵌入字幕轨",
  none: "不处理字幕",
};

const FONT_LABELS = {
  "Microsoft YaHei": "微软雅黑",
  SimSun: "宋体",
  SimHei: "黑体",
  "Yu Gothic": "游ゴシック",
  Arial: "Arial",
  "Segoe UI": "Segoe UI",
  "Noto Sans CJK SC": "Noto Sans CJK SC",
};

function fillSelect(select, values, format) {
  select.innerHTML = "";
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = format(value);
    select.appendChild(option);
  });
}

function selectValue(select, value) {
  const target = String(value);
  if (Array.from(select.options).some((option) => option.value === target)) {
    select.value = target;
  }
}

function readSavedSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "null");
    return saved && typeof saved === "object" ? saved : {};
  } catch (_error) {
    return {};
  }
}

function restoreSavedSettings(saved) {
  PERSISTED_SETTING_IDS.forEach((id) => {
    const value = saved[id];
    if (value === undefined || value === null) return;
    const control = $(id);
    if (!control) return;
    if (control.type === "checkbox") {
      control.checked = Boolean(value);
      return;
    } else if (id.endsWith("color")) {
      if (/^#[0-9a-fA-F]{6}$/.test(String(value))) control.value = value;
      return;
    }
    if (control.tagName === "SELECT") {
      selectValue(control, value);
    } else {
      control.value = String(value);
    }
  });
}

function saveSettings() {
  saveWorkspace();
}

function sameDirectory(left, right) {
  return String(left || "").replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase()
    === String(right || "").replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase();
}

function selectedImagePaths() {
  const paths = {};
  if (!state.scan) {
    return state.restoredAssignments && sameDirectory(state.restoredAssignments.path, sourceInput.value.trim())
      ? { ...state.restoredAssignments.names } : paths;
  }
  if (!sameDirectory(state.sourceDir, sourceInput.value.trim())) return paths;
  state.scan.audio.forEach((audio) => {
    const index = state.assignments[audio.name];
    paths[audio.name] = index == null ? null : state.scan.images[index]?.path || null;
  });
  return paths;
}

function workspaceSnapshot() {
  if (state.restoring) return null;
  const settings = {};
  PERSISTED_SETTING_IDS.forEach((id) => {
    const control = $(id);
    settings[id] = control.type === "checkbox" ? control.checked : control.value;
  });
  return {
    source_dir: sourceInput.value.trim(),
    output_dir: $("output-dir").value.trim(),
    output_follows_source: state.outputFollowsSource,
    output_name: $("output-name").value.trim(),
    output_name_touched: state.outputNameTouched,
    recent_sources: [...state.recentSources],
    assignments: selectedImagePaths(),
    image_paths: [...state.imagePaths],
    settings,
  };
}

window.audioVttWorkspaceSnapshot = workspaceSnapshot;

function setSaveStatus(status, error = null) {
  const element = $("settings-save-status");
  element.dataset.state = status;
  element.textContent = { loading: "正在读取…", saving: "正在保存…", saved: "已保存", failed: "保存失败", load_failed: "读取失败" }[status];
  element.title = error ? error.message || String(error) : "";
  $("settings-save-retry").hidden = !["failed", "load_failed"].includes(status);
  $("settings-save-error").hidden = !error;
  $("settings-save-error").textContent = error ? `设置${status === "load_failed" ? "读取" : "保存"}失败：${error.message || error}` : "";
}

async function writePendingWorkspace() {
  while (pendingWorkspace) {
    const snapshot = pendingWorkspace;
    pendingWorkspace = null;
    try {
      await parseResponse(await fetch("/api/v1/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot),
      }));
      lastSavedWorkspace = JSON.stringify(snapshot);
      workspaceSaveError = null;
    } catch (error) {
      pendingWorkspace ||= snapshot;
      workspaceSaveError = error;
      setSaveStatus("failed", error);
      return false;
    }
  }
  setSaveStatus("saved");
  return true;
}

function saveWorkspace() {
  const snapshot = workspaceSnapshot();
  if (!snapshot) return Promise.resolve(false);
  if (!workspaceWrites && !pendingWorkspace && JSON.stringify(snapshot) === lastSavedWorkspace) return Promise.resolve(true);
  pendingWorkspace = snapshot;
  setSaveStatus("saving");
  if (!workspaceWrites) {
    // Only one request writes at a time; edits made during it replace the pending snapshot.
    workspaceWrites = writePendingWorkspace().finally(() => {
      workspaceWrites = null;
      // A scan response can enqueue another snapshot between the last write and
      // this promise's cleanup microtask. Keep flush waiting for that write too.
      if (pendingWorkspace && !workspaceSaveError) return saveWorkspace();
    });
  }
  return workspaceWrites;
}

window.audioVttFlushWorkspace = async () => {
  if (state.restoring) return null;
  state.flushing = true;
  updateWorkspaceControls();
  try {
    await currentScan;
    await saveWorkspace();
    if (pendingWorkspace || workspaceSaveError) throw workspaceSaveError || new Error("设置尚未保存，请重试。");
    return workspaceSnapshot();
  } catch (error) {
    state.flushing = false;
    updateWorkspaceControls();
    throw error;
  }
};

window.audioVttResumeWorkspace = () => {
  state.flushing = false;
  updateWorkspaceControls();
};

function renderRecentSources() {
  const select = $("recent-sources");
  select.replaceChildren(new Option("选择最近使用的文件夹…", ""));
  state.recentSources.forEach((path) => select.add(new Option(path, path)));
  $("recent-source-row").hidden = !state.recentSources.length;
}

function rememberSource(path) {
  state.recentSources = [path, ...state.recentSources.filter((item) => !sameDirectory(item, path))].slice(0, 8);
  renderRecentSources();
}

function updateWorkspaceControls() {
  const active = Boolean(state.pollTimer || state.submitting || state.recoveringJob || state.restoring || state.flushing);
  const busy = active || state.scanning || state.picking;
  ["choose-source", "choose-output", "add-images", "recent-sources", "source-dir", "output-dir", "output-name", "output-follows-source"].forEach((id) => {
    $(id).disabled = busy;
  });
  $("scan-button").disabled = busy;
  $("start-button").disabled = busy || !state.scan?.ready || !state.serviceOnline;
  PERSISTED_SETTING_IDS.forEach((id) => { $(id).disabled = active; });
  document.querySelectorAll(".assignment-select").forEach((select) => { select.disabled = busy || !state.scan?.images.length; });
  document.querySelectorAll(".remove-image").forEach((button) => { button.disabled = busy; });
}

function applyOutputMode() {
  $("output-follows-source").checked = state.outputFollowsSource;
  if (state.outputFollowsSource) $("output-dir").value = state.sourceDir;
}

async function chooseDirectory(kind) {
  if (!state.desktop || workspaceBusy()) return;
  state.picking = true;
  updateWorkspaceControls();
  showError("");
  try {
    const initial = kind === "source" ? sourceInput.value : $("output-dir").value || state.sourceDir;
    const path = await state.desktop.select_directory(kind, initial);
    if (!path) return; // Cancelling the native dialog leaves the workspace intact.
    if (kind === "source") {
      sourceInput.value = path;
      await scanSource();
    } else {
      state.outputFollowsSource = false;
      $("output-dir").value = path;
      applyOutputMode();
      saveWorkspace();
    }
  } catch (error) {
    showError(`无法选择文件夹：${error.message || error}`);
  } finally {
    state.picking = false;
    updateWorkspaceControls();
  }
}

function workspaceBusy() {
  return Boolean(state.pollTimer || state.submitting || state.recoveringJob || state.restoring || state.scanning || state.picking || state.flushing);
}

function fileName(path) {
  return String(path || "").split(/[\\/]/).pop();
}

function parentDirectory(path) {
  return String(path || "").replace(/[\\/][^\\/]*$/, "");
}

function currentImages() {
  return state.scan?.images || state.imagePaths.map((path) => ({ name: fileName(path), path }));
}

function imageLabel(image, images = currentImages()) {
  const duplicates = images.filter((item) => item.name.toLowerCase() === image.name.toLowerCase());
  if (duplicates.length < 2) return image.name;
  const directoryParts = (item) => parentDirectory(item.path).split(/[\\/]/).filter(Boolean);
  const parts = directoryParts(image);
  let depth = 1;
  while (depth < parts.length && duplicates.some((item) => item !== image
    && directoryParts(item).slice(-depth).join("/").toLowerCase() === parts.slice(-depth).join("/").toLowerCase())) {
    depth += 1;
  }
  return `${parts.slice(-depth).join(" / ")} / ${image.name}`;
}

async function addImages() {
  if (!state.desktop || workspaceBusy()) return;
  state.picking = true;
  updateWorkspaceControls();
  showError("");
  try {
    const paths = await state.desktop.select_images(parentDirectory(state.imagePaths.at(-1)) || state.sourceDir);
    if (!Array.isArray(paths) || !paths.length) return;
    paths.forEach((path) => {
      if (typeof path === "string" && !state.imagePaths.some((item) => sameDirectory(item, path))) state.imagePaths.push(path);
    });
    saveWorkspace();
    if (sourceInput.value.trim()) await scanSource();
    else renderPendingImages();
  } catch (error) {
    showError(`无法添加图片：${error.message || error}`);
  } finally {
    state.picking = false;
    updateWorkspaceControls();
  }
}

async function removeImage(path) {
  if (workspaceBusy()) return;
  state.imagePaths = state.imagePaths.filter((item) => !sameDirectory(item, path));
  saveWorkspace();
  if (sourceInput.value.trim()) await scanSource();
  else renderPendingImages();
}

async function connectDesktop() {
  if (new URLSearchParams(location.search).get("desktop") === "1") {
    if (typeof window.pywebview?.api?.select_directory !== "function") {
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("桌面连接未就绪，请关闭窗口后重新启动。")), 15000);
        window.addEventListener("pywebviewready", () => { clearTimeout(timeout); resolve(); }, { once: true });
      });
    }
    state.desktop = window.pywebview.api;
    document.body.classList.add("is-desktop");
    $("choose-source").hidden = false;
    $("choose-output").hidden = false;
    $("add-images").hidden = false;
    $("workspace-mode").textContent = "桌面版 · 本地处理";
    $("scan-button").lastElementChild.textContent = "重新扫描";
    setServiceStatus(state.serviceOnline);
  }
}

async function restoreWorkspace() {
  state.restoring = true;
  updateWorkspaceControls();
  setSaveStatus("loading");
  let saved = await parseResponse(await fetch("/api/v1/workspace"));
  saved = saved && typeof saved === "object" ? saved : {};
  if (!Object.keys(saved.settings || {}).length) {
    // Migrate earlier browser settings once; the file-backed store owns all future writes.
    try {
      const legacy = JSON.parse(localStorage.getItem(WORKSPACE_STORAGE_KEY) || "{}");
      if (!saved.source_dir && legacy.source_dir) saved = { ...saved, ...legacy };
      saved.settings = { ...readSavedSettings(), ...(legacy.settings || {}), ...(saved.settings || {}) };
    } catch (_error) { /* No readable legacy workspace. */ }
  }
  state.outputFollowsSource = saved.output_follows_source !== false;
  state.outputNameTouched = Boolean(saved.output_name_touched);
  state.recentSources = Array.isArray(saved.recent_sources) ? saved.recent_sources.filter((item) => typeof item === "string").slice(0, 8) : [];
  state.imagePaths = Array.isArray(saved.image_paths) ? saved.image_paths.filter((item) => typeof item === "string") : [];
  if (typeof saved.source_dir === "string") sourceInput.value = saved.source_dir;
  if (typeof saved.output_dir === "string") $("output-dir").value = saved.output_dir;
  if (typeof saved.output_name === "string" && state.outputNameTouched) $("output-name").value = saved.output_name;
  if (saved.settings && typeof saved.settings === "object") restoreSavedSettings(saved.settings);
  state.restoredAssignments = { path: saved.source_dir, names: saved.assignments || {} };
  $("output-follows-source").checked = state.outputFollowsSource;
  renderRecentSources();
  updateSubtitlePreview();
  state.restoring = false;
  if (sourceInput.value.trim()) await scanSource();
  else clearScan();
  saveWorkspace();
  updateWorkspaceControls();
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setServiceStatus(online) {
  const status = $("service-status");
  status.dataset.state = online ? "online" : "offline";
  $("service-status-text").textContent = online ? "已就绪 · 本地处理" : "服务未连接";
}

function setJobState(stateName, label) {
  const target = $("job-state");
  target.dataset.state = stateName;
  target.textContent = label;
}

function setProgress(value) {
  const progress = Math.max(0, Math.min(100, Number(value) || 0));
  $("progress-bar").style.width = `${progress}%`;
  $("progress-value").textContent = `${Math.round(progress)}%`;
}

function updatePreviewTarget(preview, stage, fontName, fontSize, fontColor, text) {
  if (!preview || !stage) return;
  const outputWidth = Number($("width").value) || 1920;
  const outputScale = Math.max(stage.getBoundingClientRect().width / outputWidth, 0.01);
  preview.textContent = text;
  preview.style.fontFamily = `"${fontName}", sans-serif`;
  preview.style.fontSize = `${Math.max(6, fontSize * outputScale)}px`;
  preview.style.color = fontColor;
  const outlineWidth = Number($("font-outline-width").value) || 0;
  const outlineColor = $("font-outline-color").value || "#000000";
  const shadow = Number($("font-shadow").value) || 0;
  const shadowColor = $("font-shadow-color").value || "#000000";
  const margin = Number($("subtitle-margin").value) || 0;
  preview.style.webkitTextStroke = `${Math.max(0.1, outlineWidth * outputScale)}px ${outlineColor}`;
  preview.style.textShadow = shadow > 0
    ? `0 ${Math.max(0.1, shadow * outputScale)}px ${Math.max(0.2, shadow * 2 * outputScale)}px ${shadowColor}`
    : "none";
  preview.style.bottom = `${Math.max(0, margin) * outputScale}px`;
}

function updateSubtitlePreview() {
  const fontName = $("font-name").value;
  const fontSize = Number($("font-size").value) || 42;
  const fontColor = $("font-color").value;
  const text = $("subtitle-preview-text").value || "这是字幕样式预览";
  updatePreviewTarget($("subtitle-preview"), $("subtitle-preview-stage"), fontName, fontSize, fontColor, text);
  updatePreviewTarget($("subtitle-preview-modal-text"), $("preview-modal-stage"), fontName, fontSize, fontColor, text);
  const outputWidth = Number($("width").value) || 1920;
  const outputHeight = Math.round(outputWidth * 9 / 16);
  $("subtitle-preview-meta").textContent = `${outputWidth} × ${outputHeight} · ${fontName} · ${fontSize} px`;
  const dimensions = $("output-dimensions");
  if (dimensions) dimensions.textContent = `${outputWidth} × ${outputHeight} · 16:9`;
}

function setPreviewImage(index) {
  const image = state.scan?.images[index];
  $("preview-image-select").title = image?.path || "";
  const previewImage = $("subtitle-preview-image");
  const placeholder = $("subtitle-preview-placeholder");
  const modalImage = $("subtitle-preview-modal-image");
  const modalPlaceholder = $("subtitle-preview-modal-placeholder");
  if (!image) {
    [previewImage, modalImage].forEach((target) => {
      target.removeAttribute("src");
      target.hidden = true;
    });
    [placeholder, modalPlaceholder].forEach((target) => {
      target.hidden = false;
      target.textContent = state.scan && !state.scan.images.length
        ? "未找到图片，将使用黑色背景"
        : state.imagePaths.length ? "选择音频文件夹后预览" : "暂无图片";
    });
    return;
  }

  state.previewImageIndex = index;
  const query = new URLSearchParams({ path: image.path });
  [placeholder, modalPlaceholder].forEach((target) => {
    target.hidden = false;
    target.textContent = "正在加载图片…";
  });
  [previewImage, modalImage].forEach((target) => { target.hidden = true; });
  previewImage.onload = () => {
    previewImage.hidden = false;
    placeholder.hidden = true;
  };
  previewImage.onerror = () => {
    previewImage.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "图片预览不可用";
  };
  modalImage.onload = () => {
    modalImage.hidden = false;
    modalPlaceholder.hidden = true;
  };
  modalImage.onerror = () => {
    modalImage.hidden = true;
    modalPlaceholder.hidden = false;
    modalPlaceholder.textContent = "图片预览不可用";
  };
  previewImage.src = `/api/v1/sources/image?${query}`;
  modalImage.src = previewImage.src;
}

function renderPreviewImages(scan, previousPath = null) {
  const select = $("preview-image-select");
  select.innerHTML = "";
  scan.images.forEach((image, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = imageLabel(image, scan.images);
    option.title = image.path;
    select.appendChild(option);
  });
  select.disabled = !scan.images.length;
  if (scan.images.length) {
    const previousIndex = scan.images.findIndex((image) => sameDirectory(image.path, previousPath));
    const index = Math.max(0, previousIndex);
    select.value = String(index);
    setPreviewImage(index);
  } else {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "未找到图片";
    select.appendChild(option);
    setPreviewImage(-1);
  }
}

function showError(message) {
  $("form-error").textContent = message || "";
}

function setScanStatus(message, tone = "") {
  const status = $("scan-status");
  status.textContent = message;
  status.dataset.state = tone;
}

function scanItem(name, meta, badge) {
  const item = document.createElement("div");
  item.className = "scan-item";
  const badgeElement = document.createElement("span");
  badgeElement.className = "file-badge";
  badgeElement.textContent = badge;
  const nameElement = document.createElement("span");
  nameElement.className = "scan-item-name";
  nameElement.title = name;
  nameElement.textContent = name;
  const metaElement = document.createElement("span");
  metaElement.className = "scan-item-meta";
  metaElement.textContent = meta;
  item.append(badgeElement, nameElement, metaElement);
  return item;
}

function renderScanList(targetId, items, describe) {
  const target = $(targetId);
  target.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("span");
    empty.className = "scan-list-empty";
    empty.textContent = "未找到符合扩展名的文件";
    target.appendChild(empty);
    return;
  }
  items.forEach((item) => target.appendChild(describe(item)));
}

function imageRow(image, missing = false) {
  const item = scanItem(image.name, missing ? "文件不存在" : image.size == null ? "" : formatBytes(image.size), "IMG");
  item.classList.add("image-item");
  const name = item.querySelector(".scan-item-name");
  name.title = image.path;
  name.textContent = "";
  const title = document.createElement("strong");
  title.textContent = image.name;
  const directory = document.createElement("span");
  directory.className = "image-source";
  directory.textContent = parentDirectory(image.path);
  name.append(title, directory);
  if (missing) item.classList.add("is-missing");
  const imported = state.imagePaths.some((path) => sameDirectory(path, image.path));
  const inSource = Boolean(state.scan) && sameDirectory(parentDirectory(image.path), state.sourceDir);
  if (imported && !inSource) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-image";
    remove.textContent = "移除";
    remove.title = "从素材清单移除，保留原文件";
    remove.setAttribute("aria-label", `移除图片 ${image.name}，${parentDirectory(image.path)}`);
    remove.addEventListener("click", () => removeImage(image.path));
    item.appendChild(remove);
  }
  return item;
}

function renderPendingImages() {
  const images = currentImages();
  $("pending-images").hidden = Boolean(state.scan) || !state.imagePaths.length;
  $("pending-images-count").textContent = `已添加图片 · ${state.imagePaths.length}`;
  renderScanList("pending-image-list", images, (item) => imageRow(item));
  if (!state.scan) {
    $("preview-image-select").replaceChildren(new Option(state.imagePaths.length ? "选择音频文件夹后预览" : "暂无图片", ""));
    $("preview-image-select").disabled = true;
    setPreviewImage(-1);
  }
}

function renderAudioAssignments(scan) {
  const target = $("scan-audio-list");
  target.innerHTML = "";
  if (!scan.audio.length) {
    renderScanList("scan-audio-list", [], () => null);
    return;
  }
  scan.audio.forEach((audio) => {
    const row = document.createElement("div");
    row.className = "assignment-row";

    const info = document.createElement("div");
    info.className = "assignment-info";
    const name = document.createElement("strong");
    name.className = "assignment-name";
    name.title = audio.name;
    name.textContent = audio.name;
    const subtitle = document.createElement("span");
    subtitle.className = `assignment-subtitle${audio.subtitle ? "" : " is-warning"}`;
    subtitle.textContent = audio.subtitle ? `字幕已匹配 · ${formatBytes(audio.size)}` : `缺少字幕 · ${formatBytes(audio.size)}`;
    info.append(name, subtitle);

    const select = document.createElement("select");
    select.className = "assignment-select";
    select.setAttribute("aria-label", `${audio.name} 使用图片`);
    // The default mapping is computed by the service, not re-derived here.
    const automaticOption = document.createElement("option");
    automaticOption.value = "";
    automaticOption.textContent = !scan.images.length
      ? "黑色背景（无图片）"
      : audio.automatic_image_name
      ? `自动分配 · ${scan.images[audio.automatic_image] ? imageLabel(scan.images[audio.automatic_image], scan.images) : audio.automatic_image_name}`
      : "自动分配";
    automaticOption.title = scan.images[audio.automatic_image]?.path || "";
    select.appendChild(automaticOption);
    select.disabled = !scan.images.length;
    scan.images.forEach((image, imageIndex) => {
      const option = document.createElement("option");
      option.value = String(imageIndex);
      option.textContent = `${imageIndex + 1}. ${imageLabel(image, scan.images)}`;
      option.title = image.path;
      select.appendChild(option);
    });
    const selected = state.assignments[audio.name];
    if (selected !== undefined && selected !== null) select.value = String(selected);
    select.title = scan.images[selected ?? audio.automatic_image]?.path || "";
    select.addEventListener("change", () => {
      state.assignments[audio.name] = select.value === "" ? null : Number(select.value);
      select.title = scan.images[state.assignments[audio.name] ?? audio.automatic_image]?.path || "";
      saveWorkspace();
    });
    row.append(info, select);
    target.appendChild(row);
  });
}

function renderScan(scan) {
  const previousAssignments = sameDirectory(state.sourceDir, scan.source_dir)
    ? selectedImagePaths()
    : sameDirectory(state.restoredAssignments?.path, scan.source_dir) ? state.restoredAssignments.names : {};
  const previousPreview = currentImages()[state.previewImageIndex]?.path;
  state.scan = scan;
  state.sourceDir = scan.source_dir;
  sourceInput.value = scan.source_dir;
  state.assignments = {};
  scan.audio.forEach((audio) => {
    const imagePath = previousAssignments[audio.name];
    let index = scan.images.findIndex((image) => sameDirectory(image.path, imagePath));
    // Earlier versions saved a basename. Restore it only when it is unambiguous.
    if (index < 0 && imagePath && !/[\\/]/.test(imagePath)) {
      const matching = scan.images.filter((image) => image.name.toLowerCase() === imagePath.toLowerCase());
      if (matching.length === 1) index = scan.images.indexOf(matching[0]);
    }
    if (index >= 0) state.assignments[audio.name] = index;
  });
  state.restoredAssignments = null;
  state.autoOutputName = scan.suggested_output_name || `${scan.source_name || "result"}.mp4`;
  if (!state.outputNameTouched) $("output-name").value = state.autoOutputName;
  applyOutputMode();
  $("asset-count").textContent = `${scan.counts.total} 个文件`;
  $("scan-preview").hidden = false;
  $("scan-overview").innerHTML = "";
  [
    ["音频", scan.counts.audio, "scan-stat-audio"],
    ["图片", scan.counts.images, "scan-stat-image"],
    ["字幕", scan.counts.subtitles, "scan-stat-subtitle"],
  ].forEach(([label, value, className]) => {
    const stat = document.createElement("div");
    stat.className = `scan-stat ${className}`;
    const labelElement = document.createElement("span");
    labelElement.textContent = label;
    const valueElement = document.createElement("strong");
    valueElement.textContent = value;
    stat.append(labelElement, valueElement);
    $("scan-overview").appendChild(stat);
  });

  renderAudioAssignments(scan);
  renderScanList("scan-image-list", scan.images, (item) => imageRow(item));
  (scan.missing_images || []).forEach((path) => {
    const image = typeof path === "string" ? { name: fileName(path), path } : path;
    $("scan-image-list").appendChild(imageRow(image, true));
  });
  renderScanList("scan-subtitle-list", scan.subtitles, (item) => scanItem(item.name, formatBytes(item.size), item.name.toLowerCase().endsWith(".lrc") ? "LRC" : "VTT"));
  renderPreviewImages(scan, previousPreview);
  renderPendingImages();
  $("scan-audio-count").textContent = `${scan.counts.audio} 个`;
  $("scan-image-count").textContent = `${scan.counts.images} 个`;
  $("scan-subtitle-count").textContent = `${scan.counts.subtitles} 个`;

  const warningBox = $("scan-warning");
  const warningList = $("scan-warning-list");
  warningList.innerHTML = "";
  const warnings = [...(scan.warnings || [])];
  if (scan.missing_images?.length) warnings.push(`${scan.missing_images.length} 张外部图片已移动或删除，已跳过；可在图片清单中移除。`);
  warnings.forEach((warning) => {
    const item = document.createElement("li");
    item.textContent = warning;
    warningList.appendChild(item);
  });
  warningBox.hidden = !warnings.length;
  if (scan.ready) {
    setScanStatus(
      scan.images.length
        ? `${scan.counts.audio} 个音频 · ${scan.counts.images} 张图片`
        : `${scan.counts.audio} 个音频 · 黑色背景`,
      scan.images.length ? "ready" : "warning",
    );
    $("start-button").disabled = false;
    $("job-summary").textContent = scan.images.length
      ? `${scan.counts.audio} 个音频 · ${scan.counts.images} 张图片已就绪。`
      : `${scan.counts.audio} 个音频已就绪，将使用黑色背景。`;
  } else {
    setScanStatus("目录中未找到音频，暂时不能开始渲染。", "warning");
    $("start-button").disabled = true;
  }
  rememberSource(scan.source_dir);
  saveWorkspace();
}

function clearScan() {
  state.scan = null;
  state.sourceDir = "";
  state.assignments = {};
  state.previewImageIndex = 0;
  if (state.outputFollowsSource) $("output-dir").value = "";
  const previewSelect = $("preview-image-select");
  previewSelect.innerHTML = '<option value="">暂无图片</option>';
  previewSelect.disabled = true;
  setPreviewImage(-1);
  $("scan-preview").hidden = true;
  $("asset-count").textContent = "0 个文件";
  $("start-button").disabled = true;
  setScanStatus("请选择音频与字幕所在的文件夹。");
  renderPendingImages();
}

function eventLabel(type) {
  const labels = {
    job_queued: "任务已排队",
    job_started: "开始处理",
    probe_started: "读取音频信息",
    probe_finished: "音频信息已读取",
    task_started: "开始生成片段",
    task_progress: "片段处理中",
    task_finished: "片段已完成",
    task_skipped: "跳过已有片段",
    subtitle_missing: "未找到字幕",
    subtitle_empty: "字幕为空",
    overall_progress: "总体进度",
    merge_started: "开始合并",
    merge_finished: "合并完成",
    subtitles_merged: "总 VTT 已生成",
    job_finished: "任务完成",
    job_failed: "任务失败",
    job_cancelled: "任务已取消",
  };
  return labels[type] || type;
}

function eventDetails(event) {
  if (event.error) return event.error;
  if (event.type === "task_progress" && event.progress) {
    const audio = event.audio ? `${event.audio.split(/[\\/]/).pop()}: ` : "";
    return `${audio}${event.progress}`;
  }
  if (event.progress !== undefined && event.completed !== undefined) return `${event.completed} / ${event.total}`;
  if (event.audio) return event.audio.split(/[\\/]/).pop();
  if (event.output) return event.output.split(/[\\/]/).pop();
  return "已记录";
}

function appendEvents(items) {
  const list = $("event-list");
  if (list.querySelector(".event-empty")) list.innerHTML = "";
  items.forEach((event) => {
    if (event.type === "task_progress" && event.audio) {
      const previous = Array.from(list.querySelectorAll("tr[data-progress-audio]")).find(
        (row) => row.dataset.progressAudio === event.audio,
      );
      if (previous) {
        const cells = previous.querySelectorAll("td");
        cells[0].textContent = new Date(event.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        cells[2].textContent = eventDetails(event);
        cells[2].title = cells[2].textContent;
        list.prepend(previous);
        return;
      }
    }
    const row = document.createElement("tr");
    if (event.type === "task_progress" && event.audio) row.dataset.progressAudio = event.audio;
    const time = new Date(event.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    [time, eventLabel(event.type), eventDetails(event)].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      cell.title = value;
      row.appendChild(cell);
    });
    list.prepend(row);
  });
}

async function parseResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = data.error || {};
    const details = Array.isArray(error.details) ? ` ${error.details.join(" ")}` : "";
    throw new Error(`${error.message || `请求失败 (${response.status})`}${details}`);
  }
  return data;
}

async function checkHealth() {
  try {
    await parseResponse(await fetch("/api/v1/health"));
    state.serviceOnline = true;
    setServiceStatus(true);
  } catch (_error) {
    state.serviceOnline = false;
    setServiceStatus(false);
  }
}

function scanSource() {
  currentScan = performScanSource();
  return currentScan;
}

async function performScanSource() {
  if (state.pollTimer || state.submitting || state.recoveringJob || state.restoring || state.flushing) return;
  const sequence = ++state.scanSequence;
  showError("");
  const path = sourceInput.value.trim();
  if (!path) {
    clearScan();
    showError("请输入素材目录路径。");
    sourceInput.focus();
    return;
  }
  state.scanning = true;
  updateWorkspaceControls();
  setScanStatus("正在读取目录…");
  try {
    const scan = await parseResponse(await fetch("/api/v1/sources/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, image_paths: state.imagePaths }),
    }));
    if (sequence !== state.scanSequence) return;
    state.serviceOnline = true;
    setServiceStatus(true);
    renderScan(scan);
  } catch (error) {
    if (sequence !== state.scanSequence) return;
    clearScan();
    setScanStatus("目录扫描失败。", "warning");
    showError(error.message);
  } finally {
    if (sequence === state.scanSequence) {
      state.scanning = false;
      updateWorkspaceControls();
    }
  }
}

function buildJobPayload() {
  const overrides = state.scan.audio.map((audio) => state.assignments[audio.name]);
  const payload = {
    source_dir: state.sourceDir,
    image_paths: state.scan.images.map((image) => image.path),
    output_dir: $("output-dir").value.trim() || state.sourceDir,
    output_name: $("output-name").value.trim() || state.autoOutputName,
    subtitle: $("subtitle-mode").value,
    merge_subtitles: $("merge-subtitles").checked,
    fps: Number($("fps").value),
    width: Number($("width").value),
    workers: Number($("workers").value),
    audio_codec: $("audio-codec").value,
    font_name: $("font-name").value,
    font_size: Number($("font-size").value),
    font_color: $("font-color").value,
    font_outline_color: $("font-outline-color").value,
    font_outline_width: Number($("font-outline-width").value),
    font_shadow_color: $("font-shadow-color").value,
    font_shadow: Number($("font-shadow").value),
    subtitle_margin: Number($("subtitle-margin").value),
  };
  // Only pin assignments when the user actually changed one; otherwise the
  // service applies its own default mapping.
  if (overrides.some((value) => value !== undefined && value !== null)) {
    payload.assignments = state.scan.audio.map(
      (audio, index) => overrides[index] ?? audio.automatic_image ?? 0,
    );
  }
  return payload;
}

async function pollJob() {
  // The interval callback is not awaited, so a slow poll could overlap the next
  // tick and both would replay the same event cursor.
  if (!state.jobId || state.polling) return;
  state.polling = true;
  try {
    const job = await parseResponse(await fetch(`/api/v1/jobs/${state.jobId}`));
    state.pollFailures = 0;
    if (!state.serviceOnline) {
      state.serviceOnline = true;
      setServiceStatus(true);
    }
    setProgress(job.progress);
    if (!state.cancelPending && job.state === "queued") setJobState("queued", "排队中");
    if (!state.cancelPending && job.state === "running") setJobState("running", job.current_task ? `处理中 · ${job.current_task}` : "处理中");
    if (job.state === "queued" || job.state === "running") {
      $("cancel-button").hidden = false;
      $("cancel-button").disabled = state.cancelPending;
    }
    if (job.state === "succeeded") {
      state.cancelPending = false;
      setJobState("succeeded", "已完成");
      $("job-summary").textContent = `文件已保存到 ${job.output_path}`;
      $("result-row").hidden = false;
      $("result-name").textContent = job.output_path;
      $("result-name").title = job.output_path;
      $("open-output").hidden = !state.desktop;
      $("download-button").href = job.download_url;
      $("merged-subtitle-download").hidden = !job.merged_subtitle_ready;
      if (job.merged_subtitle_ready) {
        $("merged-subtitle-download").href = job.merged_subtitle_download_url;
      }
      $("start-button").disabled = false;
      $("cancel-button").hidden = true;
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (job.state === "failed") {
      state.cancelPending = false;
      setJobState("failed", "失败");
      $("job-summary").textContent = job.error || "渲染任务失败。";
      $("start-button").disabled = false;
      $("cancel-button").hidden = true;
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (job.state === "cancelled") {
      state.cancelPending = false;
      setJobState("cancelled", "已取消");
      $("job-summary").textContent = "任务进程已停止，临时文件已清理，素材目录未被修改。";
      $("result-row").hidden = true;
      $("start-button").disabled = false;
      $("cancel-button").hidden = true;
      forgetJob();
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    const events = await parseResponse(await fetch(`/api/v1/jobs/${state.jobId}/events?after=${state.after}`));
    if (events.items.length) {
      appendEvents(events.items);
      state.after = events.next_after;
    }
  } catch (error) {
    showError(error.message);
    state.pollFailures += 1;
    // One failed poll is usually a transient hiccup on a local socket; keep the
    // interval alive so a still-running render does not vanish from the UI.
    if (state.pollFailures < MAX_POLL_FAILURES) return;
    state.serviceOnline = false;
    setServiceStatus(false);
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    state.jobId = null;
    forgetJob();
    setJobState("failed", "状态未知");
    $("job-summary").textContent = "与本地服务的连接已中断；后台渲染可能仍在进行，请检查服务窗口。";
    $("start-button").disabled = false;
    $("cancel-button").hidden = true;
  } finally {
    state.polling = false;
    updateWorkspaceControls();
  }
}

function rememberJob(jobId) {
  try {
    sessionStorage.setItem(JOB_STORAGE_KEY, jobId);
  } catch (_error) {
    /* private mode or storage disabled: recovery is simply unavailable */
  }
}

function forgetJob() {
  try {
    sessionStorage.removeItem(JOB_STORAGE_KEY);
  } catch (_error) {
    /* nothing to clean up */
  }
}

function storedJobId() {
  try {
    return sessionStorage.getItem(JOB_STORAGE_KEY);
  } catch (_error) {
    return null;
  }
}

function applyCapabilities(report) {
  const options = report.options || {};
  const defaults = report.defaults || {};
  fillSelect($("subtitle-mode"), report.subtitle_modes || [], (value) => SUBTITLE_LABELS[value] || value);
  fillSelect($("fps"), options.fps || [], (value) => `${value} fps`);
  fillSelect($("width"), options.width || [], (value) => `${value} px`);
  fillSelect($("workers"), options.workers || [], (value) => `${value} 个`);
  fillSelect($("font-size"), options.font_size || [], (value) => `${value} px`);
  fillSelect($("font-name"), options.font_name || [], (value) => FONT_LABELS[value] || value);
  fillSelect($("audio-codec"), options.audio_codec || ["aac"], (value) => value === "alac" ? "ALAC（无损，兼容性较低）" : "AAC（通用，有损）");
  fillSelect($("font-outline-width"), options.font_outline_width || [0, 1, 2], (value) => `${value} px`);
  fillSelect($("font-shadow"), options.font_shadow || [0, 1, 2], (value) => `${value} px`);
  fillSelect($("subtitle-margin"), options.subtitle_margin || [0, 48, 96], (value) => `${value} px`);
  selectValue($("subtitle-mode"), defaults.subtitle);
  $("merge-subtitles").checked = Boolean(defaults.merge_subtitles);
  selectValue($("fps"), defaults.fps);
  selectValue($("width"), defaults.width);
  selectValue($("workers"), defaults.workers);
  selectValue($("font-size"), defaults.font_size);
  selectValue($("font-name"), defaults.font_name);
  selectValue($("audio-codec"), defaults.audio_codec || "aac");
  selectValue($("font-outline-width"), defaults.font_outline_width ?? 2);
  selectValue($("font-shadow"), defaults.font_shadow ?? 1);
  selectValue($("subtitle-margin"), defaults.subtitle_margin ?? 48);
  if (/^#[0-9a-fA-F]{6}$/.test(String(defaults.font_color))) {
    $("font-color").value = defaults.font_color;
  }
  ["font-outline-color", "font-shadow-color"].forEach((id) => {
    const key = id.replaceAll("-", "_");
    if (/^#[0-9a-fA-F]{6}$/.test(String(defaults[key]))) $(id).value = defaults[key];
  });
  updateSubtitlePreview();
}

async function loadCapabilities() {
  try {
    applyCapabilities(await parseResponse(await fetch("/api/v1/capabilities")));
    return true;
  } catch (error) {
    showError(`无法读取服务参数：${error.message}`);
    $("start-button").disabled = true;
    return false;
  }
}

async function restoreJob() {
  const jobId = storedJobId();
  if (!jobId) return;
  state.recoveringJob = true;
  updateWorkspaceControls();
  state.jobId = jobId;
  state.after = 0;
  $("start-button").disabled = true;
  $("cancel-button").hidden = false;
  setJobState("queued", "恢复中");
  $("job-summary").textContent = "正在恢复上次的渲染任务…";
  // Ask the collection instead of probing the id directly: an id that no longer
  // exists would answer 404 and log a network error in the browser console.
  let listing = null;
  try {
    listing = await parseResponse(await fetch("/api/v1/jobs"));
  } catch (_error) {
    listing = null; // service unreachable: let the poll loop report it
  }
  if (listing && !(listing.items || []).some((job) => job.id === jobId)) {
    forgetJob();
    state.jobId = null;
    $("start-button").disabled = false;
    $("cancel-button").hidden = true;
    setJobState("idle", "等待提交");
    $("job-summary").textContent = "上次的任务已经不在了，请重新扫描素材目录。";
    state.recoveringJob = false;
    updateWorkspaceControls();
    return;
  }
  state.pollTimer = setInterval(pollJob, 500);
  state.recoveringJob = false;
  await pollJob();
}

async function startJob() {
  showError("");
  if (state.pollTimer || state.submitting || state.recoveringJob) {
    showError("已有任务正在进行，请等待完成或取消后再提交。");
    return;
  }
  if (!state.scan || !state.scan.ready) {
    showError("请先输入目录并完成扫描，确认音频文件已找到。");
    return;
  }
  state.pollFailures = 0;
  state.submitting = true;
  updateWorkspaceControls();
  saveWorkspace();
  $("start-button").disabled = true;
  $("cancel-button").hidden = true;
  state.cancelPending = false;
  $("result-row").hidden = true;
  $("event-list").innerHTML = '<tr class="event-empty"><td colspan="3">正在创建本地任务…</td></tr>';
  setProgress(0);
  setJobState("queued", "提交中");
  $("job-summary").textContent = "正在创建本地渲染任务。";
  try {
    const job = await parseResponse(await fetch("/api/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildJobPayload()),
    }));
    state.jobId = job.id;
    state.after = 0;
    rememberJob(job.id);
    setJobState("queued", "排队中");
    $("cancel-button").hidden = false;
    $("cancel-button").disabled = false;
    $("job-summary").textContent = `任务 ${job.id.slice(0, 8)} 已创建。`;
    state.pollTimer = setInterval(pollJob, 500);
    await pollJob();
  } catch (error) {
    showError(error.message);
    setJobState("failed", "无法提交");
    $("job-summary").textContent = "任务没有创建。";
    $("start-button").disabled = false;
    $("cancel-button").hidden = true;
  } finally {
    state.submitting = false;
    updateWorkspaceControls();
  }
}

async function cancelJob() {
  if (!state.jobId) return;
  state.cancelPending = true;
  $("cancel-button").disabled = true;
  setJobState("running", "正在停止…");
  $("job-summary").textContent = "正在结束后台媒体进程并清理临时文件。";
  try {
    await parseResponse(await fetch(`/api/v1/jobs/${state.jobId}`, { method: "DELETE" }));
    await pollJob();
  } catch (error) {
    state.cancelPending = false;
    $("cancel-button").disabled = false;
    await pollJob();
    showError(error.message);
  }
}

sourceInput.addEventListener("input", () => {
  state.scanSequence += 1;
  state.scanning = false;
  if (state.scan && sourceInput.value.trim() !== state.sourceDir) clearScan();
  updateWorkspaceControls();
  saveWorkspace();
});
sourceInput.addEventListener("change", saveWorkspace);
sourceInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") scanSource();
});
$("scan-button").addEventListener("click", scanSource);
$("start-button").addEventListener("click", startJob);
$("cancel-button").addEventListener("click", cancelJob);
$("output-name").addEventListener("input", () => {
  state.outputNameTouched = $("output-name").value.trim() !== state.autoOutputName;
  saveWorkspace();
});
$("choose-source").addEventListener("click", () => chooseDirectory("source"));
$("choose-output").addEventListener("click", () => chooseDirectory("output"));
$("add-images").addEventListener("click", addImages);
$("settings-save-retry").addEventListener("click", async () => {
  if (state.restoring) {
    try { await restoreWorkspace(); }
    catch (error) { setSaveStatus("load_failed", error); }
  } else {
    await saveWorkspace();
  }
});
$("recent-sources").addEventListener("change", () => {
  if (!$("recent-sources").value) return;
  sourceInput.value = $("recent-sources").value;
  scanSource();
});
$("output-dir").addEventListener("input", () => {
  state.outputFollowsSource = false;
  applyOutputMode();
  saveWorkspace();
});
$("output-follows-source").addEventListener("change", () => {
  state.outputFollowsSource = $("output-follows-source").checked;
  applyOutputMode();
  saveWorkspace();
});
$("open-output").addEventListener("click", async () => {
  if (!state.desktop || !state.jobId) return;
  try { await state.desktop.open_output(state.jobId); }
  catch (error) { showError(`无法打开输出位置：${error.message || error}`); }
});
$("preview-image-select").addEventListener("change", () => {
  setPreviewImage(Number($("preview-image-select").value));
});

function openPreview() {
  const modal = $("preview-modal");
  modal.hidden = false;
  document.body.classList.add("preview-modal-open");
  $("preview-modal-close").focus();
  updateSubtitlePreview();
}

function closePreview() {
  $("preview-modal").hidden = true;
  document.body.classList.remove("preview-modal-open");
  $("preview-expand-button").focus();
}

$("preview-expand-button").addEventListener("click", openPreview);
$("subtitle-preview-stage").addEventListener("click", openPreview);
$("preview-modal-close").addEventListener("click", closePreview);
$("preview-modal").addEventListener("click", (event) => {
  if (event.target.matches("[data-preview-close]")) closePreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("preview-modal").hidden) closePreview();
  if (event.key === "Escape" && !$("subtitle-settings-modal").hidden) closeSubtitleSettings();
});
[
  "font-name",
  "font-size",
  "font-color",
  "font-outline-color",
  "font-outline-width",
  "font-shadow-color",
  "font-shadow",
  "subtitle-margin",
  "subtitle-preview-text",
].forEach((id) => {
  $(id).addEventListener("input", () => {
    updateSubtitlePreview();
    saveSettings();
  });
  $(id).addEventListener("change", () => {
    updateSubtitlePreview();
    saveSettings();
  });
});
["subtitle-mode", "fps", "width", "workers", "audio-codec", "merge-subtitles"].forEach((id) => {
  $(id).addEventListener("change", () => {
    if (id === "width") updateSubtitlePreview();
    saveSettings();
  });
});
function openSubtitleSettings() {
  $("subtitle-settings-modal").hidden = false;
  document.body.classList.add("settings-modal-open");
  $("subtitle-settings-close").focus();
}

function closeSubtitleSettings() {
  $("subtitle-settings-modal").hidden = true;
  document.body.classList.remove("settings-modal-open");
  $("subtitle-settings-button").focus();
}

$("subtitle-settings-button").addEventListener("click", openSubtitleSettings);
$("subtitle-settings-done").addEventListener("click", closeSubtitleSettings);
$("subtitle-settings-close").addEventListener("click", closeSubtitleSettings);
$("subtitle-settings-modal").addEventListener("click", (event) => {
  if (event.target.matches("[data-subtitle-settings-close]")) closeSubtitleSettings();
});
updateSubtitlePreview();
new ResizeObserver(updateSubtitlePreview).observe($("subtitle-preview-stage"));
new ResizeObserver(updateSubtitlePreview).observe($("preview-modal-stage"));
clearScan();

async function bootstrap() {
  updateWorkspaceControls();
  const desktopReady = connectDesktop().catch((error) => showError(error.message || String(error)));
  await checkHealth();
  if (!await loadCapabilities()) return;
  try { await restoreWorkspace(); }
  catch (error) { setSaveStatus("load_failed", error); }
  await restoreJob();
  await desktopReady;
  updateWorkspaceControls();
}

bootstrap();
