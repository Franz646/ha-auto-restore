# ha-auto-restore

Bring a broken **Home Assistant OS** back from another computer with one command: restart it,
restore the newest backup, or wipe it and restore your backup onto a fresh install - asking before
every destructive step.

It runs on the computer next to your HA box (Windows, Linux or macOS), uses only the Python standard
library, and pulls the backup from wherever you keep copies (NAS share, cloud-synced folder, local
folder). It is meant for the moment when HA is dead and its web UI is not there to help you.

> **Status:** personal tool made public, tested on HAOS 18.4 / Home Assistant 2026.11 on x86-64 (see
> [What has been tested](#what-has-been-tested)). Use at your own risk, read the code first, and try
> it in a VM before you need it.

## What it does

| What broke | Command | What happens |
|---|---|---|
| Only HA Core (bad config, bad update) | `restart`, then `restore` | Over the SSH app: `ha core restart`, then `ha core rebuild`; if still down, restores HA (config + database) from the newest backup on the box (`--full` also apps and folders) |
| HAOS boots but is messed up (even the SSH app is down) | `reinstall` | Over HAOS **system SSH** (port 22222): schedules a wipe of the data partition with os-agent and reboots; HAOS comes back as a fresh install and `upload` restores your backup |
| Disk dead / HAOS does not boot | (by hand) then `upload` | Put HAOS on a new disk or a USB stick, boot it, then `upload` waits for the onboarding screen, uploads the backup and restores it - no login needed |

`auto` (the default of the launchers) runs `diagnose` and then picks the steps in that order.

Other commands: `diagnose` (what answers), `where` (which backup would be used), `sync` (copy the tool
to a local emergency folder, useful if you keep it on the HA Samba share).

## Requirements

- Python 3.9+ and OpenSSH client on the computer that runs it.
- **SSH app** on HA ("Advanced SSH & Web Terminal" or "Terminal & SSH") with **key** login. Recommended:
  an alias in `~/.ssh/config` with user, key and (on Windows OpenSSH, if you see "Corrupted MAC on
  input") explicit `Ciphers`/`MACs`.
- **Backups**: automatic, **encrypted** HA backups copied **off the box** (NAS, cloud). The folder must be
  readable from this computer (on Windows, save the NAS credentials of a user that can read the share).
- **The backup encryption key** (HA: Settings > System > Backups > encryption key, or the emergency kit).
- For `reinstall`: **HAOS system SSH on port 22222** (see below).
- **Internet on the HA box during a restore**: the Supervisor refuses to restore without it.
- For `upload`: **Home Assistant 2025.1 or newer** in the fresh install (it has the onboarding backup
  API; the first boot of a fresh HAOS downloads the current HA anyway).

### Enabling system SSH (port 22222)

Root access to the HAOS host. Only if you want `reinstall`; it is a security decision, take it yourself.

1. A USB stick with **one single partition** named `CONFIG` (FAT, ext4 or NTFS).
2. In its root a file `authorized_keys` (no extension, LF line endings, ASCII) with your public key.
3. Plug it into the HA box and run `ssh <your-ssh-app> "bash -l -c 'ha os import'"` (or reboot with the
   stick in). Without `bash -l` the CLI has no token ("No API token provided").
4. You may leave the stick in: on every boot HAOS re-imports the key, and after a wipe or a reinstall the
   port comes back by itself. Removing `authorized_keys` from the stick disables the port at next boot.

**Never use a stick that still holds an old HAOS** (partitions `hassos-boot`, `hassos-overlay`,
`hassos-data*`; Windows shows only one drive letter or none). Plugged into the HA box it can boot
instead of your disk, or make HAOS mount the wrong data partition. Wipe it completely first.

## Setup

1. Copy `config.example.ini` to `config.ini` and edit it (address, SSH, backup folders, key storage).
2. Save the backup key:
   - Windows: `powershell -ExecutionPolicy Bypass -File .\save_backup_key.ps1` (DPAPI, only your user
     on this PC can decrypt it);
   - elsewhere: `storage = prompt` (asked when needed) or `storage = file` with a `chmod 600` file.
3. Check: `python ha_restore.py diagnose` and `python ha_restore.py where`.
4. Keep a copy of the tool on the computer itself (`python ha_restore.py sync`), not only on the HA share.

## Usage

```
restore.cmd            (Windows)   or   ./restore.sh            (Linux/macOS)
restore.cmd where      restore.cmd upload --ha-only      restore.cmd restore --full
```

## Things learned the hard way

- A **fresh HAOS 18+** serves onboarding on **port 80**; 8123 answers `307` to port 80 (without the
  port in `Location`). After the restore HA goes back to the port in its configuration (usually 8123).
  The tool follows both.
- The onboarding API works without login while onboarding is not done:
  `POST /api/onboarding/backup/upload?agent_id=hassio.local` (multipart) and
  `POST /api/onboarding/backup/restore` (`backup_id`, `agent_id`, `password`, `restore_database`,
  `restore_addons`, `restore_folders`). Once onboarding is done `/api/onboarding` returns 404.
- The **Supervisor blocks a restore without internet** ("blocked from execution, no supervisor
  internet connection"). In a test VM without network you can ignore that check (see below) - never on
  your real box.
- Wiping the data partition without the Supervisor: on the host,
  `busctl call io.hass.os /io/hass/os/System io.hass.os.System ScheduleWipeDevice` and reboot.
- `backup.json` sits at the **end** of the backup `.tar`: read it with random access (Python
  `tarfile` mode `"r:"`), or you download the whole file from a cloud-synced folder.
- Automatic backups are often `partial` (selected folders): pick backups that **contain HA**, not
  `type == "full"`.
- Windows `scp` uses SFTP, which the SSH apps may not have; `ssh host "cat file" > file` works.
- Raw-writing a USB stick from a script can be blocked by anti-ransomware tools (MBR protection). Use a
  dedicated imager (balenaEtcher, Raspberry Pi Imager) to write HAOS on a stick or disk.

## Trying it in a VM (recommended)

VirtualBox works (Windows Home has no Hyper-V Manager):

1. Download `haos_ova-<version>.vdi.zip` from the HAOS releases, create a VM with **EFI**, 2 CPUs,
   3 GB RAM, the disk on SATA, network **NAT** with port forwards to 80 and 8123. Start it and wait for
   the onboarding screen (10-25 min, the first boot downloads HA).
2. **Isolate it before restoring**, or you get a second HA controlling your house: switch the NIC to
   **host-only**, then on the VM console give it a static address, e.g.
   `network update enp0s3 --ipv4-method static --ipv4-address 192.168.56.50/24 --ipv4-gateway 192.168.56.1`
   (`VBoxManage controlvm ... keyboardputstring` truncates long strings: send them in pieces).
3. Without internet the Supervisor blocks the restore: in the **VM only** run
   `jobs options -i internet_system -i internet_host` on the console.
4. `HA_HOST=192.168.56.50 python ha_restore.py upload --ha-only` (add-ons need internet to be pulled).
5. Expect: upload, restore, HA back on 8123 asking for your login, `/api/onboarding` 404. Delete the VM.

To test a USB stick, copy its used part to an image (read-only), convert it to VDI **with the same
size as the stick** (the GPT header remembers the disk size; a smaller virtual disk makes Linux reject
the partition table) and attach it on a VirtualBox **USB** storage controller.

## What has been tested

- `diagnose`, `where`, `sync`: on a real HAOS 18.4 box.
- `upload --ha-only`: twice in VirtualBox on a fresh HAOS 18.4 (VDI image, and a USB stick freshly
  written with the generic-x86-64 image), backup of ~2.7 GB from a NAS share and from a Google Drive
  folder: upload 3-5 min, restore accepted, HA back on 8123.
- System SSH, `ScheduleWipeDevice` presence, reboot with a CONFIG stick inserted: on the real box.
- **Not** tested for real: `restart`, `restore`, `reinstall` on a real box, and `upload` with apps
  (needs internet in the VM). Reports welcome.

## Related

- [Eliav2/ha-disk-swap](https://github.com/Eliav2/ha-disk-swap): an app that, while HA works, prepares a
  new disk with HAOS and your setup already restored. Complementary: it prevents, this tool recovers.

## In italiano

Strumento per rimettere in piedi Home Assistant OS da un altro computer quando HA non funziona:
riavvio, ripristino dell'ultimo backup, oppure cancellazione dei dati e ripristino su un HAOS appena
installato, con conferma (scrivendo `YES`) prima di ogni passo distruttivo. Configurazione in
`config.ini` (copia di `config.example.ini`); chiave dei backup salvata con `save_backup_key.ps1` su
Windows. Prima di averne bisogno, provalo in una macchina virtuale isolata come descritto sopra.

## License

MIT
