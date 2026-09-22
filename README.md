# trascrivi

Trascrizione di file audio/video in testo con **faster-whisper**, ottimizzata per GPU.

## Installazione

Serve Python 3.10+ e una GPU NVIDIA con driver CUDA recenti (in alternativa vedi [README_CPU.md](README_CPU.md)).

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install faster-whisper
```

Se hai già `torch` installato con CUDA, `--system-site-packages` lo riutilizza e non riscarica nulla.

## Comando di default

Trascrive tutti i file dentro `lezioni/` (video `.mp4` inclusi), salva i `.txt` in `trascrizioni/` e salta quelli già fatti:

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing
```

Aggiungi `--timestamps` per avere `[mm:ss]` a inizio riga (necessario se vuoi usare `--resume`):

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing --timestamps
```

La prima esecuzione scarica il modello (~1.5 GB) da Hugging Face.

## Comandi utili

| Comando | Cosa fa |
|---|---|
| `... lezione.mp4` | Trascrive un singolo file |
| `... lezioni -d trascrizioni` | Tutta la cartella, output in `trascrizioni/` |
| `... lezioni -d out --skip-existing` | Salta i file già trascritti |
| `... lezioni -d out --timestamps` | Aggiunge `[mm:ss]` a ogni segmento |
| `... lezioni -d out --timestamps --resume` | Riprende una trascrizione interrotta a metà |
| `... lezione.mp4 --benchmark 120` | Elabora solo i primi 2 minuti (prova rapida) |
| `... lezioni -m large-v3` | Modello multilingua, più accurato e più lento |
| `... lezioni -l auto` | Rileva la lingua automaticamente (default: `en`) |
| `... lezioni --workers 4` | Più processi paralleli (utile soprattutto su CPU) |

Opzioni principali: `-m/--model`, `-l/--language`, `-d/--output-dir`, `-o/--output`, `--device`, `--compute-type`, `--batch-size`, `--beam-size`, `--workers`, `--cpu-threads`, `--no-vad`, `--verbose`. Elenco completo con `--help`.

## Note

- **Modello di default**: `distil-large-v3`, solo-inglese, ~5-6x più veloce di `large-v3` con accuratezza quasi identica. Adatto a lezioni in inglese.
- **Lingua di default**: `en`. Con `--language auto` la lingua viene rilevata a ogni file.
- **Nessun file temporaneo**: i video vengono decodificati direttamente (PyAV), quindi non serve `ffmpeg` installato.
- **Interruzioni**: senza `--timestamps` un file interrotto va rifatto da capo; con `--timestamps` più `--resume` riprende dall'ultimo segmento scritto.
- `gen_testdata.py` è uno script opzionale che genera audio sintetico per prove di velocità.
