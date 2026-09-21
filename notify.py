#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变动邮件通知：读取 data/changes.json / funds.json / change_history.json 发送邮件。
配置文件：data/mail_config.json
{
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "sender": "your@qq.com",
  "password": "SMTP授权码",
  "receivers": ["your@outlook.com"]
}
规则：
    仅当本次扫描发现「申购状态 / 单日限额变动」（data/changes.json 非空）时才发送邮件；
    无变动时静默跳过、不发邮件 —— 想看最新情况直接打开监控页面 qdii_nasdaq_sp500_monitor.html。
配置来源（环境变量优先，缺项回退到 JSON 文件）：
    SMTP_HOST / SMTP_PORT / SMTP_SENDER / SMTP_PASSWORD / SMTP_RECEIVERS
    本地运行读 data/mail_config.json；云端（GitHub Actions）由 Secrets 注入环境变量，
    授权码不落仓库。
用法：
    python3 notify.py                 # 有变动才发，附监控页面 HTML 附件
    python3 notify.py --no-attach     # 有变动才发，不带附件
    python3 notify.py --daily         # 旧参数，已废弃（无变动不再发送）
"""
import json, os, sys, smtplib, datetime
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from email import encoders

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PAGE = os.path.join(BASE_DIR, "qdii_nasdaq_sp500_monitor.html")


def load_json(name, default):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def cat_stat_html(funds):
    order = ["纳斯达克100", "纳斯达克科技", "纳斯达克生物科技", "纳斯达克精选(主动)", "标普500", "标普500等权重"]
    cnt = {}
    for f in funds:
        k = f.get("catName") or "其他"
        cnt[k] = cnt.get(k, 0) + 1
    keys = [k for k in order if k in cnt] + [k for k in cnt if k not in order]
    return " ｜ ".join(f"{k} {cnt[k]} 只" for k in keys)


def build_body(funds, changes, history, ts, daily):
    stop = sum(1 for f in funds if "暂停" in f.get("sgzt", "") or "封闭" in f.get("sgzt", ""))
    lim = sum(1 for f in funds if "限" in f.get("sgzt", ""))
    op = len(funds) - stop - lim

    h = ["<div style=\"font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:14px;color:#1b2431;line-height:1.6\">"]
    h.append("<h2 style='margin:0 0 6px;font-size:17px'>QDII 场外基金监控 · 每日扫描提醒</h2>")
    h.append(f"<p style='color:#8b95a3;font-size:12.5px;margin:0 0 14px'>扫描时间：{ts} ｜ 跟踪标的：纳指100 / 纳指科技 / 纳指生物科技 / 纳指精选 / 标普500 / 标普500等权重</p>")

    if changes:
        h.append(f"<div style='background:#fff8f1;border:1px solid #f0cfa5;border-radius:10px;padding:12px 14px;margin-bottom:14px'>")
        h.append(f"<b style='color:#8a4b0a'>本次扫描发现 {len(changes)} 项申购状态 / 单日限额变动：</b><ul style='margin:8px 0 0;padding-left:20px'>")
        for c in changes:
            h.append(f"<li style='margin:4px 0'><b>{c.get('name','')}</b>（{c.get('code','')}）{c.get('desc','')}</li>")
        h.append("</ul></div>")
    else:
        h.append("<div style='background:#f6faf7;border:1px solid #d5e8da;border-radius:10px;padding:12px 14px;margin-bottom:14px'>"
                 "<b style='color:#127a41'>本次扫描未发现申购状态 / 单日限额变动。</b></div>")

    h.append("<div style='background:#fafbfd;border:1px solid #eef1f6;border-radius:10px;padding:12px 14px;margin-bottom:14px'>")
    h.append(f"<b>当前快照</b>：监控 {len(funds)} 只 ｜ 开放申购 {op} ｜ 限大额 {lim} ｜ 暂停/封闭 {stop}<br>")
    h.append(f"<span style='color:#5b6675;font-size:12.5px'>分类构成：{cat_stat_html(funds)}</span></div>")

    recent = (history or [])[-15:][::-1]
    if recent:
        h.append("<b>最近变动记录（最新在前，最多 15 条）：</b>")
        h.append("<table style='border-collapse:collapse;font-size:12.5px;width:100%;margin-top:6px'>"
                 "<tr style='background:#f8fafc'><th style='text-align:left;padding:6px;border-bottom:1px solid #e6e9ef'>时间</th>"
                 "<th style='text-align:left;padding:6px;border-bottom:1px solid #e6e9ef'>基金</th>"
                 "<th style='text-align:left;padding:6px;border-bottom:1px solid #e6e9ef'>变动内容</th></tr>")
        for r in recent:
            tag = "新纳入监控" if r.get("type") == "add" else "状态/限额变动"
            h.append("<tr><td style='padding:6px;border-bottom:1px solid #f2f4f8;white-space:nowrap'>{}</td>"
                     "<td style='padding:6px;border-bottom:1px solid #f2f4f8'>{}<span style='color:#8b95a3'> {}</span></td>"
                     "<td style='padding:6px;border-bottom:1px solid #f2f4f8'>{} <span style='color:#b45309'>[{}]</span></td></tr>".format(
                         r.get("ts", ""), r.get("name", ""), r.get("code", ""), r.get("desc", ""), tag))
        h.append("</table>")

    if os.path.exists(PAGE):
        h.append("<p style='color:#5b6675;font-size:12.5px;margin-top:14px'>完整监控页面（含申购限额、收益、费率、持仓、公告与全部历史变动）见附件 "
                 "qdii_nasdaq_sp500_monitor.html，手机点开即可查看。</p>")
    h.append("<p style='color:#8b95a3;font-size:12px;margin-top:16px;border-top:1px solid #e6e9ef;padding-top:10px'>"
             "本邮件仅在扫描到「申购状态 / 单日限额变动」时发送，无变动不会打扰；平时想看最新情况，直接打开监控页面即可。"
             "邮件由监控脚本自动发送，仅作信息聚合提醒，不构成任何投资建议或要约。QDII 基金存在汇率、境外市场、额度限制等风险，"
             "申购状态与限额以基金公司公告及销售平台为准。</p></div>")
    return "".join(h)


def send(cfg, subject, html_body, attach):
    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("QDII基金监控", cfg["sender"]))
    msg["To"] = ",".join(cfg["receivers"])
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if attach and os.path.exists(PAGE):
        part = MIMEBase("text", "html")
        with open(PAGE, "rb") as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", "qdii_nasdaq_sp500_monitor.html"))
        msg.attach(part)
    port = int(cfg.get("smtp_port", 465))
    if port == 465:
        s = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=30)
    else:
        s = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
        s.starttls()
    s.login(cfg["sender"], cfg["password"])
    s.sendmail(cfg["sender"], cfg["receivers"], msg.as_string())
    s.quit()


def load_cfg():
    """配置来源：环境变量 SMTP_* 优先，其次 data/mail_config.json。
    本地运行用 JSON；云端（GitHub Actions）用 Secrets 注入环境变量，仓库里不存授权码。"""
    cfg = {}
    cfg_path = os.path.join(DATA_DIR, "mail_config.json")
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    for key, env in (("smtp_host", "SMTP_HOST"), ("smtp_port", "SMTP_PORT"),
                     ("sender", "SMTP_SENDER"), ("password", "SMTP_PASSWORD"),
                     ("receivers", "SMTP_RECEIVERS")):
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    rcv = cfg.get("receivers")
    if isinstance(rcv, str):
        cfg["receivers"] = [x.strip() for x in rcv.replace(";", ",").split(",") if x.strip()]
    return cfg


def main():
    cfg = load_cfg()
    if not cfg.get("sender") or not cfg.get("password") or not cfg.get("receivers"):
        print("未找到邮箱配置（data/mail_config.json 或环境变量 SMTP_SENDER/SMTP_PASSWORD/SMTP_RECEIVERS），跳过发送。")
        return
    cfg.setdefault("smtp_host", "smtp.qq.com")
    cfg.setdefault("smtp_port", 465)
    funds = load_json("funds.json", [])
    changes = load_json("changes.json", [])
    history = load_json("change_history.json", [])
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    attach = "--no-attach" not in sys.argv
    if not changes:
        print("无变动，跳过发送（当前设置：仅在申购状态 / 限额变动时邮件通知）。")
        return
    subject = f"[QDII基金监控] {ts[:10]} 发现 {len(changes)} 项申购状态/限额变动"
    send(cfg, subject, build_body(funds, changes, history, ts, True), attach)
    print("已发送至:", ", ".join(cfg["receivers"]), "| 变动:", len(changes), "| 附件:", attach and os.path.exists(PAGE))


if __name__ == "__main__":
    main()
