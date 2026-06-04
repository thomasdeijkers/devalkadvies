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

## Laag 4: OpenAI-structurering

OpenAI is vooral nuttig na OCR of bij rommelige PDF-tekst. De rol van OpenAI wordt dan:

- kolommen herkennen;
- regels corrigeren;
- bedragen aan de juiste kolom koppelen;
- ontbrekende waarden markeren;
- output teruggeven als JSON voor opslag in de database.

Kort: OCR leest, OpenAI structureert.

