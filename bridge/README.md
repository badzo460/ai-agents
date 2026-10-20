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
