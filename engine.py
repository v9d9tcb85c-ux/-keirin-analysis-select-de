# -*- coding: utf-8 -*-
import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
ENGINE_VERSION="mobile-select-v2.4-board-timeout-safe"
TARGET_STAR = 2
TARGET_LINES = {"3.2.2", "2.3.2", "2.2.3"}
TARGET_ORDERS = {"◎○△", "◎○×"}
MAX_RETRIES = 2

JST = __import__("datetime").timezone(timedelta(hours=9))

VENUES = {
"函館":"hakodate","青森":"aomori","いわき平":"iwakitaira","弥彦":"yahiko","前橋":"maebashi",
"取手":"toride","宇都宮":"utsunomiya","大宮":"omiya","西武園":"seibuen","京王閣":"keiokaku",
"立川":"tachikawa","松戸":"matsudo","千葉":"chiba","川崎":"kawasaki","平塚":"hiratsuka",
"小田原":"odawara","伊東":"ito","静岡":"shizuoka","名古屋":"nagoya","岐阜":"gifu",
"大垣":"ogaki","豊橋":"toyohashi","富山":"toyama","松阪":"matsusaka","四日市":"yokkaichi",
"福井":"fukui","奈良":"nara","向日町":"mukomachi","和歌山":"wakayama","岸和田":"kishiwada",
"玉野":"tamano","広島":"hiroshima","防府":"hofu","高松":"takamatsu","小松島":"komatsushima",
"高知":"kochi","松山":"matsuyama","小倉":"kokura","久留米":"kurume","武雄":"takeo",
"佐世保":"sasebo","別府":"beppu","熊本":"kumamoto"
}


class StopRequested(Exception):
    pass


def _log(msg):
    print(f"[WINTICKET] {msg}", flush=True)


def _log_race(venue, race, event, **kwargs):
    extra=" ".join(f"{k}={v}" for k,v in kwargs.items() if v not in (None,""))
    suffix=(" "+extra) if extra else ""
    _log(f"RACE venue={venue} race={race}R event={event}{suffix}")


def _log_venue(venue, event, **kwargs):
    extra=" ".join(f"{k}={v}" for k,v in kwargs.items() if v not in (None,""))
    suffix=(" "+extra) if extra else ""
    _log(f"VENUE venue={venue} event={event}{suffix}")


def _today_jst():
    return datetime.now(JST).date()


def _check_stop(stop_event):
    if stop_event and stop_event.is_set():
        raise StopRequested()


def _browser_args():
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--disable-features=Translate,BackForwardCache,MediaRouter,OptimizationHints",
    ]




def _open_session(playwright):
    """Create an isolated Chromium session. A venue uses its own session so one crash cannot poison all later venues."""
    browser = playwright.chromium.launch(headless=True, args=_browser_args())
    context = browser.new_context(
        locale="ja-JP", viewport={"width": 900, "height": 700}, service_workers="block"
    )
    enable_fast_mode(context)
    page = context.new_page()
    page.set_default_timeout(5000)
    return browser, context, page


def _close_session(browser=None, context=None, page=None):
    """Best-effort cleanup; cleanup failures must never abort a completed scan."""
    for obj in (page, context, browser):
        if obj is None:
            continue
        try:
            obj.close()
        except Exception:
            pass


def _page_unhealthy(page):
    try:
        return page is None or page.is_closed()
    except Exception:
        return True

def enable_fast_mode(context):
    blocked = (
        "doubleclick", "googletagmanager", "google-analytics",
        "adservice", "adsystem", "facebook.net", "clarity.ms"
    )
    def handler(route):
        req = route.request
        url = req.url.lower()
        if req.resource_type in ("image","media","font") or any(k in url for k in blocked):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", handler)


def _body_text(page):
    """body.inner_text timeout依存を避ける。"""
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return page.locator("body").text_content(timeout=2500) or ""
        except Exception:
            return ""


def goto(page, url, stop_event=None):
    """
    v2.5.5:
    - 同じURLの自動再試行をしない（重複アクセス防止）
    - 最大6秒。timeoutでも本文が取得済みなら成功扱い
    - 前後で停止要求を確認
    """
    _check_stop(stop_event)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=6000)
        _check_stop(stop_event)
        page.wait_for_timeout(100)
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            return body
        raise RuntimeError("ページ本文を取得できません")
    except StopRequested:
        raise
    except Exception as e:
        _check_stop(stop_event)
        try:
            body = _body_text(page)
        except Exception:
            body = ""
        if body:
            _log(
                f"GOTO_PARTIAL_OK attempt=1/1 timeout_ms=6000 "
                f"url={url} error={type(e).__name__}:{e}"
            )
            return body
        _log(
            f"GOTO_FAIL attempt=1/1 timeout_ms=6000 "
            f"url={url} error={type(e).__name__}:{e}"
        )
        raise


def goto_board(page, url, stop_event=None):
    """
    開催場一覧/開催場メタ情報専用。
    Render上では6秒だとWINTICKETのDOMContentLoaded待ちが間に合わないことがあるため、
    ここだけ最大12秒にする。レース巡回側の goto() は従来どおり6秒のまま。
    タイムアウトしても本文が取れていれば成功扱いにし、同一URLの自動再試行はしない。
    """
    _check_stop(stop_event)
    timeout_ms = 12000
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _check_stop(stop_event)
        page.wait_for_timeout(150)
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            return body
        raise RuntimeError("ページ本文を取得できません")
    except StopRequested:
        raise
    except Exception as e:
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            _log(
                f"BOARD_GOTO_PARTIAL_OK attempt=1/1 timeout_ms={timeout_ms} "
                f"url={url} error={type(e).__name__}:{e}"
            )
            return body
        _log(
            f"BOARD_GOTO_FAIL attempt=1/1 timeout_ms={timeout_ms} "
            f"url={url} error={type(e).__name__}:{e}"
        )
        raise


def parse_grade(text):
    """
    小さい局所テキスト専用。
    L1/L級/ガールズをF1/F2より優先する。
    ページ全体の本文には使わない。
    """
    if not text:
        return ""

    if re.search(r"(?<![A-Z0-9])L1(?![A-Z0-9])", text):
        return "L"
    if "L級" in text or "ガールズ" in text or "女子" in text:
        return "L"

    m = re.search(r"(?<![A-Z0-9])(F1|F2)(?![A-Z0-9])", text)
    return m.group(1) if m else ""


def detect_grade_from_header(page, race_no):
    """
    予想ページ全体ではなく、画面上部のレース見出し周辺だけで級を判定。
    未発表ページ内の別レース/ナビゲーションにあるL1を誤取得しない。
    """
    try:
        texts = page.evaluate("""(raceNo)=>{
          const out=[];
          const nodes=[...document.querySelectorAll('h1,h2,h3,[class*="Header"],[class*="Title"],[class*="RaceInfo"],[class*="RaceHeader"]')];
          for(const e of nodes){
            const t=(e.innerText||'').trim();
            if(!t || t.length>260) continue;
            if(t.includes(String(raceNo)+'R') || /(?:F1|F2|L1|L級|ガールズ)/.test(t)){
              out.push(t);
            }
            if(out.length>=20) break;
          }
          return out;
        }""", race_no)
        for t in texts or []:
            g=parse_grade(t)
            if g:
                return g
    except Exception:
        pass
    return ""

def confidence_dom(page):
    loc = page.locator('[aria-label*="3点中"]')
    for i in range(min(loc.count(), 10)):
        try:
            label = loc.nth(i).get_attribute("aria-label") or ""
        except Exception:
            continue
        m = re.search(r"3点中\s*([0-3])点", label)
        if m:
            return int(m.group(1)), label
    try:
        html = page.content()
        m = re.search(r'aria-label=["\']3点中\s*([0-3])点["\']', html)
        if m:
            return int(m.group(1)), m.group(0)
    except Exception:
        pass
    return None, ""


def ancestor_texts(page, label, levels=7):
    out=[]
    try:
        loc=page.get_by_text(label, exact=True)
        if loc.count()==0:
            loc=page.get_by_text(re.compile(re.escape(label)))
        for i in range(min(loc.count(),5)):
            vals=loc.nth(i).evaluate(f"""e=>{{
                const out=[];let n=e;
                for(let k=0;k<{levels}&&n;k++,n=n.parentElement){{
                    const t=(n.innerText||'').trim();
                    if(t)out.push(t);
                }}
                return out;
            }}""")
            for v in vals:
                if v and v not in out:
                    out.append(v)
    except Exception:
        pass
    return out


def extract_ai_number(page, label):
    for txt in ancestor_texts(page, label):
        cleaned=txt.replace(label," ")
        nums=re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if len(nums)==1 and len(txt)<=140:
            return int(nums[0])
    for txt in ancestor_texts(page, label):
        cleaned=txt.replace(label," ")
        nums=re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if nums and len(txt)<=240:
            return int(nums[0])
    return ""


def extract_ai_marks(page):
    return (
        extract_ai_number(page,"本命"),
        extract_ai_number(page,"対抗"),
        extract_ai_number(page,"単穴"),
        extract_ai_number(page,"連下"),
    )


def mark_for_rider(r, hon, tai, ana, ren):
    if r==hon: return "◎"
    if r==tai: return "○"
    if r==ana: return "△"
    if r==ren: return "×"
    return "他"


def line_mark_order(groups, hon, tai, ana, ren):
    return ".".join("".join(mark_for_rider(r,hon,tai,ana,ren) for r in g) for g in groups)


def three_line_order(groups, hon, tai, ana, ren):
    for g in groups:
        if len(g)==3:
            return "".join(mark_for_rider(r,hon,tai,ana,ren) for r in g)
    return ""


def _collect_riders(page):
    try:
        vals=page.evaluate("""()=> {
          const out=[],seen=new Set();
          for(const sel of ['[class*="RaceCard"] [class*="Bib"]','[class*="Racer"] [class*="Bib"]','[class*="Bib"]']){
            for(const e of document.querySelectorAll(sel)){
              const cls=typeof e.className==='string'?e.className:'';
              if(cls.includes('LinePowerBib'))continue;
              const t=(e.innerText||'').trim();
              if(/^[1-9]$/.test(t)&&!seen.has(t)){seen.add(t);out.push(Number(t));}
            }
            if(out.length>=5)break;
          }
          return out;
        }""")
        return vals if 5 <= len(vals) <= 9 else []
    except Exception:
        return []


def extract_line(page, stop_event=None):
    _check_stop(stop_event)
    all_riders=_collect_riders(page)
    formed=[]

    for attempt in range(2):
        _check_stop(stop_event)
        try:
            heading=page.get_by_text(re.compile("ラインパワー比較"))
            if heading.count():
                heading.first.scroll_into_view_if_needed(timeout=1600)
                page.wait_for_timeout(220 + 220*attempt)
                data=heading.first.evaluate("""e=>{
                    let root=e;
                    for(let k=0;k<10&&root;k++,root=root.parentElement){
                      const groups=[];
                      for(const li of root.querySelectorAll('li')){
                        const nums=[];
                        for(const b of li.querySelectorAll('[class*="LinePowerBib"]')){
                          const t=(b.innerText||'').trim();
                          if(/^[1-9]$/.test(t)&&!nums.includes(Number(t)))nums.push(Number(t));
                        }
                        if(nums.length>=2)groups.push(nums);
                      }
                      if(groups.length)return groups;
                    }
                    return [];
                }""")
                if data:
                    formed=data
                    break
        except Exception:
            pass
        try:
            page.evaluate("window.scrollBy(0,650)")
            page.wait_for_timeout(220)
        except Exception:
            pass

    if not formed:
        return "", []

    used=[]
    for g in formed:
        for n in g:
            if n not in used:
                used.append(n)

    groups=list(formed)
    if all_riders:
        groups += [[n] for n in all_riders if n not in used]

    counts=[len(g) for g in groups]
    if 4 <= sum(counts) <= 9 and len(groups)>=2:
        return ".".join(map(str,counts)), groups
    return "", []


def race_info(url):
    m=re.search(r"/raceresult/(\d{8})(\d*)/(\d+)/(\d+)", url)
    if not m:
        return None
    start=datetime.strptime(m.group(1),"%Y%m%d").date()
    sid=m.group(1)+m.group(2)
    day=int(m.group(3)); race=int(m.group(4))
    actual=start+timedelta(days=day-1)
    return sid,day,race,actual



def _session(text):
    text=text or ""
    if "ミッドナイト" in text:return "MN"
    if "ナイター" in text:return "N"
    if "モーニング" in text:return "M"
    times=[int(h)*60+int(m) for h,m in re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)",text)]
    if times:
        if max(times)>=21*60:return "MN"
        if max(times)>=18*60:return "N"
        if min(times)<=10*60:return "M"
    return "D"

def _session_from_live_card(text):
    """
    競輪トップの「開催中のレース」カードに出る「現在R＋発走時刻」だけで
    M/D/N/MN を推定する。

    v2.1ではカード祖先に混ざるページ共通の「ミッドナイト」文字を先に見ていたため、
    全会場がMNになることがあった。v2.2では開催種別の単語は一切使わない。

    考え方:
      現在のRの発走時刻から、1Rあたり約25分を戻して1R開始時刻を推定。
      モーニング: おおむね10時以前開始
      デイ      : 10時台〜13時台開始
      ナイター  : 14時台〜18時台開始
      ミッドナイト: 19時以降開始

    画面から時刻/Rが取れない場合は、誤ってMNを表示するより安全なDを返す。
    """
    text = text or ""

    # 「発走 12:19」のような現在レースの発走時刻を最優先。
    tm = re.search(r"発走\s*([0-2]?\d):([0-5]\d)", text)
    # カード内には開催日など他の数字もあるので「4R」の形だけを見る。
    rm = re.search(r"(?<!\d)(1[0-2]|[1-9])R(?!\d)", text)
    if not tm:
        return "D"

    minutes = int(tm.group(1)) * 60 + int(tm.group(2))
    race_no = int(rm.group(1)) if rm else 1

    # 競輪は概ね1Rごと20〜30分。25分で開始時刻を推定すると、
    # 昼開催の途中Rをナイター/MNと誤判定しにくい。
    estimated_first = minutes - max(0, race_no - 1) * 25

    if estimated_first >= 19 * 60:
        return "MN"
    if estimated_first >= 14 * 60:
        return "N"
    if estimated_first <= 10 * 60:
        return "M"
    return "D"


def _live_card_info_from_anchor(anchor):
    """
    開催場リンクの近くから、その場だけの「R/発走時刻/F1・F2」を拾う。
    同じカード内でF1/F2が少し離れていても取れるよう、祖先＋直近兄弟を候補化する。
    ページ全体本文は使わず、別会場の級を混ぜない。
    """
    try:
        return anchor.evaluate(r"""e=>{
          const candidates=[];
          const add=(t,bonus=0)=>{
            t=(t||'').trim();
            if(!t || t.length>1800) return;
            let score=bonus;
            if(/(?:F1|F2)/.test(t)) score+=8;
            if(/(?:\d{1,2})R/.test(t)) score+=5;
            if(/発走\s*\d{1,2}:\d{2}/.test(t)) score+=5;
            else if(/発走|締切|投票|結果|終了/.test(t)) score+=2;
            if(score>0) candidates.push({t,score,len:t.length});
          };

          let n=e;
          for(let k=0;k<12 && n;k++,n=n.parentElement){
            const t=(n.innerText||n.textContent||'');
            add(t, 12-k);

            // 級表示がカード内の隣ブロックに分かれている場合を拾う。
            if(n.parentElement){
              const sibs=[...n.parentElement.children];
              const idx=sibs.indexOf(n);
              for(let j=Math.max(0,idx-2); j<=Math.min(sibs.length-1,idx+2); j++){
                if(j===idx) continue;
                const s=sibs[j];
                add((s.innerText||s.textContent||''), 4);
              }
            }
          }

          candidates.sort((a,b)=>b.score-a.score || a.len-b.len);
          const best=candidates.find(x=>/(?:F1|F2)/.test(x.t) && /(?:\d{1,2})R|発走|締切|投票|結果|終了/.test(x.t));
          return (best||candidates[0]||{t:''}).t;
        }""") or ""
    except Exception:
        return ""


def _venue_meta(page, slug, stop_event=None):
    """トップで級が取れない時だけ、公開の競輪場ページから本日の級/開催区分を補完する。"""
    url=f"{BASE}/keirin/{slug}"
    text=goto_board(page,url,stop_event)
    # 本日のレースの塊だけを優先し、「今月のレース」以降の別開催を混ぜない。
    local=text
    m=re.search(r"本日のレース(?P<body>.*?)(?:今月のレース|競輪場情報|$)", text, re.S)
    if m:
        local=m.group('body')
    grade=parse_grade(local)
    session=_session(local)
    return grade,session


def discover_today_board(page,today,stop_event):
    """
    WINTICKET競輪トップ /keirin から本日開催場を取得する。

    v2.3:
    - F1/F2を同一祖先DOMに必須としない。
    - 「本日開催中の競輪場」に付く ref=top-page-recent-race-venue のリンクを第一候補にする。
    - それが取れない場合は「開催中のレース」周辺の場名リンクへフォールバック。
    - F1/F2がトップで取れない場だけ公開の競輪場ページで補完する。
    """
    goto_board(page, f"{BASE}/keirin", stop_event)

    discovered=[]
    seen=set()

    # もっとも安定: トップ上部「本日開催中の競輪場」専用リンク。
    try:
        links=page.locator('a[href*="top-page-recent-race-venue"]')
        for i in range(links.count()):
            _check_stop(stop_event)
            a=links.nth(i)
            href=a.get_attribute('href') or ''
            m=re.search(r"/keirin/([^/?#]+)",href)
            if not m: continue
            slug=m.group(1)
            venue=next((name for name,sl in VENUES.items() if sl==slug), '')
            if not venue or slug in seen: continue
            seen.add(slug)
            discovered.append({"venue":venue,"slug":slug,"text":_live_card_info_from_anchor(a)})
    except Exception as e:
        _log(f"BOARD_PRIMARY_LINKS_FAIL error={type(e).__name__}:{e}")

    # フォールバック: 専用refが変わっても、開催中のレース周辺から場名を拾う。
    if not discovered:
        try:
            data=page.evaluate(r"""(venueMap)=>{
              const out=[];
              const headings=[...document.querySelectorAll('h1,h2,h3,div,span')]
                .filter(e=>(e.innerText||'').trim()==='開催中のレース');
              let root=headings[0]||document.body;
              if(root!==document.body){
                for(let k=0;k<4 && root.parentElement;k++) root=root.parentElement;
              }
              const anchors=[...root.querySelectorAll('a[href]')];
              for(const a of anchors){
                const href=a.getAttribute('href')||'';
                const mm=href.match(/\/keirin\/([^/?#]+)/);
                if(!mm) continue;
                const slug=mm[1];
                const venue=Object.keys(venueMap).find(v=>venueMap[v]===slug);
                if(!venue) continue;
                const label=(a.innerText||a.textContent||'').trim();
                if(label!==venue && label!==venue+'競輪' && !label.includes(venue)) continue;
                let n=a, text='';
                for(let j=0;j<10&&n;j++,n=n.parentElement){
                  const t=(n.innerText||n.textContent||'').trim();
                  if(t.length<900 && (/(?:\d{1,2})R/.test(t)||/発走|投票|結果|終了/.test(t))){text=t;break;}
                }
                out.push({venue,slug,text});
              }
              return out;
            }""", VENUES) or []
            for x in data:
                if x.get('slug') not in seen:
                    seen.add(x.get('slug'))
                    discovered.append(x)
        except Exception as e:
            _log(f"BOARD_FALLBACK_LINKS_FAIL error={type(e).__name__}:{e}")

    if not discovered:
        raise RuntimeError("競輪トップから本日の開催場リンクを取得できません")

    # トップで取れる情報を先に採用。
    out=[]
    missing=[]
    for item in discovered:
        text=item.get('text','') or ''
        grade=parse_grade(text)
        session=_session_from_live_card(text)
        row={"venue":item['venue'],"slug":item['slug'],"grade":grade or "?","session":session}
        out.append(row)
        if grade not in ('F1','F2'):
            missing.append(row)

    # 級が見えない時だけ各場ページへ。1場失敗しても一覧全体は捨てない。
    if missing:
        _log(f"BOARD_META_FALLBACK count={len(missing)}")
    for row in missing:
        _check_stop(stop_event)
        try:
            g,sess=_venue_meta(page,row['slug'],stop_event)
            if g in ('F1','F2'):
                row['grade']=g
            # 場ページに明示的な開催区分がある場合は推定より優先。
            if sess in ('M','D','N','MN'):
                row['session']=sess
        except StopRequested:
            raise
        except Exception as e:
            _log_venue(row['venue'],'META_FALLBACK_FAIL',error=f"{type(e).__name__}:{e}")

    # 1件も級が取れなかった場合だけエラー。開催場自体は取れているので原因を分ける。
    if not any(x.get('grade') in ('F1','F2') for x in out):
        raise RuntimeError("本日の開催場は取得できましたがF1/F2を取得できません")
    return out

def get_today_races(page,slug,today,stop_event):
    goto(page,f"{BASE}/keirin/{slug}/raceresult/{today.year}{today.month:02d}",stop_event)
    rows=[];seen=set();links=page.locator("a[href*='/raceresult/']")
    for i in range(links.count()):
        href=links.nth(i).get_attribute("href") or ""; info=race_info(href)
        if not info:continue
        sid,day,race,actual=info
        if actual!=today or (sid,day,race) in seen:continue
        seen.add((sid,day,race)); rows.append({"sid":sid,"day":day,"race":race})
    return sorted(rows,key=lambda x:x["race"])

def _progress(progress,counters,**kw):
    kw["counters"]=dict(counters);progress(kw)

def load_today_board(progress=None,stop_event=None):
    progress=progress or (lambda x:None); today=_today_jst(); c={"venues_found":0,"errors":0}
    browser=context=page=None
    try:
      with sync_playwright() as p:
        try:
          browser,context,page=_open_session(p)
          _progress(progress,c,phase="board",current="今日の開催場を取得中",detail="1ページだけでF1/F2・開催区分を確認")
          board=discover_today_board(page,today,stop_event);c["venues_found"]=len(board)
          _progress(progress,c,phase="select",current="開催場を選択",detail="検索したい場にチェック",venues_info=board)
          return {"venues_info":board,"counters":c,"errors":[],"stopped":False}
        finally:
          _close_session(browser,context,page)
    except StopRequested:
      return {"venues_info":[],"counters":c,"errors":[],"stopped":True}
    except Exception as e:
      c["errors"] += 1
      msg=f"開催場取得: {type(e).__name__}: {e}"
      _log(f"BOARD_LOAD_FAIL error={type(e).__name__}:{e}")
      _progress(progress,c,phase="error",current="開催場取得エラー",detail=msg,venues_info=[])
      return {"venues_info":[],"counters":c,"errors":[msg],"stopped":False}

def scan_selected(selected,board,progress=None,stop_event=None):
    progress=progress or (lambda x:None);today=_today_jst();selected=set(selected or [])
    infos={x["slug"]:x for x in board or []};jobs=[x for x in (board or []) if x.get("slug") in selected]
    c={"venues_found":len(board or []),"selected_venues":len(jobs),"checked_races":0,"girls_l":0,"unpublished":0,"f2_races":0,"star2":0,"line_target":0,"order_target":0,"matched":0,"errors":0,"browser_restarts":0}
    result={"matches":[],"errors":[],"skipped":[],"counters":c,"venues_info":board or [],"stopped":False}
    try:
      with sync_playwright() as p:
        for vi,info in enumerate(jobs,1):
          _check_stop(stop_event)
          venue,slug=info["venue"],info["slug"]
          browser=context=page=None
          try:
            browser,context,page=_open_session(p)
            _progress(progress,c,phase="races",current=f"{venue} ({vi}/{len(jobs)})",detail=f"選択{len(jobs)}場中{vi}場目",matches=list(result["matches"]),venues_info=board)
            try:
              rows=get_today_races(page,slug,today,stop_event)
            except StopRequested:
              raise
            except Exception as e:
              c["errors"]+=1;result["errors"].append(f"{venue}: {type(e).__name__}: {e}")
              _log_venue(venue,"RACE_LIST_FAIL",error=f"{type(e).__name__}:{e}")
              continue

            for row in rows:
              _check_stop(stop_event);race=row["race"];url=f"{BASE}/keirin/{slug}/predictions/{row['sid']}/{row['day']}/{race}"
              _progress(progress,c,phase="races",current=f"{venue} {race}R",detail=f"{venue}を巡回中",matches=list(result["matches"]),venues_info=board)
              try:
                goto(page,url,stop_event);c["checked_races"]+=1
                grade=detect_grade_from_header(page,race)
                if grade=="L":c["girls_l"]+=1;continue
                c["f2_races"]+=1;star,_=confidence_dom(page)
                if star is None:page.wait_for_timeout(180);star,_=confidence_dom(page)
                if star is None:c["unpublished"]+=1;continue
                if star!=TARGET_STAR:continue
                c["star2"]+=1;line,groups=extract_line(page,stop_event)
                if line not in TARGET_LINES:continue
                c["line_target"]+=1;hon,tai,ana,ren=extract_ai_marks(page)
                if hon in ("",None) or tai in ("",None):continue
                order3=three_line_order(groups,hon,tai,ana,ren)
                if order3 not in TARGET_ORDERS:continue
                c["order_target"]+=1
                result["matches"].append({
                    "venue": venue,
                    "slug": slug,
                    "race": race,
                    "session": info.get("session", "?"),
                    "grade": info.get("grade", "?"),
                    "star": "星2",
                    "line": line,
                    "three_order": order3,
                    "order": line_mark_order(groups,hon,tai,ana,ren),
                    "prediction_url": url,
                    "winticket_url": url,
                    "event_url": f"{BASE}/keirin/{slug}/racecard/{row['sid']}",
                }); c["matched"] += 1
                _progress(progress,c,phase="races",current=f"{venue} {race}R",detail="条件一致",matches=list(result["matches"]),venues_info=board)
              except StopRequested:
                raise
              except Exception as e:
                c["errors"]+=1;result["errors"].append(f"{venue} {race}R: {type(e).__name__}: {e}")
                _log_race(venue,race,"ERROR",error=f"{type(e).__name__}:{e}")
                # If Chromium/page died, replace the whole session before the next race.
                # We intentionally do NOT retry the same URL, preserving the no-duplicate-access rule.
                if _page_unhealthy(page) or "Target page, context or browser has been closed" in str(e) or "Browser has been closed" in str(e):
                  _close_session(browser,context,page); browser=context=page=None
                  try:
                    browser,context,page=_open_session(p); c["browser_restarts"]+=1
                    _log_venue(venue,"BROWSER_RECOVERED",after_race=race)
                  except Exception as rexc:
                    c["errors"]+=1; result["errors"].append(f"{venue} ブラウザ再起動失敗: {type(rexc).__name__}: {rexc}")
                    _log_venue(venue,"BROWSER_RECOVERY_FAIL",error=f"{type(rexc).__name__}:{rexc}")
                    break
          finally:
            _close_session(browser,context,page)

        _progress(progress,c,phase="done",current="検索完了",detail="選択した場の検索完了",matches=list(result["matches"]),venues_info=board)
        return result
    except StopRequested:
      result["stopped"]=True;_progress(progress,c,phase="stopped",current="途中停止",detail="停止しました",matches=list(result["matches"]),venues_info=board);return result