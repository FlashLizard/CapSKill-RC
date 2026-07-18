// 验证 Repair 预设的便携加密路径。
// 测试强制使用 AES-GCM，并把所有运行时文件放在已忽略的 tmp/ 目录中。
import { spawn } from "node:child_process";
import { rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const port = String(Number(process.env.SKILLSBENCH_PRESET_SMOKE_PORT || 5202));
const presetPath = "tmp/preset-smoke/store.json";
const keyPath = "tmp/preset-smoke/key";
const tempDir = fileURLToPath(new URL("../tmp/preset-smoke/", import.meta.url));
const child = spawn(process.execPath, ["runner-app/server.mjs"], {
  cwd: root,
  env: {
    ...process.env,
    SKILLSBENCH_RUNNER_PORT: port,
    SKILLSBENCH_PRESET_ENCRYPTION: "portable",
    SKILLSBENCH_REPAIR_PRESET_PATH: presetPath,
    SKILLSBENCH_REPAIR_PRESET_KEY_PATH: keyPath,
  },
  stdio: ["ignore", "pipe", "pipe"],
});

let stderr = "";
child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

async function request(url, options = {}) {
  const response = await fetch(`http://127.0.0.1:${port}${url}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}: ${JSON.stringify(body)}`);
  return body;
}

try {
  let ready = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      await request("/api/health");
      ready = true;
      break;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  if (!ready) throw new Error(`web server did not become ready${stderr ? `: ${stderr}` : ""}`);

  const saved = await request("/api/repair-stage/presets", {
    method: "POST",
    body: JSON.stringify({
      name: "portable-smoke",
      settings: {
        taskKey: "test-task",
        strongApiKey: "round-trip-secret",
        reviewApiKey: "review-round-trip-secret",
      },
    }),
  });
  const loaded = await request(`/api/repair-stage/presets?id=${encodeURIComponent(saved.preset.id)}`);
  if (loaded.preset.settings.strongApiKey !== "round-trip-secret") {
    throw new Error("strong API key did not round-trip");
  }
  if (loaded.preset.settings.reviewApiKey !== "review-round-trip-secret") {
    throw new Error("review API key did not round-trip");
  }
  await request(`/api/repair-stage/presets?id=${encodeURIComponent(saved.preset.id)}`, { method: "DELETE" });
  console.log(JSON.stringify({ ok: true, encryption: "portable-aes-256-gcm", presetDeleted: true }, null, 2));
} finally {
  child.kill();
  await rm(tempDir, { recursive: true, force: true });
}
