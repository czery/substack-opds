const $ = (id) => document.getElementById(id);

async function render() {
  const { server, token, status } = await chrome.storage.local.get(["server", "token", "status"]);
  $("server").value = server || "";
  $("token").value = token || "";
  $("status").textContent = status
    ? `${status.ok ? "✓" : "✗"} ${status.msg} (${new Date(status.at).toLocaleString("de-DE")})`
    : "";
}

$("save").addEventListener("click", async () => {
  let url;
  try {
    url = new URL($("server").value.trim());
  } catch {
    $("status").textContent = "✗ Ungültige Server-URL";
    return;
  }
  if (url.protocol !== "https:") {
    $("status").textContent = "✗ Nur https:// ist erlaubt";
    return;
  }
  const granted = await chrome.permissions.request({ origins: [url.origin + "/*"] });
  if (!granted) {
    $("status").textContent = "✗ Berechtigung für den Server abgelehnt";
    return;
  }
  await chrome.storage.local.set({ server: url.origin, token: $("token").value.trim() });
  await chrome.runtime.sendMessage({ type: "sync" });
  render();
});

render();
