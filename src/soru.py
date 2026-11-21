"""Telegram soru-cevap: "Galatasaray son 10 maçta ilk yarıda kaç kez 0.5 üst yaptı?" gibi sorulara cevap.

- Bota özelden her mesaj, grupta /sor ile başlayan mesajlar soru sayılır.
- Bir sinyal mesajını yanıtlayarak sorulursa "ev sahibi"/"deplasman" o maçın takımları olur.
- Claude sadece soruyu yapılandırılmış hale getirir (takım, maç sayısı, ölçüt, eşik). Rakamlar gerçek maç
  verisinden hesaplanır; dil modeli sayı üretmez.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
import unicodedata

from main import DATA, TZ, Claude, _tg, log

SAYAC = DATA / "soru_sayac.json"
GUNLUK_SINIR = 40
KISI_SINIR = 12
BITTI = ("FT", "AET", "PEN")

OLCUTLER = {
    "mac_gol_ust": "maçta toplam gol {e} üst",
    "iy_gol_ust": "ilk yarı {e} üst",
    "iy2_gol_ust": "ikinci yarı {e} üst",
    "takim_gol_ust": "{t} – {e} üstü gol attığı maç",
    "takim_iy_gol_ust": "{t} – ilk yarıda {e} üstü gol attığı maç",
    "yedigi_gol_ust": "{t} – {e} üstü gol yediği maç",
    "kg": "karşılıklı gol",
    "korner_toplam": "maçta toplam korner",
    "takim_korner": "{t} – kullandığı korner",
    "kart_toplam": "maçta toplam kart",
    "takim_kart": "{t} – gördüğü kart",
    "sonuc": "galibiyet / beraberlik / mağlubiyet",
    "ozet": "genel özet",
}

YARDIM = ("🔍 <b>Sherlock'a soru sor</b>\n\n"
          "Örnekler:\n"
          "• Galatasaray son 10 maçta ilk yarıda kaç kez 0.5 üst yaptı?\n"
          "• Fenerbahçe deplasmanda son 5 maçta kaç korner kullandı?\n"
          "• Arsenal son 8 maçta kaç kez karşılıklı gol oldu?\n"
          "• Bayern son 5 maç özeti\n\n"
          "Bir sinyal mesajını yanıtlayıp \"ev sahibi son 5 maçta kaç korner kullanmış?\" diye de sorabilirsin.\n"
          "Grupta soruların başına /sor yaz.")


def _ascii(s: str) -> str:
    s = s.replace("ı", "i").replace("İ", "I")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


class QA:
    def __init__(self, api, engine=None):
        self.api = api
        self.engine = engine
        self.claude = Claude()
        self.offset = None
        self.teams: dict[str, tuple] = {}
        self.stop = False

    # ------------------------------------------------------------------ döngü
    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop:
            try:
                params = {"timeout": 25, "allowed_updates": ["message"]}
                if self.offset is not None:
                    params["offset"] = self.offset
                res = _tg("getUpdates", _timeout=40, **params)
                if not res.get("ok"):
                    time.sleep(10)  # başka oturum da dinliyorsa (geçiş anı) çakışma olur
                    continue
                for u in res.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    try:
                        self.handle(msg)
                    except Exception as e:
                        log("Soru-cevap hatası:", e)
            except Exception as e:
                log("Soru-cevap bağlantı hatası:", e)
                time.sleep(15)

    def handle(self, msg: dict):
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        if not text or (msg.get("from") or {}).get("is_bot"):
            return
        if time.time() - (msg.get("date") or 0) > 12 * 3600:  # çok eski sorulara cevap verilmez
            return
        private = chat.get("type") == "private"
        low = text.lower()
        if low.startswith("/start") or low.startswith("/yardim") or low.startswith("/yardım") or low.startswith("/help"):
            return self.reply(chat["id"], msg["message_id"], YARDIM)
        if low.startswith("/sor"):
            text = re.sub(r"^/sor(@\w+)?\s*", "", text, flags=re.I)
        elif not private:
            return  # grupta sadece /sor
        if not text:
            return self.reply(chat["id"], msg["message_id"], YARDIM)
        uid = str((msg.get("from") or {}).get("id"))
        ok, why = self.quota(uid)
        if not ok:
            return self.reply(chat["id"], msg["message_id"], why)
        ctx = self.reply_context(msg.get("reply_to_message"))
        try:
            answer = self.answer(text, ctx)
        except Exception as e:
            log("Soru cevaplanamadı:", e)
            answer = "⚠️ Şu an bu soruya cevap veremiyorum, biraz sonra tekrar dene."
        self.reply(chat["id"], msg["message_id"], answer)

    def reply(self, chat_id, message_id, html):
        _tg("sendMessage", chat_id=chat_id, text=html[:4000], parse_mode="HTML", disable_web_page_preview=True,
            reply_parameters={"message_id": message_id, "allow_sending_without_reply": True})

    def quota(self, uid: str):
        today = dt.datetime.now(TZ).date().isoformat()
        try:
            c = json.loads(SAYAC.read_text())
        except Exception:
            c = {}
        if c.get("tarih") != today:
            c = {"tarih": today, "toplam": 0, "kisi": {}}
        if c["toplam"] >= GUNLUK_SINIR:
            return False, "⏳ Bugünkü soru sınırına ulaşıldı, yarın tekrar dene."
        if c["kisi"].get(uid, 0) >= KISI_SINIR:
            return False, f"⏳ Günde en fazla {KISI_SINIR} soru sorabilirsin, yarın tekrar dene."
        c["toplam"] += 1
        c["kisi"][uid] = c["kisi"].get(uid, 0) + 1
        try:
            SAYAC.write_text(json.dumps(c))
        except OSError:
            pass
        return True, ""

    # ------------------------------------------------------------------ bağlam
    def reply_context(self, rep: dict | None) -> dict | None:
        """Sinyal mesajına yanıt verildiyse o maçın takımları."""
        if not rep or not self.engine:
            return None
        mid = rep.get("message_id")
        for r in reversed(self.engine.rows_today):
            if str(r.get("tg_mesaj")) == str(mid):
                fid = int(r["fixture_id"])
                m = self.engine.matches.get(fid)
                if m and m.home_id:
                    return {"ev": (m.home_id, m.home), "deplasman": (m.away_id, m.away), "mac": m.name}
                try:
                    f = self.api.get("fixtures", id=fid)[0]
                    return {"ev": (f["teams"]["home"]["id"], f["teams"]["home"]["name"]),
                            "deplasman": (f["teams"]["away"]["id"], f["teams"]["away"]["name"]), "mac": r["mac"]}
                except Exception:
                    return None
        return None

    # ------------------------------------------------------------------ soru → yapı
    def parse(self, text: str, ctx: dict | None) -> dict:
        if not self.claude.available():
            raise RuntimeError("Claude kullanılamıyor")
        olc = "\n".join(f'- "{k}": {v.format(t="takım", e="eşik")}' for k, v in OLCUTLER.items())
        prompt = (
            "Bir futbol istatistik botusun. Kullanıcının Türkçe sorusunu aşağıdaki JSON yapısına çevir. "
            "Sayı ya da sonuç UYDURMA; sadece soruyu yapılandır.\n"
            f"{'Soru şu maçla ilgili bir sinyal mesajına yanıt: ' + ctx['mac'] + '. Ev sahibi/deplasman denirse takim alanına \"ev\" ya da \"deplasman\" yaz.' if ctx else ''}\n"
            f"Ölçütler:\n{olc}\n"
            "Kurallar: 'son N maç' yoksa son=5; en fazla 20. Eşik yoksa gol ölçütlerinde 0.5 kullan; "
            "korner/kart ölçütlerinde eşik sorulmadıysa null. 'deplasmanda'/'dış sahada' → saha=\"dis\", "
            "'evinde'/'iç sahada' → saha=\"ic\", yoksa \"hepsi\". Takım adını sorudaki gibi yaz.\n"
            f"SORU: {text}\n\n"
            "SADECE JSON yaz:\n"
            '{"anlasildi": true, "takim": "...", "son": 5, "olcut": "...", "esik": 0.5, "saha": "hepsi"}\n'
            'Futbol istatistiği sorusu değilse ya da anlaşılmıyorsa: {"anlasildi": false}'
        )
        txt = self.claude.ask(prompt, timeout=90)
        mt = re.search(r"\{.*\}", txt, re.S)
        if not mt:
            raise RuntimeError("Claude JSON dönmedi")
        q = json.loads(mt.group(0))
        return q

    def find_team(self, name: str, ctx: dict | None):
        key = (name or "").strip().lower()
        if ctx and key in ("ev", "ev sahibi", "home"):
            return ctx["ev"]
        if ctx and key in ("deplasman", "dep", "away", "misafir"):
            return ctx["deplasman"]
        if key in self.teams:
            return self.teams[key]
        q = _ascii(name).strip()
        if len(q) < 3:
            return None
        res = self.api.get("teams", search=q)
        if not res:  # tek kelimeyle tekrar dene ("Galatasaray SK" → "Galatasaray")
            first = max(q.split(), key=len)
            res = self.api.get("teams", search=first) if len(first) >= 3 and first != q else []
        bad = re.compile(r"\b(U\d\d|W|Women|II|B|Reserves?|Youth)\b", re.I)
        res = [t for t in res if not bad.search(t["team"]["name"])] or res
        if not res:
            return None
        exact = [t for t in res if _ascii(t["team"]["name"]).lower() == q.lower()]
        t = (exact or res)[0]["team"]
        self.teams[key] = (t["id"], t["name"])
        return self.teams[key]

    # ------------------------------------------------------------------ veri
    def matches(self, tid: int, n: int, saha: str) -> list[dict]:
        last = self.api.get("fixtures", team=tid, last=20 if saha != "hepsi" else n)
        last = [f for f in last if f["fixture"]["status"]["short"] in BITTI]
        if saha == "ic":
            last = [f for f in last if f["teams"]["home"]["id"] == tid]
        elif saha == "dis":
            last = [f for f in last if f["teams"]["away"]["id"] == tid]
        last.sort(key=lambda f: f["fixture"]["timestamp"], reverse=True)
        last = last[:n]
        ids = [str(f["fixture"]["id"]) for f in last]
        full = {}
        for i in range(0, len(ids), 20):
            for f in self.api.get("fixtures", ids="-".join(ids[i:i + 20])):
                full[f["fixture"]["id"]] = f
        out = []
        for f in last:
            f = full.get(f["fixture"]["id"], f)
            home = f["teams"]["home"]["id"] == tid
            g, ht = f.get("goals") or {}, (f.get("score") or {}).get("halftime") or {}
            gf, ga = (g.get("home"), g.get("away")) if home else (g.get("away"), g.get("home"))
            hf, ha = (ht.get("home"), ht.get("away")) if home else (ht.get("away"), ht.get("home"))
            st = {}
            for b in f.get("statistics") or []:
                side = "own" if b["team"]["id"] == tid else "opp"
                for x in b.get("statistics") or []:
                    try:
                        v = float(str(x.get("value") or 0).rstrip("%"))
                    except ValueError:
                        v = 0.0
                    if x.get("type") == "Corner Kicks":
                        st[f"korner_{side}"] = v
                    elif x.get("type") in ("Yellow Cards", "Red Cards"):
                        st[f"kart_{side}"] = st.get(f"kart_{side}", 0) + v
            opp = f["teams"]["away" if home else "home"]["name"]
            out.append({"tarih": dt.datetime.fromtimestamp(f["fixture"]["timestamp"], TZ).strftime("%d.%m"),
                        "rakip": opp, "saha": "E" if home else "D", "gf": gf or 0, "ga": ga or 0,
                        "hf": hf, "ha": ha, **st})
        return out

    # ------------------------------------------------------------------ cevap
    def answer(self, text: str, ctx: dict | None) -> str:
        q = self.parse(text, ctx)
        if not q.get("anlasildi"):
            return "🤔 Soruyu anlayamadım.\n\n" + YARDIM
        olcut = q.get("olcut") if q.get("olcut") in OLCUTLER else "ozet"
        n = max(1, min(20, int(q.get("son") or 5)))
        saha = q.get("saha") if q.get("saha") in ("ic", "dis") else "hepsi"
        team = self.find_team(q.get("takim") or "", ctx)
        if not team:
            return f"🔍 \"{q.get('takim')}\" adında bir takım bulamadım. Takım adını farklı yazmayı dene."
        tid, tname = team
        ms = self.matches(tid, n, saha)
        if not ms:
            return f"📭 {tname} için bitmiş maç bulunamadı."
        e = q.get("esik")
        try:
            e = None if e is None else float(e)
        except (TypeError, ValueError):
            e = None
        saha_txt = {"ic": " (iç sahada)", "dis": " (deplasmanda)"}.get(saha, "")
        head = (f"📊 <b>{tname}</b> – son {len(ms)} maç{saha_txt}\n"
                f"<i>Skorlar {tname} önce yazıldı • E: evinde, D: deplasmanda</i>")
        return self.compute(head, tname, olcut, e, ms)

    def compute(self, head, tname, olcut, e, ms) -> str:
        es = 0.5 if e is None else e
        lines, hits, vals = [], 0, []

        def row(x, mark, extra=""):
            sk = f"{x['gf']}-{x['ga']}"
            iy = f" (İY {x['hf']}-{x['ha']})" if x.get("hf") is not None else ""
            return f"{mark} {x['tarih']} {x['rakip']} ({x['saha']}) {sk}{iy}{extra}"

        check = {
            "mac_gol_ust": lambda x: x["gf"] + x["ga"] > es,
            "iy_gol_ust": lambda x: None if x["hf"] is None else x["hf"] + x["ha"] > es,
            "iy2_gol_ust": lambda x: None if x["hf"] is None else (x["gf"] + x["ga"] - x["hf"] - x["ha"]) > es,
            "takim_gol_ust": lambda x: x["gf"] > es,
            "takim_iy_gol_ust": lambda x: None if x["hf"] is None else x["hf"] > es,
            "yedigi_gol_ust": lambda x: x["ga"] > es,
            "kg": lambda x: x["gf"] > 0 and x["ga"] > 0,
        }
        if olcut in check:
            for x in ms:
                r = check[olcut](x)
                if r is None:
                    lines.append(row(x, "▫️", " – veri yok"))
                    continue
                hits += r
                vals.append(r)
                lines.append(row(x, "✅" if r else "❌"))
            label = OLCUTLER[olcut].format(t=tname, e=f"{es:.1f}")
            n = len(vals)
            summary = f"<b>{label}: {hits}/{n}</b> (%{round(100 * hits / n) if n else 0})"
            return "\n".join([head, summary, "", *lines])
        if olcut in ("korner_toplam", "takim_korner", "kart_toplam", "takim_kart"):
            kind = "korner" if "korner" in olcut else "kart"
            for x in ms:
                own, opp = x.get(f"{kind}_own"), x.get(f"{kind}_opp")
                v = own if olcut.startswith("takim") else (None if own is None or opp is None else own + opp)
                if v is None:
                    lines.append(row(x, "▫️", f" – {kind} verisi yok"))
                    continue
                vals.append(v)
                mark = "•" if e is None else ("✅" if v > e else "❌")
                lines.append(row(x, mark, f" – {int(v)} {kind}"))
            if not vals:
                return f"{head}\n📭 Bu maçlar için {kind} verisi yok."
            label = OLCUTLER[olcut].format(t=tname, e="")
            summary = f"<b>{label}: toplam {int(sum(vals))}, maç başı {sum(vals) / len(vals):.1f}</b>"
            if e is not None:
                h = sum(v > e for v in vals)
                summary += f"\n<b>{e:.1f} üstü: {h}/{len(vals)}</b> (%{round(100 * h / len(vals))})"
            return "\n".join([head, summary, "", *lines])
        if olcut == "sonuc":
            w = sum(x["gf"] > x["ga"] for x in ms)
            d = sum(x["gf"] == x["ga"] for x in ms)
            lines = [row(x, "🟢" if x["gf"] > x["ga"] else "🟡" if x["gf"] == x["ga"] else "🔴") for x in ms]
            return "\n".join([head, f"<b>{w} galibiyet, {d} beraberlik, {len(ms) - w - d} mağlubiyet</b>", "", *lines])
        # genel özet
        n = len(ms)
        gf, ga = sum(x["gf"] for x in ms), sum(x["ga"] for x in ms)
        ust = sum(x["gf"] + x["ga"] > 2.5 for x in ms)
        kg = sum(x["gf"] > 0 and x["ga"] > 0 for x in ms)
        iy = [x for x in ms if x["hf"] is not None]
        iy05 = sum(x["hf"] + x["ha"] > 0.5 for x in iy)
        kor = [x["korner_own"] + x["korner_opp"] for x in ms if "korner_own" in x and "korner_opp" in x]
        kar = [x["kart_own"] + x["kart_opp"] for x in ms if "kart_own" in x and "kart_opp" in x]
        s = [f"⚽ Attığı {gf}, yediği {ga} (maç başı {gf / n:.1f} – {ga / n:.1f})",
             f"📈 2.5 üst: {ust}/{n} • KG: {kg}/{n}",
             f"⏱ İlk yarı 0.5 üst: {iy05}/{len(iy)}" if iy else "",
             f"🚩 Maç başı korner (toplam): {sum(kor) / len(kor):.1f}" if kor else "",
             f"🟨 Maç başı kart (toplam): {sum(kar) / len(kar):.1f}" if kar else ""]
        return "\n".join([head, *[x for x in s if x], "", *[row(x, "•") for x in ms]])
