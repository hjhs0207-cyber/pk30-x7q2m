"""포켓몬카드 30주년 팩 편의점 재고 알림.

GitHub Actions가 10분마다 실행해요.
config.json의 감시 위치(반경 1km)에서 4개 편의점 재고를 확인하고,
"재고 없음 → 재고 생김"으로 바뀐 매장이 있으면 Gmail로 알려줘요.

재고 데이터는 오픈소스 프로젝트 daiso-mcp의 공개 API(mcp.aka.page)를 통해 받아요.
편의점 공식 API가 아니라서 예고 없이 막힐 수 있어요.
"""

import hashlib
import json
import math
import os
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
CHAIN_LABEL = {"gs25": "GS25", "cu": "CU", "seveneleven": "세븐일레븐", "emart24": "이마트24"}
PRODUCT_CACHE_HOURS = 12
UNCERTAIN_QTY = 999        # 이 이상(이마트24 9999)은 실제 수량이 아니라고 봐요
ALERT_COOLDOWN_HOURS = 6   # 같은 매장은 이 시간 안에 다시 알리지 않아요
HISTORY_DAYS = 90          # 입고 기록 보관 기간
PACK_HISTORY_HOURS = 3     # 다른 확장팩은 이 간격으로만 확인해서 기록해요 (알림 없음)
PACK_TIME_LIMIT_SEC = 600  # 확장팩 기록은 최대 10분만
DEBUG = {}  # 조회 실패 원인을 docs/debug.json에 남겨요 (로그 대신 확인용)


# ───────────── 공통 도구 ─────────────
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api(base, path, params, retries=1, timeout=60):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{base.rstrip('/')}{path}?{qs}"
    err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pokecard30-alert/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            if not body.get("success", False):
                e = body.get("error") or {}
                raise RuntimeError(f"{e.get('code', 'API_ERROR')}: {e.get('message', '')}")
            return body.get("data") or {}
        except urllib.error.HTTPError as e:
            err = RuntimeError(f"HTTP {e.code}")
            if e.code in (400, 404, 429):
                break
        except Exception as e:
            err = e
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(str(err))


def norm(t):
    return "".join(ch for ch in (t or "") if ch.isalnum()).lower()


def name_matches(name, target):
    n, t = norm(name), norm(target)
    if not n:
        return False
    return n == t or t in n or n in t


def num(v):
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def store_distance(s, loc, center_query=True):
    """매장 좌표로 거리 계산. 좌표가 없으면 API가 준 distanceM 사용(같은 중심으로 조회한 경우만)."""
    lat = s.get("latitude") or s.get("lat")
    lng = s.get("longitude") or s.get("lng")
    try:
        lat, lng = float(lat), float(lng)
        if 33 < lat < 39 and 124 < lng < 132:
            return haversine_m(loc["lat"], loc["lng"], lat, lng)
    except (TypeError, ValueError):
        pass
    if center_query and s.get("distanceM") is not None:
        return float(s["distanceM"])
    return None


# ───────────── 1) 상품 코드 찾기 ─────────────
SEARCH_FALLBACKS = ["포켓몬카드 30주년", "포켓몬 30주년", "포켓몬카드", "포켓몬"]


def search_items(base, chain, kw):
    if chain == "gs25":
        d = api(base, "/api/gs25/products", {"keyword": kw, "limit": 50})
        return [(p.get("itemCode"), p.get("itemName") or p.get("shortItemName")) for p in d.get("products", [])]
    if chain == "cu":
        d = api(base, "/api/cu/inventory", {"keyword": kw, "size": 50, "storeCheck": "false"})
        return [(p.get("itemCode"), p.get("itemName")) for p in (d.get("inventory") or {}).get("items", [])]
    if chain == "seveneleven":
        d = api(base, "/api/seveneleven/products", {"query": kw, "size": 50})
        return [(p.get("itemCode"), p.get("itemName")) for p in d.get("products", [])]
    d = api(base, "/api/emart24/products", {"keyword": kw, "pageSize": 50})
    return [(p.get("pluCd"), p.get("goodsName")) for p in d.get("products", [])]


def find_product(base, chain, target):
    """앱 등록명으로 먼저 찾고, 안 나오면 더 단순한 검색어로 다시 찾아요."""
    errors, seen = [], []
    for kw in [target] + SEARCH_FALLBACKS:
        try:
            items = [(c, n) for c, n in search_items(base, chain, kw) if c and n]
        except Exception as e:
            errors.append(f"{kw}: {e}")
            continue
        seen += [n for _, n in items[:5]]
        for code, name in items:  # 1순위: 앱 등록명과 일치
            if name_matches(name, target):
                return {"id": str(code), "name": name}
        for code, name in items:  # 2순위: 30주년 포켓몬 상품
            if "30주년" in name and "포켓몬" in name:
                return {"id": str(code), "name": name}
        time.sleep(0.5)
    DEBUG.setdefault("candidates", {})[chain] = {"seen": seen[:15], "errors": errors}
    if errors and not seen:
        raise RuntimeError("; ".join(errors[:2]))
    return None


def barcode_product(cfg, chain, target, now):
    """검색이 안 되면 공통 바코드로 재고를 바로 조회해요. (다음 실행 때 다시 검색 시도)"""
    print(f"[상품] {CHAIN_LABEL[chain]}: 바코드 {cfg['barcode']}로 대신 조회해요")
    return {"id": cfg["barcode"], "name": target, "target": target, "fallback": True, "at": now.isoformat(timespec="seconds")}


def resolve_products(cfg, cache, now):
    out = {}
    for chain, target in cfg["products"].items():
        c = cache.get(chain) or {}
        fresh = c.get("at") and now - datetime.fromisoformat(c["at"]) < timedelta(hours=PRODUCT_CACHE_HOURS)
        if fresh and c.get("id") and c.get("target") == target:
            out[chain] = c
            continue
        try:
            p = find_product(cfg["apiBase"], chain, target)
            if p:
                out[chain] = {**p, "target": target, "at": now.isoformat(timespec="seconds")}
                print(f"[상품] {CHAIN_LABEL[chain]}: {p['name']} ({p['id']})")
            else:
                print(f"[상품] {CHAIN_LABEL[chain]}: '{target}' 상품을 못 찾았어요")
                out[chain] = c if c.get("id") and not c.get("fallback") else barcode_product(cfg, chain, target, now)
        except Exception as e:
            print(f"[상품] {CHAIN_LABEL[chain]} 검색 실패: {e}")
            DEBUG.setdefault("productErrors", {})[chain] = str(e)
            out[chain] = c if c.get("id") and not c.get("fallback") else barcode_product(cfg, chain, target, now)
    return out


# ───────────── 1-2) 사이트용 다른 확장팩 상품 코드 (알림 X) ─────────────
PACK_CACHE_HOURS = 12
BROAD_TERMS = ["포켓몬카드", "포켓몬 카드", "포켓몬 확장팩"]
NOT_PACK = ["슬리브", "바인더", "파일", "케이스", "띠부", "교통카드", "티머니", "캐시비", "키링", "스티커", "젤리", "빵", "음료", "과자"]
POKEMON_BARCODES = ("8800286", "8809945", "8809581")


def pack_candidates(items, keyword):
    k = norm(keyword)
    out = []
    for code, name in items:
        n = norm(name)
        if k not in n or any(w in name for w in NOT_PACK):
            continue
        if "포켓몬" in name or str(code).startswith(POKEMON_BARCODES):
            out.append((code, name))
    out.sort(key=lambda x: ("박스" in x[1], len(x[1])))  # 낱팩 우선, 박스는 낱팩이 없을 때만
    return out


def resolve_packs(cfg, now):
    """12시간마다 확장팩별·편의점별 상품 코드를 찾아 docs/packs.json에 저장해요."""
    path = ROOT / "docs" / "packs.json"
    old = load_json(path, {})
    packs = cfg.get("extraPacks", [])
    if old.get("at") and now - datetime.fromisoformat(old["at"]) < timedelta(hours=PACK_CACHE_HOURS) \
            and old.get("packs") == packs:
        return
    base = cfg["apiBase"]
    found = {p: {} for p in packs}
    errors = {}
    for chain in CHAIN_LABEL:
        pool = []
        for kw in BROAD_TERMS:
            try:
                pool += [(c, n) for c, n in search_items(base, chain, kw) if c and n]
            except Exception as e:
                errors[f"{chain}|{kw}"] = str(e)
            time.sleep(0.5)
        for pack in packs:
            cands = pack_candidates(pool, pack)
            if not cands:
                try:
                    cands = pack_candidates([(c, n) for c, n in search_items(base, chain, pack) if c and n], pack)
                except Exception as e:
                    errors[f"{chain}|{pack}"] = str(e)
                time.sleep(0.5)
            if cands:
                code, name = cands[0]
                found[pack][chain] = {"id": str(code), "name": name}
            elif (old.get("items") or {}).get(pack, {}).get(chain) and not old["items"][pack][chain].get("fallback"):
                found[pack][chain] = old["items"][pack][chain]  # 이번에 못 찾으면 예전 값 유지
            elif chain == "seveneleven":
                # 세븐일레븐 검색 서버가 자주 죽어서, 사이트가 확장팩 이름으로 바로 재고를 찾게 해요 (이름 확인은 사이트에서)
                found[pack][chain] = {"id": "", "name": pack, "fallback": True}
    save_json(path, {"at": now.isoformat(timespec="seconds"), "packs": packs, "items": found, "errors": errors})
    for pack in packs:
        got = ", ".join(f"{CHAIN_LABEL[c]}={v['name']}" for c, v in found[pack].items()) or "없음"
        print(f"[확장팩] {pack}: {got}")


# ───────────── 2) 재고 조회 ─────────────
def row(chain, code, name, qty, dist):
    return {"chain": chain, "code": str(code), "name": name or "", "qty": qty, "distanceM": round(dist)}


def stock_by_center(cfg, chain, product, loc):
    base, limit, radius = cfg["apiBase"], cfg["storeLimit"], cfg["radiusM"]
    if chain == "gs25":
        d = api(base, "/api/gs25/inventory", {"itemCode": product["id"], "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit})
        stores, code_key, qty_key = (d.get("inventory") or {}).get("stores", []), "storeCode", "realStockQuantity"
    elif chain == "cu":
        d = api(base, "/api/cu/inventory", {"keyword": product["name"], "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit, "size": 20})
        ns = d.get("nearbyStores") or {}
        if ns.get("stockItemCode") and str(ns["stockItemCode"]) != product["id"]:
            raise RuntimeError(f"CU 상품 불일치: {ns.get('stockItemName')}")
        stores, code_key, qty_key = ns.get("stores", []), "storeCode", "stock"
    else:  # emart24: 좌표만으로는 주변 매장을 못 찾아서 동네 이름도 같이 보내요
        stores, seen, errors = [], set(), []
        kws = loc.get("sevenKeywords", [])
        for kw in kws:
            try:
                d = api(base, "/api/emart24/inventory", {"pluCd": product["id"], "storeKeyword": kw, "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit}, retries=2)
            except Exception as e:  # 동네 하나가 실패해도 나머지 결과는 살려요
                errors.append(f"{kw}: {e}")
                continue
            for st in (d.get("inventory") or {}).get("stores") or d.get("stores", []):
                if st.get("bizNo") not in seen:
                    seen.add(st.get("bizNo"))
                    stores.append(st)
        if errors:
            DEBUG.setdefault("emart24KeywordErrors", {})[loc["name"]] = errors
            if len(errors) == len(kws):
                raise RuntimeError("; ".join(errors))
        code_key, qty_key = "bizNo", "bizQty"
    out = []
    for s in stores:
        dist = store_distance(s, loc)
        if dist is not None and dist <= radius and s.get(code_key):
            out.append(row(chain, s[code_key], s.get("storeName"), num(s.get(qty_key)), dist))
    return out


def stock_seveneleven(cfg, product, loc):
    """동네 이름별로 조회해요. 중계 서버가 502를 자주 내서 재시도하고, 일부 동네만 실패하면 나머지 결과는 살려요."""
    out, errors = {}, []
    for kw in loc.get("sevenKeywords", []):
        try:
            d = api(cfg["apiBase"], "/api/seveneleven/inventory",
                    {"keyword": product["name"], "storeKeyword": kw, "storeLimit": 100}, retries=3)
            if not d.get("stockAvailable", True):
                msg = (d.get("stockError") or {}).get("message") or d.get("note") or "재고 확인 실패"
                if "확인하지 못했습니다" in msg:  # 앱에서 '재고부족'으로 나오는 경우 → 재고 없음으로 봐요
                    DEBUG.setdefault("sevenKeywordStats", {})[f"{loc['name']}|{kw}"] = "수량 미제공 → 재고 없음으로 처리"
                    continue
                raise RuntimeError(msg)
            p = d.get("product") or {}
            if product.get("fallback"):
                if p.get("itemName") and "30주년" not in p["itemName"]:
                    raise RuntimeError(f"상품 불일치: {p.get('itemName')}")
            elif p.get("itemCode") and str(p["itemCode"]) != product["id"]:
                raise RuntimeError("상품 불일치")
        except Exception as e:
            errors.append(f"{kw}: {e}")
            continue
        got = (d.get("inventory") or {}).get("stores", [])
        near = [x for x in got if (store_distance(x, loc, center_query=False) or 1e9) <= cfg["radiusM"]]
        DEBUG.setdefault("sevenKeywordStats", {})[f"{loc['name']}|{kw}"] = f"매장 {len(got)}곳 중 1km 안 {len(near)}곳"
        for s in got:
            dist = store_distance(s, loc, center_query=False)
            if dist is not None and dist <= cfg["radiusM"] and s.get("storeCode"):
                out[str(s["storeCode"])] = row("seveneleven", s["storeCode"], s.get("storeName"), num(s.get("stockQuantity")), dist)
    if errors:
        DEBUG.setdefault("sevenKeywordErrors", {})[loc["name"]] = errors
        if len(errors) == len(loc.get("sevenKeywords", [])):
            raise RuntimeError("; ".join(errors))
    return list(out.values())


def collect(cfg, products):
    """결과: {loc_id: {chain: [rows] 또는 None(조회 실패)}}"""
    results = {}
    for loc in cfg["locations"]:
        results[loc["id"]] = {}
        for chain in CHAIN_LABEL:
            if chain not in products:
                results[loc["id"]][chain] = None
                continue
            try:
                if chain == "seveneleven":
                    rows = stock_seveneleven(cfg, products[chain], loc)
                else:
                    rows = stock_by_center(cfg, chain, products[chain], loc)
                results[loc["id"]][chain] = rows
                have = sum(1 for r in rows if (r["qty"] or 0) > 0)
                print(f"[재고] {loc['name']} {CHAIN_LABEL[chain]}: 1km 안 {len(rows)}곳, 재고 있음 {have}곳")
            except Exception as e:
                results[loc["id"]][chain] = None
                print(f"[재고] {loc['name']} {CHAIN_LABEL[chain]} 조회 실패: {e}")
                DEBUG.setdefault("stockErrors", {})[f"{loc['name']}|{chain}"] = str(e)
            time.sleep(0.5)
    return results


# ───────────── 3) 비교 & 메일 ─────────────
def send_mail(to, subject, body):
    addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        print(f"[메일] GMAIL_ADDRESS / GMAIL_APP_PASSWORD가 없어서 메일을 건너뛰어요. (받는 사람 {to})\n" + body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, addr, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw.replace(" ", ""))
        s.send_message(msg)
    print(f"[메일] 보냄 → {to[:3]}***: {subject}")


def fmt_rows(rows):
    return "\n".join(f"- {CHAIN_LABEL[r['chain']]} {r['name']} : {'있음(수량 미표시)' if r['qty'] >= 999 else str(r['qty']) + '개'}" for r in rows)


def recipients(cfg):
    """받는 사람별 알림 위치. 현준(GMAIL_ADDRESS)은 config의 ownerLocations,
    친구는 GitHub Secret FRIEND_EMAILS (형식: 메일:위치id,위치id;메일:위치id) — 메일 주소를 공개 코드에 안 남기려고요."""
    out = []
    owner = os.environ.get("GMAIL_ADDRESS")
    all_ids = [l["id"] for l in cfg["locations"]]
    if owner:
        out.append((owner, cfg.get("ownerLocations") or all_ids))
    for part in (os.environ.get("FRIEND_EMAILS") or "").split(";"):
        if ":" in part:
            mail, locs = part.split(":", 1)
            ids = [x.strip() for x in locs.split(",") if x.strip() in all_ids]
            if mail.strip() and ids:
                out.append((mail.strip(), ids))
    return out


# ───────────── 4) 다른 확장팩 입고 기록 (3시간마다, 알림 없음) ─────────────
def emart24_near_codes(cfg, loc, cache, now):
    """위치별 1km 안 이마트24 매장 코드 (하루 한 번만 새로 찾아요)."""
    c = cache.get(loc["id"]) or {}
    if c.get("at") and now - datetime.fromisoformat(c["at"]) < timedelta(hours=24) and c.get("stores"):
        return c["stores"]
    found = {}
    for kw in loc.get("sevenKeywords", []):
        try:
            d = api(cfg["apiBase"], "/api/emart24/stores", {"keyword": kw, "lat": loc["lat"], "lng": loc["lng"], "limit": 50}, retries=0, timeout=20)
        except Exception:
            continue
        for st in d.get("stores", []):
            dist = store_distance(st, loc)
            if dist is not None and dist <= cfg["radiusM"] and st.get("storeCode"):
                found[str(st["storeCode"])] = {"name": st.get("storeName", ""), "d": round(dist)}
    if found:
        cache[loc["id"]] = {"at": now.isoformat(timespec="seconds"), "stores": found}
    return found or c.get("stores") or {}


def pack_stock(cfg, chain, prod, loc, e24):
    """확장팩 하나·위치 하나·편의점 하나의 1km 안 재고 → [row]"""
    if chain == "emart24":
        if not e24:
            return []
        d = api(cfg["apiBase"], "/api/emart24/inventory", {"pluCd": prod["id"], "bizNoArr": ",".join(e24), "storeLimit": len(e24)}, retries=0, timeout=25)
        qty = {str(s.get("bizNo")): num(s.get("bizQty")) for s in (d.get("inventory") or {}).get("stores") or d.get("stores", [])}
        return [row("emart24", code, v["name"], qty.get(code, 0), v["d"]) for code, v in e24.items()]
    if chain == "seveneleven":
        out = {}
        for kw in loc.get("sevenKeywords", []):
            try:
                d = api(cfg["apiBase"], "/api/seveneleven/inventory", {"keyword": prod["name"], "storeKeyword": kw, "storeLimit": 100}, retries=0, timeout=25)
            except Exception:
                continue
            got = (d.get("product") or {}).get("itemName") or ""
            if prod.get("fallback") and (not got or norm(prod["name"]) not in norm(got)):
                return []
            if not d.get("stockAvailable", True):
                continue
            for st in (d.get("inventory") or {}).get("stores", []):
                dist = store_distance(st, loc, center_query=False)
                if dist is not None and dist <= cfg["radiusM"] and st.get("storeCode"):
                    out[str(st["storeCode"])] = row("seveneleven", st["storeCode"], st.get("storeName"), num(st.get("stockQuantity")), dist)
        return list(out.values())
    base, limit = cfg["apiBase"], cfg["storeLimit"]
    if chain == "gs25":
        d = api(base, "/api/gs25/inventory", {"itemCode": prod["id"], "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit}, retries=0, timeout=25)
        stores, ck, qk = (d.get("inventory") or {}).get("stores", []), "storeCode", "realStockQuantity"
    else:  # cu
        d = api(base, "/api/cu/inventory", {"keyword": prod["name"], "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit, "size": 20}, retries=0, timeout=25)
        ns = d.get("nearbyStores") or {}
        if ns.get("stockItemCode") and str(ns["stockItemCode"]) != prod["id"]:
            raise RuntimeError("CU 상품 불일치")
        stores, ck, qk = ns.get("stores", []), "storeCode", "stock"
    out = []
    for st in stores:
        dist = store_distance(st, loc)
        if dist is not None and dist <= cfg["radiusM"] and st.get(ck):
            out.append(row(chain, st[ck], st.get("storeName"), num(st.get(qk)), dist))
    return out


def record_pack_history(cfg, state, history, now):
    last = state.get("packHistoryAt")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=PACK_HISTORY_HOURS) - timedelta(minutes=10):
        return
    packs = (load_json(ROOT / "docs" / "packs.json", {}).get("items") or {})
    if not packs:
        return
    e24_cache = state.setdefault("emart24Near", {})
    e24 = {loc["id"]: emart24_near_codes(cfg, loc, e24_cache, now) for loc in cfg["locations"]}
    jobs = [(pack, chain, prod, loc) for pack, chains in packs.items() for chain, prod in chains.items()
            if prod and (prod.get("id") or chain == "seveneleven") for loc in cfg["locations"]]

    deadline = time.monotonic() + PACK_TIME_LIMIT_SEC

    def run(job):
        pack, chain, prod, loc = job
        if time.monotonic() > deadline:
            return job, TimeoutError("시간 초과로 건너뜀")
        try:
            return job, pack_stock(cfg, chain, prod, loc, e24[loc["id"]])
        except Exception as e:
            return job, e

    old = state.get("packQty", {})
    new = dict(old)
    stamp, fails, logged = now.isoformat(timespec="minutes"), 0, set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        for (pack, chain, prod, loc), rows in ex.map(run, jobs):
            if isinstance(rows, Exception):
                fails += 1
                continue
            for r in rows:
                if r["qty"] is None or r["qty"] >= UNCERTAIN_QTY:
                    continue
                key = f"{pack}|{chain}|{r['code']}"
                was = old.get(key)
                new[key] = r["qty"]
                if key in logged or was is None:  # 처음 본 매장은 기준값만 저장
                    continue
                if was <= 0 < r["qty"] or (was > 0 and r["qty"] == 0):
                    history.append({"t": stamp, "loc": loc["name"], "chain": chain, "store": r["name"],
                                    "qty": r["qty"] if r["qty"] > 0 else was,
                                    "type": "입고" if r["qty"] > 0 else "품절", "pack": pack})
                    logged.add(key)
    state["packQty"], state["packHistoryAt"] = new, now.isoformat(timespec="seconds")
    print(f"[확장팩 기록] {len(jobs)}건 확인, 실패 {fails}건, 새 기록 {len(logged)}건")
    DEBUG["packHistory"] = {"jobs": len(jobs), "fails": fails, "events": len(logged)}


def main():
    cfg = load_json(ROOT / "config.json", None)
    state = load_json(ROOT / "state.json", {})
    now = datetime.now(KST)

    cache = load_json(ROOT / "docs" / "products.json", {})
    products = resolve_products(cfg, cache, now)
    save_json(ROOT / "docs" / "products.json", products)
    try:
        resolve_packs(cfg, now)
    except Exception as e:
        print(f"[확장팩] 상품 코드 찾기 실패: {e}")

    results = collect(cfg, products)
    old_qty = state.get("qty", {})
    new_qty = dict(old_qty)
    # 이미 감시 중이던 위치만 '재고 생김'을 판단해요 (새로 추가된 위치는 이번엔 현재 상태만 기록)
    loc_started = set(state.get("locStarted") or ([l["id"] for l in cfg["locations"]] if "qty" in state else []))
    restocks = {}  # loc_id -> rows
    last_alert = dict(state.get("lastAlert") or {})
    hist_path = ROOT / "docs" / "history.json"
    history = load_json(hist_path, {}).get("events", [])
    stamp_iso, logged = now.isoformat(timespec="minutes"), set()
    for loc in cfg["locations"]:
        for chain, rows in results[loc["id"]].items():
            if rows is None:  # 조회 실패 → 이전 상태 유지, 알림 없음
                continue
            for r in rows:
                if r["qty"] is None:
                    continue
                key = f"{chain}|{r['code']}"
                if r["qty"] >= UNCERTAIN_QTY:
                    # 이마트24의 9999는 실제 수량이 아니라 '확인 불가'에 가까워서 (9999↔0을 오락가락함)
                    # 상태도 바꾸지 않고 알림도 보내지 않아요
                    continue
                was = old_qty.get(key, 0)
                if was >= UNCERTAIN_QTY:
                    was = 0
                new_qty[key] = r["qty"]
                if key not in logged:  # 입고 기록 (여러 위치에 겹치는 매장은 한 번만)
                    if was <= 0 < r["qty"] and key in old_qty:
                        history.append({"t": stamp_iso, "loc": loc["name"], "chain": chain, "store": r["name"],
                                        "qty": r["qty"], "type": "입고", "pack": "30주년"})
                        logged.add(key)
                    elif was > 0 and r["qty"] == 0:
                        history.append({"t": stamp_iso, "loc": loc["name"], "chain": chain, "store": r["name"],
                                        "qty": was, "type": "품절", "pack": "30주년"})
                        logged.add(key)
                if was <= 0 < r["qty"] and loc["id"] in loc_started:
                    last = last_alert.get(key)
                    if last and now - datetime.fromisoformat(last) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                        print(f"[알림] {CHAIN_LABEL[chain]} {r['name']}: {ALERT_COOLDOWN_HOURS}시간 안에 이미 알려서 건너뜀")
                        continue
                    restocks.setdefault(loc["id"], []).append(r)
    loc_started |= {l["id"] for l in cfg["locations"]}

    names = {l["id"]: l["name"] for l in cfg["locations"]}
    stamp = now.strftime("%m월 %d일 %H:%M")
    # 공개 저장소라 메일 주소 대신 알아볼 수 없게 바꾼 값(해시)만 저장해요
    h = lambda m: hashlib.sha256((m or "").strip().lower().encode()).hexdigest()[:16]
    owner = os.environ.get("GMAIL_ADDRESS")
    started = set(state.get("mailStarted") or ([h(owner)] if "qty" in state and owner else []))
    for to, loc_ids in recipients(cfg):
        if h(to) not in started:  # 처음 받는 사람: 현재 재고를 알려주는 시작 메일
            parts = []
            for lid in loc_ids:
                have = [r for rows in results[lid].values() if rows for r in rows if (r["qty"] or 0) > 0]
                parts.append(f"[{names[lid]} 1km]\n" + (fmt_rows(have) if have else "- 지금은 재고 있는 곳이 없어요"))
            send_mail(to, "[포켓몬 30주년] 재고 알림을 시작했어요",
                      f"{stamp} 기준 현재 재고예요. 앞으로 재고가 새로 생기면 알려드릴게요.\n\n" + "\n\n".join(parts))
            started.add(h(to))
            continue
        seen, parts, total = set(), [], []
        for lid in loc_ids:
            uniq = [r for r in restocks.get(lid, []) if (r["chain"], r["code"]) not in seen]
            seen.update((r["chain"], r["code"]) for r in uniq)
            if uniq:
                parts.append(f"[{names[lid]} 1km]\n" + fmt_rows(uniq))
                total += uniq
        if total:
            first = total[0]
            more = f" 외 {len(total) - 1}곳" if len(total) > 1 else ""
            send_mail(to, f"[포켓몬 30주년] 재고 생김: {CHAIN_LABEL[first['chain']]} {first['name']}{more}",
                      f"{stamp} 기준 재고가 새로 생긴 매장이에요.\n\n" + "\n\n".join(parts))
    if not restocks:
        print("[알림] 새로 재고가 생긴 매장이 없어요.")
    for rows in restocks.values():
        for r in rows:
            last_alert[f"{r['chain']}|{r['code']}"] = now.isoformat(timespec="seconds")
    keep = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    history = [e for e in history if e["t"] >= keep[:16]]
    save_json(hist_path, {"since": load_json(hist_path, {}).get("since") or "2026-10-31T21:00+09:00",
                          "updatedAt": stamp_iso, "events": history})
    cutoff = now - timedelta(days=2)
    last_alert = {k: v for k, v in last_alert.items() if datetime.fromisoformat(v) > cutoff}

    state = {"qty": new_qty, "lastRun": now.isoformat(timespec="seconds"),
             "locStarted": sorted(loc_started), "mailStarted": sorted(started), "lastAlert": last_alert}
    save_json(ROOT / "state.json", state)
    save_json(ROOT / "docs" / "debug.json", {"at": now.isoformat(timespec="seconds"), **DEBUG})
    save_json(ROOT / "docs" / "status.json", {
        "checkedAt": now.isoformat(timespec="seconds"),
        "locations": {names[lid]: {CHAIN_LABEL[c]: ("조회 실패" if rows is None else rows) for c, rows in res.items()}
                      for lid, res in results.items()},
    })


def main_packs():
    """확장팩 입고 기록만 (packs.yml이 3시간마다 실행). 30주년 알림과 파일을 나눠서 서로 안 막혀요."""
    cfg = load_json(ROOT / "config.json", None)
    now = datetime.now(KST)
    pstate = load_json(ROOT / "pack_state.json", {})
    hpath = ROOT / "docs" / "history_packs.json"
    events = load_json(hpath, {}).get("events", [])
    pstate.pop("packHistoryAt", None)  # 이 작업은 예약된 때만 돌아서 간격 확인은 생략
    record_pack_history(cfg, pstate, events, now)
    keep = (now - timedelta(days=HISTORY_DAYS)).isoformat()[:16]
    save_json(hpath, {"since": load_json(hpath, {}).get("since") or now.isoformat(timespec="minutes"),
                      "updatedAt": now.isoformat(timespec="minutes"), "events": [e for e in events if e["t"] >= keep],
                      "stats": DEBUG.get("packHistory")})
    save_json(ROOT / "pack_state.json", pstate)


if __name__ == "__main__":
    sys.exit(main_packs() if "--packs" in sys.argv else main())
