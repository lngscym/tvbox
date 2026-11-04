# 自用 TVBox / 影视仓 配置

基于 [Lightconer/tvbox-ysc-config](https://github.com/Lightconer/tvbox-ysc-config) 的聚合思路重做，但换了一个关键策略：**只合并真正能合并的部分**。

## 一、为什么不直接用它生成的「单仓聚合」

原项目把所有源的 `sites` 塞进同一个 `spider` 里。实测数据：

| 事实 | 数值 |
| --- | --- |
| 其「单仓聚合」站点数 | 517 |
| 依赖 `csp_XXX`（必须由各自 spider 解释）的站点 | 405（78%） |
| 王二小 与 老刘备 的 csp 类名交集 | **0** |
| 王二小 与 小马 的 csp 类名交集 | **1** |

`csp_XXX` 是 spider 里的类名，每个源用自己的 jar/js。把 A 源的站点挂到 B 源的 spider 下，类找不到 → 点开就报错。所以那份「聚合」里大部分站点是打不开的。

**本配置的策略：**

- `sites` → 只取**基准源**（站点与 spider 严格匹配）
- 其它源里 `type=0/1` 的纯 HTTP JSON/XML 站点可以**安全并入**（不依赖 spider），共并入 25 个
- `parses`（解析器）、`lives`（直播源）与 spider 无关 → **跨源合并去重**
- 相对路径（`./libs/js/*.js`、`./libs/jar/*.jar`）→ **全部绝对化 + 实测可达性**，失效的剔除

## 二、产物

| 文件 | 用途 |
| --- | --- |
| `dist/单仓精选.json` | **日常首选**。一个地址搞定：245 站点 + 30 解析器 + 15 直播源 |
| `dist/多仓订阅.json` | 备份/换源用。App 内可切换 7 个源，每个源自洽不会互相污染 |
| `dist/预览.html` | 浏览器打开可查站点清单、搜索过滤、看源状态 |
| `dist/配置报告.md` | 本次构建明细（哪些站点被剔除、为什么） |

实测（2026-11-04）：10 个源里 7 个可用，肥猫／饭太硬／讴歌 当前挂了（反爬或域名失效）。

## 三、怎么填进 App

### 方式 A：Fork 到自己仓库（推荐，能自动更新）

1. 在 GitHub 新建一个仓库（私有也行）
2. 把本目录所有文件 push 上去
3. 仓库的 Actions 已配好，会每天 10:00 / 22:00（北京时间）自动重建并提交
4. 配置地址填：

```
https://raw.githubusercontent.com/你的用户名/你的仓库名/main/dist/单仓精选.json
```

多仓地址把文件名换成 `多仓订阅.json`。国内直连 raw 慢的话，前面加 `https://gh-proxy.com/` 前缀。

### 方式 B：本地文件（不上网托管）

把 `单仓精选.json` 传到手机/盒子，影视仓里「设置 → 配置地址」选本地文件
（或用 App 的「推送」功能），路径形如 `/sdcard/Download/单仓精选.json`。

### 方式 C：局域网 HTTP（电脑常开时最省事）

```bash
cd dist && python3 -m http.server 8080
```

App 里填 `http://电脑局域网IP:8080/单仓精选.json`。

## 四、重新构建

```bash
pip install -r requirements.txt
python build.py
```

常用参数：

```bash
python build.py --base xiaoma     # 换基准源（laoliubei / xiaoma / xiaohezi / 4k / wangerxiao）
python build.py --offline         # 只用本地缓存，不联网
python build.py --no-portable     # 不并入其它源的可移植站点
python build.py --keep-all        # 不过滤引流/广告站点
python build.py --max-parses 20 --max-lives 10
```

改源就编辑 `sources.json`，`urls` 按「主地址 → 备用」顺序排。

## 五、常见问题

**能播放但一直转圈？** 在播放器界面切换「解析线路」，`parses` 里 30 个线路总有一个通。

**某个站点打不开？** 单源配置无法避免，作者上游改了接口就会失效。用多仓订阅换一个源，或重新 `python build.py` 拉最新。

**直播源部分频道没信号？** 直播源本身更新频繁，属于上游问题。可以在 App 里换 `lives` 里的其它源。

**要不要开「多仓」？** 单仓够用就单仓；想随时换源、对比画质就用多仓。

## 六、免责声明

所有接口均来自互联网公开分享，版权归原作者所有。本项目仅做格式整合与可用性校验，仅供学习交流，请勿商用。
