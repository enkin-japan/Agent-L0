#!/usr/bin/env python3
"""Colab CLI 隧道保活（Mac 侧常驻，12h 长稳站二跑验证形态）。

CLI 自带 keep_alive 进程会被 404 自清逻辑连坐杀死（账本长稳站一跑事故），
本程序是替身：每 60s GET tun/m/<endpoint>/keep-alive/，OAuth 凭据自动刷新。
ReadTimeout 视为成功（TFE 已记账、VM 不应答属常态，CLI 源码同款语义）。

用法：python3 tunnel_keepalive.py <session_key>
  session_key = sessions.json 里的会话键（命名会话即其名，如 agentd-0810）。
只打印状态变化与失败；连续 5 次失败退出码 1（外层 daily.sh 哨兵负责告警）。
"""
import json
import sys
import time
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

CFG = Path.home() / ".config/colab-cli"
key = sys.argv[1]
sessions = json.load(open(CFG / "sessions.json"))
if key not in sessions:
    print(f"会话键 {key!r} 不在 sessions.json（现有: {list(sessions)}）")
    sys.exit(2)
ENDPOINT = sessions[key]["endpoint"]
creds = Credentials.from_authorized_user_file(str(CFG / "token.json"))
url = f"https://colab.research.google.com/tun/m/{ENDPOINT}/keep-alive/"
last = None
fails = 0
while True:
    try:
        if not creds.valid:
            creds.refresh(Request())
        try:
            r = requests.get(url, params={"authuser": "0"}, headers={
                "Authorization": f"Bearer {creds.token}",
                "Accept": "application/json",
                "X-Colab-Client-Agent": "colab-cli",
                "X-Colab-Tunnel": "Google"}, timeout=10)
            code = r.status_code
        except requests.exceptions.ReadTimeout:
            code = 200
        fails = 0 if code == 200 else fails + 1
    except Exception as e:  # 凭据刷新/网络层任意故障都计一次失败
        code = f"EXC:{type(e).__name__}"
        fails += 1
    if code != last:
        print(f"{time.strftime('%T')} keep-alive -> {code}", flush=True)
        last = code
    if fails >= 5:
        print(f"{time.strftime('%T')} KEEPALIVE_LOST 连续失败 {fails}", flush=True)
        sys.exit(1)
    time.sleep(60)
