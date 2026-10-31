"""한 번만 쓰는 진단: 옥수역 근처 이마트24 성동금호점이 사이트에서 빠지는 이유 찾기."""
import json, math, urllib.parse, urllib.request
BASE = "https://mcp.aka.page"
C = (37.5406, 127.0175)
PLU = "8800286278115"  # 메가심포니아
out = {}

def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pk30-diag", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def dist(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return round(2 * 6371000 * math.asin(math.sqrt(x)))

target = None
for kw in ["옥수", "금호", "금호동", "성동금호"]:
    b = get("/api/emart24/stores", {"keyword": kw, "lat": C[0], "lng": C[1], "limit": 50})
    st = (b.get("data") or {}).get("stores", [])
    rows = []
    for s in st[:15]:
        dd = dist(C[0], C[1], s["latitude"], s["longitude"]) if s.get("latitude") else None
        rows.append([s.get("storeCode"), s.get("storeName"), s.get("distanceM"), dd])
        if "성동금호" in (s.get("storeName") or "") and not target:
            target = s
    out[f"stores|{kw}"] = {"count": len(st), "meta": b.get("meta"), "error": b.get("error"), "first": rows}

b = get("/api/emart24/stores", {"area1": "서울특별시", "area2": "성동구", "lat": C[0], "lng": C[1], "limit": 50})
st = (b.get("data") or {}).get("stores", [])
out["stores|area 성동구"] = {"count": len(st), "meta": b.get("meta"), "names": [s.get("storeName") for s in st[:30]]}
for s in st:
    if "성동금호" in (s.get("storeName") or "") and not target:
        target = s

out["target"] = target
if target:
    code = target["storeCode"]
    out["inv|bizNoArr"] = get("/api/emart24/inventory", {"pluCd": PLU, "bizNoArr": code, "storeLimit": 1})
    out["inv|bizNoArr"].pop("meta", None)
out["inv|keyword 금호"] = get("/api/emart24/inventory", {"pluCd": PLU, "storeKeyword": "금호", "lat": C[0], "lng": C[1], "storeLimit": 60})
d = out["inv|keyword 금호"].get("data") or {}
out["inv|keyword 금호"] = {"stores": d.get("stores") or (d.get("inventory") or {}).get("stores"), "nearby": [(s.get("storeName"), s.get("distanceM")) for s in ((d.get("nearbyStores") or {}).get("stores") or [])][:15], "error": out["inv|keyword 금호"].get("error")}

open("docs/diag.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps(out, ensure_ascii=False)[:3000])
