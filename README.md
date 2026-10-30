# powerout

Javlja ti za struju na tvojoj adresi, na dva načina koja se dopunjuju. Napisano u čistom Pythonu (3.11+),
bez ijedne spoljne biblioteke.

- **Sajt** (`powerout site`): upišeš adresu prebivališta, a on čita najave planiranih isključenja koje
  distributer objavljuje i unapred ti kaže da li će te pogoditi.
- **Agent i stražar** (`powerout agent`, `powerout watchdog`): javljaju na Telegram i/ili e-mail kada struja
  zaista nestane i kada se vrati, uključujući nestanke koje distributer ne objavljuje.

```
⚡ Najavljeno isključenje struje: Stan
Sutra (31.10.) od 09:00 do 14:00 verovatno nema struje na adresi Trebinjska 12, Niš.

⚡ Nestala struja: Stan
Struja je nestala u 14:03:12. Uređaj sada radi na rezervnom napajanju (baterija/UPS).
```

## Sajt: planirana isključenja za tvoju adresu

```bash
python3 -m powerout site        # pa otvori http://localhost:8750
```

Izabereš oblast distributera i upišeš mesto, ulicu i kućni broj. Sajt svakih 30 minuta čita najave sa
[elektrodistribucija.rs](https://www.elektrodistribucija.rs) (Beograd, Novi Sad, Kraljevo, Kragujevac i Niš;
distributer ih objavljuje za danas i naredna tri dana) i za svaki dan crta *red vožnje struje*: traku od 24 sata
na kojoj se vidi kada te isključenje pogađa. Ispod nje stoji tekst najave kakav je objavljen, sa istaknutim
rečima zbog kojih je poklapanje nađeno, pa možeš sam da proveriš zaključak.

Najave su slobodan tekst na ćirilici („делови улица Требињске, Космајске…“, u Beogradu `ULICA: 9-11,`), pa se
adresa poredi po rečima, a ne po tačnom tekstu. Razume latinicu i ćirilicu, rečenice bez kvačica, padeže
(Trebinjska ~ Trebinjske, Pukovac ~ Pukovca) i skraćenice (Sv. ~ Svetog). Brojevi kuća se čitaju kao u
najavi: opseg čiji su krajevi iste parnosti važi za jednu stranu ulice (`9-11` su 9 i 11, ne 10).

| Oznaka | Šta znači | Šalje se obaveštenje |
|---|---|---|
| Verovatno te pogađa | pominje se tvoja ulica (i tvoj broj je u opsegu, ili brojevi nisu navedeni), ili tvoje selo | da |
| Moguće, proveri | pominje se tvoje mesto, ali ne i tvoja ulica | da |
| Skoro pogodak | ulica je na spisku, ali tvoj broj nije u navedenim opsezima | ne |
| U tvojoj oblasti | isključenje je u tvojoj opštini, a tvoja ulica se ne pominje | ne |

Obaveštenje stiže jednom po najavi, čim se pojavi, i još jednom ujutru na dan isključenja. Potrebna je sekcija
`[telegram]` i/ili `[email]` u configu (vidi „Brzi početak“, korak 1); dugme „Pošalji probnu poruku“ na sajtu
proverava da li radi. Ako slanje ne uspe, pokušava ponovo pri sledećem čitanju.

Šta treba imati na umu:

- Distributer objavljuje samo **planirana** isključenja. Neplanirane nestanke ova stranica ne vidi; za njih
  služe agent i stražar.
- Poređenje teksta je procena programa. Greši na strani opreza (radije lažna uzbuna nego propuštena najava), ali
  može da promaši neobičan zapis, zato uvek postoji „Sve najave u oblasti“ i veza ka zvaničnoj stranici.
- Za veći grad upiši grad (Niš), a ne gradsku opštinu, da ti ulica ne promakne ako pogrešiš opštinu.
- Stranice se čitaju najviše na svakih 30 minuta i samo ako su se promenile (`If-None-Match`), pa je opterećenje
  za server distributera zanemarljivo.
- Sajt je za jednu osobu: sluša samo na `127.0.0.1`, nema naloge, odbija zahteve sa tuđih sajtova i sa tuđih
  imena (`Host`, `Origin`), a ne treba ga izlagati na internet. Adrese su u `~/.local/state/powerout/addresses.json`.

Kao stalni servis, bez `sudo` (`loginctl enable-linger $USER` da radi i kada niko nije prijavljen):

```bash
python3 -m venv ~/.local/share/powerout/venv
~/.local/share/powerout/venv/bin/pip install .
mkdir -p ~/.config/systemd/user
cp systemd/powerout-site.user.service ~/.config/systemd/user/powerout-site.service
systemctl --user daemon-reload
systemctl --user enable --now powerout-site
```

## Nestanak struje u trenutku kad se desi (agent i stražar)

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
| `powerout/outages.py` | čitanje najava distributera i poklapanje adrese sa njima |
| `powerout/serbian.py` | ćirilica/latinica, kvačice i padeži za poređenje reči |
| `powerout/site.py`, `pages.py` | sajt: adrese, provera najava, obaveštenja i same stranice |
| `powerout/power.py` | očitavanje struje (sysfs, NUT, komanda) i filtriranje treptaja |
| `powerout/agent.py` | petlja agenta, heartbeat, izveštaj posle neočekivanog restarta |
| `powerout/watchdog.py` | HTTP server za heartbeat-ove i provera tišine |
| `powerout/notify.py`, `dispatch.py` | Telegram/e-mail i red za slanje sa ponavljanjem |
| `powerout/messages.py` | svi tekstovi poruka |
