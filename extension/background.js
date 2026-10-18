// Sendet substack.sid an den Server, sobald es sich ändert, und zusätzlich alle 6 Stunden.
async function setStatus(s) {
  await chrome.storage.local.set({ status: { ...s, at: Date.now() } });
}

async function sync() {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  if (!server || !token) return setStatus({ ok: false, msg: "Noch nicht eingerichtet" });

  const c = await chrome.cookies.get({ url: "https://substack.com/", name: "substack.sid" });
  if (!c) return setStatus({ ok: false, msg: "Kein substack.sid gefunden – bei Substack eingeloggt?" });

  try {
    const r = await fetch(server.replace(/\/$/, "") + "/api/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ cookie: c.value }),
    });
    await setStatus(r.ok ? { ok: true, msg: "Synchronisiert" } : { ok: false, msg: "Server antwortet " + r.status });
  } catch (e) {
    await setStatus({ ok: false, msg: "Server nicht erreichbar" });
  }
}

let timer;
function debounced() {
  clearTimeout(timer);
  timer = setTimeout(sync, 2000);
}

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (!removed && cookie.name === "substack.sid" && cookie.domain.replace(/^\./, "") === "substack.com") {
    debounced();
  }
});

function ensureAlarm() {
  chrome.alarms.create("resync", { periodInMinutes: 360 });
}
chrome.runtime.onInstalled.addListener(ensureAlarm);
chrome.runtime.onStartup.addListener(ensureAlarm);
chrome.alarms.onAlarm.addListener((a) => a.name === "resync" && sync());

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg.type === "sync") sync().then(() => reply({ done: true }));
  return true;
});
