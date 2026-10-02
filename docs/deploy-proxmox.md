# Deploy on Proxmox

The hub runs as a plain service in its own container. It needs no Docker. Sizing for a small host: 2 cores, 1 GB of memory, 16 GB of disk.

Status: the install script is tested with a stand-in for systemd. The service unit and the deploy script for Windows are not verified on real systems yet.

## Create the container

Run this in the shell of the Proxmox host. Replace `ID` with a free container ID, for example `102`:

```bash
pct create ID local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname claude-hub --unprivileged 1 --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:16 --net0 name=eth0,bridge=vmbr0,ip=dhcp --onboot 1 --start 1
```

- The container gets no root password. You reach it from the host with `pct enter ID`.
- `--onboot 1` starts it together with the host.
- The address comes from your router. To keep it stable, tell the router to always assign the same address to this device.

## Deploy from Windows

Prerequisite: SSH access to the Proxmox host, and the changes are committed. The script deploys the last commit, not the working folder.

```powershell
.\deploy\deploy.ps1 -ProxmoxHost HOST -Container ID
```

Replace `HOST` with the address of the Proxmox host. The script asks for the SSH password twice, unless you use an SSH key. At the end it prints the address of the hub.

Run the same command again to update. The data in `/var/lib/claude-hub` and the settings in `/etc/claude-hub.env` are kept.

## Create collector tokens

On the Proxmox host, once per machine that runs a collector:

```bash
pct exec ID -- claude-hub token add MACHINE --user USER
```

The token is shown only once. Put it into the collector configuration of that machine, together with `server_url = "http://ADDRESS:8787"`.

A new server starts empty. The collectors upload everything again on their next run, so no data has to be moved from an earlier test server.

## Protect the web view

1. On the Proxmox host, open the settings file: `pct exec ID -- nano /etc/claude-hub.env`.
2. Set `HUB_UI_PASSWORD`.
3. Restart the service: `pct exec ID -- systemctl restart claude-hub`.

## Look at the service

```bash
pct exec ID -- systemctl status claude-hub
pct exec ID -- journalctl -u claude-hub -n 50 --no-pager
```

## Back up

The database and all transcripts are in `/var/lib/claude-hub` inside the container. Back up the whole container with the **Backup** entry of the container in the Proxmox web interface, to a storage that is not the same disk.
