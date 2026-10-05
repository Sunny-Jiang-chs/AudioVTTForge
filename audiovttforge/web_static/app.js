const state = {
  sourceDir: "",
  scan: null,
  assignments: {},
  jobId: null,
  after: 0,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const sourceInput = $("source-dir");

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
  preview.style.fontSize = `${fontSize}px`;
  preview.style.color = fontColor;
  $("subtitle-preview-meta").textContent = `${fontName} · ${fontSize} px · ${fontColor.toUpperCase()}`;
}

function showError(message) {
  $("form-error").textContent = message || "";
}

function setScanStatus(message, tone = "") {
  const status = $("scan-status");
  status.textContent = message;
  status.dataset.state = tone;
}

function scanItem(name, meta, badge, tone = "") {
  const item = document.createElement("div");
  item.className = `scan-item${tone ? ` ${tone}` : ""}`;
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

function automaticImageIndex(audioIndex, audioCount, imageCount) {
  if (!imageCount) return -1;
  return Math.min(imageCount - 1, Math.floor(audioIndex * imageCount / audioCount));
}

function renderAudioAssignments(scan) {
  const target = $("scan-audio-list");
  target.innerHTML = "";
  if (!scan.audio.length) {
    renderScanList("scan-audio-list", [], () => null);
    return;
  }
  scan.audio.forEach((audio, audioIndex) => {
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
    const automaticIndex = automaticImageIndex(audioIndex, scan.audio.length, scan.images.length);
    const automaticOption = document.createElement("option");
    automaticOption.value = "";
    automaticOption.textContent = automaticIndex >= 0 ? `自动分配 · ${scan.images[automaticIndex].name}` : "自动分配";
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
  renderScanList("scan-subtitle-list", scan.subtitles, (item) => scanItem(item.name, formatBytes(item.size), "VTT"));
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
    overall_progress: "总体进度",
    merge_started: "开始合并",
    merge_finished: "合并完成",
    job_finished: "任务完成",
    job_failed: "任务失败",
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
    setServiceStatus(true);
  } catch (_error) {
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
  const assignments = state.scan.audio.map((audio, audioIndex) => {
    const selected = state.assignments[audio.name];
    if (selected !== undefined && selected !== null) return selected;
    return automaticImageIndex(audioIndex, state.scan.audio.length, state.scan.images.length);
  });
  return {
    source_dir: state.sourceDir,
    assignments,
    output_name: $("output-name").value.trim() || "result.mp4",
    subtitle: $("subtitle-mode").value,
    fps: Number($("fps").value),
    width: Number($("width").value),
    workers: Number($("workers").value),
    font_name: $("font-name").value,
    font_size: Number($("font-size").value),
    font_color: $("font-color").value,
  };
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await parseResponse(await fetch(`/api/v1/jobs/${state.jobId}`));
    setProgress(job.progress);
    if (job.state === "queued") setJobState("queued", "排队中");
    if (job.state === "running") setJobState("running", job.current_task ? `处理中 · ${job.current_task}` : "处理中");
    if (job.state === "succeeded") {
      setJobState("succeeded", "已完成");
      $("job-summary").textContent = "文件已在本机生成，可以下载。";
      $("result-row").hidden = false;
      $("result-name").textContent = job.output_name;
      $("download-button").href = job.download_url;
      $("start-button").disabled = false;
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (job.state === "failed") {
      setJobState("failed", "失败");
      $("job-summary").textContent = job.error || "渲染任务失败。";
      $("start-button").disabled = false;
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
    setServiceStatus(false);
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    $("start-button").disabled = false;
  }
}

async function startJob() {
  showError("");
  if (!state.scan || !state.scan.ready) {
    showError("请先输入目录并完成扫描，确认音频和图片都已找到。");
    return;
  }
  $("start-button").disabled = true;
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
    setJobState("queued", "排队中");
    $("job-summary").textContent = `任务 ${job.id.slice(0, 8)} 已创建。`;
    state.pollTimer = setInterval(pollJob, 500);
    await pollJob();
  } catch (error) {
    showError(error.message);
    setJobState("failed", "无法提交");
    $("job-summary").textContent = "任务没有创建。";
    $("start-button").disabled = false;
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
clearScan();
checkHealth();
