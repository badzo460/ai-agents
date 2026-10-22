# Windows MT5 / PU Prime most

**Web aplikacija radi na Macu i objavljuje se s Maca. Ovaj most, Python 3.12 x64 i MT5 rade zajedno na Windows x64 računalu ili VPS-u.** MetaTrader5 Python paket iz ove mape nije predviđen za nativni macOS Python.

Za Windows most prvo objavite web na Cloudflareu. Koristite javni HTTPS URL koji prikaže **Poveži MT5** na objavljenoj stranici; Macov lokalni razvojni server nije adresa za ovo povezivanje. Brokersku lozinku unosite samo u MT5, a most koristi već prijavljeni terminal.

U PowerShellu, iz korijenske mape projekta:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r bridge/requirements.txt
.\.venv\Scripts\python.exe bridge/mt5_bridge.py --help
```

Za sve korake, siguran unos `AURUM_BRIDGE_TOKEN` ključa i naredbu povezivanja slijedite [glavni README](../README.md#pu-prime-mt5-na-windowsu). Nije potrebno aktivirati virtualno okruženje niti mijenjati execution policy. Uključite algoritamsko trgovanje i vanjski Python API u MT5 postavkama, otvorite brokerski XAUUSD M5 graf i počnite s demo računom.

Most dopušta najviše jednu otvorenu poziciju na cijelom računu. Identitet uključuje broj računa i brokerski server. Zadano blokira izvršavanje na stvarnom računu, a NEWS filter blokira nove naloge bez povezanog izvora vijesti. Stvarna PU Prime veza ovdje nije provjerena.

Terminal i most moraju ostati pokrenuti. Čuvajte lokalni SQLite dnevnik i red potvrda; neizvjesno izvršenje zahtijeva ručnu provjeru u MT5. Pauziranje novih naloga ne zatvara postojeće pozicije. Tajne ne spremajte u Git.

## Ako most javlja zastarjeli tick ili neispravno vrijeme

Zaustavite most s **Ctrl+C**, ostavite MT5 prijavljen i iz mape projekta pokrenite:

```powershell
.\.venv\Scripts\python.exe bridge\mt5_bridge.py --diagnose --symbol XAUUSD.s
```

Dijagnostika čita dvije kotacije u razmaku od pet sekundi. Ne traži URL ni ključ, ne ispisuje broj računa ili lozinku i ne šalje naloge ni web zahtjeve. Usporedite `PC UTC`, `Tick UTC`, `Starost ticka (PC - tick)` i `Tick napreduje`.

- Pozitivna starost iznad 30 sekundi znači da je tick stariji od sata računala; negativna ispod -5 sekundi znači da izgleda kao budući tick. Provjerite oba vremena prije zaključka je li zastala kotacija ili sat računala.
- Ako se cijena mijenja, a razlika vremena je stalno velika, uključite automatsko vrijeme i **Sync now** u Windows postavkama **Time & language → Date & time**, pa ponovite provjeru. Ne mijenjajte sat računala da bi odgovarao brokerovu prikazu.
- Ako tick ne napreduje, provjerite MT5 vezu, točan simbol i kotacije u Market Watchu (**Ctrl+M**). Tržište može biti u pauzi.
- Ako imate više MT5 terminala, navedite željeni `terminal64.exe` kroz `--terminal-path`.

### Potvrđeni brokerov UTC pomak

Zadano se vremenske oznake tumače kao UTC (`--broker-utc-offset-hours 0`). Ako je Windowsov UTC sat ispravan i dvije pomične kotacije pokazuju isti potvrđeni pomak, izričito ga navedite. Primjer izmjeren na PU Prime demo računu sa simbolom `XAUUSD.s`: **PC UTC 13:29:31, sirovo vrijeme ticka 16:29:31, razlika -10800 sekundi**. Za taj slučaj provjerite konverziju ovako:

```powershell
.\.venv\Scripts\python.exe bridge\mt5_bridge.py --diagnose --symbol XAUUSD.s --broker-utc-offset-hours 3
```

Dijagnostika prikazuje izvorni timestamp, brokerovo vrijeme prije konverzije, odabrani pomak i pretvoreni `Tick UTC`. Pretvoreno vrijeme treba biti blizu `PC UTC`, starost unutar dopuštenih granica, a `Tick napreduje: DA`. Zatim dodajte isti `--broker-utc-offset-hours 3` u uobičajenu naredbu mosta s vlastitim HTTPS URL-om i ključem iz varijable okruženja.

[PU Prime opisuje serverovo vrijeme kao GMT+2 zimi / GMT+3 ljeti](https://helpcenter.puprime.com/hc/en-001/articles/360004254656-What-is-PU-Prime-GMT-Offset). Dokumentacija servera ne dokazuje kodiranje svake vremenske oznake u Python API-ju. Zato nema automatskog pogađanja pomaka iz starosti ticka ni trajnog zadanog pomaka +3. Provjerite svoje podatke i ponovite mjerenje nakon sezonske promjene; pogrešan pomak i dalje blokira naloge. Postavka pretvara vrijeme tickova i M5 svijeća u UTC. Izvorni tick i primijenjeni pomak ostaju u telemetriji; UTC rokovi naloga ne mijenjaju se.

Kod nenultog pomaka vrijeme zapisa povijesti može imati drukčije kodiranje. Most zato upituje prošireni raspon povijesti i računa donju i gornju granicu realiziranog dnevnog P&L-a: gubitak uključuje ako pripada UTC danu po bilo kojem tumačenju, dobit samo ako pripada po oba. Donja granica prikazuje se kao **konzervativni dnevni P&L**; gornja služi za konzervativan početni saldo pri računanju limita. Uključeni su profit, commission, swap i fee BUY/SELL zapisa. Zasebne brokerske knjižbe naknada izvan tih zapisa nisu uključene. Ako se granice razlikuju, stari Worker bez podrške za takav izračun prima podatke računa, a novi most ne preuzima naloge dok se Worker ne ažurira.

Most nakon uspješnog slanja ispisuje **„MT5 podaci poslani; most povezan.”** Web prikazuje stanje računa i kada posljednja kotacija kasni, uz jasno upozorenje. Za izvršavanje naloga i dalje vrijede granice: UTC tick najviše 30 sekundi star i najviše 5 sekundi u budućnosti. NEWS filter je zasebna zaštita i ostaje uključen dok ga korisnik izričito ne promijeni ili ne poveže izvor vijesti.

Za novi prikaz povezanog računa ažurirajte Windows skriptu **i** objavljeni Cloudflare Worker. Sam GitHub commit nije potvrda da je Cloudflare objavio novu verziju.
