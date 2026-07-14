"""
LANTalk — ultra-light voice chat for gaming.

One file. No browser, no accounts, no cloud, no saved data. Audio only.
- One person picks "Host" and shares their IP.
- Everyone else picks "Join", types that IP, and hits Connect.
- Everyone hears everyone else.

Transport: UDP. Codec: Opus if available, otherwise raw PCM (auto-detected).
Topology: star — the host relays each person's audio to everyone else.
"""

import socket
import struct
import threading
import time
import json
import queue
import collections

import numpy as np
import sounddevice as sd
import customtkinter as ctk

# ---- Opus (optional). Falls back to raw PCM if the library/DLL is missing. ----
try:
    import opuslib
    _OPUS_OK = True
except Exception:
    _OPUS_OK = False

# ---------------------------- audio / net config ------------------------------
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME = 960              # 20 ms at 48 kHz
DEFAULT_PORT = 50007
KEEPALIVE_S = 1.5        # muted clients still ping so the host keeps them listed
STALE_S = 5.0            # drop a client the host hasn't heard from in this long

# packet types (first byte)
P_JOIN = 1
P_LEAVE = 2
P_AUDIO = 3
P_ROSTER = 4


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_tailscale_ip():
    """Return this machine's Tailscale IP (100.x range), or '' if not on Tailscale."""
    import subprocess

    def _is_ts(ip):
        try:
            a, b = ip.split(".")[0:2]
            return int(a) == 100 and 64 <= int(b) <= 127   # Tailscale CGNAT range
        except Exception:
            return False

    # 1) ask the Tailscale CLI directly, if it's installed
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=3,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in out.stdout.splitlines():
                ip = line.strip()
                if _is_ts(ip):
                    return ip
        except Exception:
            pass

    # 2) fall back to scanning this machine's own addresses
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_ts(ip):
                return ip
    except Exception:
        pass
    return ""


# ------------------------------- relay server ---------------------------------
def run_relay(port, stop_event):
    """Star relay. Receives each client's audio and forwards it to the others."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        srv.bind(("0.0.0.0", port))
    except OSError:
        return  # port busy — likely already hosting
    srv.settimeout(1.0)
    clients = {}          # addr -> [id, name, last_seen]
    next_id = 0

    def send_roster():
        roster = {str(v[0]): v[1] for v in clients.values()}
        blob = bytes([P_ROSTER]) + json.dumps(roster).encode("utf-8")
        for a in list(clients):
            srv.sendto(blob, a)

    while not stop_event.is_set():
        try:
            data, addr = srv.recvfrom(8192)
        except socket.timeout:
            data = None
        except OSError:
            break

        if data:
            ptype = data[0]
            if ptype == P_JOIN:
                name = data[1:].decode("utf-8", "ignore")[:24] or "player"
                if addr not in clients:
                    clients[addr] = [next_id % 256, name, time.time()]
                    next_id += 1
                    send_roster()
                else:
                    clients[addr][1] = name
                    clients[addr][2] = time.time()
            elif ptype == P_AUDIO and addr in clients:
                clients[addr][2] = time.time()
                out = bytes([P_AUDIO, clients[addr][0]]) + data[1:]
                for a in clients:
                    if a != addr:
                        srv.sendto(out, a)
            elif ptype == P_LEAVE and addr in clients:
                del clients[addr]
                send_roster()

        # prune anyone who went quiet
        now = time.time()
        stale = [a for a, v in clients.items() if now - v[2] > STALE_S]
        if stale:
            for a in stale:
                del clients[a]
            send_roster()

    srv.close()


# ------------------------------- voice client ---------------------------------
class VoiceClient:
    def __init__(self, host_ip, port, name, ui_queue):
        self.host_addr = (host_ip, port)
        self.name = name
        self.ui_queue = ui_queue

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)

        self.muted = False
        self.running = threading.Event()
        self.running.set()

        self.enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip") if _OPUS_OK else None
        self.decoders = {}                      # sender_id -> opus decoder
        self.streams = {}                       # sender_id -> deque of float32 frames
        self.lock = threading.Lock()

        self.in_stream = None
        self.out_stream = None
        self.threads = []

    # --- codec helpers ---
    def _encode(self, frame_f32):
        pcm = (np.clip(frame_f32, -1, 1) * 32767).astype(np.int16).tobytes()
        if self.enc:
            return self.enc.encode(pcm, FRAME)
        return pcm

    def _decode(self, sid, payload):
        if _OPUS_OK:
            dec = self.decoders.get(sid)
            if dec is None:
                dec = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
                self.decoders[sid] = dec
            pcm = dec.decode(payload, FRAME)
        else:
            pcm = payload
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    # --- sounddevice callbacks ---
    def _mic_cb(self, indata, frames, time_info, status):
        if self.muted or not self.running.is_set():
            return
        try:
            payload = self._encode(indata[:, 0].copy())
            self.sock.sendto(bytes([P_AUDIO]) + payload, self.host_addr)
        except Exception:
            pass

    def _spk_cb(self, outdata, frames, time_info, status):
        mix = np.zeros(frames, dtype=np.float32)
        with self.lock:
            for dq in self.streams.values():
                if dq:
                    mix += dq.popleft()
        np.clip(mix, -1, 1, out=mix)
        outdata[:, 0] = mix

    # --- network threads ---
    def _recv_loop(self):
        while self.running.is_set():
            try:
                data, _ = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            ptype = data[0]
            if ptype == P_AUDIO and len(data) >= 2:
                sid = data[1]
                try:
                    frame = self._decode(sid, data[2:])
                except Exception:
                    continue
                if len(frame) != FRAME:
                    frame = np.resize(frame, FRAME)
                with self.lock:
                    dq = self.streams.get(sid)
                    if dq is None:
                        dq = collections.deque(maxlen=8)   # ~160 ms jitter cap
                        self.streams[sid] = dq
                    dq.append(frame)
            elif ptype == P_ROSTER:
                try:
                    roster = json.loads(data[1:].decode("utf-8", "ignore"))
                    names = [n for n in roster.values()]
                    self.ui_queue.put(("roster", names))
                except Exception:
                    pass

    def _keepalive_loop(self):
        pkt = bytes([P_JOIN]) + self.name.encode("utf-8")[:24]
        while self.running.is_set():
            try:
                self.sock.sendto(pkt, self.host_addr)
            except OSError:
                break
            time.sleep(KEEPALIVE_S)

    # --- lifecycle ---
    def start(self):
        self.in_stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=FRAME,
            dtype="float32", callback=self._mic_cb,
        )
        self.out_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=FRAME,
            dtype="float32", callback=self._spk_cb,
        )
        self.in_stream.start()
        self.out_stream.start()
        for target in (self._recv_loop, self._keepalive_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def set_muted(self, muted):
        self.muted = muted

    def stop(self):
        self.running.clear()
        try:
            self.sock.sendto(bytes([P_LEAVE]), self.host_addr)
        except Exception:
            pass
        for s in (self.in_stream, self.out_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------- UI ----------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LANTalk")
        self.geometry("360x440")
        self.resizable(False, False)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.name = ""
        self.client = None
        self.relay_stop = None
        self.ui_queue = queue.Queue()

        self._build_name_screen()
        self._build_main_screen()
        self._show(self.name_frame)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_ui)

    # ---- screen 1: name ----
    def _build_name_screen(self):
        f = ctk.CTkFrame(self, fg_color="transparent")
        self.name_frame = f
        ctk.CTkLabel(f, text="LANTalk", font=("Segoe UI", 28, "bold")).pack(pady=(60, 6))
        ctk.CTkLabel(f, text="talk to your squad, nothing else",
                     text_color="gray60").pack(pady=(0, 30))
        self.name_entry = ctk.CTkEntry(f, placeholder_text="Your name", width=220,
                                       justify="center")
        self.name_entry.pack(pady=10)
        self.name_entry.bind("<Return>", lambda e: self._continue())
        ctk.CTkButton(f, text="Continue", width=220, command=self._continue).pack(pady=10)

    def _continue(self):
        name = self.name_entry.get().strip()
        if not name:
            self.name_entry.configure(placeholder_text="enter a name first")
            return
        self.name = name[:24]
        self._show(self.main_frame)

    # ---- screen 2: call ----
    def _build_main_screen(self):
        f = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame = f

        self.mode = ctk.StringVar(value="Join")
        seg = ctk.CTkSegmentedButton(f, values=["Host", "Join"], variable=self.mode,
                                     command=lambda _: self._mode_changed())
        seg.pack(pady=(24, 12))

        self.ip_entry = ctk.CTkEntry(f, placeholder_text="Host IP (e.g. 192.168.1.10)",
                                     width=260, justify="center")
        self.ip_entry.pack(pady=6)

        self.host_ip_label = ctk.CTkLabel(f, text="", text_color="gray60",
                                          font=("Consolas", 12))
        self.host_ip_label.pack(pady=(0, 6))

        self.connect_btn = ctk.CTkButton(f, text="Connect", width=260,
                                         command=self._toggle_connect)
        self.connect_btn.pack(pady=6)

        self.status = ctk.CTkLabel(f, text="Not connected", text_color="gray60")
        self.status.pack(pady=(4, 6))

        ctk.CTkLabel(f, text="In call", font=("Segoe UI", 12, "bold")).pack()
        self.roster = ctk.CTkTextbox(f, width=260, height=120, activate_scrollbars=True)
        self.roster.pack(pady=6)
        self.roster.configure(state="disabled")

        self.mute_btn = ctk.CTkButton(f, text="🎙  Mic on", width=260,
                                      fg_color="#2b7a3d", hover_color="#246634",
                                      command=self._toggle_mute)
        self.mute_btn.pack(pady=6)

        self._mode_changed()

    def _mode_changed(self):
        if self.mode.get() == "Host":
            self.ip_entry.configure(state="disabled")
            ts = get_tailscale_ip()
            if ts:
                text = f"Internet (Tailscale): {ts}\nSame Wi-Fi (LAN): {get_local_ip()}"
            else:
                text = f"Share this IP: {get_local_ip()}"
            self.host_ip_label.configure(text=text)
        else:
            self.ip_entry.configure(state="normal")
            self.host_ip_label.configure(text="")

    # ---- actions ----
    def _toggle_connect(self):
        if self.client:
            self._disconnect()
            return

        if self.mode.get() == "Host":
            host_ip = "127.0.0.1"
            self.relay_stop = threading.Event()
            threading.Thread(target=run_relay,
                             args=(DEFAULT_PORT, self.relay_stop),
                             daemon=True).start()
            time.sleep(0.15)
        else:
            host_ip = self.ip_entry.get().strip()
            if not host_ip:
                self.status.configure(text="type the host IP first", text_color="#d08")
                return

        try:
            self.client = VoiceClient(host_ip, DEFAULT_PORT, self.name, self.ui_queue)
            self.client.start()
        except Exception as e:
            self.status.configure(text=f"audio error: {e}", text_color="#d08")
            self.client = None
            self._stop_relay()
            return

        codec = "Opus" if _OPUS_OK else "PCM"
        self.status.configure(text=f"Connected ({codec})", text_color="#3ad07a")
        self.connect_btn.configure(text="Disconnect", fg_color="#8a2b2b",
                                   hover_color="#6e2222")

    def _disconnect(self):
        if self.client:
            self.client.stop()
            self.client = None
        self._stop_relay()
        self.status.configure(text="Not connected", text_color="gray60")
        self.connect_btn.configure(text="Connect", fg_color=["#3B8ED0", "#1F6AA5"],
                                   hover_color=["#36719F", "#144870"])
        self._set_roster([])

    def _stop_relay(self):
        if self.relay_stop:
            self.relay_stop.set()
            self.relay_stop = None

    def _toggle_mute(self):
        if not self.client:
            return
        muted = not self.client.muted
        self.client.set_muted(muted)
        if muted:
            self.mute_btn.configure(text="🔇  Muted", fg_color="#8a2b2b",
                                    hover_color="#6e2222")
        else:
            self.mute_btn.configure(text="🎙  Mic on", fg_color="#2b7a3d",
                                    hover_color="#246634")

    # ---- ui plumbing ----
    def _set_roster(self, names):
        self.roster.configure(state="normal")
        self.roster.delete("1.0", "end")
        self.roster.insert("end", "\n".join(f"•  {n}" for n in names))
        self.roster.configure(state="disabled")

    def _poll_ui(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "roster":
                    self._set_roster(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_ui)

    def _show(self, frame):
        for fr in (self.name_frame, self.main_frame):
            fr.pack_forget()
        frame.pack(fill="both", expand=True)

    def _on_close(self):
        self._disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
