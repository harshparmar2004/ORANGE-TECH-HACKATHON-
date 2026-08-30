import os
import time
import requests
import math
import struct
import base64
import tempfile
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR)

@app.errorhandler(Exception)
def handle_exception(e):
    """Ensure all server exceptions return clean JSON instead of HTML error pages."""
    return jsonify({"detail": f"Internal server error: {str(e)}"}), 500


def create_demo_wav_bytes(duration_sec=8):
    """Generate a clean synthetic WAV audio file for demo/fallback audio playback."""
    sample_rate = 22050
    num_samples = int(sample_rate * duration_sec)
    
    # WAV Header construction
    header = bytearray()
    header.extend(b'RIFF')
    header.extend(struct.pack('<I', 36 + num_samples * 2))
    header.extend(b'WAVEfmt ')
    header.extend(struct.pack('<I', 16)) # Subchunk1Size (16 for PCM)
    header.extend(struct.pack('<H', 1))  # AudioFormat (1 for PCM)
    header.extend(struct.pack('<H', 1))  # NumChannels (1 mono)
    header.extend(struct.pack('<I', sample_rate))
    header.extend(struct.pack('<I', sample_rate * 2))
    header.extend(struct.pack('<H', 2))  # BlockAlign
    header.extend(struct.pack('<H', 16)) # BitsPerSample
    header.extend(b'data')
    header.extend(struct.pack('<I', num_samples * 2))

    # Generate multi-frequency speech-like harmonics
    samples = bytearray()
    for i in range(num_samples):
        t = i / sample_rate
        # Voice-like cadence modulation
        cadence = 0.5 + 0.5 * math.sin(2 * math.pi * 3.5 * t)
        freq1 = 220 + 40 * math.sin(2 * math.pi * 1.5 * t)
        freq2 = 440 + 80 * math.sin(2 * math.pi * 2.2 * t)
        
        val = (math.sin(2 * math.pi * freq1 * t) * 0.6 + math.sin(2 * math.pi * freq2 * t) * 0.4) * cadence
        sample_val = int(val * 12000)
        samples.extend(struct.pack('<h', max(-32768, min(32767, sample_val))))

    return bytes(header + samples)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)


# In-memory store for recent broadcasts (last 5 items)
RECENT_BROADCASTS = []

@app.route("/history", methods=["GET"])
def get_history():
    return jsonify(RECENT_BROADCASTS), 200


@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json(silent=True) or {}
        url = data.get("url", "").strip()
        persona = data.get("persona", "standard").strip().lower()

        if not url:
            return jsonify({"detail": "URL is required."}), 400

        # Ensure URL has protocol
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url

        ctx_key = os.getenv("CONTEXT_DEV_API_KEY", "").strip()
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        fish_key = os.getenv("FISH_API_KEY", "").strip()

        is_placeholder_key = (
            not ctx_key or "your_key" in ctx_key or
            not groq_key or "your_key" in groq_key or
            not fish_key or "your_key" in fish_key
        )

        # Demo mode trigger if explicitly requested or if user tests with demo url when keys are missing
        is_demo_request = "demo" in url.lower() or "example.com" in url.lower()

        if is_placeholder_key:
            if is_demo_request:
                # Generate demo response for trial without API keys
                demo_script = (
                    "Welcome to On Air Voice Web Briefing. This is a demonstration broadcast summarizing key web content. "
                    "Context dot dev retrieves clean markdown from the target URL, Groq LLM condenses it into spoken radio prose, "
                    "and Fish Audio generates natural voice synthesis. Add your production API keys to the environment file to stream live web pages."
                )
                audio_bytes = create_demo_wav_bytes(duration_sec=10)
                output_path = os.path.join(STATIC_DIR, "output.mp3")
                with open(output_path, "wb") as f:
                    f.write(audio_bytes)

                timestamp = int(time.time())
                res_payload = {
                    "title": "On Air Broadcast Demo",
                    "url": url,
                    "persona": persona,
                    "script": demo_script,
                    "audio_url": f"/static/output.mp3?t={timestamp}"
                }
                RECENT_BROADCASTS.insert(0, res_payload)
                if len(RECENT_BROADCASTS) > 5:
                    RECENT_BROADCASTS.pop()
                return jsonify(res_payload), 200
            else:
                return jsonify({
                    "detail": "API credentials (CONTEXT_DEV_API_KEY, GROQ_API_KEY, FISH_API_KEY) are missing or set to defaults on the server. Please configure valid environment variables in your Render deployment."
                }), 502

        # Stage 1: Context.dev Scrape
        try:
            ctx_resp = requests.get(
                "https://api.context.dev/v1/web/scrape/markdown",
                headers={"Authorization": f"Bearer {ctx_key}"},
                params={"url": url, "useMainContentOnly": "true"},
                timeout=25
            )
        except requests.RequestException as e:
            return jsonify({"detail": f"Failed to connect to Context.dev scraper: {str(e)}"}), 502

        if ctx_resp.status_code != 200:
            err_msg = "Scraping failed."
            try:
                err_json = ctx_resp.json()
                if err_json.get("error_code") == "WEBSITE_BLOCKED":
                    err_msg = "This page couldn't be read due to anti-bot wall or login requirement."
                else:
                    err_msg = err_json.get("message") or err_json.get("detail") or ctx_resp.text[:200]
            except Exception:
                err_msg = ctx_resp.text[:200] or f"HTTP {ctx_resp.status_code}"
            return jsonify({"detail": f"Context.dev error: {err_msg}"}), 502

        ctx_json = ctx_resp.json()
        if ctx_json.get("error_code") == "WEBSITE_BLOCKED":
            return jsonify({"detail": "Context.dev error: This page couldn't be read due to anti-bot wall or login requirement."}), 502

        markdown_content = ctx_json.get("markdown", "")
        if not markdown_content:
            return jsonify({"detail": "Context.dev error: No content could be extracted from this URL."}), 502

        # Extract title from metadata if available
        page_title = ctx_json.get("metadata", {}).get("title") or url.split("//")[-1].split("/")[0]

        # Truncate markdown to ~3000 chars for rapid LLM processing
        truncated_md = markdown_content[:3000]

        # Persona style prompts optimized for fast, punchy 50-word scripts
        persona_instructions = {
            "upbeat": (
                "Persona: Tech Pulse Anchor (Upbeat & Energetic).\n"
                "Use an emotion cue like [excited] at the start. Keep it fast-paced, punchy, under 60 words total."
            ),
            "calm": (
                "Persona: Deep Dive Analyst (Calm & Thoughtful).\n"
                "Use an emotion cue like [calm] at the start. Keep it slow, clear, reflective, under 60 words total."
            ),
            "vintage": (
                "Persona: 1940s Vintage Radio Newsreel.\n"
                "Start with 'Good evening listeners, breaking news!' Use dramatic vintage phrasing, under 60 words total."
            ),
            "standard": (
                "Persona: Standard Radio News Anchor.\n"
                "Deliver a crisp, professional, 3-sentence radio briefing, under 60 words total."
            )
        }

        selected_persona_prompt = persona_instructions.get(persona, persona_instructions["standard"])

        # Stage 2: Fast Groq LLM Condensation
        prompt = (
            f"{selected_persona_prompt}\n"
            "Summarize this web page into a fast 3-sentence spoken radio briefing. "
            "STRICT CONSTRAINTS: No markdown (no asterisks, headers, bullets). Plain spoken English under 60 words.\n\n"
            f"Web Page Content:\n{truncated_md}"
        )

        groq_models = ["openai/gpt-oss-20b", "qwen/qwen3.6-27b", "openai/gpt-oss-120b", "groq/compound-mini"]
        script = None
        groq_err_msg = "Unknown Groq error"

        for model_name in groq_models:
            try:
                groq_resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5
                    },
                    timeout=15
                )
                if groq_resp.status_code == 200:
                    script = groq_resp.json()["choices"][0]["message"]["content"].strip()
                    break
                else:
                    try:
                        groq_err_msg = groq_resp.json().get("error", {}).get("message") or groq_resp.text[:200]
                    except Exception:
                        groq_err_msg = groq_resp.text[:200]
            except requests.RequestException as e:
                groq_err_msg = str(e)


        if not script:
            return jsonify({"detail": f"Groq LLM error: {groq_err_msg}"}), 502

        # Stage 3: Fast Fish Audio TTS
        fish_models = ["s2.1-pro-free", "s2-pro", "s2.1-pro"]
        fish_resp = None
        fish_err_msg = "Fish Audio error"

        for f_model in fish_models:
            try:
                resp = requests.post(
                    "https://api.fish.audio/v1/tts",
                    headers={
                        "Authorization": f"Bearer {fish_key}",
                        "Content-Type": "application/json",
                        "model": f_model
                    },
                    json={
                        "text": script,
                        "reference_id": "536d3a5e000945adb7038665781a4aca",
                        "format": "mp3"
                    },
                    timeout=20
                )
                if resp.status_code == 200:
                    fish_resp = resp
                    break
                else:
                    try:
                        fish_err_msg = resp.json().get("message") or resp.json().get("detail") or resp.text[:200]
                    except Exception:
                        fish_err_msg = resp.text[:200]
            except requests.RequestException as e:
                fish_err_msg = str(e)

        if not fish_resp or fish_resp.status_code != 200:
            return jsonify({"detail": f"Fish Audio TTS error: {fish_err_msg}"}), 502

        # Convert audio bytes directly to Base64 Data URL (100% stateless, works on read-only filesystems like /var/task)
        audio_b64 = base64.b64encode(fish_resp.content).decode("utf-8")
        audio_data_url = f"data:audio/mp3;base64,{audio_b64}"

        # Attempt to save to /tmp or static folder if writable, ignore filesystem errors
        try:
            tmp_dir = tempfile.gettempdir()
            tmp_path = os.path.join(tmp_dir, "output.mp3")
            with open(tmp_path, "wb") as f:
                f.write(fish_resp.content)
        except Exception:
            pass

        res_payload = {
            "title": page_title,
            "url": url,
            "persona": persona,
            "script": script,
            "audio_url": audio_data_url
        }

        RECENT_BROADCASTS.insert(0, res_payload)
        if len(RECENT_BROADCASTS) > 5:
            RECENT_BROADCASTS.pop()

        return jsonify(res_payload), 200
    except Exception as exc:
        return jsonify({"detail": f"Server processing error: {str(exc)}"}), 500


    except Exception as exc:
        return jsonify({"detail": f"Server processing error: {str(exc)}"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)


