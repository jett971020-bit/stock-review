# A股复盘程序

基于 AKShare 获取 A 股日线行情，输入股票代码后显示：

- 日 K 线
- 5/10/20/60 日均线
- 成交量
- 量比
- 输入股票代码或名称
- 放量突破标记
- 缩量回踩 20 日线标记
- 重点监测列表和回踩提醒
- 历史搜索记录

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

如果不想安装到系统 Python，也可以安装到当前项目目录：

```powershell
python -m pip install -r requirements.txt --target .python_packages
```

## 启动

```powershell
python -m streamlit run app.py --global.developmentMode=false --server.port 8501 --server.headless true
```

打开 Streamlit 输出的本地地址后，在侧边栏输入股票代码或名称即可。支持 `000001`、`sh600519`、`sz000001`、`平安银行`、`贵州茅台` 这类格式。

重点监测列表和历史搜索记录会保存在当前目录的 `stock_review_data.json`，只在本机使用。

## 免费云端部署

推荐用 Streamlit Community Cloud 部署网页，用 GitHub Gist 保存重点监测列表和历史记录。

### 1. 准备 GitHub 仓库

把本项目推送到 GitHub。Streamlit Community Cloud 会从 GitHub 仓库读取代码并部署。

### 2. 创建云端数据文件

在 GitHub 新建一个 Gist，文件名建议用：

```text
stock_review_data.json
```

初始内容：

```json
{
  "watchlist": [],
  "history": [],
  "reminders": []
}
```

然后创建一个 GitHub Token，给它读取和更新 Gist 的权限。

### 3. Streamlit Cloud Secrets

在 Streamlit Community Cloud 的 App secrets 中填写：

```toml
GITHUB_TOKEN = "你的 GitHub Token"
GIST_ID = "你的 Gist ID"
GIST_FILENAME = "stock_review_data.json"
```

配置后，网页里的重点监测列表和历史搜索记录会保存到 Gist，而不是服务器临时文件。

## 每天 14:30 邮件提醒

项目已包含 GitHub Actions 定时任务：

```text
.github/workflows/daily-reminder.yml
```

它会在工作日北京时间 14:30 运行一次 `reminder.py`，扫描重点监测列表，发现缩量回踩 20 日线后发送邮件。即使没有触发，也会发送一封“暂无触发”的扫描结果邮件。

邮件提醒会优先使用新浪实时行情作为 14:30 盘中快照：20 日线来自历史日 K，当前价、当日最低价、当日成交量来自实时行情接口。这样更适合 14:45 前做一次交易前检查。

需要在 GitHub 仓库的 Actions secrets 中配置：

```text
GIST_TOKEN      GitHub Token，用于读取/更新 Gist
GIST_ID         Gist ID
GIST_FILENAME   stock_review_data.json
SMTP_HOST       邮箱 SMTP 地址，例如 smtp.qq.com
SMTP_PORT       SMTP SSL 端口，例如 465
SMTP_USER       发件邮箱
SMTP_PASSWORD   邮箱 SMTP 授权码，不是登录密码
MAIL_TO         收件邮箱
```

可选仓库变量：

```text
PULLBACK_VOLUME_RATIO   默认 0.8
PULLBACK_TOLERANCE_PCT  默认 2.0
```

注意：14:30 提醒是盘中快照提醒，不是逐秒实时推送。GitHub Actions 的计划任务偶尔可能延迟几分钟；如果需要秒级提醒，需要换成常驻服务器或专业行情推送服务。

## 信号规则

- 量比：当日成交量 / 前 5 个交易日平均成交量
- 放量突破：收盘价突破前 20 日高点，且量比达到侧边栏阈值
- 缩量回踩 20 日线：最低价接近 20 日均线，收盘价未明显跌破 20 日线，且量比低于侧边栏阈值

这些阈值都可以在侧边栏调整。
