# Parser-aanpak begrotingen

De parser werkt in lagen. Zo kunnen we snel testen met gewone PDF's en later OCR/OpenAI toevoegen zonder de hele app om te bouwen.

## Laag 1: PDF-tekst

De app leest eerst de tekstlaag uit een PDF. Dit werkt bij digitale begrotingen waarbij tekst selecteerbaar is.

## Laag 2: Regelherkenning

Uit de tekst worden begrotingsregels gehaald met deze doelkolommen:

- Omschrijving / werkzaamheden
- Hvh
- Ehd
- Norm / arbeid
- Uren
- Materiaal
- Materieel
- O.A.
- Totaal prijs per regel
- Eenheidsprijs

De huidige parser herkent alvast omschrijving, hoeveelheid, eenheid, eenheidsprijs en totaalprijs uit tabelachtige regels.

## Laag 3: OCR

OCR is nodig voor gescande PDF's of screenshots in PDF's. OCR zet beeld om naar tekst, maar begrijpt de begrotingsstructuur nog niet vanzelf.

De app gebruikt hiervoor Tesseract in de Docker-container. Standaard staat `OCR_LANG` op:

```text
nld+eng
```

## Laag 4: OpenAI-structurering

OpenAI is vooral nuttig na OCR of bij rommelige PDF-tekst. De rol van OpenAI wordt dan:

- kolommen herkennen;
- regels corrigeren;
- bedragen aan de juiste kolom koppelen;
- ontbrekende waarden markeren;
- output teruggeven als JSON voor opslag in de database.

Kort: OCR leest, OpenAI structureert.

De app gebruikt dezelfde hoofdaanpak als Top Groep Nederland: een directe call naar de OpenAI Responses API met JSON schema output. Zet hiervoor in GitHub Actions Secrets:

```text
OPENAI_API_KEY
OPENAI_MODEL
OPENAI_BUDGET_FALLBACK_ENABLED
OCR_ENABLED
OCR_LANG
```

Aanbevolen waarden:

```text
OPENAI_MODEL=gpt-4.1-mini
OPENAI_BUDGET_FALLBACK_ENABLED=true
OCR_ENABLED=true
OCR_LANG=nld+eng
```
