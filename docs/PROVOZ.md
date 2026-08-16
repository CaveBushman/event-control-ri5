# Krabička v závodní den

Krátký seznam pro obsluhu u trati. Podrobnosti jsou v `README.md`.

## Před závodem (večer nebo ráno)

1. **Zapojit** — napájení, ethernet do stejného switche jako dekodéry.
2. **Podívat se na displej.** Do minuty má svítit zelená tečka a text
   *připojen jako … (organizace)*. Pod formulářem přibývají řádky spojení na
   dekodéry a kameru — všechny mají být zelené.
3. **V aplikaci** otevřít *Nastavení dekodérů* — u agenta má být zelená tečka
   a jméno krabičky. V horní liště mají svítit **Hill**, **Finish** a **Kamera**.
4. **Dohledat MAC adresy** — když projde, vidí krabička na dekodéry.

Když něco z toho nesvítí, jde o síť, ne o aplikaci: viz *Když to nejde* níž.

## Během závodu

Krabička nepotřebuje obsluhu. Na displeji je pořád vidět stav, takže se dá
jedním pohledem poznat, jestli spojení drží.

Když vypadne wifi nebo se přepojí kabel, agent se sám připojí znovu —
v aplikaci se to projeví jen tím, že kontrolky na chvíli zšednou.

## Po závodě

Nic. Krabička může zůstat zapojená; když se vypne, nic se neztratí —
průjezdy jsou v dekodérech i v aplikaci.

## Když to nejde

| Co je vidět | Co s tím |
|---|---|
| Displej ukazuje *nenastaveno* | Vyplnit adresu aplikace a párovací kód z *Nastavení dekodérů* (tlačítko **Spárovat krabičku**). |
| *Kód neplatí* | Kód platí půl hodiny a jen jednou — vydat v aplikaci nový. |
| *server token odmítl* | Někdo vydal nový token — spárovat krabičku znovu. |
| *server není k dispozici* | Krabička nemá internet. Zkontrolovat kabel a router. |
| Kontrolky Hill/Finish jsou červené | Krabička nevidí dekodéry: jiná síť, vypnutý switch, nebo špatná IP v *Nastavení dekodérů*. |
| Kamera červená | Software kamery neběží, nebo má jinou adresu než v *Nastavení aplikace*. |
| Displej je černý | Krabička běží dál, jen zhasla obrazovka — dotknout se jí. Agent na displeji nezávisí. |

Log krabičky (přes SSH):

```bash
journalctl -u event-control-agent -f
```

## Náhradní řešení, když krabička chybí

Program agenta se dá spustit na notebooku u trati — je to tentýž soubor:

```bash
python3 track_agent.py
```

Stáhne se v aplikaci v *Nastavení dekodérů* odkazem **Stáhnout agenta**.
Nastavení se pak zadává na `http://127.0.0.1:8088/`.
