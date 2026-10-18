const $ = (id) => document.getElementById(id);

// config.json is only present in the personalized download from the dashboard.
async function loadConfig() {
  try {
    const r = await fetch(chrome.runtime.getURL("config.json"));
    return r.ok ? await r.json() : {};
  } catch {
    return {};
  }
}

async function render() {
  const { server, token, status } = await chrome.storage.local.get(["server", "token", "status"]);
  const cfg = await loadConfig();
  $("server").value = server || cfg.server || "";
  $("token").value = token || cfg.token || "";
  $("status").textContent = status
    ? `${status.ok ? "✓" : "✗"} ${status.msg} (${new Date(status.at).toLocaleString()})`
    : "";
}

$("save").addEventListener("click", async () => {
  let url;
  try {
    url = new URL($("server").value.trim());
  } catch {
    $("status").textContent = "✗ Invalid server URL";
    return;
  }
  if (url.protocol !== "https:") {
    $("status").textContent = "✗ Only https:// is allowed";
    return;
  }
  const granted = await chrome.permissions.request({
    // Firefox does not grant host_permissions at install time, so ask for substack.com as well.
    origins: [url.origin + "/*", "https://substack.com/*"],
  });
  if (!granted) {
    $("status").textContent = "✗ Permission for the server was denied";
    return;
  }
  await chrome.storage.local.set({ server: url.origin, token: $("token").value.trim() });
  await chrome.runtime.sendMessage({ type: "sync" });
  render();
});

render();
