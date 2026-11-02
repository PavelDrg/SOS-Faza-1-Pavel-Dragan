# Procesarea facturilor

Proiect A, faza 1. Programul citește facturile XML sau PDF din hub, verifică datele și înregistrează deciziile, notificările și raportul final.

## Pași pentru rulare

Este necesar Python 3.10 sau mai nou. Din rădăcina repository-ului:

```powershell
python -m pip install -r requirements.txt
python solutie.py BASE_URL
```

`BASE_URL` este adresa rulării create de runner-ul cursului. Poate fi transmisă și prin `BPA_BASE_URL`. Cheia personală este folosită doar de runner, nu de soluție. Dependențele sunt `requests` și `pypdf`, cu versiunile din `requirements.txt`.

Pentru rularea în container:

```
image: python:3.12-slim
setup: pip install -r requirements.txt
command: python solutie.py {base_url}
```

## Observații

Documentele care încep cu `%PDF` sunt citite ca PDF, iar celelalte ca XML. Un șablon PDF necunoscut sau un CUI care nu are 2-10 cifre după normalizare este tratat ca ilizibil. Dacă pentru o monedă nu există limită de aprobare în configurație, verificarea acelei limite se omite.

Data de lucru, pragurile și destinatarii se citesc din hub.

Logurile sunt în `logs/`. Nota de arhitectură este în `Arhitectura.pdf`.

Link-ul catre repo este: https://github.com/PavelDrg/SOS-Faza-1-Pavel-Dragan