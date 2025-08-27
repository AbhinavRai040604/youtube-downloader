"""
Advanced YouTube Downloader (Tkinter + yt-dlp)

Features:
- URL (video or playlist)
- Quality selector (best / resolution / audio-only)
- Subtitles language selection (auto/manual)
- Trim start/end times (seconds or HH:MM:SS)
- Estimated filesize & download time (quick speed test)
- Queue & concurrent downloads
- Progress, status, history
- Optional MP3 conversion (ffmpeg)
- Clipboard paste, theme toggle
Requirements:
    pip install yt-dlp ttkthemes pyperclip requests
FFmpeg required in PATH for trimming/conversion.
"""

import os, sys, json, time, threading, queue, subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.ttk import Combobox, Progressbar
from ttkthemes import ThemedTk
import yt_dlp
import pyperclip
import requests

# ---------------- Config ----------------
DEFAULT_SAVE = os.path.join(os.path.expanduser("~"), "Downloads")
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".yt_downloader_history.json")
FFMPEG_CMD = "ffmpeg"  # must be in PATH
MAX_WORKERS = 2  # concurrent downloads default

# ---------------- Helpers ----------------
def safe_filename(name):
    # Keep it short and safe for Windows
    bad = '<>:"/\\|?*\n\r\t'
    for c in bad:
        name = name.replace(c, '')
    return name[:120].strip()

def human_size(bytesize):
    if not bytesize:
        return "Unknown"
    for unit in ['B','KB','MB','GB','TB']:
        if bytesize < 1024:
            return f"{bytesize:.2f} {unit}"
        bytesize /= 1024
    return f"{bytesize:.2f} PB"

def quick_speed_test():
    # Downloads a small resource to estimate bandwidth (bytes/sec)
    urls = [
        "https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png",
        "https://www.cloudflare.com/img/cf-horizontal-bw.svg"
    ]
    for u in urls:
        try:
            t0 = time.time()
            r = requests.get(u, stream=True, timeout=6)
            total = 0
            for chunk in r.iter_content(8192):
                total += len(chunk)
                if total > 200000:  # ~200 KB enough
                    break
            t1 = time.time()
            if t1 - t0 > 0:
                return total / (t1 - t0)  # bytes/sec
        except Exception:
            continue
    return None

# ---------------- Worker Thread ----------------
class DownloadWorker(threading.Thread):
    def __init__(self, tasks_q, ui):
        super().__init__(daemon=True)
        self.q = tasks_q
        self.ui = ui
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                task = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_task(task)
            except Exception as e:
                self.ui.log(f"Task error: {e}")
            finally:
                self.q.task_done()

    def process_task(self, task):
        url, opts = task
        self.ui.log(f"Start: {url}")
        self.ui.schedule(lambda: self.ui.set_status("Downloading..."))
        ydl_opts = {
            'outtmpl': os.path.join(opts['save_folder'], '%(title).120s.%(ext)s'),
            'noplaylist': opts['noplaylist'],
            'continuedl': True,
            'retries': 6,
            'no_warnings': True,
            'quiet': True,
        }

        # format selection
        if opts['audio_only']:
            ydl_opts['format'] = 'bestaudio/best'
        else:
            q = opts['quality']
            if q == 'best':
                ydl_opts['format'] = 'bestvideo+bestaudio/best'
            elif q.endswith('p') and q[:-1].isdigit():
                # pick best video <= that height + best audio
                ydl_opts['format'] = f'bestvideo[height<={q[:-1]}]+bestaudio/best'
            else:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'

        # progress hook
        def progress(d):
            st = d.get('status')
            if st == 'downloading':
                tb = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                db = d.get('downloaded_bytes') or 0
                pct = (db / tb * 100) if tb else 0
                self.ui.schedule(lambda: self.ui.update_progress(pct))
                self.ui.schedule(lambda: self.ui.set_status(
                    f"Downloading... {pct:.1f}% {human_size(db)} / {human_size(tb)}"
                ))
            elif st == 'finished':
                self.ui.schedule(lambda: self.ui.update_progress(100))
                self.ui.schedule(lambda: self.ui.set_status("Finalizing..."))

        ydl_opts['progress_hooks'] = [progress]

        # download
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                out_file = ydl.prepare_filename(info)
        except Exception as e:
            self.ui.log(f"Download failed: {e}")
            self.ui.schedule(lambda: messagebox.showerror("Download failed", str(e)))
            return

        # convert audio to mp3 if requested
        if opts['audio_only'] and opts['convert_mp3']:
            mp3_path = os.path.splitext(out_file)[0] + ".mp3"
            try:
                subprocess.run([FFMPEG_CMD, '-y', '-i', out_file, '-vn', '-ab', '192k', '-ar', '44100', mp3_path],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.remove(out_file)
                out_file = mp3_path
            except Exception as e:
                self.ui.log(f"MP3 conversion failed: {e}")

        # trimming
        if opts['start'] or opts['end']:
            def normalize(ts):
                s = ts.strip()
                if not s:
                    return None
                if ':' in s:
                    return s
                try:
                    sec = int(float(s))
                    h = sec // 3600
                    m = (sec % 3600) // 60
                    sec = sec % 60
                    return f"{h:02d}:{m:02d}:{sec:02d}"
                except:
                    return None
            ss = normalize(opts['start'])
            to = normalize(opts['end'])
            trimmed = os.path.splitext(out_file)[0] + "_trimmed" + os.path.splitext(out_file)[1]
            cmd = [FFMPEG_CMD, '-y', '-i', out_file]
            if ss:
                cmd += ['-ss', ss]
            if to:
                cmd += ['-to', to]
            cmd_copy = cmd + ['-c', 'copy', trimmed]
            try:
                res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res.returncode != 0:
                    # fallback re-encode
                    cmd_re = cmd + ['-c:v', 'libx264', '-c:a', 'aac', trimmed]
                    subprocess.run(cmd_re, check=True)
                os.remove(out_file)
                out_file = trimmed
            except Exception as e:
                self.ui.log(f"Trim failed: {e}")

        # subtitles
        if opts['subs']:
            try:
                sub_opts = {
                    'skip_download': True,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': [opts['subs_lang']] if opts['subs_lang'] else ['en'],
                    'outtmpl': os.path.join(opts['save_folder'], '%(title).120s.%(ext)s'),
                    'quiet': True,
                }
                with yt_dlp.YoutubeDL(sub_opts) as yd:
                    yd.download([url])
            except Exception as e:
                self.ui.log(f"Sub download failed: {e}")

        # final UI updates
        self.ui.schedule(lambda: self.ui.add_history(url, out_file))
        self.ui.schedule(lambda: self.ui.update_progress(0))
        self.ui.schedule(lambda: self.ui.set_status("Ready"))
        self.ui.log(f"Completed: {out_file}")

# ---------------- UI ----------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("YT Downloader — Advanced")
        root.geometry("820x620")

        # Data
        self.tasks_q = queue.Queue()
        self.workers = []
        self.max_workers = MAX_WORKERS
        self.history = self.load_history()

        # UI Variables
        self.url = tk.StringVar()
        self.quality = tk.StringVar(value='best')
        self.save_folder = tk.StringVar(value=DEFAULT_SAVE)
        self.start = tk.StringVar()
        self.end = tk.StringVar()
        self.audio_only = tk.BooleanVar(value=False)
        self.convert_mp3 = tk.BooleanVar(value=False)
        self.subs = tk.BooleanVar(value=False)
        self.subs_lang = tk.StringVar(value='en')
        self.estimate_label_var = tk.StringVar(value='Size: --   ETA: --')
        self.status_var = tk.StringVar(value='Ready')
        self.progress_var = tk.DoubleVar(value=0)
        self.worker_count_var = tk.IntVar(value=self.max_workers)

        # Top frame
        f = tk.Frame(root)
        f.pack(fill='x', padx=10, pady=8)

        tk.Label(f, text="YouTube URL:").grid(row=0, column=0, sticky='w')
        tk.Entry(f, textvariable=self.url, width=70).grid(row=0, column=1, columnspan=3, padx=6)
        tk.Button(f, text="Paste", command=self.paste_clip).grid(row=0, column=4)

        tk.Label(f, text="Quality:").grid(row=1, column=0, sticky='w', pady=6)
        qbox = Combobox(f, textvariable=self.quality, values=['best','360p','480p','720p','1080p','1440p','2160p','audio'], width=12)
        qbox.grid(row=1, column=1, sticky='w')

        tk.Checkbutton(f, text="Audio only", variable=self.audio_only, command=self.on_audio_toggle).grid(row=1, column=2, sticky='w')
        tk.Checkbutton(f, text="Convert to MP3", variable=self.convert_mp3).grid(row=1, column=3, sticky='w')

        tk.Label(f, text="Subtitles:").grid(row=2, column=0, sticky='w')
        tk.Checkbutton(f, text="Download subs", variable=self.subs).grid(row=2, column=1, sticky='w')
        tk.Entry(f, textvariable=self.subs_lang, width=6).grid(row=2, column=2, sticky='w')
        tk.Label(f, text="(lang code, e.g. en, hi)").grid(row=2, column=3, sticky='w')

        tk.Label(f, text="Start (s or HH:MM:SS):").grid(row=3, column=0, sticky='w', pady=6)
        tk.Entry(f, textvariable=self.start, width=12).grid(row=3, column=1, sticky='w')
        tk.Label(f, text="End (s or HH:MM:SS):").grid(row=3, column=2, sticky='w')
        tk.Entry(f, textvariable=self.end, width=12).grid(row=3, column=3, sticky='w')

        tk.Button(f, text="Choose Folder", command=self.choose_folder).grid(row=4, column=0, pady=8)
        tk.Label(f, textvariable=self.save_folder).grid(row=4, column=1, columnspan=3, sticky='w')

        tk.Button(f, text="Estimate Size & Time", command=self.estimate).grid(row=5, column=0, pady=6)
        tk.Label(f, textvariable=self.estimate_label_var).grid(row=5, column=1, columnspan=3, sticky='w')

        tk.Label(f, text="Concurrent Downloads:").grid(row=6, column=0, sticky='w')
        tk.Spinbox(f, from_=1, to=6, textvariable=self.worker_count_var, width=5, command=self.update_workers).grid(row=6, column=1, sticky='w')

        tk.Button(f, text="Add to Queue", command=self.add_to_queue).grid(row=7, column=0, pady=10)
        tk.Button(f, text="Start Queue", command=self.start_workers).grid(row=7, column=1)
        tk.Button(f, text="Clear Queue", command=self.clear_queue).grid(row=7, column=2)

        # Progress / status
        mid = tk.Frame(root)
        mid.pack(fill='x', padx=10, pady=8)
        Progressbar(mid, variable=self.progress_var, length=760).pack()
        tk.Label(mid, textvariable=self.status_var).pack(anchor='w')

        # Queue list
        qframe = tk.Frame(root)
        qframe.pack(fill='both', expand=True, padx=10, pady=6)
        tk.Label(qframe, text="Queue:").pack(anchor='w')
        self.queue_listbox = tk.Listbox(qframe, height=6)
        self.queue_listbox.pack(fill='x')

        # History
        tk.Label(qframe, text="History:").pack(anchor='w', pady=(8,0))
        self.history_listbox = tk.Listbox(qframe, height=8)
        self.history_listbox.pack(fill='both', expand=True)

        tk.Button(root, text="Toggle Theme", command=self.toggle_theme).pack(pady=6)
        tk.Button(root, text="Open History Folder", command=self.open_history_folder).pack()

        # log
        self.log_area = tk.Text(root, height=6)
        self.log_area.pack(fill='x', padx=10, pady=6)

        self.set_status("Ready")
        self.refresh_history_list()

        # initialize default workers collection (not started)
        self.tasks_q = queue.Queue()
        self.workers = []
        self.update_workers()

    # --- UI helpers ---
    def schedule(self, func):
        self.root.after(0, func)

    def log(self, msg):
        t = time.strftime("%H:%M:%S")
        self.log_area.insert('end', f"[{t}] {msg}\n")
        self.log_area.see('end')

    def set_status(self, txt):
        self.status_var.set(txt)

    def update_progress(self, pct):
        try:
            self.progress_var.set(pct)
        except:
            pass

    def load_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except:
            pass
        return []

    def save_history(self):
        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except:
            pass

    def add_history(self, url, filepath):
        item = {'time': time.time(), 'url': url, 'file': filepath}
        self.history.insert(0, item)
        self.save_history()
        self.refresh_history_list()

    def refresh_history_list(self):
        self.history_listbox.delete(0, 'end')
        for h in self.history[:100]:
            self.history_listbox.insert('end', f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(h['time']))}  {os.path.basename(h['file'])}")

    def open_history_folder(self):
        folder = self.save_folder.get()
        if os.path.exists(folder):
            if sys.platform.startswith('win'):
                os.startfile(folder)
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', folder])
        else:
            messagebox.showinfo("Info", "Folder does not exist")

    def paste_clip(self):
        try:
            txt = pyperclip.paste()
            if txt and txt.startswith("http"):
                self.url.set(txt)
                self.log("Pasted URL from clipboard.")
            else:
                self.log("Clipboard does not contain a URL.")
        except Exception as e:
            self.log(f"Clipboard error: {e}")

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.save_folder.get())
        if d:
            self.save_folder.set(d)

    def on_audio_toggle(self):
        if self.audio_only.get():
            self.quality.set('audio')

    def estimate(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("Input", "Paste a YouTube URL first.")
            return
        # quick info fetch (no download), find estimated size for chosen format
        try:
            ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            # choose format estimate
            if self.audio_only.get():
                # estimate best audio abr
                abr = info.get('abr') or 128
                est_bytes = (abr * 1000 / 8) * (info.get('duration') or 0)
            else:
                # find best matching format entry
                formats = info.get('formats', [])
                sel = None
                q = self.quality.get()
                if q == 'best':
                    sel = formats[-1] if formats else None
                elif q.endswith('p') and q[:-1].isdigit():
                    h = int(q[:-1])
                    # pick closest <= h
                    cand = [f for f in formats if f.get('height') and f['height'] <= h and f.get('filesize')]
                    if cand:
                        sel = max(cand, key=lambda x: x.get('height') or 0)
                else:
                    # fallback to best with filesize
                    cand = [f for f in formats if f.get('filesize')]
                    sel = max(cand, key=lambda x: x.get('filesize')) if cand else None
                est_bytes = sel.get('filesize') or info.get('filesize') or None
            # perform quick speed test
            spd = quick_speed_test()
            if spd:
                eta = (est_bytes / spd) if est_bytes else None
                if eta:
                    mins = int(eta // 60); secs = int(eta % 60)
                    eta_str = f"{mins}m{secs}s"
                else:
                    eta_str = "Unknown"
            else:
                eta_str = "Unknown"
            self.estimate_label_var.set(f"Size: {human_size(est_bytes)}   ETA: {eta_str}")
            self.log(f"Estimate done — size {human_size(est_bytes)} ETA {eta_str}")
        except Exception as e:
            messagebox.showerror("Estimate failed", str(e))

    def add_to_queue(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("Input required", "Please paste a YouTube link.")
            return
        opts = {
            'quality': self.quality.get(),
            'save_folder': self.save_folder.get(),
            'start': self.start.get(),
            'end': self.end.get(),
            'audio_only': self.audio_only.get(),
            'convert_mp3': self.convert_mp3.get(),
            'subs': self.subs.get(),
            'subs_lang': self.subs_lang.get(),
            'noplaylist': False if 'playlist' in url.lower() else True
        }
        self.tasks_q.put((url, opts))
        self.queue_listbox.insert('end', f"{url} [{opts['quality']}]")
        self.log("Added to queue.")

    def clear_queue(self):
        with self.tasks_q.mutex:
            self.tasks_q.queue.clear()
        self.queue_listbox.delete(0, 'end')
        self.log("Queue cleared.")

    def start_workers(self):
        if len(self.workers) > 0:
            messagebox.showinfo("Info", "Workers already running.")
            return
        cnt = self.worker_count_var.get()
        self.max_workers = max(1, int(cnt))
        for _ in range(self.max_workers):
            w = DownloadWorker(self.tasks_q, self)
            w.start()
            self.workers.append(w)
        self.log(f"Started {len(self.workers)} workers.")
        self.set_status("Running")

    def update_workers(self):
        # will apply at next start
        self.max_workers = max(1, int(self.worker_count_var.get()))
        self.log(f"Worker limit set to {self.max_workers}")

    def stop_workers(self):
        for w in self.workers:
            w.stop()
        self.workers.clear()
        self.set_status("Ready")
        self.log("Workers stopped.")

    def toggle_theme(self):
        # switch between two themes available in ttkthemes
        current = self.root.get_theme()
        new = 'breeze' if current != 'breeze' else 'arc'
        try:
            self.root.set_theme(new)
            self.log(f"Theme: {new}")
        except:
            pass

# ---------------- Run ----------------
if __name__ == "__main__":
    # quick ffmpeg check
    try:
        subprocess.run([FFMPEG_CMD, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        tk.messagebox.showwarning("FFmpeg not found", "FFmpeg not detected in PATH. Trimming/MP3 conversion will fail without it.")

    root = ThemedTk(theme="arc")
    app = App(root)
    try:
        root.mainloop()
    finally:
        app.stop_workers()
