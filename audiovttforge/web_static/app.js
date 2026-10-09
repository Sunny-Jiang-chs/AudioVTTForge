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
};

const MAX_POLL_FAILURES = 10;

const $ = (id) => document.getElementById(id);
const sourceInput = $("source-dir");

// Survives a page reload so a running render can be picked up again (the job
// registry itself only lives in the service process).
const JOB_STORAGE_KEY = "audiovttforge.jobId";

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

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setServiceStatus(online) {
  const status = $("service-status");
  status.dataset.state = online ? "online" : "offline";
  $("service-status-text").textContent = online ? "本地服务在线" : "服务未连接";
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

function updateSubtitlePreview() {
  const fontName = $("font-name").value;
  const fontSize = Number($("font-size").value) || 42;
  const fontColor = $("font-color").value;
  const text = $("subtitle-preview-text").value || "这是字幕样式预览";
  const preview = $("subtitle-preview");
  preview.textContent = text;
  preview.style.fontFamily = `"${fontName}", sans-serif`;
  const stage = $("subtitle-preview-stage");
  const outputScale = stage.getBoundingClientRect().height / 1080;
  preview.style.fontSize = `${Math.max(6, fontSize * outputScale)}px`;
  preview.style.color = fontColor;
  preview.style.webkitTextStroke = `${Math.max(0.4, 2 * outputScale)}px #000`;
  preview.style.textShadow = `0 ${Math.max(0.3, outputScale)}px ${Math.max(0.6, 2 * outputScale)}px #000`;
  $("subtitle-preview-meta").textContent = `${fontName} · ${fontSize} px · ${fontColor.toUpperCase()}`;
}

function setPreviewImage(index) {
  const image = state.scan?.images[index];
  const previewImage = $("subtitle-preview-image");
  const placeholder = $("subtitle-preview-placeholder");
  if (!image) {
    previewImage.removeAttribute("src");
    previewImage.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "扫描素材目录后可预览导入图片";
    return;
  }

  state.previewImageIndex = index;
  const query = new URLSearchParams({ directory: state.sourceDir, name: image.name });
  placeholder.hidden = false;
  placeholder.textContent = "正在加载图片…";
  previewImage.hidden = true;
  previewImage.onload = () => {
    previewImage.hidden = false;
    placeholder.hidden = true;
  };
  previewImage.onerror = () => {
    previewImage.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "图片预览不可用";
  };
  previewImage.src = `/api/v1/sources/image?${query}`;
}

function renderPreviewImages(scan) {
  const select = $("preview-image-select");
  select.innerHTML = "";
  scan.images.forEach((image, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = image.name;
    select.appendChild(option);
  });
  select.disabled = !scan.images.length;
  if (scan.images.length) {
    select.value = "0";
    setPreviewImage(0);
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
    automaticOption.textContent = audio.automatic_image_name
      ? `自动分配 · ${audio.automatic_image_name}`
      : "自动分配";
    select.appendChild(automaticOption);
    scan.images.forEach((image, imageIndex) => {
      const option = document.createElement("option");
      option.value = String(imageIndex);
      option.textContent = `${imageIndex + 1}. ${image.name}`;
      select.appendChild(option);
    });
    const selected = state.assignments[audio.name];
    if (selected !== undefined && selected !== null) select.value = String(selected);
    select.addEventListener("change", () => {
      state.assignments[audio.name] = select.value === "" ? null : Number(select.value);
    });
    row.append(info, select);
    target.appendChild(row);
  });
}

function renderScan(scan) {
  state.scan = scan;
  state.sourceDir = scan.source_dir;
  state.assignments = {};
  state.autoOutputName = scan.suggested_output_name || `${scan.source_name || "result"}.mp4`;
  if (!state.outputNameTouched) $("output-name").value = state.autoOutputName;
  $("output-dir").value = scan.source_dir;
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
  renderScanList("scan-image-list", scan.images, (item) => scanItem(item.name, formatBytes(item.size), "IMG"));
  renderScanList("scan-subtitle-list", scan.subtitles, (item) => scanItem(item.name, formatBytes(item.size), item.name.toLowerCase().endsWith(".lrc") ? "LRC" : "VTT"));
  renderPreviewImages(scan);
  $("scan-audio-count").textContent = `${scan.counts.audio} 个`;
  $("scan-image-count").textContent = `${scan.counts.images} 个`;
  $("scan-subtitle-count").textContent = `${scan.counts.subtitles} 个`;

  const warningBox = $("scan-warning");
  const warningList = $("scan-warning-list");
  warningList.innerHTML = "";
  scan.warnings.forEach((warning) => {
    const item = document.createElement("li");
    item.textContent = warning;
    warningList.appendChild(item);
  });
  warningBox.hidden = !scan.warnings.length;
  if (scan.ready) {
    setScanStatus(`已扫描 ${scan.source_name}，可以开始渲染。`, "ready");
    $("start-button").disabled = false;
    $("job-summary").textContent = `${scan.counts.audio} 个音频 · ${scan.counts.images} 张图片已就绪。`;
  } else {
    setScanStatus("目录缺少音频或图片，暂时不能开始渲染。", "warning");
    $("start-button").disabled = true;
  }
}

function clearScan() {
  state.scan = null;
  state.sourceDir = "";
  state.assignments = {};
  state.previewImageIndex = 0;
  $("output-dir").value = "";
  const previewSelect = $("preview-image-select");
  previewSelect.innerHTML = '<option value="">扫描目录后选择图片</option>';
  previewSelect.disabled = true;
  setPreviewImage(-1);
  $("scan-preview").hidden = true;
  $("asset-count").textContent = "0 个文件";
  $("start-button").disabled = true;
  setScanStatus("输入目录路径后扫描，结果会显示在这里。");
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

async function scanSource() {
  showError("");
  const path = sourceInput.value.trim();
  if (!path) {
    clearScan();
    showError("请输入素材目录路径。");
    sourceInput.focus();
    return;
  }
  $("scan-button").disabled = true;
  setScanStatus("正在读取目录…");
  try {
    const scan = await parseResponse(await fetch("/api/v1/sources/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }));
    renderScan(scan);
  } catch (error) {
    clearScan();
    setScanStatus("目录扫描失败。", "warning");
    showError(error.message);
  } finally {
    $("scan-button").disabled = false;
  }
}

function buildJobPayload() {
  const overrides = state.scan.audio.map((audio) => state.assignments[audio.name]);
  const payload = {
    source_dir: state.sourceDir,
    output_dir: $("output-dir").value.trim() || state.sourceDir,
    output_name: $("output-name").value.trim() || state.autoOutputName,
    subtitle: $("subtitle-mode").value,
    fps: Number($("fps").value),
    width: Number($("width").value),
    workers: Number($("workers").value),
    font_name: $("font-name").value,
    font_size: Number($("font-size").value),
    font_color: $("font-color").value,
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
      $("download-button").href = job.download_url;
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
  selectValue($("subtitle-mode"), defaults.subtitle);
  selectValue($("fps"), defaults.fps);
  selectValue($("width"), defaults.width);
  selectValue($("workers"), defaults.workers);
  selectValue($("font-size"), defaults.font_size);
  selectValue($("font-name"), defaults.font_name);
  if (/^#[0-9a-fA-F]{6}$/.test(String(defaults.font_color))) {
    $("font-color").value = defaults.font_color;
  }
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
    return;
  }
  state.pollTimer = setInterval(pollJob, 500);
  await pollJob();
}

async function startJob() {
  showError("");
  if (state.pollTimer) {
    showError("已有任务正在进行，请等待完成或取消后再提交。");
    return;
  }
  if (!state.scan || !state.scan.ready) {
    showError("请先输入目录并完成扫描，确认音频和图片都已找到。");
    return;
  }
  state.pollFailures = 0;
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
  if (state.scan && sourceInput.value.trim() !== state.sourceDir) clearScan();
});
sourceInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") scanSource();
});
$("scan-button").addEventListener("click", scanSource);
$("start-button").addEventListener("click", startJob);
$("cancel-button").addEventListener("click", cancelJob);
$("output-name").addEventListener("input", () => {
  state.outputNameTouched = $("output-name").value.trim() !== state.autoOutputName;
});
$("preview-image-select").addEventListener("change", () => {
  setPreviewImage(Number($("preview-image-select").value));
});
[
  "font-name",
  "font-size",
  "font-color",
  "subtitle-preview-text",
].forEach((id) => {
  $(id).addEventListener("input", updateSubtitlePreview);
  $(id).addEventListener("change", updateSubtitlePreview);
});
updateSubtitlePreview();
new ResizeObserver(updateSubtitlePreview).observe($("subtitle-preview-stage"));
clearScan();

async function bootstrap() {
  await checkHealth();
  await loadCapabilities();
  await restoreJob();
}

bootstrap();
