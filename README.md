# Generate Video for YouTube and Instagram

Type a topic → get a finished vertical short video (script, voiceover, subtitles, stock footage, background music) → optionally publish it to **YouTube Shorts** and **Instagram Reels**.

This repo is based on [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) (MIT License) and is set up to run **for free on your own PC**:

| Step | Service | Cost |
|---|---|---|
| Write the script + search keywords | [Ollama](https://ollama.com) running `qwen2.5:14b` locally | Free |
| Find footage | [Pixabay](https://pixabay.com/api/docs/) stock videos | Free API key |
| Voiceover | Edge TTS (built in) | Free, no key |
| Subtitles | Generated from the voice timing (faster-whisper as fallback) | Free |
| Publish to YouTube / Instagram | [Upload-Post](https://upload-post.com) | Check their pricing |

> More docs from the original project: [README-en.md](README-en.md) · [中文](README-zh.md) · [日本語](README-ja.md)

---

## Requirements

- Windows 10/11 with **WSL2 + Ubuntu** (also works on native Linux or macOS)
- 8 GB RAM minimum (16 GB+ recommended)
- An NVIDIA GPU is optional but makes Ollama much faster. `qwen2.5:14b` needs about 10 GB of VRAM; on smaller GPUs use `qwen2.5:7b`
- About 15 GB free disk (Python packages and the AI model)

---

## Step 1: Install WSL and Ubuntu (Windows only)

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

Restart when asked, then open **Ubuntu** from the Start menu and create a username and password.

All remaining commands are run **inside the Ubuntu terminal**.

---

## Step 2: Install the basic tools

```bash
sudo apt update && sudo apt install -y git curl
```

Install **uv**, the Python package manager this project uses:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

Check that it works:

```bash
uv --version
```

---

## Step 3: Download this project

```bash
mkdir -p ~/video_gen && cd ~/video_gen
git clone https://github.com/mahi80/Generate-Video-for-youtube-and-instragram.git
cd Generate-Video-for-youtube-and-instragram
```

Install the Python dependencies (the first run takes a few minutes):

```bash
uv sync --frozen
```

---

## Step 4: Install Ollama and the AI model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:14b
```

That downloads about 9 GB. On a GPU with less than 12 GB of VRAM, use `ollama pull qwen2.5:7b` instead.

Check that it runs:

```bash
ollama run qwen2.5:14b "Say hello in one sentence"
```

> Ollama usually starts in the background after it's installed. If you see "could not connect", run `ollama serve` in a separate terminal.

---

## Step 5: Get a free Pixabay API key

1. Create an account at <https://pixabay.com>
2. Open <https://pixabay.com/api/docs/> while logged in
3. Your key is shown in the **Parameters → key** row. Copy it.

> Why not Pexels? Pexels also works, but Pexels keys can get paused. Pixabay is a drop-in replacement.

---

## Step 6: Create your config file

```bash
cp config.example.toml config.toml
nano config.toml
```

Find each line below (in nano, press `Ctrl+W` to search) and change it to:

```toml
video_source = "pixabay"
pixabay_api_keys = ["PASTE_YOUR_PIXABAY_KEY_HERE"]

llm_provider = "ollama"
ollama_model_name = "qwen2.5:14b"
```

Leave `ollama_base_url = ""` empty; the app finds your local Ollama by itself.

Save with `Ctrl+O`, press `Enter`, then exit with `Ctrl+X`.

> ⚠️ **Never commit or share `config.toml`.** It holds your API keys. It is already listed in `.gitignore`.

---

## Step 7: Start the app

```bash
./webui.sh
```

Then open **<http://localhost:8501>** in your Windows browser (Chrome or Edge).

> A `gio: Operation not supported` message is harmless. WSL just can't open a browser by itself.
> If port 8501 is busy, the script picks the next free one (8502, 8503…) and prints the address.

---

## Step 8: Make your first video

1. **Video Subject:** type a topic, e.g. `5 surprising facts about the ocean`
2. Click **Generate Video Script and Keywords** and wait 10–30 seconds. You can edit the script afterwards.
3. **Video Settings:**
   - Video Source: **Pixabay**
   - Video Ratio: **Portrait 9:16** (required for Shorts and Reels)
4. **Audio Settings:**
   - Voiceover Service: **Azure TTS V1 (Edge TTS)**
   - Voiceover Voice: pick an English one, e.g. `en-US-AriaNeural`. The default voice is Chinese, so change it.
   - Click **Play Voice** to preview it
5. **Subtitle Settings:** pick a font and size you like
6. Click **Generate Video**

A 30–60 second video usually takes a few minutes. When it's done you can play and download it right on the page. Files are saved in:

```
storage/tasks/<task-id>/final-1.mp4
```

---

## Step 9 (optional): Auto-publish to YouTube Shorts and Instagram

Publishing goes through **Upload-Post**, which connects to your social accounts.

### 9.1 Connect your accounts

1. Sign up at <https://upload-post.com>
2. Go to **Manage Users** (<https://app.upload-post.com/manage-users>) and create a profile, or use `default`
3. Connect **YouTube** (sign in with Google and allow access)
4. Connect **Instagram**. Instagram generally needs a **Business or Creator** account linked to a Facebook Page; follow Upload-Post's prompts.
5. Go to **API Keys** (<https://app.upload-post.com/api-keys>) and create a key

### 9.2 Add it to `config.toml`

```toml
upload_post_enabled = true
upload_post_api_key = "PASTE_YOUR_UPLOAD_POST_KEY_HERE"
upload_post_username = "default"              # your Upload-Post profile name
upload_post_platforms = ["youtube", "instagram"]
upload_post_auto_upload = true                # publish automatically when a video finishes
upload_post_youtube_privacy_status = "private" # "private", "unlisted", or "public"
upload_post_youtube_made_for_kids = false
```

You can also change these in the app under **Settings → Auto-Publish Settings**.

Restart the app (`Ctrl+C`, then `./webui.sh` again) so it loads the new settings.

> 💡 Start with `"private"`. Each video lands in **YouTube Studio** as private so you can watch it before switching it to Public. Once you trust the results, change it to `"public"`.
>
> ⚠️ With `upload_post_auto_upload = true`, **every** video you generate is published. Set it back to `false` to stop publishing.

### Check that your key works

```bash
curl -s -H "Authorization: Apikey YOUR_UPLOAD_POST_KEY" https://api.upload-post.com/api/uploadposts/users
```

You should see your profile name and the connected platforms.

---

## Tips for better Shorts and Reels

- Keep videos **under 60 seconds**. Under **Advanced Script Settings**, set paragraphs to 1–2.
- Use **Custom Script Requirements** to set the style, e.g. `Hook in the first sentence, casual tone, end with a question`.
- If the footage doesn't match the script well, edit the **Video Keywords** box. Short English terms such as `ocean waves` or `deep sea fish` work best.
- YouTube can limit channels that post **repetitive, mass-produced** content. Review videos before making them public and vary your topics.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Please Enter the Pixabay API Key` | Check `pixabay_api_keys` in `config.toml`, then restart the app |
| Keyword generation fails or returns odd text | Use a bigger model (`qwen2.5:14b`), or click **Generate Video Keywords** again |
| `could not connect to ollama` | Run `ollama serve`, then check with `ollama list` |
| Video has Chinese voice | Change **Voiceover Voice** to an `en-US-…` voice |
| Page is blank | Use Chrome or Edge and refresh |
| Slow script generation | Normal on CPU. A GPU speeds it up a lot; check with `nvidia-smi` |
| Nothing posted to YouTube | Check `upload_post_enabled = true` and `upload_post_auto_upload = true`, and that the profile name matches Upload-Post exactly |

---

## Updating

```bash
cd ~/video_gen/Generate-Video-for-youtube-and-instragram
git pull
uv sync --frozen
```

---

## Credits and license

Built on [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) by harry0703, released under the [MIT License](LICENSE).
