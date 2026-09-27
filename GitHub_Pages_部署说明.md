# QDII 场外基金监控 · GitHub Pages 部署说明（新手版）

**这份文档解决什么问题**：把监控页面变成一个固定网址，iPhone 上点开就能看，并且每天由云端自动更新 —— 不用开着电脑、不用装任何软件。

全程网页操作，约 15 分钟。做完后你会得到：一个像 App 一样的手机入口（添加到主屏幕），每天北京时间 **08:30** 自动刷新数据。

| 步骤 | 做什么 | 大约耗时 | 做完后得到 |
| --- | --- | --- | --- |
| 一 | 注册 GitHub 账号 | 3 分钟 | 一个用户名 |
| 二 | 新建 **Public** 仓库 | 1 分钟 | 仓库 `qdii-monitor` |
| 三 | 把项目文件传上去 | 5 分钟 | 脚本与数据在云端 |
| 四 | 加上每日 workflow | 2 分钟 | 每天 08:30 自动跑 |
| 五 | 开启 GitHub Pages | 1 分钟 | 一个可访问的网址 |
| 六 | 配置 5 个 Secrets | 3 分钟 | 有变动时邮件能发出 |
| 七 | 手动跑一次 + iPhone 添加到主屏幕 | 3 分钟 | 桌面图标，点开即看 |

## 开始前必须知道的三件事

1. **免费版 GitHub Pages 只能用于 Public（公开）仓库**。一旦公开，仓库里的基金名单、限额快照、净值数据任何人打开仓库或网址都能看到。若不能接受，先看第九节 FAQ 第 6 条再决定。
2. **`data/mail_config.json` 绝对不能上传**（里面是邮箱 SMTP 授权码明文）。项目自带的 `.gitignore` 已排除它：用 git 命令上传不会带上；用网页上传时请手动跳过该文件。
3. **网址格式**：`https://你的用户名.github.io/仓库名/`，例如 `https://cooke-qdii.github.io/qdii-monitor/`。用户名会出现在网址里，注册后虽然能改但旧网址会失效，建议一次想好（全小写字母数字，如 `cooke-qdii`）。

---

## 一、注册 GitHub 账号

> 已有账号直接跳到第二节。

1. 浏览器打开 https://github.com/signup
2. 依次填写：邮箱（建议用常用的）→ 密码（至少 8 位，含数字与小写字母）→ 用户名（Username，见上文第 3 条要求）
3. 完成人机验证（拼图），再填入邮箱收到的 8 位验证码
4. 后面关于团队人数、用途、是否学生的选项随便选，一路 **Continue**，套餐选 **Free** 即可
5. 到邮箱点验证链接，账号激活

---

## 二、新建仓库

1. 登录后点右上角 **+** → **New repository**
2. **Repository name** 填 `qdii-monitor`（后续步骤都按这个名字写，你也可以换成别的）
3. **Description** 可留空
4. **可见性务必选 Public** —— 免费版 Pages 只支持公开仓库（选 Private 的话 Pages 需要付费的 GitHub Pro 才能用）
5. 下方 **Add a README file**、**.gitignore**、**license** 全部**不要勾**（勾了会和后面上传的文件冲突）
6. 点 **Create repository**

---

## 三、把项目文件传到仓库

先打开本地项目文件夹 `D:\QDII监控`，看清里面有什么，尤其是 `data\mail_config.json`（记住它，别传）。

### 方式 A：网页上传（不用装软件，推荐新手）

1. 仓库页点 **uploading an existing file**（或 **Add file → Upload files**）
2. 按下面的清单，把要传的拖进网页：

| 要传 | 不要传（原因） |
| --- | --- |
| `scan.py`、`notify.py`、`requirements.txt` | `data\mail_config.json` —— 含邮箱授权码明文 |
| `run_daily.bat`、`install_windows.bat`、`run_daily.sh`、`install_mac_linux.sh` | `.cache\` 文件夹 —— 抓数缓存，体积大且无用 |
| `data\` 里的全部 json（`snapshot.json`、`change_history.json`、`history.json`、`nav_series.json` 等），但**不含** `mail_config.json` | `__pycache__\`、`run_log.txt` —— 本机运行缓存与日志 |
| `.gitignore`、`每日自动运行说明.md`、本部署说明 | `index.html`、`.nojekyll` —— 云端每次运行会自动生成 |
| `.github\workflows\daily.yml`（每日抓数 + 自动部署的 workflow，**必须传**） | 旧目录 `cloud-github-actions\` —— 已废弃（其 `daily.yml` 已移至 `.github\workflows\`） |
| `qdii_nasdaq_sp500_monitor.html`（可传可不传，传了便于在仓库里直接看） | 无 |

3. 页面下方点 **Commit changes**，再点一次绿色 **Commit changes**

> `data` 里的 json **必须一起上传**：`snapshot.json` 是上次快照，缺了它首次运行会把所有基金都当成"新变动"而发出一大堆记录；`change_history.json` 是页面底部变动历史表的数据来源。

### 方式 B：git 命令上传（一次到位）

项目自带 `.gitignore`（已排除 `mail_config.json`、`.cache/` 等），直接在本地项目目录执行：

```
cd /d "D:\QDII监控"
git init
git add .
git commit -m "init: QDII monitor"
git branch -M main
git remote add origin https://github.com/你的用户名/qdii-monitor.git
git push -u origin main
```

- 弹出登录窗口时选浏览器授权；若要求输入密码，要输 **Personal Access Token**（不是登录密码）
- 推送完成后刷新仓库页，应能看到脚本、`data` 目录，且**看不到** `mail_config.json`

> **误传了 `mail_config.json` 怎么办**：仓库页点开该文件 → 右上垃圾桶 → Commit changes；然后**立刻去邮箱后台重置/更换 SMTP 授权码**（公开仓库里的授权码一律视为已泄露）。

---

## 四、加上"每日抓数 + 自动部署"的 workflow

1. 仓库页 **Add file → Create new file**
2. 文件名框输入 `.github/workflows/daily.yml`（输入 `/` 会自动建目录，位置必须是 `.github/workflows/`）
3. 用记事本打开本地项目的 `.github\workflows\daily.yml`，全选复制，粘贴进网页编辑框
4. 右上 **Commit changes**

> 本项目的 workflow 文件已经放在 `.github\workflows\daily.yml`（原 `cloud-github-actions\` 目录已废弃并删除）：第三节用**方式 A（网页上传）**时把 `.github` 文件夹一起拖上去、用**方式 B（git 命令）**时随 `git push` 自动到位，两种情况本节 1~4 步都可跳过；只有上传时漏掉了 `.github` 文件夹，才需要按本节手动新建。

这个 workflow 每天会做四件事：

1. 抓取基金申购状态与限额（`scan.py`）
2. **只有发现变动时**才发邮件（`notify.py`）
3. 把最新数据与页面提交回仓库
4. 重新生成监控页面，并自动部署到 GitHub Pages（新增）

5. 最后给机器人写权限：**Settings → Actions → General → Workflow permissions → 选 Read and write permissions → Save**

---

## 五、开启 GitHub Pages

1. 仓库页点 **Settings**
2. 左侧栏找到 **Pages**（在 Code and automation 分组下）
3. **Build and deployment → Source** 下拉选 **GitHub Actions**（**不要**选 Deploy from a branch）
4. 选完立即生效，没有额外的保存按钮

> 若下拉里只有 "Deploy from a branch" 而没有 "GitHub Actions"，说明仓库还是 Private：到 **Settings → General → 最下方 Danger Zone → Change visibility** 改成 Public。

---

## 六、配置 5 个 SMTP Secrets（云端发邮件用）

1. **Settings → Secrets and variables → Actions → New repository secret**
2. 依次新增 5 条，**Name 必须一字不差**，Value 从本地 `data/mail_config.json` 里对应字段取：

| Name | Value 取哪个字段 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | `smtp_host` | 如 `smtp.qq.com` |
| `SMTP_PORT` | `smtp_port` | 如 `465` |
| `SMTP_SENDER` | `sender` | 你的发信邮箱 |
| `SMTP_PASSWORD` | `password` | 邮箱 **SMTP 授权码**（不是邮箱登录密码） |
| `SMTP_RECEIVERS` | `receivers` | 收件邮箱，多个用英文逗号隔开 |

3. 全部加完后列表显示 5 条，Value 不再显示（正常）；写错了只能删掉重建，无法查看原值

> 作用：Secrets 是 GitHub 的加密变量，只在运行时临时注入给脚本，仓库代码与文件里都不会出现授权码。`daily.yml` 已经把 5 个 Secret 接到了"发邮件"那一步的 `env` 上。

---

## 七、手动跑一次，验证整条链路

1. 仓库页点 **Actions** 标签 → 左侧选 **QDII Daily Scan & Deploy Pages** → 右侧 **Run workflow** → 绿色按钮
2. 等 2~5 分钟并刷新页面，这次运行下会有**两个作业**（job）：
   - `scan`：抓数据 / 发邮件 / 提交数据回仓库 / 打包页面
   - `deploy`：把页面部署到 GitHub Pages
   两个都显示绿色对勾即为成功
3. 点开 `deploy` 作业，里面有部署网址 `https://你的用户名.github.io/qdii-monitor/`；点开即是监控页面（首次部署后可能需等 1~2 分钟才能打开）
4. 邮件：本次会与仓库里的快照比对，有变化就发一封；日志显示 `无变动，跳过发送` 也属正常
5. 之后每天北京时间 **08:30** 自动执行一次，页面自动更新，无需人工干预

---

## 八、在 iPhone 上添加到主屏幕（日常怎么用）

1. 用 **Safari** 打开 `https://你的用户名.github.io/qdii-monitor/`（不要用微信 / QQ 内置浏览器，否则无法添加主屏幕）
2. 点底部 **分享** 按钮（方框加向上箭头）
3. 选 **添加到主屏幕** → 名称可改成"QDII监控" → **添加**
4. 桌面出现图标，点开是全屏显示（页面已按 iPhone 做移动端适配，无浏览器地址栏遮挡）
5. 每天 08:30 之后下拉刷新，看到的就是当天最新数据

**iPhone 端操作说明（本次专为手机新增的适配）**

- **三张走势图**（指数走势图、对比业绩走势图、历史定投推演图）：**手指点按或按住左右拖动**，图上方会浮出该天的数值框，**松手自动复位**。以前只有鼠标悬停才能看数值，现在触摸屏同样可用；推演动画播放期间不响应点按，动画结束后即可拖动查看。
- **安全区适配**：已适配刘海 / 灵动岛与底部 Home 指示条，顶部标题与底部内容不会被遮挡。
- **窄屏排布**：375~430px 宽度下卡片、表格、图表均已逐项复核，表格可横向滚动，图表按屏宽自适应缩放。

---

## 九、常见问题（FAQ）

| # | 问题 | 处理办法 |
| --- | --- | --- |
| 1 | 网址打开 404 / 打不开 | 依次检查：⑤ Pages 的 Source 是否为 GitHub Actions；⑦ `deploy` 作业是否绿勾；刚部署完等 1~2 分钟；网址结尾的 `/` 别丢；用户名与仓库名拼写是否正确 |
| 2 | 能打开但内容是旧的 | Pages 每次部署约 1 分钟生效，看 `deploy` 最近一次的时间；手机上强制刷新（下拉） |
| 3 | 收不到变动邮件 | ① 5 个 Secret 名称大小写、空格是否完全一致；② 日志显示 `无变动，跳过发送` 说明当天确实没有变化（正常，不会发邮件）；③ 授权码过期 → 重新生成后更新 `SMTP_PASSWORD`；④ 查垃圾邮件箱 |
| 4 | 怎么暂停 / 停止 | 暂停定时：Actions → 选该 workflow → 右上 **···** → **Disable workflow**；彻底不要：Settings → Pages 关闭站点，或 Settings 最下方 Danger Zone → Delete this repository |
| 5 | 每天提交，仓库会不会爆 | 每天新增约 0.9MB（页面 + 数据），一年约 320MB；GitHub 单仓库建议不超过 1GB，短期无压力。若在意，可把 `daily.yml` 中"把最新数据与页面提交回仓库"那一步删掉 |
| 6 | 我不想让数据公开 | 免费版做不到"仓库私有 + Pages 公开"（私有仓库用 Pages 需 GitHub Pro）。三种做法：① 接受公开（监控的是公募基金公开信息，实际暴露的是"你关注哪些基金"）；② 只在本地跑 `scan.py`，把生成的 `index.html` 单独传到一个专门的 Public 仓库（不放 `data` 与脚本）；③ 升级 GitHub Pro |
| 7 | 想改自动运行时间 | 编辑 `.github/workflows/daily.yml` 里的 `cron`。GitHub 用 UTC 时间，**北京时间 = UTC + 8**，`30 0 * * *` 即北京时间 08:30 |
| 8 | 定时突然不跑了 | 仓库连续 60 天无提交活动时，免费定时任务会被自动暂停，到 Actions 页面点一次 **Run workflow** 即恢复 |
| 9 | 不想用 Actions 部署（分支模式备选） | **Settings → Pages → Source** 选 `Deploy from a branch` → 分支 `main`、目录 `/(root)` → Save；同时把 `.gitignore` 里的 `index.html`、`.nojekyll` 两行删掉，本地跑一次 `python scan.py` 后把 `index.html`、`.nojekyll` 提交上去。注意：此模式下 `daily.yml` 里的 `deploy` 作业会报错，需整段删除 |
| 10 | 页面约 0.9MB 会不会太慢 | 页面是单文件自包含（样式、脚本、全部基金数据都在里面），换来"一个文件即整站、离线可看、加载无额外请求"。0.9MB 在 4G/5G 下基本秒开，可接受 |

---

## 十、以后要改东西看哪里

| 想改什么 | 改哪里 |
| --- | --- |
| 基金名单、抓取规则、页面样式与交互 | `scan.py`（改完本地跑一次确认，再推送） |
| 每日运行时间 | `.github/workflows/daily.yml` 的 `cron` |
| 收件邮箱 / 发信账号 | GitHub Secrets（云端）与本地 `data/mail_config.json`（本机运行） |
| 页面新增内容 | 改 `scan.py` 的生成逻辑，云端每天自动用新逻辑重新生成页面 |

---

本文档只讲"部署到 GitHub Pages 并在 iPhone 上随时查看"这一条链路。本机定时运行（Windows 任务计划 / macOS / Linux）与邮件细节见同目录《每日自动运行说明.md》。
