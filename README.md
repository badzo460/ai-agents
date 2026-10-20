# AURUM · XAUUSD agent control room

Kontrolna soba za šest analitičkih agenata, simulaciju trgovanja i povezivanje s PU Prime MetaTrader 5 računom. **Web aplikaciju razvijate i objavljujete s Maca; MT5 terminal i Python most rade na Windows x64 računalu ili VPS-u.** Instrument je isključivo XAUUSD, uključujući eksplicitne brokerske sufikse poput `XAUUSD.m`.

Instalacija je namijenjena jednom vlasniku i jednom povezanom MT5 računu. Početno stanje je jasno označena simulacija s virtualnim saldom 10.000 USD i pauziranim nalozima.

## Pokretanje demo stranice na Macu

1. Koristite **macOS 13.5 ili noviji** i instalirajte **Node.js 24 za macOS** s [nodejs.org](https://nodejs.org), uz odgovarajuću verziju za Apple Silicon (arm64) ili Intel (x64) Mac. Projekt zahtijeva najmanje Node.js 22.12. Ponovno otvorite Terminal nakon instalacije.
2. Preuzmite projekt naredbama ispod. Ako Git još nije instaliran, macOS može zatražiti instalaciju Command Line Tools. Alternativno na GitHubu odaberite **Code → Download ZIP**, raspakirajte arhivu i otvorite Terminal u raspakiranoj mapi; tada preskočite prve dvije naredbe.

```sh
git clone https://github.com/badzo460/ai-agents.git
cd ai-agents
npm ci
npm run setup:local
npm run dev
```

`setup:local` stvara `.dev.vars` iz primjera samo ako datoteka još ne postoji. Postojeće lokalne postavke čuva. `npm run dev` gradi aplikaciju i pokreće Vite na portu 5173 te lokalni Cloudflare Worker na portu 8787. U pregledniku na Macu otvorite adresu koju Vite ispiše uz **Local**. Ostavite Terminal otvoren; **Control + C** zaustavlja razvojne procese.

U lokalnom razvoju `DEV_MODE=true` omogućava pristup bez lozinke samo na loopback adresama. Uključivanje prekidača **Automatsko trgovanje** u demo sobi izvršava simulirane naloge. Za povezivanje sa zasebnim Windows MT5 računalom prvo objavite aplikaciju na javnoj HTTPS adresi prema sljedećem poglavlju: Macov lokalni razvojni server nije adresa za Windows most.

Python i MetaTrader paket potrebni su na Windows računalu za stvarni most. Nisu preduvjet za web demo na Macu.

## Cloudflare objava s Maca

Aplikacija se objavljuje kao **Cloudflare Worker s ugrađenim webom i Durable Objectom**. Potrebna je cijela konfiguracija iz `wrangler.jsonc`; sama mapa `dist` na statičkom hostingu ne uključuje API ni stanje računa.

U novom prozoru Terminala, iz mape projekta:

```sh
npx wrangler login
npm run deploy
```

Prijavite se u Cloudflare kroz preglednik koji otvori prva naredba. `deploy` izvršava build, stvara Worker i primjenjuje SQLite Durable Object migraciju `TradingRoom`. Terminal ispisuje adresu objavljene aplikacije. Dok ne postavite sljedeće tajne, javni API odbija pristup uz poruku o potrebnoj konfiguraciji.

Postavite vlastitu lozinku aplikacije, najmanje **12 znakova**:

```sh
npx wrangler secret put APP_PASSWORD
```

Zatim na Macu generirajte nasumični ključ za sesije i kopirajte ga u međuspremnik:

```sh
openssl rand -hex 32 | pbcopy
npx wrangler secret put SESSION_SECRET
```

U sigurni unos druge naredbe zalijepite vrijednost s **Command + V** i potvrdite. `SESSION_SECRET` mora imati najmanje 32 znaka. Tajne unosite kroz Wrangler, bez spremanja u Git ili chat. Nakon toga otvorite objavljenu aplikaciju i prijavite se lozinkom `APP_PASSWORD`. Vlastitu domenu možete dodati u postavkama Cloudflare Workera.

**U objavljenom Workeru `DEV_MODE` mora biti izostavljen.** Lokalna `.dev.vars` služi razvoju na vašem računalu. Podržana je jedna privatna kontrolna soba; za javni SaaS s više korisnika potrebno je zasebno implementirati identitet i izolaciju računa.

## PU Prime MT5 na Windowsu

Mac koristite za web i upravljanje preko preglednika. **MT5 terminal i most ostaju na Windows x64 računalu ili Windows VPS-u** i moraju stalno raditi za automatizaciju. Most se izlaznim HTTPS zahtjevima povezuje s objavljenim Workerom; ne trebate javno otvarati MT5 port. Detalji se nalaze i u [uputama mosta](bridge/README.md).

1. Na Windows računalu instalirajte PU Prime MT5 i prijavite se na **demo račun** koristeći točan brokerski server. Brokersku lozinku unosite samo lokalno u MT5.
2. U MT5 uključite algoritamsko trgovanje i dopustite vanjski Python API u postavkama Expert Advisors. U Market Watchu uključite XAUUSD i otvorite M5 graf. Ako račun koristi sufiks, koristite točan naziv, primjerice `XAUUSD.m`.
3. Na istom Windows računalu instalirajte **Python 3.12 x64** i preuzmite ovaj projekt. Otvorite PowerShell u mapi projekta i izvršite:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r bridge/requirements.txt
```

Nije potrebno aktivirati virtualno okruženje niti mijenjati PowerShell execution policy.

4. Na **objavljenoj HTTPS stranici** kliknite **Poveži MT5 → Generiraj ključ**. Ključ se prikazuje samo u tom prozoru, vrijedi 24 sata i zamjenjuje prethodni ključ povezivanja. U Windows PowerShellu unesite ga bez prikazivanja:

```powershell
$bridgeSecret = Read-Host 'Ključ MT5 mosta' -AsSecureString
$env:AURUM_BRIDGE_TOKEN = [System.Net.NetworkCredential]::new('', $bridgeSecret).Password
```

5. Kopirajte URL mosta iz web prozora i umjesto `URL_IZ_PROZORA` upišite tu cijelu HTTPS adresu koja završava s `/api/bridge`:

```powershell
.\.venv\Scripts\python.exe bridge/mt5_bridge.py --url "URL_IZ_PROZORA" --token-env AURUM_BRIDGE_TOKEN --symbol XAUUSD
```

URL je adresa vašeg Cloudflare Workera, a ne Macova loopback adresa. Ako je brokerski simbol drukčiji, prilagodite `--symbol`. Terminal i prozor mosta ostavite otvorene.

6. Pričekajte da web prikaže povezani račun. Provjerite identitet **`login@server`**, vrstu računa, valutu i spread prije omogućavanja automatizacije. Za početak postavite **Maksimalno pozicija = 1**: Windows most uvijek dopušta najviše jednu otvorenu poziciju na cijelom računu, čak i ako u web postavkama odaberete veći limit.

Most prenosi M5 svijeće, stanje računa i sve otvorene pozicije. Provjerava simbol, identitet računa i servera, rok naloga, spread, odstupanje cijene, lot, SL/TP i marginu. Lokalni SQLite dnevnik i red potvrda čuvaju ishod naloga kroz restart i sprječavaju ponovno slanje. Kod neizvjesnog izvršenja most se zaustavlja radi ručne provjere u MT5; nemojte brisati dnevnik radi ponovnog pokretanja prije provjere pozicija i povijesti.

### Vijesti i stvarni račun

**Vanjski ekonomski kalendar nije povezan.** Zbog toga zadani `requireNews` filter u MT5 načinu blokira nove naloge bez svježeg izvora. Za probu na demo računu možete ga svjesno isključiti u postavkama rizika; tada nema zaštite od ekonomskih objava. Simulirani događaji označeni su kao demo.

Stvarno trgovanje je zadano isključeno. Za stvarni račun potrebne su obje radnje: potvrda **`LIVE XAUUSD`** u web postavkama i zastavica **`--allow-live`** pri pokretanju Windows mosta. Automatizaciju zatim uključujete zasebno. Stvarna PU Prime veza i izvršavanje naloga nisu provjereni u ovom razvojnom okruženju.

Pauziranje zaustavlja nove naloge i povlači naloge koji još nisu preuzeti. **Postojeće pozicije ostaju otvorene** pod brokerskim SL/TP pravilima ili ih ručno zatvarate u MT5. Zahtjev koji je već stigao do brokera može se izvršiti nakon pritiska na pauzu.

## Agenti i provjere

- **SCOUT:** trend iz EMA 9/21 i režim tržišta.
- **NEWS:** svježina dostavljenih događaja i zabrana naloga tijekom označenog događaja.
- **SENTIMENT:** momentum izveden iz cijene; društvene mreže nisu povezane.
- **CHARTS:** RSI, ATR i tehnički uvjeti.
- **RISK:** lot prema brokerskim specifikacijama, spread, dnevni gubitak, svježina podataka i otvorene pozicije.
- **DECISION:** konsenzus analiza i prijedlog naloga uz zadovoljene kontrole.

Agenti koriste provjerljiva matematička pravila. LLM nije povezan; ocjena signala označava snagu pravila, a ne vjerojatnost profita. Strategija nema jamstvo prinosa i nije potvrđena backtestom na PU Prime podacima.

Iz mape projekta na Macu:

```sh
npm run build
npm test
npx playwright install chromium
npm run test:e2e
```

Offline testovi mosta dodatno zahtijevaju dostupan `python3`, ali ne traže MT5 paket:

```sh
npm run test:bridge
```

E2E testovi resetiraju lokalnu demo sobu i ključeve povezivanja; izvršavajte ih na lokalnom razvojnom okruženju. Lokalne razvojne provjere izvršene su na Linuxu. Build, TypeScript testovi, Python testovi s lažnim MT5 adapterom i E2E testovi prošli su i na izvornom macOS GitHub Actions runneru: [uspješna macOS provjera](https://github.com/badzo460/ai-agents/actions/runs/39500374434). Stvarna veza s Windows MT5 terminalom i izvršavanje naloga na PU Prime računu ostaju neprovjereni. Sama priprema projekta ne objavljuje aplikaciju na vašem Cloudflare računu.

## Arhitektura

```text
Mac: React / Vite razvoj i preglednik
      │ HTTPS + HttpOnly sesija
Cloudflare Worker ── Durable Object (stanje, agenti, kontrole, nalozi)
      │ HTTPS + vremenski ograničeni ključ mosta
Windows: Python most ── lokalni MT5 terminal ── PU Prime
```

Analitički modul je u `shared/engine.ts`, simulator u `shared/demo.ts`, Worker u `worker/index.ts`, a Windows most u `bridge/mt5_bridge.py`. `.dev.vars`, ovisnosti, izgrađene datoteke i lokalni dnevnici ignorirani su Gitom. Objavite izvorni kod bez tajni i uvijek zadržite provjeru TLS certifikata.
