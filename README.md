# ai-ide-checkin

**WorkBuddy / Trae CN / Qoder 每日自动签到** —— 单文件、零第三方依赖、多账号、自动续期。

> 非官方工具。所有接口均逆向自各客户端本体,仅供学习交流与个人效率使用,
> 请自行评估账号风控风险(见文末免责声明)。

## 每日收益

| 平台 | 接口 | 每日收益 |
|---|---|---|
| WorkBuddy(腾讯 CodeBuddy) | `POST /v2/billing/meter/daily-checkin` | 100 积分 + 连续签到 |
| Trae CN | `POST /trae/api/v2/ug/checkin_credits/claim` | 150 积分 * |
| Qoder(国内/国际) | `POST /sash/api/v1/me/campaigns/{cid}/claim` | 100 Credits |

> \* Trae 的接口回报随账号状态浮动(实测 `credits` 字段 100~150,另有
> `extra_credits` 加成字段),以客户端展示为准。

## 特性

- **零依赖**:纯 Python 标准库(含内置 AES-256-GCM / AES-128-CBC 实现,已与 OpenSSL 交叉验证)
- **多账号**:客户端当前登录的账号会被自动"记住"(截留进本地账号库),登录一个跑一次即可积累多个账号一起签
- **自动续期**:Trae / Qoder 的 refreshToken 轮换全自动处理并写回客户端登录态,不依赖打开客户端
- **幂等安全**:已签到判定为成功不重复领;限流自动重试;写回全部原子替换 + 备份保护
- **双端可用**:本地 Windows 计划任务(推荐)或 GitHub Actions(仅 WorkBuddy)

## 支持矩阵

| 平台 | 本地签到 | 本地自动续期 | GitHub Actions |
|---|---|---|---|
| WorkBuddy | ✅ Windows(需先提取密钥,见下) | —(由客户端保活) | ✅ 长期可用 |
| Trae CN | ✅ Windows | ✅ 全自动 | ⚠️ 仅一个 token 周期 |
| Qoder | ✅ Windows | ✅ 全自动 | ⚠️ 仅一个 token 周期 |

> Trae / Qoder 的续期会轮换 refreshToken,而 Actions 无法把新值持久化回
> Secrets,所以云端的 token 用完一个周期(约 14/30 天)就失效。**长期免维护
> 请用本地计划任务**;Actions 适合当作短期兜底。

## 快速开始(3 步)

前提:Windows + Python 3.9+,且本机装有对应客户端并已登录。

```bash
git clone https://github.com/LiSeafood/ai-ide-checkin.git
cd ai-ide-checkin

# 1. (仅 WorkBuddy 需要)提取本地凭据解密密钥 —— 需要 Node.js >= 22
node extract_wbkey.mjs        # 自动定位客户端、提取密钥、写入 config.json

# 2. 先手动跑一次验证
python checkin.py

# 3. 注册 Windows 计划任务(免管理员;默认每天 10:00)
install_task.bat              # 交互式询问运行时间
install_task.bat 09:30        # 或直接指定时间
```

运行后脚本会自动扫描本机客户端登录态并逐账号签到:

- WorkBuddy:`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
- Trae:`%APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json`(及 `Trae CN`)
- Qoder:`%APPDATA%\com.qodercn.app.*\auth.v1.dat`(及国际版 `com.qoder.app.*`)

输出写入 `logs/checkin.log`,任务后台静默执行。

## WorkBuddy 密钥提取

WorkBuddy 5.6.2 起对本地登录态加密(AES-256-GCM),解密密钥是客户端编译期内嵌
的静态密钥(所有安装相同,非用户数据),磁盘上没有明文。`extract_wbkey.mjs`
的做法是带 `--inspect` 启动客户端,通过 V8 调试协议在主进程里调用
`workbuddyStorage.loggerGet()`,让客户端自己交出密钥,然后写入 `config.json`。

- 整个过程**只读**,不修改客户端任何数据
- 需要先完全退出 WorkBuddy(托盘图标右键退出)
- WorkBuddy 更新后若日志提示 keyId 不一致,重跑一次本脚本即可

## 多账号（傻瓜式教程）

脚本用 `accounts.local.json` 充当"账号库"：**客户端当前登录的账号，只要跑一次
脚本就会被自动记住**；之后无论客户端换登成谁，已记住的账号照常每天签。

**添加多个账号的步骤**（每个客户端一次只能登一个，登一个、跑一次即可）：

```
第 1 步  在客户端里登录 账号 A
第 2 步  运行 python checkin.py        → 日志出现"账号 A 已入库"
第 3 步  在客户端里退出登录,改登 账号 B
第 4 步  再运行 python checkin.py      → 账号 B 也已入库
完成。之后每天运行会自动同时签 A 和 B,两个账号都无需再管
```

**已记住账号的日常维护**：

- **WorkBuddy**：被记住的账号 token 约 55 天有效。快过期时日志会提前 7 天
  告警，届时**在客户端里登录一次该账号**（客户端会自动刷新 token，脚本自动
  更新留档），不需要其它任何操作。
- **Trae / Qoder**：已记住的账号由脚本自动续期（Token 过期前自动换新并
  写回账号库），与客户端当前登录的是谁无关，**全程无需打开客户端**。

> 想修改/删除账号库里的账号，直接编辑 `accounts.local.json`（每平台一个
> 分区，按账号 ID 为键）；或删掉整个文件重新走一遍上面的步骤。

## GitHub Actions 部署(仅 WorkBuddy)

1. Fork/新建 **Private** 仓库并推送本项目
2. 本地运行 `python checkin.py --export`,把打印的
   `WORKBUDDY_ACCESS_TOKEN` / `WORKBUDDY_UID` 填入仓库 Secrets
3. 手动触发一次 workflow 验证

仓库自带的 workflow 每天北京时间 09:05 运行。Trae/Qoder 也可以把 token 放进
Secrets,但如上所述无法持久化续期,不建议。

## 命令行选项

```bash
python checkin.py            # 全部平台(默认)
python checkin.py --no-trae  # 跳过 Trae
python checkin.py --no-qoder # 跳过 Qoder
python checkin.py --export   # 导出 token(用于更新 GitHub Secrets)
python checkin.py --help
```

## 常见问题

**Q: 提示"缺少 WorkBuddy at-rest 解密密钥"?**
A: 运行 `node extract_wbkey.mjs`。需要 Node.js ≥ 22,且先完全退出 WorkBuddy。

**Q: WorkBuddy 更新后签到失败,日志提示 keyId 不一致?**
A: 客户端更换了内置密钥,重跑 `node extract_wbkey.mjs` 更新 config.json。

**Q: Trae 提示 refreshToken 已失效?**
A: 打开一次 Trae 客户端重新登录该账号,脚本会自动截留新登录态。

**Q: macOS / Linux 能用吗?**
A: WorkBuddy 的 macOS 路径已内置但未实测;Qoder 凭据提取依赖 Windows DPAPI,
仅限 Windows。签到本体是纯 Python,欢迎 PR 补充。

**Q: 会不会把我的 token 传到别的地方?**
A: 不会。脚本只与本平台官方 API 通信,不打印完整 token(日志只显示前 12 位),
`accounts.local.json` / `config.json` / `logs/` 均已在 `.gitignore` 中。

## 技术细节

### WorkBuddy 本地凭据解密

登录态里的敏感字段是 `{"$wbEncrypted":1,"envelope":"<base64>"}`,envelope 为
AES-256-GCM(套件 1)封套,密钥派生:`key = SHA256(密钥 base64 字符串的 UTF-8 字节)`,
`keyId = SHA256(key).hex()[:16]`。AAD 按
`"WB-AAD\0" || 0x01 || LP("WBEV1") || LP("sym-v1") || u32be(suite) || LP(keyId) || 0x02 || 0x00 || 0x00`
构造。内置纯 Python AES-256-GCM 实现,与 OpenSSL 交叉验证一致。

### Trae 登录态与续期

`storage.json` 的 `iCubeAuthInfo://icube.cloudide` 是自研 `tc` 容器:
header(6B) + 随机 key(32B) + AES-128-CBC(SHA512(payload) ‖ payload + PKCS7),
key 派生自内置常量表 XOR。续期走
`POST api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken`
(ClientID + RefreshToken + `ClientSecret:"-"`,**无需设备指纹签名**),
轮换后的新登录态重新加密写回。签到请求头为完整的客户端伪装 +
按 user_id 确定性派生的稳定伪设备身份(算法经 trae-mate 测试向量验证)。

### Qoder 凭据与续期

`auth.v1.dat` 为 Chromium os_crypt 格式(`v10` + nonce + AES-256-GCM),密钥在
`Local State` 的 `os_crypt.encrypted_key`(DPAPI)。**DPAPI 解 key 必须经
PowerShell(.NET ProtectedData)子进程** —— 在部分装有安全行为防护的机器上,
Python 直接调用 Crypt*Data 会被终止进程(实测)。续期走
`POST /api/v1/deviceToken/refresh`(无需设备签名),请求头需带 `Cosy-*`
设备标识(客户端自带 `runtime-info.exe` 生成)。

## 免责声明

- 本项目与腾讯、字节跳动、阿里巴巴没有任何关联,仅为个人学习研究用途。
- 所有接口均为逆向获取的**非官方**接口,随时可能因客户端更新而失效。
- 使用本项目产生的任何账号风控、积分清零、封禁等后果由使用者自行承担。
- 请合理控制运行频率(默认每天一次),不要滥用。
