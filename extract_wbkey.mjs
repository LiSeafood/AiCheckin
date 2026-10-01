// extract_wbkey.mjs — 提取 WorkBuddy 编译期内嵌的 at-rest 解密密钥
//
// 背景: WorkBuddy 5.6.2 起本地登录态(access token 等)为 AES-256-GCM 密文,
// 解密密钥是客户端编译期内嵌的静态密钥(所有安装相同,非用户数据),磁盘上无明文。
// 本脚本通过 V8 Inspector 协议让客户端自己交出密钥,并写入 config.json 的
// "wb_at_rest_key" 字段(checkin.py 按需读取),同时打印到屏幕。
//
// 什么时候需要跑: 首次配置、或 checkin.py 日志提示"keyId 不一致"
// (WorkBuddy 更新更换了内置密钥)时,重新跑一次即可。
//
// 用法:  node extract_wbkey.mjs
// 前提:  Node.js >= 22(需要内置 WebSocket/fetch);WorkBuddy 已安装且登录过;
//        运行前请先完全退出 WorkBuddy(托盘图标右键退出)。
// 原理:  带 --inspect 启动 WorkBuddy(Electron fuses 允许 Node inspector),
//        在主进程里调 process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()
//        拿到编译期内嵌的密钥 payload。整个过程只读,不修改客户端任何数据。
import { spawn, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const PORT = process.env.WORKBUDDY_INSPECT_PORT || "9229";
const HERE = dirname(fileURLToPath(import.meta.url));
const CONFIG_FILE = join(HERE, "config.json");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 1. 定位 WorkBuddy.exe(注册表卸载项优先,经 PowerShell 枚举) ────
// 注意: WorkBuddy 的卸载项常没有 InstallLocation,需从 DisplayIcon /
// UninstallString 推导(如 "E:\...\WorkBuddy.exe,0")。
function findWorkBuddyExe() {
  if (process.env.WORKBUDDY_INSTALL_DIR) {
    return join(process.env.WORKBUDDY_INSTALL_DIR, "WorkBuddy.exe");
  }
  const ps = `
$ErrorActionPreference = 'SilentlyContinue'
foreach ($root in 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
                    'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
                    'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall') {
  Get-ChildItem $root | ForEach-Object {
    $p = Get-ItemProperty $_.PSPath
    if ($p.DisplayName -match 'WorkBuddy') {
      foreach ($v in @($p.DisplayIcon, $p.UninstallString, $p.InstallLocation)) {
        if ($v) { $v }
      }
    }
  }
}
`;
  const r = spawnSync("powershell", ["-NoProfile", "-Command", ps], { encoding: "utf8" });
  for (const line of (r.stdout || "").split(/\r?\n/).map(s => s.trim()).filter(Boolean)) {
    let p = line.replace(/^"+|"+$/g, "").replace(/,0$/, "").trim();
    if (!/\.exe$/i.test(p)) continue;
    if (/uninstall/i.test(p)) p = join(dirname(p), "WorkBuddy.exe");
    if (existsSync(p)) return p;
  }
  throw new Error("未找到 WorkBuddy 安装位置。请设置环境变量 WORKBUDDY_INSTALL_DIR 指向安装目录后重试。");
}

// ── 2. WorkBuddy 已在运行则拒绝(--inspect 只对首个实例生效) ────────
function assertNotRunning() {
  try {
    const out = execSync("tasklist /FO CSV /NH", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    if (/workbuddy/i.test(out)) {
      throw new Error("检测到 WorkBuddy 正在运行 —— 请先完全退出(托盘图标右键退出)再运行本脚本。");
    }
  } catch (e) {
    if (e.message?.includes("退出")) throw e;
    // tasklist 不可用时忽略检查
  }
}

// ── 3. 带 --inspect 启动,等 inspector 就绪 ────────────────────────
assertNotRunning();
const exe = findWorkBuddyExe();
console.error(`[extract] WorkBuddy: ${exe}`);
const child = spawn(exe, [`--inspect=${PORT}`], { stdio: "ignore" });

async function findTarget() {
  for (let i = 0; i < 120; i++) {
    if (child.exitCode !== null) throw new Error("WorkBuddy 进程提前退出(可能已在运行或启动失败)");
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const t = list.find((x) => x.webSocketDebuggerUrl);
      if (t) return t;
    } catch { /* inspector 未就绪,继续等 */ }
    await sleep(500);
  }
  throw new Error("inspector 未能就绪");
}

let payload;
try {
  const target = await findTarget();
  console.error(`[extract] 已连上主进程 inspector: ${target.type}`);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws 连接失败")); });

  let msgId = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    }
  };
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++msgId;
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const EXPR =
    "JSON.stringify(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet())";
  let result = null;
  for (let i = 0; i < 90; i++) {
    try {
      result = await send("Runtime.evaluate", { expression: EXPR, returnByValue: true });
      if (result?.result?.value) break;
    } catch { /* 上下文可能还没就绪 */ }
    await sleep(1000);
  }
  if (!result?.result?.value) throw new Error("evaluate 失败:未能从主进程取得密钥 payload");
  // loggerGet() 返回原始 JSON 文本,经 JSON.stringify 后可能双重编码,循环解到对象为止
  payload = result.result.value;
  for (let i = 0; i < 2 && typeof payload === "string"; i++) {
    try { payload = JSON.parse(payload); } catch { break; }
  }
  ws.close();
} finally {
  // 只结束我们自己拉起的进程,绝不动用户可能在用的实例
  try { child.kill(); } catch { /* 已退出 */ }
}

// ── 4. 写入 config.json + 打印 ────────────────────────────────────
if (!payload?.atRestSecretKey) throw new Error("payload 里没有 atRestSecretKey: " + JSON.stringify(payload).slice(0, 200));

let cfg = {};
try { cfg = JSON.parse(readFileSync(CONFIG_FILE, "utf8")); } catch { /* 不存在则新建 */ }
cfg.wb_at_rest_key = payload.atRestSecretKey;
writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2) + "\n");

console.log(`atRestSecretKey = ${payload.atRestSecretKey}`);
console.log(`已写入 ${CONFIG_FILE} (wb_at_rest_key 字段)`);
console.log("完成。现在可以运行 python checkin.py 了。");
