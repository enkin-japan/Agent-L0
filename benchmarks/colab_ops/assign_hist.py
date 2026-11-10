import re, sys, datetime as dt, collections
# 只取时间戳 + 方法 + 路径 + 状态码；不输出任何原文。
ts = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')
resp = re.compile(r'"POST (/tun/m/assign)[^"]*" (\d{3})')
req = re.compile(r'Request: POST https?://[^/\s]+/tun/m/assign\b')
events = []   # (time, kind, code)
for line in open(sys.argv[1], errors='replace'):
    if 'assign' not in line: continue
    m = ts.match(line)
    if not m: continue
    t = dt.datetime.strptime(m[1], '%Y-%m-%d %H:%M:%S')
    r = resp.search(line)
    if r and 'assignments' not in r[1]:
        events.append((t, 'resp', r[2])); continue
    if req.search(line) and '/tun/m/assignments' not in line:
        events.append((t, 'req', None))
# 归并为一次 colab new：相邻 POST 请求间隔 < 100 s 视为同一次调用内的重试
attempts = []
cur = None
for t, kind, code in events:
    if kind == 'req':
        if cur is None or (t - cur['last']).total_seconds() > 100:
            cur = {'start': t, 'last': t, 'codes': [], 'lat': [], 'pending': t}
            attempts.append(cur)
        else:
            cur['pending'] = t
        cur['last'] = t
    elif cur is not None and cur.get('pending'):
        cur['codes'].append(code)
        cur['lat'].append(int((t - cur['pending']).total_seconds()))
        cur['pending'] = None
        cur['last'] = t
out = open(sys.argv[2], 'w')
day = collections.OrderedDict()
for a in attempts:
    codes = a['codes'] + (['(无响应)'] if a['pending'] else [])
    first_ok = bool(a['codes']) and a['codes'][0] == '200'
    ok = '200' in a['codes']
    out.write(f"{a['start']}  {' '.join(codes):28s} 耗时秒={a['lat']}\n")
    d = day.setdefault(a['start'].date(), [0, 0, 0, 0])
    d[0] += 1; d[1] += first_ok; d[2] += ok and not first_ok; d[3] += not ok
print('日期        次数 首发即成 重试后成 未成')
for d, v in day.items():
    print(d, f'{v[0]:5d} {v[1]:7d} {v[2]:7d} {v[3]:5d}')
