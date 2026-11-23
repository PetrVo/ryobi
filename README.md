# Slevy Ryobi na Bauhaus.cz

Stránka `index.html` (na webu https://petrvo.github.io/ryobi/) ukazuje aktuálně zlevněné produkty Ryobi z e-shopu Bauhaus.cz.

## Aktualizace na vyžádání

GitHub → záložka **Actions** → **Slevy Ryobi** → **Run workflow**. Za pár minut workflow stáhne ceny, přegeneruje stránku a commitne ji; GitHub Pages ji pak publikuje.

## Lokální spuštění

```bash
pip install -r scripts/requirements.txt
python scripts/ryobi_slevy.py --out index.html
# rychlý test na 20 produktech:
python scripts/ryobi_slevy.py --limit 20 --out /tmp/ryobi.html
```

## Jak to funguje

1. Ze sitemap e-shopu (`sitemap-index.xml`) se vyberou všechny produkty s URL `/ryobi-…`.
2. Projde se stránka každého produktu (s pauzou 0,5 s mezi požadavky, celý běh trvá zhruba 15–20 minut) a přečte se název, aktuální cena, přeškrtnutá cena a štítky („Doprodej“ apod.).
3. Do stránky jdou dvě tabulky: slevy s původní cenou (sleva v Kč a %) a doprodej/akce bez původní ceny. Vedle vzniká i `data.json`.

Když se najde podezřele málo produktů nebo selže víc než polovina stránek, skript skončí chybou a stávající stránku nepřepíše.
