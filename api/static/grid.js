const $ = (id) => document.getElementById(id);

function api(path, opts) {
  return fetch(path, opts).then(async (res) => {
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    return res.status === 204 ? null : res.json();
  });
}

// One HLS instance per live tile so every grid stream plays at once,
// and a separate instance for the fullscreen modal so the tile keeps
// playing while the selected stream is also shown large.
const tiles = new Map(); // cameraId -> { hls, card, statusDot }
let modalHls = null;

function createHls(video, url) {
  if (Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(url);
    hls.attachMedia(video);
    return hls;
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    return null;
  }
  return null;
}

function destroyHls(hls) {
  if (hls) {
    try { hls.destroy(); } catch (e) { /* noop */ }
  }
}

function ensureTile(c, grid) {
  let t = tiles.get(c.id);
  const online = c.status === "online";
  if (!t) {
    const card = document.createElement("div");
    card.className = "tile";

    const video = document.createElement("video");
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    card.appendChild(video);

    const bar = document.createElement("div");
    bar.className = "tile-bar";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = c.name;
    const status = document.createElement("span");
    status.className = "status " + (c.status || "unknown");
    status.textContent = "● " + (c.status || "unknown");
    bar.appendChild(name);
    bar.appendChild(status);
    card.appendChild(bar);

    grid.appendChild(card);
    t = { hls: null, card, video, status };
    tiles.set(c.id, t);

    if (online) t.hls = createHls(video, c.hls_url);
    card.addEventListener("click", () => online && openModal(c));
  } else {
    t.status.className = "status " + (c.status || "unknown");
    t.status.textContent = "● " + (c.status || "unknown");
    if (online && !t.hls) {
      t.hls = createHls(t.video, c.hls_url);
      t.card.addEventListener("click", () => openModal(c));
    }
  }
}

function render(cams) {
  const grid = $("grid");
  const ids = new Set(cams.map((c) => c.id));
  // drop tiles for cameras that are no longer present
  for (const [id, t] of tiles) {
    if (!ids.has(id)) {
      destroyHls(t.hls);
      t.video.pause();
      t.card.remove();
      tiles.delete(id);
    }
  }
  // add or refresh tiles (only online cameras have a playable stream)
  for (const c of cams) {
    ensureTile(c, grid);
  }
  $("grid-empty").style.display = cams.length === 0 ? "block" : "none";
}

function openModal(c) {
  const modal = $("modal");
  const player = $("modal-player");
  $("modal-label").textContent = `Previewing: ${c.name}`;
  // fresh HLS instance on the same URL → stream stays live in the grid tile.
  modalHls = createHls(player, c.hls_url);
  modal.classList.remove("hidden");
  player.play().catch(() => {});
}

function closeModal() {
  if (modalHls) {
    destroyHls(modalHls);
    modalHls = null;
  }
  $("modal-player").pause();
  $("modal").classList.add("hidden");
}

$("modal-close").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) closeModal();
});
$("modal-fs").addEventListener("click", async () => {
  const m = $("modal-content");
  if (!document.fullscreenElement) {
    await m.requestFullscreen();
  } else {
    await document.exitFullscreen();
  }
});

// clean up everything on navigation away from the page
window.addEventListener("beforeunload", () => {
  modalHls && destroyHls(modalHls);
  tiles.forEach((t) => destroyHls(t.hls));
});

async function refresh() {
  try {
    const cams = await api("/api/live/cameras");
    render(cams);
  } catch (e) {
    console.error("grid refresh:", e);
  }
}

refresh();
setInterval(refresh, 4000);
