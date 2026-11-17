# ha-auto-restore

*[Read in English](README.md)*

Rimette in piedi un **Home Assistant OS** che non funziona più, da un altro computer e con un solo
comando: lo riavvia, ripristina l'ultimo backup, oppure cancella i dati e ripristina il backup su
un'installazione nuova. Prima di ogni passo distruttivo chiede conferma.

Gira sul computer vicino al tuo HA (Windows, Linux o macOS), usa solo la libreria standard di Python
e prende il backup da dove ne tieni le copie (cartella condivisa del NAS, cartella sincronizzata con
il cloud, cartella locale). Serve proprio quando HA è fermo e la sua interfaccia web non c'è.

> **Stato:** strumento personale reso pubblico, provato su HAOS 18.4 / Home Assistant 2026.11 su x86-64
> (vedi [Cosa è stato provato](#cosa-è-stato-provato)). Lo usi a tuo rischio: leggi prima il codice e
> provalo in una macchina virtuale prima di averne bisogno.

## Cosa fa

| Cosa si è rotto | Comando | Cosa succede |
|---|---|---|
| Solo HA Core (configurazione sbagliata, aggiornamento difettoso) | `restart`, poi `restore` | Tramite l'app SSH: `ha core restart`, poi `ha core rebuild`; se HA non riparte, ripristina HA (configurazione e database) dall'ultimo backup presente sul mini PC (`--full` anche app e cartelle) |
| HAOS si avvia ma è rovinato (non risponde nemmeno l'app SSH) | `reinstall` | Tramite l'**SSH di sistema** di HAOS (porta 22222): programma con os-agent la cancellazione della partizione dati e riavvia; HAOS riparte come appena installato e `upload` ripristina il backup |
| Disco guasto / HAOS non si avvia | (a mano) poi `upload` | Metti HAOS su un disco nuovo o su una chiavetta USB e avvialo; poi `upload` aspetta la schermata di prima configurazione, carica il backup e lo ripristina, senza bisogno di login |

`auto` (quello che parte con i lanciatori senza argomenti) esegue `diagnose` e poi sceglie i passi in
quest'ordine.

Altri comandi: `diagnose` (cosa risponde), `where` (quale backup verrebbe usato), `sync` (copia lo
strumento in una cartella locale di emergenza, utile se lo tieni nella cartella Samba di HA, che
sparisce insieme a HA).

## Cosa serve

- Python 3.9 o successivo e il client OpenSSH sul computer che lo esegue.
- L'**app SSH** su HA ("Advanced SSH & Web Terminal" o "Terminal & SSH") con accesso **a chiave**.
  L'utente è `root` per "Terminal & SSH", e quello impostato nelle opzioni dell'app (`username`) per
  "Advanced SSH". Consigliato: un alias in `~/.ssh/config` con utente, chiave e, se OpenSSH di Windows
  dà "Corrupted MAC on input", `Ciphers`/`MACs` espliciti.
- **Backup**: automatici, **cifrati**, copiati **fuori** dal mini PC (NAS, cloud). La cartella deve
  essere leggibile da questo computer (su Windows salva le credenziali del NAS di un utente che può
  leggere quella condivisione).
- **La chiave di crittografia dei backup**: in HA vai su **Impostazioni > Sistema > Backup**, scheda
  **Chiave di crittografia**, poi **Mostra la mia chiave di crittografia** (oppure scarica il kit di
  emergenza). Tienine una copia fuori da HA.
- Per `reinstall`: l'**SSH di sistema di HAOS sulla porta 22222** (vedi sotto).
- **Internet sul mini PC durante il ripristino**: senza, il Supervisor si rifiuta di ripristinare.
- Per `upload`: **Home Assistant 2025.1 o successivo** nell'installazione nuova (ha le API per il
  ripristino in prima configurazione; comunque al primo avvio un HAOS nuovo scarica l'HA attuale).

### Attivare l'SSH di sistema (porta 22222)

È l'accesso root al sistema di HAOS. Serve solo per `reinstall`; è una scelta di sicurezza, falla tu.

1. Una chiavetta USB con **una sola partizione** chiamata `CONFIG` (FAT, ext4 o NTFS).
2. Nella cartella principale un file `authorized_keys` (senza estensione, a capo in stile Linux, solo
   caratteri ASCII) con la tua chiave pubblica.
3. Inseriscila nel mini PC e lancia `ssh <la-tua-app-ssh> "bash -l -c 'ha os import'"` (oppure riavvia
   con la chiavetta inserita). Senza `bash -l` il comando `ha` non ha il token ("No API token provided").
4. La chiavetta può restare inserita: a ogni avvio HAOS rilegge la chiave e, dopo una cancellazione o
   una reinstallazione, la porta si riattiva da sola. Se togli `authorized_keys` dalla chiavetta, al
   riavvio successivo la porta si disattiva.

**Non usare mai una chiavetta che contiene ancora un vecchio HAOS** (partizioni `hassos-boot`,
`hassos-overlay`, `hassos-data*`; Windows mostra una sola lettera di unità o nessuna). Inserita nel
mini PC potrebbe avviarsi al posto del tuo disco, o far montare a HAOS la partizione dati sbagliata.
Prima cancellala completamente.

## Installazione

1. Copia `config.example.ini` in `config.ini` e modificalo (indirizzo, SSH, cartelle dei backup, dove
   tenere la chiave). `config.ini` è escluso da git: descrive la tua rete, tienilo privato.
2. Salva la chiave dei backup:
   - Windows: `powershell -ExecutionPolicy Bypass -File .\save_backup_key.ps1` (cifrata con DPAPI: può
     leggerla solo il tuo utente su questo PC);
   - altri sistemi: `storage = prompt` (chiesta quando serve) oppure `storage = file` con un file
     protetto da `chmod 600`.
3. Controlla: `python ha_restore.py diagnose` e `python ha_restore.py where` (su Linux/macOS usa
   `python3`, o semplicemente `./restore.sh diagnose`).
4. Tieni una copia dello strumento sul computer stesso (`python ha_restore.py sync`), non solo nella
   cartella condivisa di HA.

## Uso

```
restore.cmd            (Windows)   oppure   ./restore.sh            (Linux/macOS)
restore.cmd where      restore.cmd upload --ha-only      restore.cmd restore --full
```

Prima di ogni passo distruttivo lo strumento chiede di scrivere `YES`. La chiave dei backup non viene
mai stampata.

## Cose imparate a caro prezzo

- Un **HAOS 18+ appena installato** risponde in prima configurazione sulla **porta 80**; la 8123
  risponde `307` verso la porta 80 (senza la porta nell'intestazione `Location`). Dopo il ripristino HA
  torna sulla porta della sua configurazione (di solito 8123). Lo strumento le segue tutte e due.
- Le API di prima configurazione funzionano senza login finché la prima configurazione non è finita:
  `POST /api/onboarding/backup/upload?agent_id=hassio.local` (multipart) e
  `POST /api/onboarding/backup/restore` (`backup_id`, `agent_id`, `password`, `restore_database`,
  `restore_addons`, `restore_folders`). Finita la prima configurazione `/api/onboarding` risponde 404.
- Il **Supervisor blocca il ripristino senza internet** ("blocked from execution, no supervisor
  internet connection"). In una VM di prova senza rete si può ignorare quel controllo (vedi sotto),
  mai sul mini PC vero.
- Cancellare la partizione dati senza il Supervisor: sul sistema,
  `busctl call io.hass.os /io/hass/os/System io.hass.os.System ScheduleWipeDevice` e poi riavvio.
- `backup.json` sta **in fondo** al `.tar` del backup: va letto ad accesso diretto (modo `"r:"` di
  `tarfile` in Python), altrimenti da una cartella sincronizzata col cloud si scarica tutto il file.
- I backup automatici spesso risultano `partial` (cartelle scelte): bisogna scegliere i backup che
  **contengono HA**, non quelli con `type == "full"`.
- Lo `scp` di Windows usa SFTP, che le app SSH possono non avere; `ssh host "cat file" > file` funziona.
- Scrivere una chiavetta USB a basso livello da uno script può essere bloccato dagli antiransomware
  (protezione dell'MBR). Per scrivere HAOS su una chiavetta o un disco usa un programma apposito
  (balenaEtcher, Raspberry Pi Imager).

## Provarlo in una macchina virtuale (consigliato)

VirtualBox va bene (Windows Home non ha la gestione di Hyper-V):

1. Scarica `haos_ova-<versione>.vdi.zip` dai rilasci di HAOS e crea una VM con **EFI**, 2 CPU, 3 GB di
   RAM, il disco su SATA e la rete in **NAT** con l'inoltro delle porte 80 e 8123. Avviala e aspetta la
   schermata di prima configurazione (10-25 minuti: al primo avvio scarica HA).
2. **Isolala prima del ripristino**, altrimenti ti ritrovi un secondo HA che comanda la casa: passa la
   scheda di rete a **solo host (host-only)** e dalle un indirizzo fisso. La console della VM mostra il
   prompt della CLI di HA `ha >`: scrivi lì i comandi **senza** `ha` davanti, per esempio
   `network update enp0s3 --ipv4-method static --ipv4-address 192.168.56.50/24 --ipv4-gateway 192.168.56.1`
   (se li mandi con `VBoxManage controlvm ... keyboardputstring`, le stringhe lunghe vengono tagliate:
   mandale a pezzi).
3. Senza internet il Supervisor blocca il ripristino: **solo nella VM**, allo stesso prompt `ha >`,
   lancia `jobs options -i internet_system -i internet_host`.
4. `HA_HOST=192.168.56.50 python ha_restore.py upload --ha-only` (su Windows prima `set HA_HOST=...`;
   le app restano fuori perché per scaricarle serve internet).
5. Risultato atteso: caricamento, ripristino, HA di nuovo sulla 8123 che chiede il tuo login,
   `/api/onboarding` che risponde 404. Poi elimina la VM.

Per provare una chiavetta USB, copiane la parte usata in un file immagine (in sola lettura), convertila
in VDI **della stessa dimensione della chiavetta** (l'intestazione GPT ricorda la dimensione del disco:
con un disco virtuale più piccolo Linux scarta la tabella delle partizioni) e collegala a un controller
di archiviazione **USB** di VirtualBox.

## Cosa è stato provato

- `diagnose`, `where`, `sync`: su un HAOS 18.4 vero.
- `upload --ha-only`: due volte in VirtualBox su un HAOS 18.4 appena installato (immagine VDI, e una
  chiavetta USB appena scritta con l'immagine generic-x86-64), con un backup di circa 2,7 GB preso da
  una cartella condivisa del NAS e da una cartella di Google Drive: caricamento in 3-5 minuti,
  ripristino accettato, HA di nuovo sulla 8123.
- SSH di sistema, presenza di `ScheduleWipeDevice`, riavvio con la chiavetta CONFIG inserita: sul mini
  PC vero.
- **Non** provati davvero: `restart`, `restore`, `reinstall` su un mini PC vero, e `upload` con le app
  (in VM serve internet). Le segnalazioni sono benvenute.

## Domande frequenti

**Serve Claude, Codex o un'altra AI per usarlo?** No. È un normale script Python: bastano Python 3.9 o
successivo, il client `ssh` e la configurazione descritta sopra. È stato scritto con l'aiuto di un
assistente AI per la programmazione, e se ne usi uno puoi chiedergli di lanciare lo script e leggere
gli errori al posto tuo, ma è una comodità, non un requisito.

## Progetti affini

- [Eliav2/ha-disk-swap](https://github.com/Eliav2/ha-disk-swap): un'app che, mentre HA funziona,
  prepara un disco nuovo con HAOS e la tua configurazione già ripristinata. È complementare: quella
  previene, questo strumento recupera.

## Licenza

MIT
