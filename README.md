# powerout

Javlja ti na Telegram i/ili e-mail kada nestane struja na tvojoj lokaciji, i kada se vrati.
Napisano u čistom Pythonu (3.11+), bez ijedne spoljne biblioteke.

```
⚡ Nestala struja: Stan
Struja je nestala u 14:03:12. Uređaj sada radi na rezervnom napajanju (baterija/UPS).

💡 Struja se vratila: Stan
Struja se vratila u 14:58:40. Nije je bilo 55 min 28 s.
```

## Zašto su potrebna dva dela

Uređaj kome nestane struja ne može sam da pošalje poruku, jer je i on ostao bez struje. Zato postoje tri
mehanizma koja se dopunjuju:

| Mehanizam | Gde radi | Kada javlja | Šta ti treba |
|---|---|---|---|
| **Lokalno otkrivanje** (`agent`) | uređaj koji preživi nestanak: laptop, računar ili Raspberry Pi na UPS-u | odmah (oko 10 s) | internet koji radi i bez struje (ruter na UPS-u ili mobilni hotspot) |
| **Stražar** (`watchdog`) | drugi računar koji **ne gubi** struju zajedno sa lokacijom (VPS, prijatelj, kancelarija) | kada agent prestane da se javlja (oko 3 min), čak i kad se sve na lokaciji ugasi | server koji radi non-stop |
| **Naknadni izveštaj** (`agent`) | isti uređaj, posle ponovnog pokretanja | kad se računar upali: „bio je ugašen od 14:03 do 14:58“ | ništa; stiže tek kad se struja vrati |

Šta izabrati:

| Na lokaciji imaš | Pokreni | Poruka stiže |
|---|---|---|
| običan desktop, bez UPS-a | agent (samo heartbeat) + stražar na drugom mestu | oko 3 min posle nestanka; naknadni izveštaj ako se računar sam upali |
| laptop | agent (+ stražar) | odmah, ako ruter ima struju ili laptop ima drugi internet |
| računar/Raspberry Pi na UPS-u | agent (+ stražar) | odmah |

Agent i stražar su isti program (`powerout agent`, `powerout watchdog`). Stražar je jedino pouzdan način da
saznaš za nestanak kada ruter ostane bez struje, jer tada nikakva poruka ne može da izađe sa lokacije.
Ako je laptop prijavio nestanak a nije imao internet, poruka čeka i stiže kasnije, sa napomenom da kasni.

## Brzi početak

### 1. Kanal za poruke

**Telegram** (najlakše):

1. U Telegramu otvori [@BotFather](https://t.me/BotFather), pošalji `/newbot` i prati pitanja. Dobijaš token oblika `123456789:AAH...`.
2. Otvori razgovor sa svojim novim botom i pošalji mu `/start` (bot ne može prvi da piše).
3. Upiši token u `config.toml` (vidi dole) i pokreni `powerout telegram-chat-id`. Ispisuje `chat_id = "..."` koji kopiraš u config.

**E-mail**: popuni sekciju `[email]` (SMTP server, korisnik, lozinka). Za Gmail uključi dvostepenu proveru i napravi
[lozinku aplikacije](https://myaccount.google.com/apppasswords), jer obična lozinka ne radi.

Možeš oba. Podrazumevano (`delivery = "all"`) poruka ide na svaki kanal. Sa `delivery = "fallback"` ide samo na prvi
koji uspe: prvo e-mail, pa Telegram.

### 2. Isprobavanje (bez instalacije)

```bash
mkdir -p ~/.config/powerout
cp config.example.toml ~/.config/powerout/config.toml
chmod 600 ~/.config/powerout/config.toml      # u njemu su tokeni i lozinke
nano ~/.config/powerout/config.toml

python3 -m powerout test           # šalje probnu poruku na svaki kanal i kaže šta je prošlo
python3 -m powerout check-power    # šta ovaj uređaj sam može da očita
```

`check-power --watch` prati promene uživo: na laptopu izvuci punjač pa ga vrati i vidi da li se stanje menja.
Na običnom desktopu ispisuje da nema šta da očitava. To je očekivano, njemu služi heartbeat + stražar.

### 3. Stražar (na mašini koja ne gubi struju)

Na toj mašini (VPS ili bilo šta što radi non-stop) napravi config sa kanalom za poruke i sekcijom `[watchdog]`:

```toml
[general]
timezone = "Europe/Belgrade"   # server je obično na UTC
[telegram]
token = "..."
chat_id = "..."
[watchdog]
token = "..."                  # python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Na lokaciji, u configu agenta, upiši gde je stražar i isti token:

```toml
[general]
name = "Stan"                  # ime lokacije; svaka lokacija svoje, i ne menjaj ga kasnije
[heartbeat]
url = "https://watchdog.example.com/heartbeat"
token = "..."
```

Stražar sluša na `127.0.0.1:8787` i treba mu HTTPS ispred, inače token putuje otvoreno. Najlakše je sa Caddy-jem:

```
watchdog.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Provera: `curl https://watchdog.example.com/health` treba da vrati `ok`. Kada agent počne da se javlja, stražar ga pamti;
posle `timeout_seconds` (180 s) tišine javlja „Nema signala“, a kada se agent vrati javlja koliko je trajalo.

*Bez sopstvenog servera:* u `[heartbeat] url` možeš upisati bilo koji „ping“ URL, na primer iz servisa
[healthchecks.io](https://healthchecks.io), koji sam šalje Telegram/e-mail kad pingovi prestanu (token ostaje prazan).
Ovo ti ne daje podatak da li je uređaj bio na bateriji, a poruke nisu na srpskom.

### 4. Stalni rad (systemd)

Isto za agenta i za stražara. Evo agenta (za stražara zameni `agent` sa `watchdog`):

```bash
sudo apt install python3-venv                  # ako nije instalirano
sudo python3 -m venv /opt/powerout
sudo /opt/powerout/bin/pip install .           # pokreni iz ovog foldera

sudo install -d /etc/powerout
sudo install -m 600 config.example.toml /etc/powerout/config.toml
sudo nano /etc/powerout/config.toml

sudo cp systemd/powerout-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powerout-agent
journalctl -u powerout-agent -f                # log uživo
```

Servis radi kao privremeni korisnik bez prava (`DynamicUser`), config čita systemd kao root i daje ga samo servisu,
a stanje se čuva u `/var/lib/powerout-agent`. Nadogradnja: `sudo /opt/powerout/bin/pip install --force-reinstall --no-deps .`
pa `sudo systemctl restart powerout-agent`.

## Šta još treba znati

- **Laptop**: isključi automatsko spavanje na bateriji, inače posle nekog vremena zaspi i prestane da javlja.
- **Ruter**: ako nema struje ni on, laptop ne može da pošalje poruku. Alarm tada stiže od stražara. Poruka koja je
  čekala stiže čim se veza vrati (pokušaji na najviše 60 s), obeležena kao zakasnela.
- **Naknadni izveštaj** računa na tačan sat. Raspberry Pi bez RTC modula posle nestanka struje može da se digne sa
  pogrešnim vremenom pa taj izveštaj propusti. Stražar nema taj problem.
- **Desktop koji se sam upali** posle nestanka struje: u BIOS-u podesi „Restore on AC Power Loss“ na „Power On“.
- **Kratki treptaji** mreže kraći od `debounce_seconds` (10 s) se ignorišu, da ne stižu parovi poruka.
- **Stražar je jedna tačka otkaza**: ako njegov server padne, nećeš dobiti ništa. Biraj pouzdan.
- **UPS** (`source = "nut"`) se čita preko `upsc` iz paketa Network UPS Tools, a za sve ostalo postoji `source = "command"`:
  tvoja komanda vraća izlazni kod 0 (ima struje) ili 1 (nema).

## Razvoj

```bash
python3 -m unittest discover -s tests -t .     # radi bez mreže i bez spoljnih paketa
```

| Fajl | Šta radi |
|---|---|
| `powerout/power.py` | očitavanje struje (sysfs, NUT, komanda) i filtriranje treptaja |
| `powerout/agent.py` | petlja agenta, heartbeat, izveštaj posle neočekivanog restarta |
| `powerout/watchdog.py` | HTTP server za heartbeat-ove i provera tišine |
| `powerout/notify.py`, `dispatch.py` | Telegram/e-mail i red za slanje sa ponavljanjem |
| `powerout/messages.py` | svi tekstovi poruka |
