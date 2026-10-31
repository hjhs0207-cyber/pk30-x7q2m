"""포켓몬카드 30주년 팩 편의점 재고 알림.

GitHub Actions가 10분마다 실행해요.
config.json의 감시 위치(반경 1km)에서 4개 편의점 재고를 확인하고,
"재고 없음 → 재고 생김"으로 바뀐 매장이 있으면 Gmail로 알려줘요.

재고 데이터는 오픈소스 프로젝트 daiso-mcp의 공개 API(mcp.aka.page)를 통해 받아요.
편의점 공식 API가 아니라서 예고 없이 막힐 수 있어요.
"""

import json
import math
import os
import smtplib
import sys
import time
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


def api(base, path, params, retries=1):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{base.rstrip('/')}{path}?{qs}"
    err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pokecard30-alert/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
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
        stores, seen = [], set()
        for kw in loc.get("sevenKeywords", []):
            d = api(base, "/api/emart24/inventory", {"pluCd": product["id"], "storeKeyword": kw, "lat": loc["lat"], "lng": loc["lng"], "storeLimit": limit})
            for st in (d.get("inventory") or {}).get("stores") or d.get("stores", []):
                if st.get("bizNo") not in seen:
                    seen.add(st.get("bizNo"))
                    stores.append(st)
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
def send_mail(subject, body):
    addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        print("[메일] GMAIL_ADDRESS / GMAIL_APP_PASSWORD가 없어서 메일을 건너뛰어요.\n" + body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, addr, addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw.replace(" ", ""))
        s.send_message(msg)
    print(f"[메일] 보냄: {subject}")


def fmt_rows(rows):
    return "\n".join(f"- {CHAIN_LABEL[r['chain']]} {r['name']} : {'있음(수량 미표시)' if r['qty'] >= 999 else str(r['qty']) + '개'}" for r in rows)


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
    first_run = "qty" not in state
    old_qty = state.get("qty", {})
    new_qty = dict(old_qty)
    restocks = {}  # loc_id -> rows
    for loc in cfg["locations"]:
        for chain, rows in results[loc["id"]].items():
            if rows is None:  # 조회 실패 → 이전 상태 유지, 알림 없음
                continue
            for r in rows:
                if r["qty"] is None:
                    continue
                key = f"{chain}|{r['code']}"
                was = old_qty.get(key, 0)
                new_qty[key] = r["qty"]
                if was <= 0 < r["qty"] and not first_run:
                    restocks.setdefault(loc["id"], []).append(r)

    names = {l["id"]: l["name"] for l in cfg["locations"]}
    stamp = now.strftime("%m월 %d일 %H:%M")
    if first_run:
        parts = []
        for loc in cfg["locations"]:
            have = [r for rows in results[loc["id"]].values() if rows for r in rows if (r["qty"] or 0) > 0]
            parts.append(f"[{loc['name']} 1km]\n" + (fmt_rows(have) if have else "- 지금은 재고 있는 곳이 없어요"))
        send_mail("[포켓몬 30주년] 재고 알림을 시작했어요",
                  f"{stamp} 기준 현재 재고예요. 앞으로 재고가 새로 생기면 알려드릴게요.\n\n" + "\n\n".join(parts))
    elif restocks:
        seen, parts, total = set(), [], []
        for lid, rows in restocks.items():
            uniq = [r for r in rows if (r["chain"], r["code"]) not in seen]
            seen.update((r["chain"], r["code"]) for r in uniq)
            if uniq:
                parts.append(f"[{names[lid]} 1km]\n" + fmt_rows(uniq))
                total += uniq
        first = total[0]
        more = f" 외 {len(total) - 1}곳" if len(total) > 1 else ""
        send_mail(f"[포켓몬 30주년] 재고 생김: {CHAIN_LABEL[first['chain']]} {first['name']}{more}",
                  f"{stamp} 기준 재고가 새로 생긴 매장이에요.\n\n" + "\n\n".join(parts))
    else:
        print("[알림] 새로 재고가 생긴 매장이 없어요.")

    state = {"qty": new_qty, "lastRun": now.isoformat(timespec="seconds")}
    save_json(ROOT / "state.json", state)
    save_json(ROOT / "docs" / "debug.json", {"at": now.isoformat(timespec="seconds"), **DEBUG})
    save_json(ROOT / "docs" / "status.json", {
        "checkedAt": now.isoformat(timespec="seconds"),
        "locations": {names[lid]: {CHAIN_LABEL[c]: ("조회 실패" if rows is None else rows) for c, rows in res.items()}
                      for lid, res in results.items()},
    })


if __name__ == "__main__":
    sys.exit(main())
