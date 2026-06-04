# DeValk advies Begrotingsparser

Een eerste projectopzet in de huisstijlrichting van DeValk advies, waarin inkomende begrotings-PDF's worden geupload, geparsed en omgezet naar bruikbare databasegegevens en Excel-bestanden.

## Wat zit erin

- Webapp voor uploaden en controleren van inkomende begrotingen.
- Nieuwe database-opzet voor database `devalkadvies`.
- PDF-parser die tekst uit PDF's haalt en begrotingsregels probeert te herkennen.
- Excel-export met de gewenste kolommen.
- Dashboard voor status, aantallen en recente stukken.
- Detailpagina om gevonden begrotingsregels te controleren.

## Installatie

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Starten

```powershell
uvicorn annemieke_app.main:app --reload --port 9000
```

Open daarna:

```text
http://127.0.0.1:9000
```

Publiek komt de app achter:

```text
https://dva.opticore-insights.nl
```

## Database

Standaard gebruikt de app lokaal SQLite:

```text
database/devalkadvies.db
```

Voor je PostgreSQL-server kan `DATABASE_URL` naar de nieuwe database `devalkadvies` worden gezet:

```text
postgresql+psycopg://gebruiker:wachtwoord@136.144.183.127:5432/devalkadvies
```

De database zelf maak je op de server aan met:

```sql
CREATE DATABASE devalkadvies;
```

## Volgende logische stappen

1. Enkele echte begrotings-PDF's testen.
2. Parserregels uitbreiden voor de leveranciersindeling die Annemieke ontvangt.
3. OCR toevoegen voor gescande PDF's.
4. Correcties opslaan zodat het systeem per leverancier slimmer wordt.

## Publiceren

De GitHub Actions en Docker-publicatie staan klaar. Zie [docs/publish.md](docs/publish.md).
