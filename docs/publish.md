# Publiceren

Deze repository is voorbereid om via GitHub Actions te testen en te publiceren.

## GitHub repository maken

De GitHub repository is:

```text
https://github.com/thomasdeijkers/devalkadvies.git
```

Koppel daarna deze lokale map:

```powershell
git init
git branch -M main
git add .
git commit -m "Initial DeValk advies begrotingsparser"
git remote add origin https://github.com/thomasdeijkers/devalkadvies.git
git push -u origin main
```

## Secrets

Zet in GitHub bij `Settings > Secrets and variables > Actions` deze secrets:

```text
DATABASE_URL
PUBLISH_HOST
PUBLISH_USER
PUBLISH_SSH_KEY
PUBLISH_PATH
```

Gebruik voor `PUBLISH_PATH`:

```text
/home/opticore/projects/extern/devalkadvies
```

`DATABASE_URL` wijst naar PostgreSQL:

```text
postgresql+psycopg://gebruiker:wachtwoord@136.144.183.127:5432/devalkadvies
```

Als PostgreSQL op dezelfde VPS draait als de Docker-container, gebruik dan:

```text
postgresql+psycopg://opticore:opticore%402025!@host.docker.internal:5432/devalkadvies
```

De workflow voegt `host.docker.internal` toe aan de container met:

```text
--add-host=host.docker.internal:host-gateway
```

## SSH key controleren

`PUBLISH_SSH_KEY` moet de private key zijn. De bijbehorende public key moet op de server staan bij de gebruiker uit `PUBLISH_USER`.

Voor deze setup betekent dat:

```text
PUBLISH_USER=opticore
PUBLISH_HOST=136.144.183.127
PUBLISH_PATH=/home/opticore/projects/extern/devalkadvies
```

De public key hoort op de server in:

```text
/home/opticore/.ssh/authorized_keys
```

De private key plak je in GitHub als secret:

```text
PUBLISH_SSH_KEY
```

Als de key niet klopt, faalt de workflow meestal met een melding zoals `permission denied`, `unable to authenticate`, of `handshake failed`. Als alleen `missing server host` verschijnt, ontbreekt `PUBLISH_HOST` en is de key nog niet getest.

## Servermap

De workflow maakt deze map automatisch aan als hij nog niet bestaat:

```text
/home/opticore/projects/extern/devalkadvies
```

Daarna wordt de Docker image daar geplaatst als:

```text
devalkadvies.tar.gz
```

De container draait op poort:

```text
9000
```

## Docker op de server

De server moet Docker hebben, omdat GitHub Actions een Docker image publiceert en als container start.

Installeer Docker op de server:

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker
```

Geef gebruiker `opticore` toegang tot Docker:

```bash
sudo usermod -aG docker opticore
```

Log daarna uit en opnieuw in op de server, of open een nieuwe SSH-sessie. Controleer daarna:

```bash
docker --version
docker ps
```

## Domein

Het publieke adres wordt:

```text
https://dva.opticore-insights.nl
```

De app-container blijft lokaal op de server draaien op poort `9000`. Zet daarom een reverse proxy voor het domein naar:

```text
http://127.0.0.1:9000
```

Voor Nginx kan dat bijvoorbeeld zo:

```nginx
server {
    listen 80;
    server_name dva.opticore-insights.nl;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Daarna SSL aanzetten, bijvoorbeeld met:

```bash
sudo certbot --nginx -d dva.opticore-insights.nl
```

## Publish-logica

Bij iedere push naar `main`:

1. draait de CI;
2. wordt een Docker image gebouwd;
3. wordt de image naar de server gekopieerd;
4. wordt de container opnieuw gestart.

De workflow kan ook handmatig gestart worden via `Actions > Publish > Run workflow`.
