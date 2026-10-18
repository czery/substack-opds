const $ = (id) => document.getElementById(id);

async function render() {
  const { server, token, status } = await chrome.storage.local.get(["server", "token", "status"]);
  $("server").value = server || "";
  $("token").value = token || "";
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
  const granted = await chrome.permissions.request({ origins: [url.origin + "/*"] });
  if (!granted) {
    $("status").textContent = "✗ Permission for the server was denied";
    return;
  }
  await chrome.storage.local.set({ server: url.origin, token: $("token").value.trim() });
  await chrome.runtime.sendMessage({ type: "sync" });
  render();
});

render();
