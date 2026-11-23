#!/usr/bin/env python3
"""Stáhne aktuální slevy Ryobi z Bauhaus.cz a vygeneruje statickou HTML stránku.

Postup:
  1. Ze sitemap e-shopu (sitemap-index.xml) vybere všechny produkty, jejichž URL
     začíná na /ryobi-.
  2. Projde stránku každého produktu a přečte název, aktuální cenu,
     přeškrtnutou (původní) cenu a štítky jako „Doprodej“.
  3. Zlevněné produkty zapíše do HTML (a JSON) pro GitHub Pages.

Použití:
  python scripts/ryobi_slevy.py --out index.html
  python scripts/ryobi_slevy.py --limit 20 --debug-dir debug   # rychlý test

Závislosti: requests, beautifulsoup4
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.bauhaus.cz"
SITEMAP_INDEX = f"{BASE}/sitemap-index.xml"
BRAND_SLUG = "ryobi"
DEBUG = False
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Štítky, které znamenají, že je produkt v akci.
SALE_LABELS = ["Doprodej", "Výprodej", "Akce", "Leták", "Akční cena", "Sleva"]
INFO_LABELS = ["Pouze online", "Pouze v prodejně", "Exkluzivně v Bauhaus", "Novinka"]
CART_MARKERS = ["Přidat do košíku", "Doručení domů", "Rezervovat a vyzvednout", "Rezervovat na prodejně"]

PRICE_RE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[   ]\d{3})*|\d+)\s*,\s*(-|\d{2})(?!\d)(?!\s*/)"
)
SAVE_RE = re.compile(r"Ušetříte\s*(\d+)\s*%")
PRODUCT_URL_RE = re.compile(rf"^{re.escape(BASE)}/{BRAND_SLUG}-[^/?#]*-\d{{6,}}/?$", re.I)


@dataclass
class Product:
    url: str
    name: str = ""
    price: float | None = None          # aktuální (akční) cena
    original: float | None = None       # přeškrtnutá cena
    labels: list[str] = field(default_factory=list)
    save_pct_shown: int | None = None   # „Ušetříte X %“ z e-shopu (vůči min. ceně za 30 dní)
    error: str | None = None

    @property
    def discount(self) -> float | None:
        if self.price is not None and self.original and self.original > self.price:
            return self.original - self.price
        return None

    @property
    def discount_pct(self) -> float | None:
        d = self.discount
        return round(d / self.original * 100, 1) if d else None

    @property
    def on_sale(self) -> bool:
        return bool(self.discount) or any(l in SALE_LABELS for l in self.labels)


# --------------------------------------------------------------------------- HTTP

class Client:
    def __init__(self, delay: float, timeout: float = 30, retries: int = 3):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "cs-CZ,cs;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._last = 0.0

    def get(self, url: str) -> str:
        err: Exception | None = None
        for attempt in range(self.retries):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=self.timeout)
                self._last = time.monotonic()
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                # Bauhaus neposílá charset v hlavičce; requests by pak zvolil latin-1.
                return r.content.decode("utf-8", errors="replace")
            except requests.RequestException as e:
                err = e
                self._last = time.monotonic()
                time.sleep(2 ** attempt * 2)
        raise RuntimeError(f"{url}: {err}")


# --------------------------------------------------------------------------- sitemap

def _locs(xml_text: str) -> tuple[list[str], list[str]]:
    """Vrátí (vnořené sitemapy, url) ze sitemap XML."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    locs = [e.text.strip() for e in root.iter(f"{ns}loc") if e.text]
    if root.tag.endswith("sitemapindex"):
        return locs, []
    return [], locs


def product_urls(client: Client) -> list[str]:
    queue, seen, found = [SITEMAP_INDEX], set(), []
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            nested, urls = _locs(client.get(sm))
        except Exception as e:  # noqa: BLE001
            print(f"  ! sitemap {sm}: {e}", file=sys.stderr)
            continue
        queue += nested
        hits = [u for u in urls if PRODUCT_URL_RE.match(u)]
        print(f"  sitemap {sm}: {len(urls)} url, {len(hits)} Ryobi")
        found += hits
    return sorted(set(u.rstrip("/") for u in found))


# --------------------------------------------------------------------------- produkt

def _num(s: str) -> float:
    whole, dec = PRICE_RE.match(s).groups() if PRICE_RE.match(s) else (s, "-")
    v = float(re.sub(r"\D", "", whole))
    if dec and dec != "-":
        v += int(dec) / 100
    return v


def _strings(node: Tag) -> list[tuple[str, bool]]:
    """Textové uzly v pořadí dokumentu: (text, je_uvnitř_odkazu)."""
    out = []
    for s in node.descendants:
        if isinstance(s, NavigableString) and not isinstance(s, type(None)):
            t = " ".join(str(s).split())
            if t and s.parent and s.parent.name not in ("script", "style", "noscript", "template"):
                out.append((t, s.find_parent("a") is not None))
    return out


def parse_product(page: str, url: str) -> Product:
    soup = BeautifulSoup(page, "html.parser")
    p = Product(url=url)

    h1 = soup.find("h1")
    if h1:
        p.name = " ".join(h1.get_text(" ").split())
    if not p.name:
        og = soup.find("meta", property="og:title")
        p.name = (og.get("content") or "").strip() if og else url.rsplit("/", 1)[-1]

    for t in soup(["script", "style", "noscript", "template", "header", "footer", "nav"]):
        if t.name == "script" and t.get("type") == "application/ld+json":
            continue
        t.decompose()

    body = soup.body or soup
    strings = _strings(body)
    texts = [t for t, _ in strings]

    # najdi nadpis produktu v proudu textu
    start = 0
    if p.name:
        first_word = p.name.split()[0]
        for i, t in enumerate(texts):
            if t == p.name or (first_word in t and t in p.name and len(t) > 10):
                start = i
                break

    # konec cenového bloku = první tlačítko košíku / doručení
    end = min(len(texts), start + 80)
    for i in range(start, min(len(texts), start + 200)):
        if any(m in texts[i] for m in CART_MARKERS):
            end = i
            break

    window = " ".join(texts[start + 1:end])
    if DEBUG:
        print("  DEBUG start/end", start, end, "| before:", texts[max(0, start - 15):start + 1])
        print("  DEBUG window:", repr(window[:1500]))
    prices = [_num(m.group(0)) for m in PRICE_RE.finditer(window)]
    prices = [x for x in prices if x > 0]
    if prices:
        if len(prices) >= 2 and prices[0] != prices[1]:
            p.original, p.price = max(prices[:2]), min(prices[:2])
        else:
            p.price = prices[0]

    m = SAVE_RE.search(window)
    if m:
        p.save_pct_shown = int(m.group(1))

    # štítky: kousek nad nadpisem až po konec cenového bloku, mimo odkazy
    lab_from = max(0, start - 40)
    for t, in_link in strings[lab_from:end]:
        if in_link:
            continue
        for lab in SALE_LABELS + INFO_LABELS:
            if t.lower() == lab.lower() and lab not in p.labels:
                p.labels.append(lab)

    # záloha: JSON-LD s cenou
    if p.price is None:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except json.JSONDecodeError:
                continue
            for d in data if isinstance(data, list) else [data]:
                offers = d.get("offers") if isinstance(d, dict) else None
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict) and offers.get("price"):
                    p.price = float(str(offers["price"]).replace(",", "."))
    if p.price is None:
        p.error = "cena nenalezena"
    return p


# --------------------------------------------------------------------------- výstup

def fmt_kc(v: float | None) -> str:
    if v is None:
        return ""
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def render(products: list[Product], generated: datetime, total: int) -> str:
    sale = sorted([p for p in products if p.discount], key=lambda p: -(p.discount_pct or 0))
    clearance = sorted([p for p in products if p.on_sale and not p.discount], key=lambda p: p.name)
    e = html.escape

    def row(p: Product, with_disc: bool) -> str:
        labels = ", ".join(p.labels)
        cells = [
            f'<td data-v="{e(p.name.lower())}"><a href="{e(p.url)}" target="_blank" rel="noopener">{e(p.name)}</a>'
            + (f'<div class="lab">{e(labels)}</div>' if labels else "") + "</td>",
        ]
        if with_disc:
            cells += [
                f'<td class="n" data-v="{p.original}">{fmt_kc(p.original)}&nbsp;Kč</td>',
                f'<td class="n" data-v="{p.price}">{fmt_kc(p.price)}&nbsp;Kč</td>',
                f'<td class="n" data-v="{p.discount}">{fmt_kc(p.discount)}&nbsp;Kč</td>',
                f'<td class="n b" data-v="{p.discount_pct}">{str(p.discount_pct).replace(".", ",")}&nbsp;%</td>',
            ]
        else:
            cells += [f'<td class="n" data-v="{p.price}">{fmt_kc(p.price)}&nbsp;Kč</td>']
        cells.append(f'<td class="u"><a href="{e(p.url)}" target="_blank" rel="noopener">{e(p.url.replace(BASE, ""))}</a></td>')
        return "<tr>" + "".join(cells) + "</tr>"

    def th(label: str, typ: str) -> str:
        return f'<th><button data-t="{typ}">{label}</button></th>'

    t1 = "".join(row(p, True) for p in sale) or '<tr><td colspan="6">Žádné slevy s původní cenou.</td></tr>'
    t2 = "".join(row(p, False) for p in clearance) or '<tr><td colspan="3">Nic.</td></tr>'
    when = generated.strftime("%-d. %-m. %Y %H:%M")
    top = sale[0] if sale else None

    return f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Slevy Ryobi na Bauhaus.cz</title>
<style>
:root{{color-scheme:light dark;--bg:light-dark(#fff7f4,#1c1512);--card:light-dark(#fff,#271d19);--ink:light-dark(#1a1817,#f3e9e5);--mut:light-dark(#5d524e,#b8aaa4);--acc:light-dark(#0d7680,#4fb7c0);--hi:light-dark(#990f3d,#ff7aa3);--ln:light-dark(#ecd3cb,#3d2e28)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:32px max(4vw,14px)}}
h1{{font-size:clamp(28px,4vw,42px);margin:0 0 8px;line-height:1.1}}h2{{margin:36px 0 4px;font-size:22px}}
.mut{{color:var(--mut);font-size:14px}}.kpi{{display:flex;flex-wrap:wrap;gap:12px 40px;margin:20px 0}}
.kpi div{{color:var(--mut);font-size:14px}}.kpi b{{display:block;font-size:32px;color:var(--ink)}}
input{{font:inherit;padding:10px 12px;width:min(100%,380px);border:1px solid var(--ln);border-radius:6px;background:var(--card);color:var(--ink);margin:12px 0}}
.wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;background:var(--card)}}
th{{text-align:left;font-size:13px;color:var(--mut);border-bottom:2px solid var(--ink);padding:8px 10px;white-space:nowrap}}
th button{{all:unset;cursor:pointer}}th button::after{{content:" ↕";opacity:.4}}th button[aria-sort=ascending]::after{{content:" ↑";opacity:1}}th button[aria-sort=descending]::after{{content:" ↓";opacity:1}}
td{{padding:8px 10px;border-bottom:1px solid var(--ln);vertical-align:top}}td a{{color:var(--ink)}}
.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}.b{{font-weight:700;color:var(--hi)}}
.lab{{font-size:12px;color:var(--mut)}}.u{{font-size:12px;max-width:22em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.u a{{color:var(--acc)}}
</style></head><body><main>
<h1>Slevy Ryobi na Bauhaus.cz</h1>
<p class="mut">Aktualizováno {when} · prošlo se {total} produktů Ryobi z e-shopu · ceny přebrané z bauhaus.cz, v prodejnách se mohou lišit.</p>
<div class="kpi">
<div><b>{len(sale)}</b>slev s původní cenou</div>
<div><b>{len(clearance)}</b>v doprodeji / akci bez původní ceny</div>
{f'<div><b>{str(top.discount_pct).replace(".", ",")}&nbsp;%</b>největší sleva ({e(top.name)})</div>' if top else ''}
</div>
<h2>Slevy s původní cenou</h2>
<p class="mut">Sleva = přeškrtnutá cena minus aktuální cena. Klikem na záhlaví sloupce řadíte.</p>
<input type="search" placeholder="Hledat produkt…" data-for="t1" aria-label="Hledat ve slevách">
<div class="wrap"><table id="t1"><thead><tr>{th("Produkt","s")}{th("Původní cena","n")}{th("Akční cena","n")}{th("Sleva Kč","n")}{th("Sleva %","n")}{th("Odkaz","s")}</tr></thead><tbody>{t1}</tbody></table></div>
<h2>Doprodej a akce bez původní ceny</h2>
<p class="mut">E-shop u nich ukazuje štítek akce/doprodeje, ale ne přeškrtnutou cenu.</p>
<input type="search" placeholder="Hledat produkt…" data-for="t2" aria-label="Hledat v doprodeji">
<div class="wrap"><table id="t2"><thead><tr>{th("Produkt","s")}{th("Cena","n")}{th("Odkaz","s")}</tr></thead><tbody>{t2}</tbody></table></div>
</main>
<script>
document.querySelectorAll('input[data-for]').forEach(i=>i.addEventListener('input',()=>{{const q=i.value.toLowerCase();document.querySelectorAll('#'+i.dataset.for+' tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))}}));
document.querySelectorAll('th button').forEach(b=>b.addEventListener('click',()=>{{const th=b.parentElement,tb=th.closest('table').tBodies[0],ix=[...th.parentElement.children].indexOf(th),num=b.dataset.t==='n',asc=b.getAttribute('aria-sort')!=='ascending';th.parentElement.querySelectorAll('button').forEach(x=>x.removeAttribute('aria-sort'));b.setAttribute('aria-sort',asc?'ascending':'descending');const v=r=>{{const c=r.children[ix];const d=c?.dataset.v??c?.textContent??'';return num?parseFloat(d)||0:d}};[...tb.rows].sort((a,c)=>{{const x=v(a),y=v(c);return (num?x-y:String(x).localeCompare(String(y),'cs'))*(asc?1:-1)}}).forEach(r=>tb.appendChild(r))}}));
</script>
</body></html>
"""


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="index.html", help="cesta k výstupnímu HTML")
    ap.add_argument("--json", default=None, help="cesta k JSON s daty (výchozí: vedle HTML jako data.json)")
    ap.add_argument("--delay", type=float, default=0.5, help="pauza mezi požadavky v sekundách")
    ap.add_argument("--limit", type=int, default=0, help="zpracovat jen prvních N produktů (test)")
    ap.add_argument("--urls", nargs="*", help="zpracovat jen zadané URL produktů (test)")
    ap.add_argument("--debug-dir", default=None, help="uložit HTML prvních produktů pro ladění")
    ap.add_argument("--min-products", type=int, default=50,
                    help="selhat, pokud se najde méně produktů (chrání stránku před prázdným přepsáním)")
    ap.add_argument("--verbose", action="store_true", help="vypsat ladicí výřezy textu")
    a = ap.parse_args()
    global DEBUG
    DEBUG = a.verbose

    client = Client(delay=a.delay)
    print("Hledám produkty Ryobi v sitemap…")
    urls = a.urls or product_urls(client)
    if a.limit:
        urls = urls[: a.limit]
    print(f"Produktů ke kontrole: {len(urls)}")
    if not urls or (not a.urls and not a.limit and len(urls) < a.min_products):
        print(f"Chyba: nalezeno jen {len(urls)} produktů (< {a.min_products}). Stránku nepřepisuji.", file=sys.stderr)
        return 2

    dbg = Path(a.debug_dir) if a.debug_dir else None
    if dbg:
        dbg.mkdir(parents=True, exist_ok=True)

    products: list[Product] = []
    for i, u in enumerate(urls, 1):
        try:
            page = client.get(u)
            if dbg and i <= 5:
                (dbg / f"{u.rsplit('-', 1)[-1]}.html").write_text(page, encoding="utf-8")
            p = parse_product(page, u)
        except Exception as e:  # noqa: BLE001
            p = Product(url=u, name=u.rsplit("/", 1)[-1], error=str(e))
        products.append(p)
        flag = f"SLEVA {p.discount_pct} %" if p.discount else ("akce" if p.on_sale else "")
        print(f"[{i}/{len(urls)}] {p.name[:60]:60} {fmt_kc(p.price):>8} {fmt_kc(p.original):>8} {flag} {p.error or ''}")

    errors = [p for p in products if p.error]
    ok = len(products) - len(errors)
    print(f"\nHotovo: {ok} načteno, {len(errors)} chyb, "
          f"{sum(1 for p in products if p.discount)} slev, "
          f"{sum(1 for p in products if p.on_sale and not p.discount)} akcí bez původní ceny.")
    if products and ok < len(products) * 0.5:
        print("Chyba: víc než polovina produktů selhala. Stránku nepřepisuji.", file=sys.stderr)
        return 3

    now = datetime.now(ZoneInfo("Europe/Prague"))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(products, now, len(products)), encoding="utf-8")
    jpath = Path(a.json) if a.json else out.with_name("data.json")
    jpath.write_text(json.dumps({
        "generated": now.isoformat(timespec="seconds"),
        "checked": len(products),
        "products": [asdict(p) | {"discount": p.discount, "discount_pct": p.discount_pct}
                     for p in products if p.on_sale],
        "errors": [asdict(p) for p in errors],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Zapsáno: {out} a {jpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
