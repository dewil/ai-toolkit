#!/usr/bin/env python3
"""
transcribe-meeting - локальная транскрипция аудио/видео встречи в markdown.

Движок (отобран сравнением с облачным сервисом и Whisper):
  ASR         - GigaAM v3 e2e_rnnt (русский, локально; бьет локальный Whisper, паритет с облаком по тексту)
  Диаризация  - sherpa-onnx (pyannote-segmentation-3.0 + campplus эмбеддинг), опционально, token-free

Запускается python-ом из venv проекта (launcher в ~/.local/bin это настраивает).
Пути к venv/моделям берутся из GIGA_TRANSCRIBE_HOME (дефолт: ~/.local/share/giga-transcribe).

Диаризация: sherpa сегментирует речь, дальше свой per-segment эмбеддинг + k-means (при
известном --speakers N - k=N; иначе число оценивается по silhouette). На длинных содержательных
репликах разметка надежна (~95% против облака), короткие backchannel ("угу", быстрые вопросы) могут
уходить не тому спикеру - это неустранимый хвост смешанной записи. Спикеры обезличены (Спикер N).

Многодорожечный вход снимает этот хвост целиком: если голоса уже разложены по дорожкам
(пофайлово или по каналам), спикер known by construction - это сама дорожка, и диаризация
не запускается вовсе. Разметка становится точной, а не приблизительной, и получает имена.
Побочная выгода: на своей дорожке голос не перекрыт чужим, поэтому и ASR работает чище.

Примеры:
  transcribe-meeting "встреча.mp4"
  transcribe-meeting rec.webm -o out.md --speakers 3
  transcribe-meeting audio.wav --no-diar --title "Недельный синк"
  transcribe-meeting ivan.wav petr.wav --track-names "Иван,Петр"    # дорожка = спикер
  transcribe-meeting call.wav --split-channels                      # каналы = спикеры

Устойчивость: все промежуточные файлы - в своем temp-каталоге на прогон (параллель-безопасно),
md пишется атомарно, при остановке (Ctrl-C / kill) temp-файлы чистятся.

"""
import argparse, os, sys, time, tempfile, subprocess, datetime, shutil, signal, atexit, fcntl, json, re

HOME_DIR = os.environ.get("GIGA_TRANSCRIBE_HOME", os.path.expanduser("~/.local/share/giga-transcribe"))
LOCK_FILE = os.path.join(HOME_DIR, ".transcribe.lock")
DIAR_DIR = os.path.join(HOME_DIR, "models")
SEG_MODEL = os.path.join(DIAR_DIR, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
EMB = {"campplus": os.path.join(DIAR_DIR, "embedding.onnx"),
       "titanet":  os.path.join(DIAR_DIR, "titanet.onnx")}
ASR_MODEL = "v3_e2e_rnnt"
CHUNK_S = 24            # лимит GigaAM .transcribe - 25 сек
FFMPEG_TIMEOUT = 3600   # потолок на вызов ffmpeg/ffprobe: держим лок машины, зависать нельзя
SR = 16000
DIAR_MIN_ON = 0.5      # мин. длина речевого сегмента (баланс: короткие реплики vs шум эмбеддинга)
DIAR_KMAX = 6          # верхняя граница числа спикеров при авто-оценке (silhouette)

# --- ресурсы прогона, чистятся при выходе/сигнале ---
_tmpdir = None
_partial_md = None
_lock_fd = None


# --- предохранители общей машины (см. SKILL.md, "Очередь прогонов и защита машины") ---


def detect_project(path):
    """Чей это прогон - имя папки проекта, из которой пришел файл.

    Ищем вверх по дереву маркер рабочей папки. Нужно только для человеческого
    сообщения ждущему: "занято проектом X" понятнее, чем "ресурс занят".
    """
    cur = os.path.abspath(path)
    while True:
        parent = os.path.dirname(cur)
        if os.path.isdir(parent):
            for marker in ("CLAUDE.md", ".claude", ".git"):
                if os.path.exists(os.path.join(parent, marker)):
                    return os.path.basename(parent)
        if parent == cur:
            return os.path.basename(os.getcwd())
        cur = parent


def read_lock_info():
    try:
        with open(LOCK_FILE, encoding="utf-8") as fh:
            info = json.load(fh)
        # валидный JSON может оказаться не объектом (чужой формат, обрезок) -
        # тогда .get() ниже уронил бы ждущего вместо честного ожидания
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def describe_holder(info):
    if not info:
        return "занято другим прогоном (подробностей нет)"
    parts = []
    if info.get("project"):
        parts.append(f'проект "{info["project"]}"')
    if info.get("file"):
        parts.append(f"файл {info['file']}")
    if info.get("started"):
        try:
            started = datetime.datetime.fromisoformat(info["started"])
            mins = int((datetime.datetime.now() - started).total_seconds() // 60)
            parts.append(f"идет {mins} мин (с {started.strftime('%H:%M')})")
        except ValueError:
            pass
    if info.get("pid"):
        parts.append(f"pid {info['pid']}")
    return ", ".join(parts)


def acquire_lock(source, wait, timeout):
    """Движок один на машину, прогоны идут по очереди.

    Почему именно взаимное исключение, а не просто уникальные имена файлов:
      - каждый процесс держит порядка полутора гигабайт (модель плюс torch);
      - каждый ставит число потоков по числу ядер, и два прогона дерутся за CPU,
        оба идут медленнее, чем шли бы по очереди;
      - gigaam качает веса НЕ атомарно (пишет прямо в целевой файл и считает
        существующий готовым), поэтому одновременный первый запуск с новой
        моделью оставляет битый кэш.

    Лок берется ДО тяжелых импортов: ждущий висит на десятках мегабайт, а не
    на полутора гигабайтах, поэтому очередь любой длины стоит по памяти как
    один прогон. Ядро снимает лок при завершении процесса, в том числе аварийном.
    """
    global _lock_fd
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    _lock_fd = open(LOCK_FILE, "a+", encoding="utf-8")  # "a+", не "w": не затираем данные держателя
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = describe_holder(read_lock_info())
        if not wait:
            die(f"занято: на машине уже идет транскрибация - {holder}.\n"
                "Запусти позже или убери --no-wait, чтобы встать в очередь.")
        print(f"Жду очереди: сейчас {holder}", flush=True)
        print(f"(движок один на машину, прогоны идут последовательно; "
              f"жду не дольше {timeout // 60} мин)", flush=True)
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    die(f"не дождался за {timeout // 60} мин: все еще "
                        f"{describe_holder(read_lock_info())}.\n"
                        "Если тот прогон завис - сними его вручную и запусти снова.")
                time.sleep(5)
    _lock_fd.seek(0)
    _lock_fd.truncate()
    json.dump({
        "pid": os.getpid(),
        "project": detect_project(source),
        "file": os.path.basename(source),
        "model": ASR_MODEL,
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
    }, _lock_fd, ensure_ascii=False)
    _lock_fd.flush()


def mem_available_mb():
    """Свободная память в МБ; None - если система не дает ее дешево узнать."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None          # macOS и прочие без /proc - проверку пропускаем
    return None


def wait_for_memory(need_mb, wait, timeout):
    """Лок свободен - память может быть все равно занята (браузер, агенты, БД).

    Дешевле подождать, чем словить OOM: жертву при нехватке памяти выбирает
    ядро, а не мы, и ей становится не обязательно транскрибация.
    """
    free = mem_available_mb()
    if free is None:
        # молчание тут читалось бы как "проверено и хватает"
        print("Предупреждение: свободную память проверить нечем (нет /proc/meminfo) - "
              "предохранитель по памяти не работает на этой системе",
              file=sys.stderr, flush=True)
        return
    if free >= need_mb:
        return
    if not wait:
        die(f"мало памяти: свободно {free} МБ, нужно от {need_mb} МБ. "
            "Освободи память или запусти позже.")
    print(f"Жду память: свободно {free} МБ, нужно от {need_mb} МБ", flush=True)
    deadline = time.time() + timeout
    while (mem_available_mb() or need_mb) < need_mb:
        if time.time() > deadline:
            die(f"память так и не освободилась за {timeout // 60} мин "
                f"(сейчас {mem_available_mb()} МБ). Прерываюсь, чтобы не ронять машину.")
        time.sleep(10)


def reexec_in_scope(memory_max):
    """Перезапуск себя в cgroup с лимитом памяти (systemd-run --user --scope).

    Без лимита превышение памяти обрабатывает ГЛОБАЛЬНЫЙ OOM-killer, и жертву он
    выбирает по всей системе - падает база, докер, что угодно, а не транскрибация.
    Внутри scope OOM срабатывает локально: худший исход - умер наш прогон, машина жива.

    Своп режем (MemorySwapMax=0): уход инференса в своп кладет отзывчивость машины
    не хуже нехватки памяти. Нет systemd (macOS, контейнер) - предупреждаем и идем
    без лимита: молчать нельзя, иначе прогон выглядит защищенным, не будучи им.
    """
    if os.environ.get("TRANSCRIBE_SCOPE"):
        return
    if not shutil.which("systemd-run"):
        print("Предупреждение: systemd-run недоступен (macOS, контейнер) - "
              "иду без лимита памяти; следите за свободной памятью сами",
              file=sys.stderr, flush=True)
        return
    # Бинарь есть - еще не значит, что scope создастся: в SSH-сессии и контейнере
    # часто нет user-manager/D-Bus. Проверяем дешевым пробным запуском, потому что
    # execvpe уже заменил бы наш процесс и откатиться было бы нечем - прогон просто
    # умер бы с ошибкой systemd вместо работы без лимита.
    try:
        probe = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--", "true"],
                               capture_output=True, timeout=15)
        ok = probe.returncode == 0
        why = probe.stderr.decode(errors="ignore").strip()[:200]
    except (OSError, subprocess.SubprocessError) as exc:
        ok, why = False, str(exc)
    if not ok:
        print(f"Предупреждение: systemd-run не может создать scope ({why or 'причина неизвестна'}) - "
              "иду без лимита памяти", file=sys.stderr, flush=True)
        return
    # перезапускаем через интерпретатор, а не сам файл: скрипт запускают и как
    # "python transcribe_meeting.py" (бита +x нет), и как установленную команду
    cmd = ["systemd-run", "--user", "--scope", "--quiet",
           "-p", f"MemoryMax={memory_max}", "-p", "MemorySwapMax=0",
           "--", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    env = dict(os.environ, TRANSCRIBE_SCOPE="1")
    try:
        os.execvpe(cmd[0], cmd, env)
    except OSError as exc:                       # нет systemd-user - работаем как есть
        print(f"Предупреждение: не удалось ограничить память через systemd-run ({exc}); "
              "иду без лимита", file=sys.stderr, flush=True)


def _cleanup():
    global _tmpdir, _partial_md
    if _tmpdir and os.path.isdir(_tmpdir):
        shutil.rmtree(_tmpdir, ignore_errors=True)
        _tmpdir = None
    if _partial_md and os.path.exists(_partial_md):
        try:
            os.remove(_partial_md)
        except OSError:
            pass
        _partial_md = None


def _on_signal(signum, frame):
    _cleanup()
    print(f"[прервано] сигнал {signum}, временные файлы очищены", file=sys.stderr)
    sys.exit(130)


atexit.register(_cleanup)
signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


def die(msg, code=1):
    print(f"[ошибка] {msg}", file=sys.stderr)
    sys.exit(code)


def mmss(t):
    t = int(t)
    return f"{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d}" if t >= 3600 else f"{t//60:02d}:{t%60:02d}"


def extract_wav(src, dst):
    if not shutil.which("ffmpeg"):
        die("ffmpeg не найден в PATH (поставь его, напр. `brew install ffmpeg`)")
    try:
        # таймаут обязателен: лок машины уже взят, и зависший ffmpeg (сетевой
        # файл, FIFO, битый контейнер) держал бы очередь всех прогонов
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-ar", str(SR), "-ac", "1", dst],
                           capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"ffmpeg не ответил за {FFMPEG_TIMEOUT} с - файл поврежден или недоступен: {src}")
    if r.returncode != 0 or not os.path.exists(dst):
        die("ffmpeg не смог извлечь аудио:\n" + r.stderr.decode(errors="ignore")[-500:])


def probe_channels(src):
    """Сколько аудиоканалов в файле. Нет ffprobe или не разобрали - считаем моно."""
    if not shutil.which("ffprobe"):
        die("ffprobe не найден в PATH - число каналов не определить "
            "(поставьте ffmpeg или уберите --split-channels)")
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=channels", "-of", "csv=p=0", src],
                           capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"ffprobe не ответил за {FFMPEG_TIMEOUT} с: {src}")
    try:
        return int(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        # молчаливое "1" маскировало бы отказ probe под моно-файл: пользователь
        # получил бы "каналов нет" вместо диагноза
        die(f"не удалось определить число каналов в {src}: {r.stderr.strip()[:200]}")


def extract_channel_wav(src, idx, dst):
    """Один канал многоканального файла -> моно wav 16 кГц (без сведения с соседями)."""
    if not shutil.which("ffmpeg"):
        die("ffmpeg не найден в PATH (поставь его, напр. `brew install ffmpeg`)")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src,
                            "-filter_complex", f"[0:a]pan=mono|c0=c{idx}[out]", "-map", "[out]",
                            "-ar", str(SR), "-ac", "1", dst],
                           capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"ffmpeg не ответил за {FFMPEG_TIMEOUT} с при извлечении канала {idx}")
    if r.returncode != 0 or not os.path.exists(dst):
        die(f"ffmpeg не смог достать канал {idx}:\n" + r.stderr.decode(errors="ignore")[-500:])


def plan_tracks(inputs, split_channels, names_arg):
    """Список (имя_спикера, источник, индекс_канала|None) для многодорожечного прогона.

    Два источника дорожек, дальше по пайплайну неразличимы:
      - пофайловый (несколько входных файлов) - имя по умолчанию из имени файла;
      - поканальный (--split-channels) - имя по умолчанию "Дорожка N".
    """
    if split_channels:
        if len(inputs) != 1:
            die("--split-channels работает с одним файлом (каналы внутри него)")
        n = probe_channels(inputs[0])
        if n < 2:
            die(f"в файле {os.path.basename(inputs[0])} один канал - раскладывать нечего")
        tracks = [(f"Дорожка {i+1}", inputs[0], i) for i in range(n)]
    else:
        tracks = [(os.path.splitext(os.path.basename(p))[0], p, None) for p in inputs]
    if names_arg:
        names = [n.strip() for n in names_arg.split(",")]
        if len(names) != len(tracks):
            die(f"--track-names: имен {len(names)}, а дорожек {len(tracks)}")
        if any(not n for n in names):
            die("--track-names: пустое имя дорожки")
        tracks = [(names[i], src, ch) for i, (_, src, ch) in enumerate(tracks)]
    # Имена дорожек - идентификатор спикера: одинаковые склеили бы разных людей
    # в одного и заодно отключили бы между ними фильтр протечки голоса.
    # Совпасть могут и по умолчанию - у файлов с одинаковым basename из разных папок.
    labels = [name for name, _, _ in tracks]
    dupes = sorted({n for n in labels if labels.count(n) > 1})
    if dupes:
        die(f"имена дорожек повторяются: {', '.join(dupes)} - задайте разные через --track-names")
    return tracks


def load_asr(threads):
    """Модель грузится один раз на прогон - на многодорожечном входе это дорогая операция."""
    import gigaam
    if threads > 0:
        import torch
        torch.set_num_threads(threads)
    return gigaam.load_model(ASR_MODEL, device="cpu")


def asr_words(model, audio, sr, tmpdir, tag="chunk"):
    import soundfile as sf
    words, ch = [], CHUNK_S * sr
    for i in range(0, len(audio), ch):
        p = os.path.join(tmpdir, f"{tag}_{i}.wav")   # tmpdir уникален -> параллель-безопасно
        sf.write(p, audio[i:i+ch], sr)
        off = i / sr
        try:
            for w in model.transcribe(p, word_timestamps=True).words:
                words.append([w.start + off, w.end + off, w.text])
        finally:
            os.path.exists(p) and os.remove(p)
    return words


# --- Диаризация: sherpa только сегментирует речь, кластеризуем сами. ---
# Встроенная FastClustering у sherpa вырождается на длинных файлах (все в 1 спикера
# либо сотни микрокластеров из-за дрейфа эмбеддингов). Поэтому: берем сегменты речи,
# считаем эмбеддинг каждого, и кластеризуем сферическим k-means (взвешенным по длине)
# с известным N. Проверено против облака: на длинных репликах ~95% совпадение.

def _diar_models_ok(emb):
    return os.path.exists(SEG_MODEL) and emb and os.path.exists(emb)


def _segment(audio, threads):
    """Временные границы речи (лейблы sherpa игнорируем - кластеризуем сами)."""
    import sherpa_onnx
    nt = threads if threads > 0 else 1
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL),
            num_threads=nt),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB["campplus"], num_threads=nt),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=2),
        min_duration_on=DIAR_MIN_ON, min_duration_off=0.5)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    return [(r.start, r.end) for r in sd.process(audio).sort_by_start_time()]


def _embed_segments(audio, segs, emb_path, threads):
    """Эмбеддинг каждого сегмента (по всему его аудио). Возвращает (матрица, оставленные сегменты)."""
    import numpy as np, sherpa_onnx
    nt = threads if threads > 0 else 1
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_path, num_threads=nt))
    X, kept = [], []
    for s0, s1 in segs:
        a = audio[int(s0*SR):int(s1*SR)]
        if len(a) < int(0.25*SR):     # слишком коротко для надежного эмбеддинга
            continue
        st = ext.create_stream(); st.accept_waveform(SR, a); st.input_finished()
        X.append(np.asarray(ext.compute(st), dtype="float32")); kept.append((s0, s1))
    if not X:
        return None, []
    return np.vstack(X), kept


def _skmeans(Xn, w, k, iters=100):
    """Сферический k-means (косинус) на L2-нормированных Xn, веса w (длительности)."""
    import numpy as np
    n = len(Xn)
    k = max(1, min(k, n))
    if k == 1:
        return np.zeros(n, dtype=int)
    # инициализация: жадно самые непохожие центры среди длинных сегментов
    li = np.argsort(-w)[:max(k*10, 40)]
    idx = [int(li[int(np.argmax(w[li]))])]
    for _ in range(k-1):
        sims = (Xn @ Xn[idx].T).max(1)
        idx.append(int(np.argmin(sims)))
    C = Xn[idx].copy()
    lab = np.full(n, -1)
    for _ in range(iters):
        new = (Xn @ C.T).argmax(1)
        if (new == lab).all():
            break
        lab = new
        for c in range(k):
            m = lab == c
            if m.any():
                v = (Xn[m] * w[m, None]).sum(0)
                C[c] = v / (np.linalg.norm(v) + 1e-9)
    return lab


def _silhouette(Xn, lab):
    import numpy as np
    D = 1.0 - Xn @ Xn.T
    np.fill_diagonal(D, 0.0)
    labs = np.unique(lab)
    if len(labs) < 2:
        return -1.0
    s = np.zeros(len(lab))
    for i in range(len(lab)):
        same = lab == lab[i]; same[i] = False
        if not same.any():
            # Одиночный кластер: своей внутрикластерной дистанции нет. Классический
            # silhouette дает такому объекту 0, а не (b-0)/b = 1 - иначе разбиение
            # "каждый сегмент отдельный спикер" получало бы идеальную оценку и
            # выигрывало перебор k (наблюдалось на коротких записях).
            s[i] = 0.0
            continue
        a = D[i, same].mean()
        others = [D[i, lab == c].mean() for c in labs if c != lab[i]]
        b = min(others) if others else 0.0
        s[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return float(s.mean())


def run_diar(audio, n_speakers, emb_key, threads):
    import numpy as np
    emb = EMB.get(emb_key)
    # _segment строит sherpa-конфиг на campplus независимо от выбора, поэтому
    # проверяем оба файла: иначе с --embedding titanet проверка проходила бы,
    # а сегментация падала на отсутствующем campplus
    if not _diar_models_ok(EMB["campplus"]):
        die(f"нет модели campplus в {DIAR_DIR} (нужна для сегментации при любом --embedding)")
    if not _diar_models_ok(emb):
        die(f"нет моделей диаризации в {DIAR_DIR} - доустановите ONNX-модели sherpa-onnx, "
            f"см. SKILL.md, \"Установка движка\"")
    segs = _segment(audio, threads)
    if not segs:
        return []
    X, kept = _embed_segments(audio, segs, emb, threads)
    if X is None:
        return []
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    w = np.array([s1 - s0 for s0, s1 in kept], dtype="float32")
    if n_speakers and n_speakers > 0:
        lab = _skmeans(Xn, w, n_speakers)
    else:
        # авто-оценка числа спикеров по silhouette (порог у sherpa ненадежен)
        best_s, lab = -2.0, np.zeros(len(Xn), dtype=int)
        # верхняя граница - на сегмент меньше, чем сегментов: k == len(Xn) это
        # разбиение "каждый сегмент - свой спикер", осмысленным решением не бывает
        for k in range(2, min(DIAR_KMAX, len(Xn) - 1) + 1):
            l = _skmeans(Xn, w, k)
            if len(np.unique(l)) < k:
                continue
            s = _silhouette(Xn, l)
            if s > best_s:
                best_s, lab = s, l
    # перенумеровать метки в непрерывные 0..m-1 по времени первого появления
    remap, nxt = {}, 0
    for c in lab:
        if c not in remap:
            remap[c] = nxt; nxt += 1
    return [(kept[i][0], kept[i][1], remap[int(lab[i])]) for i in range(len(kept))]


def group_by_speaker(words, segs):
    def spk(a, b):
        best, ov = None, 0.0
        for s0, s1, s in segs:
            o = max(0, min(b, s1) - max(a, s0))
            if o > ov:
                ov, best = o, s
        return best if best is not None else 0
    lines, cur, buf, st = [], None, [], 0
    for a, b, tx in words:
        s = spk(a, b)
        if s != cur:
            if buf:
                lines.append((st, cur, " ".join(buf)))
            cur, buf, st = s, [tx], a
        else:
            buf.append(tx)
    if buf:
        lines.append((st, cur, " ".join(buf)))
    return lines


def group_by_pause(words, gap=1.2):
    lines, buf, st, prev = [], [], 0, None
    for a, b, tx in words:
        if prev is not None and a - prev > gap and buf:
            lines.append((st, None, " ".join(buf)))
            buf, st = [], a
        if not buf:
            st = a
        buf.append(tx)
        prev = b
    if buf:
        lines.append((st, None, " ".join(buf)))
    return lines


# --- Многодорожечный вход: спикер известен по построению, диаризация не нужна. ---

def group_track(words, label, gap=1.2):
    """Слова одной дорожки -> реплики (start, end, label, text), разрез по паузе."""
    blocks, buf, st, prev = [], [], 0.0, None
    for a, b, tx in words:
        if prev is not None and a - prev > gap and buf:
            blocks.append((st, prev, label, " ".join(buf)))
            buf, st = [], a
        if not buf:
            st = a
        buf.append(tx)
        prev = b
    if buf:
        blocks.append((st, prev if prev is not None else st, label, " ".join(buf)))
    return blocks


def text_similarity(a, b):
    """Доля общих слов относительно более короткой реплики (0..1)."""
    A, B = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def drop_bleed(blocks, loudness, min_overlap=0.5, min_sim=0.5, quiet_ratio=0.35):
    """Убирает протечки - одну и ту же речь, попавшую сразу в две дорожки.

    Дорожки бывают изолированные (пофайловый экспорт по участникам) и нет (общий
    микрофон в комнате, эхо из колонок). Во втором случае чужой голос попадает в
    соседнюю дорожку тише и обычно с искаженным текстом, и наивное "спикер =
    дорожка" напечатает одну реплику дважды от разных людей.

    Признак протечки - пересечение по времени ПЛЮС одно из двух: похожий текст
    либо заметно более тихая запись у одного из двоих. Одного пересечения мало:
    люди перебивают друг друга и по-настоящему.

    loudness(индекс) -> RMS этой реплики на ее дорожке.
    """
    order = sorted(range(len(blocks)), key=lambda i: blocks[i][0])
    dropped = set()
    for pos, i in enumerate(order):
        if i in dropped:
            continue
        s1, e1, l1, t1 = blocks[i]
        for j in order[pos + 1:]:
            if j in dropped:
                continue
            s2, e2, l2, t2 = blocks[j]
            if s2 >= e1:
                break                    # блоки отсортированы - дальше пересечений не будет
            if l1 == l2:
                continue                 # своя же дорожка, это просто соседние реплики
            overlap = min(e1, e2) - max(s1, s2)
            shorter = min(e1 - s1, e2 - s2)
            if shorter <= 0 or overlap / shorter < min_overlap:
                continue
            r1, r2 = loudness(i), loudness(j)
            quiet = i if r1 <= r2 else j
            ratio = min(r1, r2) / max(r1, r2) if max(r1, r2) > 0 else 1.0
            # Схожесть текста обязательна: одной разницы громкости мало - на
            # дорожках с разным gain она выбрасывала настоящую тихую реплику,
            # сказанную поверх чужой. Протечка же - это ТОТ ЖЕ текст тише.
            if text_similarity(t1, t2) >= min_sim and ratio < quiet_ratio:
                dropped.add(quiet)
                if quiet == i:
                    break
    return [b for k, b in enumerate(blocks) if k not in dropped]


def write_md(path, title, src, dur, lines, engine, note=None):
    global _partial_md
    today = datetime.date.today().isoformat()
    head = [f"# {title}", "",
            f"**Файл:** {src}  ",
            f"**Длительность:** {mmss(dur)}  ",
            f"**Обработано:** {today}, локально  ",
            f"**Движок:** {engine}  "]
    if note:
        head.append(f"{note}  ")
    head += ["", "## Транскрипт", ""]
    body = []
    for st, label, tx in lines:
        prefix = f"**[{mmss(st)}] {label}:** " if label else f"**[{mmss(st)}]** "
        body.append(prefix + tx.strip())
    # атомарно: пишем в .tmp рядом с целью (та же ФС) и переименовываем
    _partial_md = path + ".tmp"
    with open(_partial_md, "w") as f:
        f.write("\n".join(head) + "\n" + "\n\n".join(body) + "\n")
    os.replace(_partial_md, path)
    _partial_md = None


def main():
    global _tmpdir
    ap = argparse.ArgumentParser(description="Локальная транскрипция встречи (GigaAM + sherpa) в markdown.")
    ap.add_argument("input", nargs="+",
                    help="аудио/видео файл (mp4/webm/wav/m4a/...); несколько файлов = дорожки участников")
    ap.add_argument("-o", "--output", help="путь к .md (по умолчанию рядом с входным файлом)")
    ap.add_argument("--speakers", type=int, default=0, help="число участников (точнее диаризация); 0 = авто")
    ap.add_argument("--no-diar", action="store_true", help="без диаризации, чистый текст по паузам")
    ap.add_argument("--split-channels", action="store_true",
                    help="один многоканальный файл: каждый канал - отдельный спикер")
    ap.add_argument("--track-names",
                    help='имена спикеров по дорожкам через запятую ("Иван,Петр")')
    ap.add_argument("--no-bleed-filter", action="store_true",
                    help="не убирать протечки чужого голоса между дорожками")
    ap.add_argument("--embedding", choices=list(EMB), default="campplus", help="эмбеддинг диаризации")
    ap.add_argument("--title", help="заголовок в md (по умолчанию - имя файла)")
    ap.add_argument("--threads", type=int, default=0, help="число CPU-потоков (0 = авто/дефолт библиотек)")
    ap.add_argument("--no-wait", action="store_true",
                    help="не ждать очереди и памяти, а сразу выйти, если занято")
    ap.add_argument("--wait-timeout", type=int, default=7200,
                    help="сколько секунд ждать очереди/памяти (по умолчанию 2 ч)")
    ap.add_argument("--min-free-mb", type=int, default=2500,
                    help="не стартовать, пока свободной памяти меньше этого (МБ; только Linux)")
    ap.add_argument("--memory-max", default="4G",
                    help="лимит памяти cgroup для прогона (systemd-run --scope; только Linux)")
    ap.add_argument("--no-limit", action="store_true",
                    help="не заворачивать прогон в cgroup с лимитом памяти")
    args = ap.parse_args()

    # до всего остального: перезапуск себя под лимитом памяти, чтобы промах
    # убивал только транскрибацию, а не случайный процесс на машине
    if not args.no_limit:
        reexec_in_scope(args.memory_max)

    if args.threads and args.threads > 0:
        for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                   "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[_v] = str(args.threads)

    inputs = [os.path.abspath(os.path.expanduser(p)) for p in args.input]
    for p in inputs:
        if not os.path.exists(p):
            die(f"файл не найден: {p}")
    src = inputs[0]
    multitrack = len(inputs) > 1 or args.split_channels
    out = os.path.abspath(os.path.expanduser(args.output)) if args.output else os.path.splitext(src)[0] + ".md"
    # Транскрипт пишется поверх цели, поэтому цель обязана быть .md и не входом:
    # "-o запись.mp4" иначе заменил бы саму запись текстом (данные не вернуть)
    if os.path.splitext(out)[1].lower() != ".md":
        die(f"выходной файл должен быть .md, получено: {out}")
    if any(os.path.abspath(os.path.expanduser(p)) == out for p in inputs):
        die("выходной файл совпадает с входным - запись была бы уничтожена")
    title = args.title or os.path.splitext(os.path.basename(src))[0]

    # очередь и память - ДО тяжелых импортов ниже: ждущий процесс должен стоить
    # десятки мегабайт, а не полтора гигабайта загруженной модели
    acquire_lock(src, wait=not args.no_wait, timeout=args.wait_timeout)
    wait_for_memory(args.min_free_mb, wait=not args.no_wait, timeout=args.wait_timeout)

    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass

    import soundfile as sf
    _tmpdir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        if multitrack:
            tracks = plan_tracks(inputs, args.split_channels, args.track_names)
            print(f"[1/4] извлекаю аудио: дорожек {len(tracks)}...", file=sys.stderr)
            loaded, sr = [], SR
            for k, (name, path, ch) in enumerate(tracks):
                wav = os.path.join(_tmpdir, f"track_{k}.wav")
                extract_wav(path, wav) if ch is None else extract_channel_wav(path, ch, wav)
                a, sr = sf.read(wav, dtype="float32")
                loaded.append((name, a))
                os.remove(wav)
            dur = max(len(a) for _, a in loaded) / sr

            print(f"[2/4] распознавание речи ({dur/60:.1f} мин x {len(loaded)} дорожек)...", file=sys.stderr)
            t = time.time()
            model = load_asr(args.threads)
            blocks, signals = [], []
            for k, (name, a) in enumerate(loaded):
                w = asr_words(model, a, sr, _tmpdir, tag=f"t{k}")
                print(f"      {name}: слов={len(w)}", file=sys.stderr)
                for b in group_track(w, name):
                    blocks.append(b)
                    signals.append(a)
            print(f"      {time.time()-t:.0f}с", file=sys.stderr)

            print("[3/4] свожу дорожки...", file=sys.stderr)
            if not args.no_bleed_filter:
                import numpy as np

                def loudness(i):
                    s0, s1, _, _ = blocks[i]
                    seg = signals[i][int(s0 * sr):int(s1 * sr)]
                    return float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0

                kept = drop_bleed(blocks, loudness)
                if len(kept) < len(blocks):
                    print(f"      убрано протечек между дорожками: {len(blocks) - len(kept)}",
                          file=sys.stderr)
                blocks = kept
            lines = [(s0, name, tx) for s0, _, name, tx in sorted(blocks)]
            engine = f"GigaAM {ASR_MODEL} (ASR), спикер = дорожка"
            note = (f"**Дорожек:** {len(tracks)} ({', '.join(n for n, _, _ in tracks)}) - "
                    f"разметка точная, диаризация не применялась")
            src_label = ", ".join(dict.fromkeys(os.path.basename(p) for _, p, _ in tracks))
        else:
            print("[1/4] извлекаю аудио...", file=sys.stderr)
            wav = os.path.join(_tmpdir, "audio.wav")
            extract_wav(src, wav)
            audio, sr = sf.read(wav, dtype="float32")
            dur = len(audio) / sr

            print(f"[2/4] распознавание речи ({dur/60:.1f} мин)...", file=sys.stderr)
            t = time.time()
            model = load_asr(args.threads)
            words = asr_words(model, audio, sr, _tmpdir)
            print(f"      слов={len(words)}, {time.time()-t:.0f}с", file=sys.stderr)

            if args.no_diar:
                lines = [(st, None, tx) for st, _, tx in group_by_pause(words)]
                engine, note = f"GigaAM {ASR_MODEL} (ASR)", None
            else:
                print("[3/4] диаризация...", file=sys.stderr)
                t = time.time()
                segs = run_diar(audio, args.speakers, args.embedding, args.threads)
                print(f"      {time.time()-t:.0f}с", file=sys.stderr)
                grouped = group_by_speaker(words, segs)
                lines = [(st, f"Спикер {s+1}" if s is not None else None, tx) for st, s, tx in grouped]
                n_spk = len({s for _, s, _ in grouped if s is not None})
                engine = f"GigaAM {ASR_MODEL} (ASR) + sherpa-onnx/{args.embedding} (диаризация)"
                note = (f"**Спикеров выделено:** {n_spk} (разметка надежна на длинных репликах, "
                        f"короткие могут путаться; сверить и проставить имена вручную)")
            src_label = os.path.basename(src)

        print("[4/4] пишу markdown...", file=sys.stderr)
        write_md(out, title, src_label, dur, lines, engine, note)
        print(out)   # stdout = путь к результату (для пайпов)
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
