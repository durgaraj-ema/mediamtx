const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.status === 204 ? null : res.json();
}

function badge(text) {
  return `<span class="badge ${text}">${text}</span>`;
}

async function loadCameras() {
  const cams = await api("/api/live/cameras");
  const rows = $("camera-rows");
  rows.innerHTML = "";
  $("camera-empty").style.display = cams.length ? "none" : "block";
  for (const c of cams) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${c.name}</td>
      <td>${c.site}</td>
      <td>${badge(c.status)}</td>
      <td>${c.assigned_worker ?? "<span class='muted'>unassigned</span>"}</td>
      <td><span class="link" data-hls="${c.hls_url}" data-name="${c.name}">watch</span></td>
      <td><span class="link" data-del="${c.id}">remove</span></td>
    `;
    rows.appendChild(tr);
  }
  rows.querySelectorAll("[data-hls]").forEach((el) =>
    el.addEventListener("click", () => playStream(el.dataset.hls, el.dataset.name))
  );
  rows.querySelectorAll("[data-del]").forEach((el) =>
    el.addEventListener("click", async () => {
      await api(`/api/live/cameras/${el.dataset.del}`, { method: "DELETE" });
      refreshAll();
    })
  );
}

let hls;
function playStream(url, name) {
  const video = $("player");
  $("player-label").textContent = `Previewing: ${name}`;
  if (hls) { hls.destroy(); hls = null; }
  if (Hls.isSupported()) {
    hls = new Hls();
    hls.loadSource(url);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
  }
}

async function loadEvents() {
  const events = await api("/api/live/events?limit=50");
  const rows = $("event-rows");
  rows.innerHTML = "";
  for (const e of events) {
    const tr = document.createElement("tr");
    const objCount = (e.objects || []).length;
    tr.innerHTML = `
      <td>${new Date(e.start_ts).toLocaleTimeString()}</td>
      <td>${e.camera_id.slice(0, 8)}</td>
      <td>${e.event_type}</td>
      <td>${objCount} object(s)</td>
      <td>${badge(e.verification_status)}</td>
    `;
    rows.appendChild(tr);
  }
}

async function loadWorkers() {
  const workers = await api("/api/live/workers");
  const rows = $("worker-rows");
  rows.innerHTML = "";
  for (const w of workers) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${w.worker_id}</td><td>${w.camera_count}</td><td class="muted">${w.camera_ids.map(id => id.slice(0,8)).join(", ")}</td>`;
    rows.appendChild(tr);
  }
}

async function refreshAll() {
  await Promise.all([loadCameras(), loadEvents(), loadWorkers()]);
}

$("camera-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/live/cameras", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: $("f-name").value,
      site: $("f-site").value || "default",
      rtsp_url: $("f-url").value,
    }),
  });
  e.target.reset();
  $("f-site").value = "default";
  refreshAll();
});

$("use-demo").addEventListener("click", () => {
  $("f-name").value = "Demo test-pattern camera";
  $("f-url").value = "rtsp://mediamtx:8554/demo/cam-1";
});

refreshAll();
setInterval(refreshAll, 4000);
