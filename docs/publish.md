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

## Servermap

De workflow maakt deze map automatisch aan als hij nog niet bestaat:

```text
/home/opticore/projects/extern/devalkadvies
```

Daarna wordt de Docker image daar geplaatst als:

```text
devalkadvies.tar.gz
```

## Publish-logica

Bij iedere push naar `main`:

1. draait de CI;
2. wordt een Docker image gebouwd;
3. wordt de image naar de server gekopieerd;
4. wordt de container opnieuw gestart.

De workflow kan ook handmatig gestart worden via `Actions > Publish > Run workflow`.
