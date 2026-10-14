"""
Index Engine v1 : ES / NQ (S&P 500 / Nasdaq 100)
Daily (contexte) -> 5m/15m (VWAP, value area J-1, niveaux) -> 5m (declencheur)
Setups : A) Pullback dans le sens du biais  B) Faux breakout (sweep + reclaim d'un niveau)
Score de confluence -> SETUP CONFIRMED -> Entry / SL / TP / R:R / taille de position (micros)
+ Backtest integre (/api/backtest) sur ~60 jours de bougies 5m.

Outil d'aide a la decision, pas un conseil financier. Tester en demo avant tout argent reel.
Render : Build = pip install -r requirements.txt
         Start = gunicorn app:app --workers 1 --threads 4 --timeout 180
Variables (optionnelles) : ACCOUNT (defaut 10000), RISK_PCT (defaut 0.5)
Donnees : Yahoo Finance + calendrier ForexFactory, gratuits, sans cle (futures differes de ~10 min).
"""
import os
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, Response

ACCOUNT = float(os.getenv("ACCOUNT", "10000"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))
MIN_RR, MIN_SCORE, MAX_SCORE = 2.0, 7, 11
UA = {"User-Agent": "Mozilla/5.0"}
NY = ZoneInfo("America/New_York")
SYMS = {
    "ES": dict(y="ES=F", name="S&P 500 (ES)", mpt=5.0, micro="MES", bkt=0.5, cost=0.5, tick=0.25),
    "NQ": dict(y="NQ=F", name="Nasdaq 100 (NQ)", mpt=2.0, micro="MNQ", bkt=2.0, cost=1.5, tick=0.25),
}
HIGHS, LOWS = ("PDH", "ONH", "VAH J-1", "PWH"), ("PDL", "ONL", "VAL J-1", "PWL")
MID = ("VWAP", "Open", "POC J-1", "PDC", "Mid J-1")

app = Flask(__name__)
LOCK = threading.Lock()
CACHE = {}
STATE = {s: dict(plan=None, log=[], day=None, losses=0, used=set()) for s in SYMS}


def cached(key, ttl, fn):
    e = CACHE.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    try:
        v = fn()
    except Exception:
        if e:
            return e[1]
        raise
    CACHE[key] = (time.time(), v)
    return v


def log(sym, msg):
    L = STATE[sym]["log"]
    L.insert(0, {"t": datetime.now(NY).strftime("%d/%m %H:%M NY"), "msg": msg})
    del L[15:]


def sign(x, th):
    return 1 if x >= th else (-1 if x <= -th else 0)


# ====================== DONNEES ======================

def yahoo(symbol, rng, interval):
    err = None
    for h in ("query1", "query2"):
        try:
            r = requests.get("https://%s.finance.yahoo.com/v8/finance/chart/%s" % (h, symbol),
                             params=dict(range=rng, interval=interval), headers=UA, timeout=20).json()
            res = r["chart"]["result"][0]
            break
        except Exception as e:
            err = e
    else:
        raise err
    q, out = res["indicators"]["quote"][0], []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        dt = datetime.fromtimestamp(t + (21600 if interval == "1d" else 0), NY)
        out.append(dict(t=t, o=q["open"][i], h=q["high"][i], l=q["low"][i], c=q["close"][i],
                        v=q["volume"][i] or 0, d=dt.date(), m=dt.hour * 60 + dt.minute))
    return out


def sessions(b5):
    """Seances RTH (9:30-16:00 NY) par date."""
    S = {}
    for x in b5:
        if 570 <= x["m"] < 960:
            S.setdefault(x["d"], []).append(x)
    return S


def calendar():
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=20).json()
    now, out = time.time(), []
    for e in r:
        if e.get("impact") != "High" or e.get("country") != "USD":
            continue
        t = datetime.fromisoformat(e["date"])
        m = int((t.timestamp() - now) / 60)
        if -30 <= m <= 24 * 60:
            out.append({"title": e["title"], "time": t.astimezone(NY).strftime("%a %H:%M NY"), "minutes": m})
    return sorted(out, key=lambda x: x["minutes"])


def inter():
    out, vs = [], 0
    for s, nm in (("^VIX", "VIX"), ("^TNX", "US10Y")):
        try:
            cl = [x["c"] for x in yahoo(s, "5d", "5m")]
            ch = cl[-1] / cl[-7] - 1
            out.append("%s %.2f (%+.1f%% / 30 min)" % (nm, cl[-1], ch * 100))
            if nm == "VIX":
                vs = 1 if ch < -0.01 else (-1 if ch > 0.01 else 0)  # +1 = VIX baisse = favorable aux achats
        except Exception:
            out.append(nm + " indisponible")
    return vs, out


# ====================== OUTILS ======================

def atr(c, n=14):
    n = min(n, len(c) - 1)
    return sum(max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
               for i in range(len(c) - n, len(c))) / n


def ema(v, n):
    k, e = 2 / (n + 1), v[0]
    for x in v[1:]:
        e = x * k + e * (1 - k)
    return e


def swings(c, k=3):
    hi, lo = [], []
    for i in range(k, len(c) - k):
        w = c[i - k:i + k + 1]
        if c[i]["h"] == max(x["h"] for x in w):
            hi.append(c[i]["h"])
        if c[i]["l"] == min(x["l"] for x in w):
            lo.append(c[i]["l"])
    return hi, lo


def trend(c, k=3):
    hi, lo = swings(c, k)
    if len(hi) < 2 or len(lo) < 2:
        return 0
    return 1 if (hi[-1] > hi[-2] and lo[-1] > lo[-2]) else (-1 if (hi[-1] < hi[-2] and lo[-1] < lo[-2]) else 0)


def flip(c, d):
    """d=-1 : prix inverses, toute la logique 'achat' sert aussi cote vente."""
    if d == 1:
        return c
    return [dict(o=-x["o"], h=-x["l"], l=-x["h"], c=-x["c"], v=x["v"]) for x in c]


def profile(f, B):
    """Volume profile : POC, VAH, VAL (70 % du volume)."""
    prof = {}
    for x in f:
        a, b = int(x["l"] / B), int(x["h"] / B)
        for k in range(a, b + 1):
            prof[k] = prof.get(k, 0) + x["v"] / (b - a + 1)
    tot = sum(prof.values())
    if not tot:
        return None
    poc = max(prof, key=prof.get)
    lo = hi = poc
    acc, kmin, kmax = prof[poc], min(prof), max(prof)
    while acc < 0.7 * tot and (lo > kmin or hi < kmax):
        up = prof.get(hi + 1, 0) if hi < kmax else -1
        dn = prof.get(lo - 1, 0) if lo > kmin else -1
        if up >= dn:
            hi += 1
            acc += max(up, 0)
        else:
            lo -= 1
            acc += max(dn, 0)
    return dict(poc=(poc + .5) * B, vah=(hi + 1) * B, val=lo * B)


def vwap_series(T):
    out, pv, v = [], 0.0, 0.0
    for x in T:
        w = x["v"] or 1
        pv += (x["h"] + x["l"] + x["c"]) / 3 * w
        v += w
        out.append(pv / v)
    return out


# ====================== CONTEXTE (timeframe eleve) ======================

def daily_ctx(daily, D):
    dd = [x for x in daily if x["d"] < D]
    if len(dd) < 60:
        return dict(trend=0, adr=None, pwh=None, pwl=None)
    cl = [x["c"] for x in dd]
    e20, e50 = ema(cl[-60:], 20), ema(cl[-120:], 50)
    tr = trend(dd[-120:])
    if tr == 0:
        tr = 1 if cl[-1] > e20 > e50 else (-1 if cl[-1] < e20 < e50 else 0)
    if (tr == 1 and cl[-1] < e50) or (tr == -1 and cl[-1] > e50):
        tr = 0
    wk = lambda x: x["d"].isocalendar()[:2]
    pw = [x for x in dd if wk(x) < D.isocalendar()[:2]]
    W = [x for x in pw if wk(x) == wk(pw[-1])] if pw else []
    return dict(trend=tr, adr=sum(x["h"] - x["l"] for x in dd[-10:]) / 10,
                pwh=max(x["h"] for x in W) if W else None, pwl=min(x["l"] for x in W) if W else None)


def day_ctx(b5, S, D, daily, bkt):
    prev = [d for d in sorted(S) if d < D and len(S[d]) >= 40]
    if not prev:
        return None
    P = S[prev[-1]]
    t0 = S[D][0]["t"] if D in S else 10 ** 12
    on = [x for x in b5 if P[-1]["t"] < x["t"] < t0]
    dc = daily_ctx(daily, D)
    pdh, pdl = max(x["h"] for x in P), min(x["l"] for x in P)
    R = {"PDH": pdh, "PDL": pdl, "PDC": P[-1]["c"], "Mid J-1": (pdh + pdl) / 2}
    pf = profile(P, bkt)
    if pf:
        R.update({"POC J-1": pf["poc"], "VAH J-1": pf["vah"], "VAL J-1": pf["val"]})
    if on:
        R["ONH"], R["ONL"] = max(x["h"] for x in on), min(x["l"] for x in on)
    if dc["pwh"]:
        R["PWH"], R["PWL"] = dc["pwh"], dc["pwl"]
    return dict(refs=R, dc=dc, warm=[x for x in b5 if x["t"] < t0][-40:])


def context(C, T, vw):
    n, p = len(T), T[-1]["c"]
    a = atr((C["warm"] + T)[-40:])
    R = C["refs"]
    vah, val, o = R.get("VAH J-1"), R.get("VAL J-1"), T[0]["o"]
    oa = 1 if (vah and o > vah and p > val) else (-1 if (val and o < val and p < vah) else 0)
    vs = 1 if p > vw[n - 1] else -1
    htf = C["dc"]["trend"]
    bs = 2 * htf + oa + vs
    adr = C["dc"]["adr"]
    rng = max(x["h"] for x in T) - min(x["l"] for x in T)
    m = T[-1]["m"]
    return dict(a=a, p=p, oa=oa, vs=vs, htf=htf, bs=bs, bias=sign(bs, 2),
                adrp=rng / adr if adr else 1, win=1 if (585 <= m < 690 or 840 <= m < 945) else 0)


# ====================== SETUPS (espace 'achat', d=-1 = prix inverses) ======================

def setup_a(F, R, a, sup, tick):
    """Pullback : repli sur un niveau (VWAP, open, niveau casse...) puis bougie de reprise."""
    last, prev = F[-1], F[-2]
    if not (last["c"] > last["o"] and last["c"] > prev["h"]):
        return None
    hh, lo5 = max(x["h"] for x in F[-30:-1]), min(x["l"] for x in F[-5:])
    c = [(R[k], k) for k in sup if k in R and R[k] <= last["c"] and R[k] - .6 * a <= lo5 <= R[k] + .35 * a
         and hh - R[k] >= 1.5 * a]
    if not c:
        return None
    s = max(p for p, _ in c)
    return dict(typ="Pullback", lvl=", ".join(k for p, k in c if abs(p - s) <= .5 * a), lvlp=s,
                entry=last["c"], sl=min(lo5, s) - max(.25 * a, tick))


def setup_b(F, R, a, names, tick):
    """Faux breakout : cassure d'un niveau bas puis cloture de retour au-dessus (sweep + reclaim)."""
    n, last, best = len(F), F[-1], None
    if not last["c"] > last["o"]:
        return None
    for k in names:
        if k not in R:
            continue
        p = R[k]
        for j in range(max(0, n - 8), n - 1):
            if (F[j]["l"] < p and p - F[j]["l"] <= 3 * a and last["c"] > p
                    and any(F[m]["c"] > p for m in range(j, min(n, j + 3)))
                    and all(F[m]["c"] > p for m in range(min(n, j + 3), n))):
                if not best or j > best[0]:
                    best = (j, k, p, min(x["l"] for x in F[j:]))
    if not best:
        return None
    j, k, p, lo = best
    return dict(typ="Faux breakout", lvl=k, lvlp=p, entry=last["c"], sl=lo - max(.25 * a, tick))


def make_plan(d, c, R, a, F, cfg, own):
    e, sl = c["entry"], c["sl"]
    risk = e - sl
    if risk < .5 * a or risk > 4 * a:
        return None
    tg = [(R[k], k) for k in own + MID if k in R and R[k] > e + .5 * a]
    dx = max(x["h"] for x in F[:-1])
    if dx > e + .5 * a:
        tg.append((dx, "Extreme du jour"))
    tg.sort()
    for i, (t, k) in enumerate(tg):
        if (t - e) / risk >= MIN_RR:
            t2 = tg[i + 1] if i + 1 < len(tg) else None
            n = int(ACCOUNT * RISK_PCT / 100 // (risk * cfg["mpt"]))
            return dict(dir=d, entry=d * e, sl=d * sl, tp=d * t, tp_kind=k, rr=round((t - e) / risk, 2),
                        tp2=d * t2[0] if t2 else None, tp2_kind=t2[1] if t2 else None,
                        rr2=round((t2[0] - e) / risk, 2) if t2 else None,
                        pts_sl=round(risk, 2), usd_sl=round(risk * cfg["mpt"], 2), micros=n,
                        micro=cfg["micro"], risk_usd=round(n * risk * cfg["mpt"], 2), t=time.time())
    return None


def evaluate(C, T, vw, cfg, vixs=0, used=()):
    """Meilleur setup (achat ou vente) sur la derniere bougie 5m cloturee."""
    X = context(C, T, vw)
    a, best = X["a"], None
    R0 = dict(C["refs"])
    R0["VWAP"], R0["Open"] = vw[len(T) - 1], T[0]["o"]
    vols = [x["v"] for x in (C["warm"] + T)[-21:-1]]
    vok = bool(vols) and T[-1]["v"] > 1.2 * sum(vols) / len(vols)
    for d in (1, -1):
        F = flip(T[-80:], d)
        R = {k: d * v for k, v in R0.items()}
        own, opp = (HIGHS, LOWS) if d == 1 else (LOWS, HIGHS)
        cands = []
        if X["bias"] == d:
            cands.append(setup_a(F, R, a, MID + own, cfg["tick"]))
        if X["bias"] != -d:
            cands.append(setup_b(F, R, a, opp, cfg["tick"]))
        for c in cands:
            if not c or (c["typ"], c["lvl"]) in used:
                continue
            conf = sum(1 for v in R.values() if abs(v - c["lvlp"]) <= .5 * a)
            body = F[-1]["c"] - F[-1]["o"] >= .5 * (F[-1]["h"] - F[-1]["l"])
            third = (("VWAP du bon cote", int(X["vs"] == d), 1) if c["typ"] == "Pullback" else
                     ("Niveau majeur (J-1 / semaine)", int(c["lvl"] in ("PDH", "PDL", "PWH", "PWL")), 1))
            ch = [("Tendance journaliere alignee", 2 if X["htf"] == d else (-2 if X["htf"] == -d else 0), 2),
                  ("Ouverture vs value area J-1", 1 if X["oa"] == d else (-1 if X["oa"] == -d else 0), 1), third,
                  ("Confluence de niveaux (%d)" % conf, min(2, conf), 2),
                  ("Bougie de confirmation" + (" + volume" if vok else ""), int(body) + int(vok), 2),
                  ("Fenetre horaire (9h45-11h30 / 14h-15h45 NY)", X["win"], 1),
                  ("Range du jour < ADR", int(X["adrp"] < .9), 1),
                  ("VIX favorable", 1 if vixs * d > 0 else (-1 if vixs * d < 0 else 0), 1)]
            sc = sum(g for _, g, _ in ch)
            pl = make_plan(d, c, R, a, F, cfg, own)
            r = dict(d=d, typ=c["typ"], lvl=c["lvl"], lvlp=round(d * c["lvlp"], 2), score=sc, plan=pl,
                     checks=[{"label": l, "got": g, "max": m} for l, g, m in ch],
                     grade="A+" if sc >= 9 else ("A" if sc >= MIN_SCORE else "B"))
            if not best or (bool(pl), sc) > (bool(best["plan"]), best["score"]):
                best = r
    return best


# ====================== BACKTEST ======================

def simulate(T, i, pl, cfg):
    d, e, sl, tp = pl["dir"], pl["entry"], pl["sl"], pl["tp"]
    cost, j = cfg["cost"] / abs(e - sl), i
    for j in range(i + 1, len(T)):
        x = T[j]
        if (d == 1 and x["l"] <= sl) or (d == -1 and x["h"] >= sl):
            return -1 - cost, j
        if (d == 1 and x["h"] >= tp) or (d == -1 and x["l"] <= tp):
            return pl["rr"] - cost, j
        if x["m"] >= 955:
            break
    return d * (T[j]["c"] - e) / abs(e - sl) - cost, j


def stats(tr):
    if not tr:
        return dict(n=0)
    rs = [t["r"] for t in tr]
    w, l = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq, c, pk, dd = [], 0, 0, 0
    for r in rs:
        c += r
        eq.append(round(c, 2))
        pk, dd = max(pk, c), max(dd, max(pk, c) - c)
    return dict(n=len(rs), win=round(len(w) / len(rs) * 100), avg=round(sum(rs) / len(rs), 2),
                pf=round(sum(w) / -sum(l), 2) if l and sum(l) else None, total=round(c, 2), dd=round(dd, 2), eq=eq)


def backtest(sym):
    cfg = SYMS[sym]
    b5 = cached("b5" + sym, 90, lambda: yahoo(cfg["y"], "60d", "5m"))
    daily = cached("d" + sym, 3600, lambda: yahoo(cfg["y"], "2y", "1d"))
    S, tr = sessions(b5), []
    for D in sorted(S):
        T = S[D]
        C = day_ctx(b5, S, D, daily, cfg["bkt"]) if len(T) >= 60 else None
        if not C:
            continue
        vw, used, i, n, loss = vwap_series(T), set(), 6, 0, 0
        while i < len(T) - 3 and n < 3 and loss < 2:
            if 580 <= T[i]["m"] < 930:
                sg = evaluate(C, T[:i + 1], vw[:i + 1], cfg, 0, used)
                if sg and sg["plan"] and sg["score"] >= MIN_SCORE:
                    r, j = simulate(T, i, sg["plan"], cfg)
                    used.add((sg["typ"], sg["lvl"]))
                    tr.append(dict(d=str(D), typ=sg["typ"], dir=sg["d"], lvl=sg["lvl"], score=sg["score"], r=round(r, 2)))
                    n += 1
                    loss += r < 0
                    i = j
            i += 1
    by = {k: stats([t for t in tr if t["typ"] == k]) for k in ("Pullback", "Faux breakout")}
    return dict(sym=sym, days=len(S), all=stats(tr), by=by, last=tr[-15:][::-1],
                note="Couts inclus (%.2f pt A/R). Sans filtre news/VIX. Passe = pas garantie du futur." % cfg["cost"])


# ====================== MOTEUR LIVE ======================

def analyse(sym):
    cfg, st = SYMS[sym], STATE[sym]
    b5 = cached("b5" + sym, 90, lambda: yahoo(cfg["y"], "60d", "5m"))
    daily = cached("d" + sym, 3600, lambda: yahoo(cfg["y"], "2y", "1d"))
    now, price = time.time(), b5[-1]["c"]
    b5c = [x for x in b5 if x["t"] + 300 <= now]
    nw = datetime.now(NY)
    D, mm = nw.date(), nw.hour * 60 + nw.minute
    S = sessions(b5c)
    if st["day"] != D:
        st.update(day=D, losses=0, used=set())
    C = day_ctx(b5c, S, D, daily, cfg["bkt"])
    if not C:
        raise RuntimeError("Donnees insuffisantes")
    T = S.get(D, [])
    vw = vwap_series(T) if T else []
    X = context(C, T, vw) if len(T) >= 6 else None
    closed = now - b5[-1]["t"] > 5400
    try:
        news = cached("cal", 900, calendar)
    except Exception:
        news = []
    try:
        vixs, itxt = cached("inter", 120, inter)
    except Exception:
        vixs, itxt = 0, []
    news_risk = any(-15 <= n["minutes"] <= 60 for n in news)

    plan, sg, status, msg = st["plan"], None, "NEUTRE", ""
    if plan:
        d = plan["dir"]
        seg = [x for x in b5 if x["t"] >= plan["t"] - 300]
        hi, lo = max(x["h"] for x in seg), min(x["l"] for x in seg)
        hit_sl = lo <= plan["sl"] if d == 1 else hi >= plan["sl"]
        hit_tp = hi >= plan["tp"] if d == 1 else lo <= plan["tp"]
        if hit_sl:
            st["losses"] += 1
            st["plan"], plan = None, None
            status, msg = "STOP", "Stop touche : scenario invalide."
            log(sym, "STOP touche @ %.2f" % price)
        elif hit_tp:
            st["plan"], plan = None, None
            status, msg = "OBJECTIF ATTEINT", "TP touche : scenario termine."
            log(sym, "TP atteint @ %.2f" % price)
        elif time.time() - plan["t"] > 3 * 3600 or mm >= 955:
            st["plan"], plan = None, None
            status, msg = "EXPIRE", "Scenario expire (3 h ou fin de seance)."
            log(sym, "Scenario expire")
        else:
            status, msg = "EN COURS", "Le scenario est actif : gerer SL/TP."
    if not plan and status not in ("STOP", "OBJECTIF ATTEINT", "EXPIRE"):
        if closed:
            status, msg = "MARCHE FERME", "Pas de donnees recentes (week-end / ferie)."
        elif not T or mm < 580:
            status, msg = "PRE-MARKET", "Niveaux du jour prets. Entrees a partir de 9h40 NY."
        elif mm >= 930:
            status, msg = "SEANCE TERMINEE", "Plus d'entree apres 15h30 NY."
        elif news_risk:
            status, msg = "PAUSE NEWS", "News USD majeure proche : pas de nouvelle entree."
        elif st["losses"] >= 2:
            status, msg = "STOP JOURNEE", "2 pertes aujourd'hui : on arrete (regle de risque)."
        elif X:
            sg = evaluate(C, T, vw, cfg, vixs, st["used"])
            if not sg:
                status, msg = "ATTENTE", "Aucun setup (biais %s)." % {1: "haussier", -1: "baissier", 0: "neutre"}[X["bias"]]
            elif not sg["plan"]:
                status, msg = "ATTENTE", "Setup %s mais R:R < %.1f ou stop invalide." % (sg["typ"], MIN_RR)
            elif sg["score"] < MIN_SCORE:
                status, msg = "SURVEILLANCE", "%s sur %s : score %d/%d (min %d)." % (sg["typ"], sg["lvl"], sg["score"], MAX_SCORE, MIN_SCORE)
            elif abs(price - sg["plan"]["entry"]) > X["a"]:
                status, msg = "SIGNAL PASSE", "Setup valide mais le prix s'est trop eloigne de l'entree."
            else:
                st["plan"] = plan = sg["plan"]
                st["used"].add((sg["typ"], sg["lvl"]))
                status, msg = "SETUP CONFIRMED", "%s sur %s : score %d/%d (%s)." % (sg["typ"], sg["lvl"], sg["score"], MAX_SCORE, sg["grade"])
                log(sym, "SETUP %s %s %s @ %.2f (R:R %.2f)" % ("ACHAT" if sg["d"] == 1 else "VENTE", sg["typ"], sg["lvl"], plan["entry"], plan["rr"]))

    refs = dict(C["refs"])
    if T:
        refs["VWAP"], refs["Open"] = vw[-1], T[0]["o"]
    bi = X or dict(bias=C["dc"]["trend"], bs=2 * C["dc"]["trend"], oa=0, vs=0, htf=C["dc"]["trend"], adrp=None, win=0)
    return dict(sym=sym, name=cfg["name"], price=round(price, 2), status=status, message=msg, closed=closed,
                bias=dict(dir=bi["bias"], score=bi["bs"], htf=bi["htf"], oa=bi["oa"], vs=bi["vs"]),
                levels=[{"n": k, "p": round(v, 2)} for k, v in sorted(refs.items(), key=lambda kv: -kv[1])],
                setup=sg, plan=plan, news=news[:6], inter=itxt, log=st["log"], losses=st["losses"],
                sess=dict(ny=nw.strftime("%H:%M NY"), win=bool(X and X["win"]),
                          adr=round(X["adrp"] * 100) if X and X["adrp"] else None),
                updated=nw.strftime("%H:%M:%S NY"))


def sym_arg():
    s = request.args.get("s", "ES").upper()
    return s if s in SYMS else "ES"


@app.route("/api/analysis")
def api():
    try:
        with LOCK:
            return jsonify(analyse(sym_arg()))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/backtest")
def api_bt():
    try:
        s = sym_arg()
        with LOCK:
            return jsonify(cached("bt" + s, 3600, lambda: backtest(s)))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/health")
def health():
    return "ok"


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Index Engine ES/NQ</title>
<style>
:root{--bg:#0e1116;--card:#171c24;--tx:#e6e9ee;--mut:#8b94a3;--g:#22c55e;--r:#ef4444;--y:#eab308;--b:#3b82f6}
*{box-sizing:border-box}body{margin:auto;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding:12px;max-width:720px}
.card{background:var(--card);border-radius:12px;padding:14px;margin-bottom:12px}
h2{margin:0 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px}.price{font-size:28px;font-weight:700}
.badge{padding:6px 12px;border-radius:20px;font-weight:700;font-size:13px;text-align:center;background:#334}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #222a35;gap:8px}.row:last-child{border:0}
.s{font-weight:700;min-width:34px;text-align:right}.mut{color:var(--mut);font-size:12px}
button{background:var(--b);color:#fff;border:0;border-radius:8px;padding:8px 14px;margin-right:6px}
button.tab{background:#232b38}button.tab.on{background:var(--b)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.kv{background:#10151c;border-radius:8px;padding:8px}.kv b{display:block;font-size:16px}
</style></head><body>
<div style="margin-bottom:10px"><button class="tab on" data-s="ES" onclick="tab('ES')">ES - S&amp;P 500</button><button class="tab" data-s="NQ" onclick="tab('NQ')">NQ - Nasdaq</button></div>
<div class="card"><div class="top"><div><div class="mut" id="nm"></div><div class="price" id="price">...</div></div>
<div class="badge" id="status">Chargement</div></div><p id="msg" style="margin:10px 0 0"></p><div class="mut" id="upd"></div></div>
<div class="card" id="planCard" style="display:none"><h2>Plan de trade</h2><div id="plan"></div></div>
<div class="card"><h2>Biais (daily + ouverture + VWAP)</h2><div id="bias"></div></div>
<div class="card"><h2>Checklist du meilleur setup</h2><div id="setup"></div></div>
<div class="card"><h2>Niveaux (J-1, overnight, semaine, VWAP, value area)</h2><div id="lv"></div></div>
<div class="card"><h2>Contexte &amp; news USD</h2><div id="ctx"></div></div>
<div class="card"><h2>Journal</h2><div id="log"></div></div>
<div class="card"><h2>Backtest (~60 jours, bougies 5m)</h2><button onclick="bt()">Lancer</button><div id="bt" style="margin-top:10px"></div></div>
<button onclick="load()">Actualiser</button>
<p class="mut">Outil d'aide a la decision, pas un conseil financier. Donnees Yahoo (futures differes ~10 min). Tester en demo.</p>
<script>
let S='ES';const $=id=>document.getElementById(id);
const col=s=>s>0?'var(--g)':(s<0?'var(--r)':'var(--mut)');
const f=(x,n)=>x==null?'-':Number(x).toFixed(n==null?2:n);
const SC={'SETUP CONFIRMED':'var(--g)','EN COURS':'var(--g)','OBJECTIF ATTEINT':'var(--g)','STOP':'var(--r)','STOP JOURNEE':'var(--r)','SURVEILLANCE':'var(--b)','PAUSE NEWS':'var(--y)','SIGNAL PASSE':'var(--y)'};
const rows=(a,fn)=>a.length?a.map(fn).join(''):'<div class="mut">-</div>';
const kv=(l,v,s,c)=>'<div class="kv">'+l+'<b'+(c?' style="color:'+c+'"':'')+'>'+v+'</b><span class="mut">'+(s||'')+'</span></div>';
function tab(s){S=s;document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('on',b.dataset.s==s));$('bt').innerHTML='';load()}
function render(r){
 $('nm').textContent=r.name;$('price').textContent=f(r.price);
 const st=$('status');st.textContent=r.status;st.style.background=SC[r.status]||'#334';
 $('msg').textContent=r.message;$('upd').textContent='Maj '+r.updated;
 const p=r.plan;$('planCard').style.display=p?'block':'none';
 if(p){$('plan').innerHTML='<div style="font-weight:700;color:'+(p.dir>0?'var(--g)':'var(--r)')+'">'+(p.dir>0?'ACHAT':'VENTE')+' (entree = cloture M5)</div>'
  +'<div class="grid" style="margin-top:8px">'+kv('Entree',f(p.entry))+kv('Stop',f(p.sl),p.pts_sl+' pts - '+p.usd_sl+' $/contrat','var(--r)')
  +kv('TP1 ('+p.tp_kind+')',f(p.tp),'R:R '+p.rr,'var(--g)')+kv('TP2'+(p.tp2?' ('+p.tp2_kind+')':''),p.tp2?f(p.tp2):'-',p.rr2?'R:R '+p.rr2:'')+'</div>'
  +'<div class="mut" style="margin-top:8px">Taille : <b>'+p.micros+' '+p.micro+'</b> pour ~'+p.risk_usd+' $ de risque</div>';}
 const b=r.bias,t=v=>v>0?'haussier':(v<0?'baissier':'neutre');
 $('bias').innerHTML='<div class="row"><span>Biais intraday</span><span class="s" style="color:'+col(b.dir)+'">'+t(b.dir).toUpperCase()+' ('+b.score+')</span></div>'
  +'<div class="row"><span>Tendance journaliere</span><span class="s" style="color:'+col(b.htf)+'">'+t(b.htf)+'</span></div>'
  +'<div class="row"><span>Ouverture vs value area J-1</span><span class="s" style="color:'+col(b.oa)+'">'+(b.oa>0?'au-dessus':(b.oa<0?'en-dessous':'dedans'))+'</span></div>'
  +'<div class="row"><span>Prix vs VWAP</span><span class="s" style="color:'+col(b.vs)+'">'+(b.vs>0?'au-dessus':(b.vs<0?'en-dessous':'-'))+'</span></div>';
 const s=r.setup;
 $('setup').innerHTML=s?('<div class="row"><b>'+(s.d>0?'ACHAT':'VENTE')+' '+s.typ+' sur '+s.lvl+' ('+f(s.lvlp)+')</b><span class="s">'+s.score+'/11 '+s.grade+'</span></div>'
  +rows(s.checks,c=>'<div class="row"><span>'+c.label+'</span><span class="s" style="color:'+col(c.got)+'">'+c.got+'/'+c.max+'</span></div>')):'<div class="mut">Aucun setup detecte sur la derniere bougie.</div>';
 $('lv').innerHTML=rows(r.levels,x=>'<div class="row"><span'+(x.p>r.price?'':' style="color:var(--mut)"')+'>'+x.n+'</span><b>'+f(x.p)+'</b></div>');
 $('ctx').innerHTML='<div class="row"><span>Heure</span><b>'+r.sess.ny+(r.sess.win?' - fenetre active':'')+'</b></div>'
  +'<div class="row"><span>Range du jour / ADR</span><b>'+(r.sess.adr==null?'-':r.sess.adr+'%')+'</b></div>'
  +r.inter.map(x=>'<div class="mut">'+x+'</div>').join('')
  +rows(r.news,n=>'<div class="row"><span>'+n.title+'</span><span class="mut">'+n.time+'</span></div>');
 $('log').innerHTML=rows(r.log,l=>'<div class="row"><span>'+l.msg+'</span><span class="mut">'+l.t+'</span></div>');
}
async function load(){try{const r=await (await fetch('/api/analysis?s='+S)).json();
 if(r.error){$('msg').textContent='Erreur : '+r.error;return}render(r)}catch(e){$('msg').textContent='Erreur reseau'}}
const sb=(n,x)=>x.n?'<div class="row"><span>'+n+' ('+x.n+' trades)</span><span class="s">win '+x.win+'% | moy '+x.avg+'R | PF '+(x.pf||'-')+' | total '+x.total+'R</span></div>':'<div class="row"><span>'+n+'</span><span class="mut">aucun trade</span></div>';
async function bt(){$('bt').innerHTML='<div class="mut">Calcul en cours (10-30 s)...</div>';
 try{const r=await (await fetch('/api/backtest?s='+S)).json();if(r.error){$('bt').textContent='Erreur : '+r.error;return}
 const a=r.all;if(!a.n){$('bt').innerHTML='<div class="mut">Aucun trade sur la periode.</div>';return}
 const e=a.eq,mx=Math.max(...e,0),mn=Math.min(...e,0),W=300,H=70;
 const pts=e.map((v,i)=>(i/(e.length-1||1)*W).toFixed(1)+','+(H-(v-mn)/((mx-mn)||1)*H).toFixed(1)).join(' ');
 $('bt').innerHTML='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:80px"><polyline fill="none" stroke="#3b82f6" stroke-width="2" points="'+pts+'"/></svg>'
  +sb('Tous',a)+'<div class="row"><span>Drawdown max</span><span class="s">'+a.dd+'R</span></div>'+sb('Pullback',r.by['Pullback'])+sb('Faux breakout',r.by['Faux breakout'])
  +rows(r.last,t=>'<div class="row"><span>'+t.d+' '+(t.dir>0?'ACHAT':'VENTE')+' '+t.typ+' '+t.lvl+'</span><span class="s" style="color:'+col(t.r)+'">'+t.r+'R</span></div>')+'<div class="mut">'+r.note+'</div>'
 }catch(e){$('bt').textContent='Erreur reseau'}}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
