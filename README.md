# Event Control — krabička u trati

Raspberry Pi s displejem, které stojí u časomíry a **přeposílá data mezi
aplikací a železem na trati**. Aplikace Event Control běží na serveru
v datovém centru; dekodéry MyLaps a cílová kamera mají privátní adresy
v klubové síti (`192.168.x.y`), na které server nedosáhne. Krabička se serveru
sama hlásí a spojení naváže za něj.

```
   klubová síť u trati                        internet            datové centrum
 ┌───────────────────────┐                                     ┌────────────────┐
 │ dekodéry 192.168.9.x  │◄── TCP ──┐                          │  Event Control │
 │ kamera   192.168.1.x  │◄── TCP ──┤                          │     server     │
 └───────────────────────┘          │                          └────────────────┘
                            ┌───────┴────────┐   odchozí HTTPS          ▲
                            │    krabička    │─────────────────────────►│
                            └────────────────┘
```

**Ven jde jen odchozí HTTPS.** Na routeru pořadatele se nic neotevírá, nikde se
nenastavuje přesměrování portů a krabička nepotřebuje veřejnou adresu.

## Proč krabička, a ne program na notebooku

Program `track_agent.py` běží i na notebooku (Windows, macOS, Linux) a je to
tentýž soubor. Krabička má ale tři věci navíc, které se u trati počítají:

* **běží pořád** a nezávisí na tom, kdo má zapnutý notebook a jestli ho
  nezavřel;
* **je vidět** — na displeji svítí stav a nikdo se nemusí ptát, jestli to jede;
* **nikdo do ní nesahá** — nespouští se na ní nic jiného, takže nemá jak
  přestat fungovat.

## Co je potřeba koupit

| Díl | Poznámka |
|---|---|
| Raspberry Pi 5 (4 GB stačí) | 8 GB je zbytečné, agent je jeden proces |
| Aktivní chladič nebo krabička s ventilátorem | Pi 5 se u trati zahřeje |
| Napájení 27 W USB-C | originální; poddimenzované zdroje dělají restarty |
| microSD 32 GB (A2) nebo NVMe | SD stačí, zápisů je minimum |
| Displej | oficiální 7" dotykový, nebo malý HDMI |
| Ethernet kabel | wifi funguje, ale u trati je drát spolehlivější |

Displej je kvůli obsluze, ne kvůli funkci: krabička bez displeje dělá totéž
a stav se dá otevřít z notebooku v síti.

## Instalace

1. Do Raspberry Pi Imageru vyberte **Raspberry Pi OS (64-bit)**, v nastavení
   zapněte SSH a vyplňte uživatele. Kartu nastrčte do Pi a nastartujte.
2. Zkopírujte na Pi tenhle adresář (nebo `git clone`) a spusťte:

   ```bash
   sudo ./scripts/install.sh
   ```

   Skript nainstaluje agenta do `/opt/event-control-agent`, zapne službu a
   nastaví kiosk na displeji. Trvá to minutu a nic se neptá.
3. V nastavení krabičky (`http://<ip-krabicky>:8088/nastaveni`, nebo odkaz
   *nastavení* dole na displeji) vyplňte **adresu aplikace**.
4. Na displeji se ukáže **token krabičky** — šest čtveřic znaků. Opište ho
   v aplikaci do *Nastavení dekodérů* → **Přihlásit krabičku**.
5. Do pěti vteřin naskočí na displeji velké zelené **OK**.

Token vyrábí krabička, ne aplikace: na dotykovém displeji se nic nepíše,
opisuje se tam, kde je klávesnice. **Přihlášená krabička token schová** —
zůstane z něj jen začátek a konec, protože klíč do klubové sítě nemá viset
celý den na obrazovce u trati. Celý je v jejím nastavení.

## Co je na displeji

```
                 BIKODY.COM
              STAV SERVERU:
                ✓   OK          ← zelená: hlásí se aplikaci
     připojen jako … (organizace)
             TOKEN KRABIČKY:
             AKUW–…–7G59
   AKTUALIZOVÁNO: 16.08.2026 21:11:43 · nastavení
```

Tři stavy, které displej ukazuje:

| Stav | Co znamená |
|---|---|
| **NASTAVIT** (žlutá) | Chybí adresa aplikace — doplňte ji v nastavení krabičky. |
| **ČEKÁ** (žlutá) | Token je vidět celý; opište ho v aplikaci. |
| **OK** (zelená) | Krabička se hlásí aplikaci a přeposílá data. |

## Jak poznat, že to jede

* Na displeji krabičky svítí zelené **OK** a text *připojen jako … (organizace)*.
* V nastavení krabičky přibývají řádky **posledních spojení** — kdy, kam a jak
  to dopadlo. To je u trati nejrychlejší způsob, jak poznat, že dekodéry
  a kamera odpovídají.
* V aplikaci v **Nastavení dekodérů** je u agenta zelená tečka a jméno stroje.
* V horní liště aplikace svítí kontrolky **Hill**, **Finish** a **Kamera**.
* Tlačítko **Dohledat MAC adresy** projde i z produkce.

Když krabička neběží, aplikace se chová jako dřív a spojení zkouší navázat
sama — u trati to nefunguje, ale nic se nerozbije.

## Údržba

```bash
sudo systemctl status event-control-agent     # stav
journalctl -u event-control-agent -f          # log
sudo ./scripts/update.sh                      # nová verze agenta ze serveru
sudo systemctl restart event-control-agent    # restart
```

Agent umí jen tři věci — připojit se, poslat bajty, vrátit, co přišlo. Znalost
protokolů (MyLaps P3, XML cílové kamery) zůstává na serveru, takže **aktualizace
aplikace neznamená aktualizaci krabiček**. `update.sh` se hodí jen tehdy, když
se mění samotný způsob spojení.

## Síť

Krabička musí **vidět dekodéry a kameru** a **dostat se ven na HTTPS**. Nic víc.

Pevná adresa se hodí jen proto, abyste na její stránku trefili z notebooku;
spojení navazuje vždycky ona směrem ven. Nastaví se buď rezervací v DHCP na
routeru (jednodušší), nebo na krabičce:

```bash
sudo ./scripts/set-static-ip.sh 192.168.9.10/24 192.168.9.1
```

**Adresy dekodérů a kamery se do krabičky nezadávají** — patří do aplikace
(*Nastavení dekodérů*, *Nastavení aplikace*). Dvě místa pravdy by si jednou
přestala odpovídat.

## Bezpečnost

Token je **heslo do vaší klubové sítě**: kdo ho má, může přes krabičku otevřít
TCP spojení kamkoliv v ní. Proto:

* přihlášená krabička token na displeji nezobrazuje celý;
* nastavení je v `/opt/event-control-agent/config.json` s právy `600`;
* nový token se vyrábí jen na výslovné přání v nastavení krabičky (tichá
  výměna by ji odpojila) a musí se pak znovu opsat v aplikaci;
* stránka krabičky je dostupná v celé místní síti (proto se na ni dostanete
  z notebooku) — nepatří tedy do veřejné wifi pro diváky.

## Návrh displeje

Podoba obrazovky vychází z `docs/navrh-displeje.html` — zadání, jak má
krabička vypadat. Skutečný displej je vlastní stránka agenta (bez Tailwindu
z CDN: u trati se nespoléhá na nic, co se stahuje).

## Odkud se bere agent

Zdrojem pravdy je hlavní repozitář Event Control, soubor `tools/track_agent.py`.
Tady je jeho kopie v `agent/track_agent.py`, aby se krabička dala postavit
i bez přístupu k němu. `scripts/update.sh` si stáhne aktuální verzi přímo
z vašeho serveru.
