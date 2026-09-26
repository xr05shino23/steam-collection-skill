# 步骤1：抓取游戏列表的方法库

目标产出：`steam_library.csv`（`appid,name,store_url,installed`，UTF-8 BOM）。
所有权（库里有什么）与安装状态（装了什么）来自不同数据源，必须分开获取再合并。

## 数据源总表

| 源 | 地址 | 凭据 | 给什么 | 注意 |
|---|---|---|---|---|
| Web API GetOwnedGames | `api.steampowered.com/IPlayerService/GetOwnedGames/v1/` | API key（[申请](https://steamcommunity.com/dev/apikey)） | 所有权 appid+名称，一次全量 | **要求账号"游戏详情"隐私为公开**，否则返回 0 条（key 归属无关） |
| 社区 games 页 | `steamcommunity.com/id/<vanity>/games/?tab=all` | 匿名 或 steamLoginSecure Cookie | 所有权 | 详情非公开时重定向登录页；2025 改版后页面是 React 内嵌 JSON（见下） |
| 许可和产品序列号激活页 | `store.steampowered.com/account/licenses/` | Cookie | 全部许可（含 CD-Key/订阅）+ 激活日期 | 无 appid，按名称比对做审计；页面 AJAX 翻页 |
| 本地 appmanifest | `<steam_root>/steamapps/libraryfolders.vdf` 列出所有库 → 各库 `appmanifest_*.acf` | 无 | **安装状态（唯一权威）** | Steam 根目录读注册表 `HKCU\Software\Valve\Steam\SteamPath` |
| SteamDB / 商店页 | steambd.net / `store.steampowered.com/app/<id>` | 无 | 单款信息补充 | 用于核对，不宜全库抓 |

**steamLoginSecure Cookie 获取方式**（三选一）：
1. 浏览器控制自动提取（见下节，推荐）
2. 用户手动：浏览器登录 steamcommunity.com → F12 → 应用程序 → Cookie → 复制 `steamLoginSecure` 值
3. 用户从已登录浏览器导出后粘贴给 agent

## 合并规则

```
所有权列表 (L1/L2) 为"库存里有什么"的唯一基准
manifest 为安装状态: appid 在所有权 ∩ manifest → installed=true
manifest 有而所有权无 → 疑似家庭共享/免费周末残留: 只在报告里列出, 不写入
系统组件过滤: 排除 Steamworks Common Redistributables (appid 228980) 等非游戏条目
名称冲突: 所有权名称为空时用 manifest 的 name 兜底
```

## 浏览器控制登录（推荐的凭据获取方式）

用 Playwright 以**持久化上下文**启动系统浏览器，让用户手动登录，自动提取 Cookie：

```python
from playwright.sync_api import sync_playwright
# 依赖: pip install playwright ; 浏览器用系统自带的 Edge/Chrome(channel), 无需下载内核
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        profile_dir,          # 持久化目录, 保留会话下次免登录
        channel="msedge",     # 失败再试 "chrome"
        headless=False,       # 必须可见, 用户要手动登录
        proxy={"server": proxy} if proxy else None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://steamcommunity.com/login/home/", timeout=90000,
              wait_until="domcontentloaded")   # 关键: 社区页慢资源会卡死默认 load 事件
    # 轮询 cookie 直到出现 steamLoginSecure (给用户 5-10 分钟)
    for c in ctx.cookies("https://steamcommunity.com"):
        if c.get("name") == "steamLoginSecure" and c.get("value"):
            save_to_local_config(c["value"])
    ctx.close()
```

要点：
- 用户在弹出的窗口里正常登录（账号密码 + Steam Guard 令牌/邮件验证都能过）
- 检测到 Cookie 后写回本地配置（同目录 `local_config.json`），之后所有请求走 requests + Cookie
- 轮询期间浏览器窗口被用户关闭要捕获异常并优雅退出
- 同一会话可以顺带打开 games 页抓取列表，省一次请求

## 新版社区 games 页解析（2025 改版后）

旧版页面有 `rgGames = [...]` JS 数组；改版后没有了，改为**内嵌 React Query JSON 数据块**。
两种结构都要兼容：

```python
def parse_games(html):
    out = {}
    m = re.search(r'rgGames\s*=\s*\[(.*?)\]\s*;', html, re.S)   # 旧版
    if m:
        for blk in re.finditer(r'\{[^{}]*\}', m.group(1)):
            a = re.search(r'"appid"\s*:\s*(\d+)', blk.group(0))
            n = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', blk.group(0))
            if a: out[a.group(1)] = json.loads('"%s"' % n.group(1)) if n else ""
    if out: return out
    # 新版: name 与 store_url_path 紧邻配对; name 字符集排除引号和反斜杠, 防止跨嵌套 JSON 误抓
    for name, appid in re.findall(
            r'\\{2,}"name\\{2,}":\\{2,}"([^"\\]*)\\{2,}",\\{2,}"store_url_path\\{2,}":\\{2,}"app/(\d+)',
            html, re.S):
        if appid != "0" and appid not in out:
            try: name = json.loads('"%s"' % name)
            except Exception: pass
            if name.strip(): out[appid] = name.strip()
    return out
```

注意：新版页面里个人资料装饰品（Steam 点数商品）也有同名结构，`store_url_path` 以 `app/` 开头
这一条件可以过滤掉绝大部分；appid 0 的条目丢弃。

## 许可页审计（可选）

- 首页 `GET /account/licenses/`，行结构 `class="license_row"` 内含 `license_game_name` 与 `license_date_col`
- 翻页参数不确定，逐个尝试 `?p=N`、`?ajax=1&p=N`，能翻出新行即有效，连续 2 页无新行停止
- 页面无 appid，只能按**名称**（casefold 归一）与列表比对，差异项人工判断（免费领取未入库/改名/订阅）

## 产出与汇报

- 写 CSV 前备份旧文件；输出 diff 报告（新增/移除/安装变化/疑似残留）
- 向用户汇报：用了哪个方法、总数、已安装数、任何可疑差异 → **等确认后进步骤2**
