---
name: page
description: 把查到的信息做成一张一目了然的网页并自动打开（检索 → 写单文件 HTML → 打开）。用户说「做成页面／网页／一览」「ページにまとめて／見やすく／一覧で／比べやすく」时用
requires: [web_research, write_file, open_path]
---

You are turning the user's question into ONE self-contained web page they can take in
at a glance. The page is the deliverable. The chat reply only points to it.

## Sequence

1. **Do not ask clarifying questions** unless the request cannot be attempted at all.
   Pick sensible assumptions (dates, starting point, budget) and state them in the
   page's lead sentence.
2. **Research with `web_research`: 1 to 3 calls**, each one specific question about
   what is true right now (status, dates, prices, opening hours). Stop as soon as you
   can fill the page. More searching does not make the page better; it makes the user
   wait.
3. **Write the page with ONE `write_file` call.** Path: `~/AgentPages/<name>.html`,
   where `<name>` is a short lowercase ASCII name (e.g. `koyo-daytrip`). Start from
   the skeleton below.
4. **Open it: `open_path` with the same path.**
5. **Reply in 2-4 sentences** in the user's language: the conclusion, that the page
   is now open, and the file path. Do not repeat the page's content in the chat.
6. **Then stop. The task is finished once `open_path` has returned.** Do not read the
   file back, do not run any command, do not check or improve the page, and never
   output HTML in the chat.

If `write_file` is declined or fails, say so plainly. Do not paste the HTML into the
chat as a substitute.

## Page rules

1. **Language**: the page is written in the user's language.
2. **Copy the `<style>` block exactly as given.** Change one thing only: the accent
   colour `--a`, chosen by topic — autumn / foliage `#b5432e`, spring / blossom
   `#c2577a`, sea / summer `#1f6fb2`, snow / winter `#3b5b8c`, nature / green
   `#2f7d5b`, city / food / anything else `#8a5a2b`. Do not add CSS rules.
3. **Single quotes for every HTML attribute; no double-quote character anywhere in
   the file.** Quote text with 「」. (Double quotes break the tool call.)
4. **Nothing external**: no `<img>`, `<script>`, `<link>`, `<iframe>`, web fonts or
   JavaScript. The page must look right with the network unplugged.
5. **Use only the blocks in the skeleton.** Always: eyebrow, h1, lead, pick, footer.
   Then choose the 2 or 3 blocks that fit the question: cards (options), table
   (side-by-side comparison), timeline (a schedule), note (cautions). `stats` only if
   you have 2-3 real numbers worth showing large.
6. **Answer first.** The lead and the pick box state the conclusion; everything below
   is the evidence.
7. **Keep it small.** Cards 2-4, never more (with five or more options, the best
   three get cards and the table lists them all), each with at most two short
   sentences and two `dl` rows. Table at most 5 rows by 5 columns. Timeline at most 6 entries. Notes at most
   3. Sources 2-5. The whole file stays under 5,000 characters.
8. **Every fact comes from this session's research results**: dates, prices, travel
   times, opening status, crowding. If a value was not found, write 「確認できず」
   (or the equivalent in the page's language) or drop the row. Never fill a cell with
   a guess: this page will be shown to other people.
9. **Sources are real.** The footer lists the pages the facts came from, each as a
   link whose `href` is a URL returned by the research and whose text is that page's
   title. The footer also states the date of the research (add the clock time only if
   you know it).

## Skeleton

Replace the text in ｛ ｝. Delete the blocks you do not use.

```html
<!doctype html>
<html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>｛ページ名｝</title>
<style>
:root{--a:#b5432e;--ink:#1f2328;--sub:#667085;--line:#e6e2da;--bg:#faf8f4}
*{box-sizing:border-box;margin:0}
body{font:17px/1.75 'Hiragino Sans','Yu Gothic',system-ui,sans-serif;color:var(--ink);background:linear-gradient(180deg,color-mix(in srgb,var(--a) 14%,#fff),var(--bg) 440px) no-repeat var(--bg)}
main{max-width:1080px;margin:auto;padding:60px 28px 72px}
.eyebrow{color:var(--a);font-size:14px;font-weight:700;letter-spacing:.14em}
h1{font:700 46px/1.25 'Hiragino Mincho ProN','Yu Mincho',serif;margin:10px 0 14px}
.lead{font-size:19px;max-width:44em}
.stats{display:flex;flex-wrap:wrap;gap:14px 44px;margin-top:28px}
.stats b{display:block;font:700 34px/1.3 'Hiragino Mincho ProN',serif;color:var(--a)}
.stats span,dt,th,footer{font-size:14px;color:var(--sub)}
h2{font-size:22px;margin:54px 0 18px;padding-left:12px;border-left:5px solid var(--a)}
.pick,.card,table{background:#fff;border:1px solid var(--line);border-radius:14px}
.pick{border-left:6px solid var(--a);padding:22px 26px;margin-top:34px;font-size:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}
.card{padding:22px}
.card h3{font-size:21px;margin:10px 0 6px}
.tag{display:inline-block;font-size:13px;font-weight:700;padding:1px 11px;border-radius:99px;background:var(--a);color:#fff}
.tag.soft{background:#eee9df;color:#5b5346}
dl{display:grid;grid-template-columns:5em 1fr;gap:2px 12px;font-size:15px;margin-top:12px}
table{width:100%;border-spacing:0;overflow:hidden}
th,td{padding:12px 16px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#f3efe7}
tr:last-child td{border:0}
.time{list-style:none;padding:0;margin-left:8px;border-left:2px solid var(--line)}
.time li{position:relative;padding:0 0 18px 26px}
.time li::before{content:'';position:absolute;left:-7px;top:9px;width:12px;height:12px;border-radius:50%;background:var(--a)}
.time b{display:inline-block;min-width:4.2em;color:var(--a)}
.note{background:#f3efe7;border-radius:14px;padding:18px 24px;font-size:16px}
.note li{margin-left:1.2em}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line)}
a{color:var(--a)}
</style></head><body><main>

<div class='eyebrow'>｛テーマ｝ ｜ ｛調査日｝ 調べ</div>
<h1>｛問いへの答えが分かる見出し｝</h1>
<p class='lead'>｛結論と前提を1〜2文で｝</p>
<div class='stats'>
<div><b>｛数字｝</b><span>｛その数字の意味｝</span></div>
<div><b>｛数字｝</b><span>｛その数字の意味｝</span></div>
</div>

<div class='pick'><b>結論：</b>｛おすすめと理由を2文以内で｝</div>

<h2>｛候補の見出し｝</h2>
<div class='grid'>
<div class='card'><span class='tag'>｛状態｝</span><h3>｛名前｝</h3><p>｛特徴を2文以内で｝</p>
<dl><dt>｛項目｝</dt><dd>｛値｝</dd><dt>｛項目｝</dt><dd>｛値｝</dd></dl></div>
<div class='card'><span class='tag soft'>｛控えめな状態｝</span><h3>｛名前｝</h3><p>｛特徴｝</p>
<dl><dt>｛項目｝</dt><dd>｛値｝</dd><dt>｛項目｝</dt><dd>｛値｝</dd></dl></div>
</div>

<h2>｛比較の見出し｝</h2>
<table>
<tr><th>｛列｝</th><th>｛列｝</th><th>｛列｝</th></tr>
<tr><td>｛値｝</td><td>｛値｝</td><td>｛値｝</td></tr>
</table>

<h2>｛予定の見出し｝</h2>
<ul class='time'>
<li><b>｛時刻｝</b>｛すること｝</li>
<li><b>｛時刻｝</b>｛すること｝</li>
</ul>

<h2>｛注意の見出し｝</h2>
<ul class='note'>
<li>｛注意点｝</li>
</ul>

<footer>
<p>調査時刻：｛日付と時刻｝</p>
<ol>
<li><a href='｛URL｝'>｛ページの題名｝</a></li>
</ol>
</footer>
</main></body></html>
```
