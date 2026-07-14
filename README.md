# LANTalk

Dead-simple voice chat for gaming. No browser, no accounts, no cloud, no saved
data. One window: your name, an IP box, a Connect button, a mute toggle, and the
list of who's in the call. Audio only. Everyone hears everyone.

- **Transport:** UDP (lowest latency)
- **Codec:** Opus if installed, otherwise raw PCM — auto-detected, no config
- **Topology:** star — the host relays each person's audio to everyone else
  (fine for a small squad, ~5 people)

## Run it (from source)

```powershell
pip install -r requirements.txt
python app.py
```

1. Type your name → **Continue**.
2. **One** person picks **Host** and shares the IP shown on screen.
3. Everyone else picks **Join**, types the host's IP, and hits **Connect**.

The host runs the relay *and* joins the call in the same window.

> Same house / LAN? Use the `192.168.x.x` IP shown. Over the internet, the host
> must port-forward UDP **50007** (or use a VPN like Tailscale/ZeroTier and share
> that IP instead — easier and no router config).

## Build a single .exe

```powershell
pip install pyinstaller
pyinstaller --onefile --noconsole --name LANTalk app.py
```

The result is `dist\LANTalk.exe` — copy it to any Windows 10/11 machine, no
Python needed. (If you want the Opus codec baked in, install `opuslib` and its
opus DLL before building; otherwise the exe just uses raw PCM.)

## Notes

- Mic + speakers use your default Windows audio devices.
- CPU cost is tiny — 20 ms Opus/PCM frames, no video, no extra threads spinning.
- Firewall: allow the app on **Private networks** the first time Windows asks,
  or connections get silently dropped.
