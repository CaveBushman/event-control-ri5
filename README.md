# Event Control — krabička u trati

*Ri5 = **R**aspberry P**i 5**. Není to překlep, neopravovat.*

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
| Displej | oficiální 7" dotykový, nebo malý HDMI (dotyk není potřeba — nic se na něm nepíše) |
| Ethernet kabel | wifi funguje, ale u trati je drát spolehlivější |

Displej je kvůli obsluze, ne kvůli funkci: krabička bez displeje dělá totéž
a stav se dá otevřít z notebooku v síti.

## Instalace

1. Do Raspberry Pi Imageru vyberte **Raspberry Pi OS (64-bit)**, v nastavení
   zapněte SSH a vyplňte uživatele. Kartu nastrčte do Pi a nastartujte.
2. Zkopírujte na Pi tenhle adresář (nebo `git clone`) a spusťte:

   ```bash
   sudo bash deploy.sh
   ```

   Jeden příkaz nastaví všechno: balíčky, agenta jako službu, displej,
   watchdog i časové pásmo. Trvá to minutu a nic se neptá. **Adresa aplikace
   se nezadává** — krabička se hlásí na `bikody.com`. Vlastní server se dá
   přidat přepínačem `--server`.

   *Proč `bash` a ne `./deploy.sh`: kopírování na Pi umí sebrat souboru právo
   ke spuštění (scp, rozbalený archiv, klon s vypnutým `core.fileMode`) a
   `sudo ./deploy.sh` pak hlásí „command not found". Přes `bash` to jde vždy;
   kdo chce, může si právo vrátit: `chmod +x deploy.sh`.*
3. Na displeji se ukáže **token krabičky** — šest čtveřic na dvou řádcích. Opište ho
   v aplikaci do *Nastavení aplikace* → **Přihlásit krabičku**. To je jediný
   krok, který po instalaci zbývá.
4. Do pěti vteřin naskočí na displeji velké zelené **OK**.

Co skript umí navíc:

```bash
sudo bash deploy.sh --hostname krabicka-brno \
                    --static-ip 192.168.9.10/24 --gateway 192.168.9.1
bash deploy.sh --dry-run          # jen vypíše, co by udělal, a nic nezmění
sudo bash deploy.sh --no-kiosk    # krabička bez displeje
```

Pouštět se dá opakovaně — je to nastavení, ne instalace. **Token se přitom
nikdy nepřepíše**, protože ho obsluha má opsaný v aplikaci.

Token vyrábí krabička, ne aplikace: na dotykovém displeji se nic nepíše,
opisuje se tam, kde je klávesnice. **Přihlášená krabička token schová** —
zůstane z něj jen začátek a konec, protože klíč do klubové sítě nemá viset
celý den na obrazovce u trati. Celý je v jejím nastavení.

## Co se stane po zapnutí zdroje

Nic se nespouští ručně a nikdo se nikam nepřihlašuje:

1. Systemd nastartuje **agenta** (`event-control-agent`) — hlásí se aplikaci
   a přeposílá data. Běží jako systémová služba, takže na ploše nezávisí.
2. Systemd nastartuje **displej** (`event-control-kiosk@<uživatel>`) — přes
   `cage` (minimální Wayland kompozitor) pustí prohlížeč na holé obrazovce.
   Proto se nemusí zapínat automatické přihlášení ani instalovat plocha.
3. Displej počká, až se agent ozve, a teprve pak otevře jeho stránku. Chybová
   stránka prohlížeče totiž vypadá jako rozbitá krabička.
4. Když prohlížeč spadne, systemd ho do tří vteřin zvedne. Když zamrzne celé
   Pi, restartuje ho **hardwarový watchdog** do půl minuty.

Obojí se dá zkontrolovat:

```bash
systemctl status event-control-agent
systemctl status event-control-kiosk@$USER
```

Agent na displeji nezávisí: i s černou obrazovkou jede časomíra dál.

## Co je na displeji

```
                 BIKODY.COM
              STAV SERVERU:
                ✓   OK          ← zelená: hlásí se aplikaci
     připojen jako … (organizace)
             TOKEN KRABIČKY:
             AKUW–…–7G59
   AKTUALIZOVÁNO: 16.08.2026 21:11:43
```

Tři stavy, které displej ukazuje:

| Stav | Co znamená |
|---|---|
| **NASTAVIT** (žlutá) | V nastavení krabičky je smazaná adresa aplikace. Běžně nenastane — adresa je předvyplněná. |
| **ČEKÁ** (červená) | Token je vidět celý; opište ho v aplikaci. Červeně, aby se od OK lišilo přes půl závodiště. |
| **OK** (zelená) | Krabička se hlásí aplikaci a přeposílá data. |

## Jak poznat, že to jede

* Na displeji krabičky svítí zelené **OK** a text *připojen jako … (organizace)*.
* V nastavení krabičky přibývají řádky **posledních spojení** — kdy, kam a jak
  to dopadlo. To je u trati nejrychlejší způsob, jak poznat, že dekodéry
  a kamera odpovídají.
* V aplikaci v **Nastavení aplikace** je u agenta zelená tečka a jméno stroje.
* V horní liště aplikace svítí kontrolky **Hill**, **Finish** a **Kamera**.
* Tlačítko **Dohledat MAC adresy** projde i z produkce.
* **Průjezdy naskakují do Parsingu** — to je ta hlavní věc, kvůli které
  krabička je. Odběr se ptá dekodérů po sekundě a půl na to, co ještě
  nedorazilo. Když se nic neobjevuje, bývá to dnem závodu: dekodér drží pamět
  i z předchozích závodů a aplikace průjezdy z jiného dne zahazuje.

Když krabička neběží, aplikace se chová jako dřív a spojení zkouší navázat
sama — u trati to nefunguje, ale nic se nerozbije.

## Údržba

```bash
sudo systemctl status event-control-agent     # stav
journalctl -u event-control-agent -f          # log
sudo bash scripts/update.sh                   # nová verze agenta ze serveru
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
sudo bash scripts/set-static-ip.sh 192.168.9.10/24 192.168.9.1
```

**Adresy dekodérů a kamery se do krabičky nezadávají** — patří do aplikace
(*Nastavení dekodérů*, *Nastavení aplikace*). Dvě místa pravdy by si jednou
přestala odpovídat.

## Bezpečnost

Token je **heslo do vaší klubové sítě**: kdo ho má, může přes krabičku otevřít
TCP spojení kamkoliv v ní. Proto:

* přihlášená krabička token na displeji nezobrazuje celý;
* nastavení je v `/opt/event-control-agent/config.json` s právy `600`;
* nový token se vyrábí jen na výslovné přání — tlačítkem **Nový token** na
  displeji (jištěné dvěma klepnutími) nebo v nastavení krabičky — a musí se
  pak znovu opsat v aplikaci;
* stránka krabičky je dostupná v celé místní síti (proto se na ni dostanete
  z notebooku) — nepatří tedy do veřejné wifi pro diváky.

## Návrh displeje

Podoba obrazovky vychází z `docs/navrh-displeje.html` — zadání, jak má
krabička vypadat. Skutečný displej je vlastní stránka agenta (bez Tailwindu
z CDN: u trati se nespoléhá na nic, co se stahuje).

## Odkud se bere agent

Zdrojem pravdy je hlavní repozitář aplikace
([CaveBushman/event-control](https://github.com/CaveBushman/event-control)),
soubor `tools/track_agent.py` — tam se agent vyvíjí a tam je pokrytý testy.
Tady je jeho kopie v `agent/track_agent.py`, aby se krabička dala postavit
i bez přístupu k němu. `scripts/update.sh` si stáhne aktuální verzi přímo
z vašeho serveru.
