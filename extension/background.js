// Sends substack.sid to the server whenever it changes, and additionally every 6 hours.
async function setStatus(s) {
  await chrome.storage.local.set({ status: { ...s, at: Date.now() } });
}

async function sync() {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  if (!server || !token) return setStatus({ ok: false, msg: "Not set up yet" });

  const c = await chrome.cookies.get({ url: "https://substack.com/", name: "substack.sid" });
  if (!c) return setStatus({ ok: false, msg: "No substack.sid found – are you logged in to Substack?" });

  try {
    const r = await fetch(server.replace(/\/$/, "") + "/api/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ cookie: c.value }),
    });
    await setStatus(r.ok ? { ok: true, msg: "Synced" } : { ok: false, msg: "Server responded with " + r.status });
  } catch (e) {
    await setStatus({ ok: false, msg: "Server unreachable" });
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
