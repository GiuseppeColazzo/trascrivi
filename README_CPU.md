# Run su CPU (macchina senza GPU)

Lo stesso script funziona su una macchina senza GPU NVIDIA, **senza modifiche al codice**. Quello che cambia è la velocità e le scelte da fare.

## Installazione

Identica, ma non serve `torch`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install faster-whisper
```

Senza `--system-site-packages` il venv è più leggero: `faster-whisper` porta con sé `ctranslate2` e `onnxruntime` in versione CPU.

## Comando di partenza

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing --device cpu
```

`--device cpu` non è obbligatorio (`auto` fa la stessa scelta da solo), ma è esplicito ed evita il tentativo di rilevamento CUDA.

## Le tre modifiche che contano

### 1. Modello più piccolo (è il 90% del guadagno)

Su CPU il collo di bottiglia è il modello. `distil-large-v3` è pur sempre un *large*: 756M parametri, troppo pesante per la CPU.

```powershell
--model small.en           # buon compromesso, ~5-8x più veloce del large
--model distil-medium.en   # via di mezzo
--model base.en            # molto veloce, qualità scarsa su lessico tecnico
```

Nota: `distil-small.en` **non esiste** in faster-whisper. I modelli `.en` funzionano solo in inglese.

### 2. Precisione `int8`

È già il default con `--device cpu`, ma conviene forzarlo:

```powershell
--compute-type int8
```

Su CPU molto vecchie, senza AVX2, `int8` può risultare **più lento** di `float32`: in quel caso usa `--compute-type int8_float32` o `float32`.

### 3. Parallelismo su più file

Su CPU è qui il moltiplicatore. La regola: `workers × cpu-threads ≈ core fisici`.

```powershell
--workers 4 --cpu-threads 2
```

Il default dello script è prudente (`core // 4`), quindi su CPU conviene alzarlo. Troppi worker peggiorano le cose perché si contendono la cache.

## Comando consigliato, tutto insieme

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing --device cpu --model small.en --compute-type int8 --workers 4 --cpu-threads 2
```

## Cosa si guadagna e cosa no rispetto alla GPU

Il miglioramento rispetto allo script sequenziale originale resta significativo (**circa 5-10x**), ma per ragioni diverse:

- **Continuano a valere**: faster-whisper/CTranslate2 al posto di openai-whisper, il modello distillato, il filtro VAD che salta i silenzi, la decodifica diretta dei video, la lingua fissa invece dell'auto-detection.
- **Non valgono più**: il batching (`--batch-size` torna a 1) e `float16`. Su CPU saturi i core con un segmento per volta; non ci sono migliaia di core da riempire.

La velocità assoluta però è un'altra categoria: con `distil-large-v3` una CPU sta intorno a 1-3x realtime, mentre la stessa macchina con `small.en` sale molto. Non fidarti delle stime: misura.

## Misurare prima di lanciare tutto

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezione.mp4 --benchmark 120 --device cpu --model small.en
```

Elabora solo i primi 2 minuti e stampa gli `x realtime`: da lì capisci se `small.en` basta o se serve scendere a `base.en`.

## Note

- Il primo avvio scarica il modello scelto da Hugging Face.
- I fix per Windows presenti nello script (symlink Hugging Face, DLL CUDA) restano innocui su CPU: puoi usare lo stesso identico file senza toccarlo.
- Vale sempre `--timestamps` + `--resume`: su CPU una trascrizione dura molto più a lungo, quindi perdere il lavoro a metà è più doloroso.
