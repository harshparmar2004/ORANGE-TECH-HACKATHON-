import os
import time
import requests
import math
import struct
import base64
import tempfile
import json
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
        language = data.get("language", "en").strip().lower()
        length = data.get("length", "60").strip()

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
                demo_script = (
                    "Welcome to On Air Voice Web Briefing. This is a demonstration broadcast summarizing key web content. "
                    "Context dot dev retrieves clean markdown from the target URL, Groq LLM condenses it into spoken radio prose, "
                    "and Fish Audio generates natural voice synthesis. Add your production API keys to the environment file to stream live web pages."
                )
                audio_bytes = create_demo_wav_bytes(duration_sec=10)
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                audio_data_url = f"data:audio/mp3;base64,{audio_b64}"

                timestamp = int(time.time())
                res_payload = {
                    "title": "On Air Broadcast Demo",
                    "url": url,
                    "persona": persona,
                    "language": language,
                    "length": length,
                    "script": demo_script,
                    "chapters": [
                        {"time": 0, "label": "0:00 - Introduction & Hook"},
                        {"time": 4, "label": "0:04 - Key Technology Insights"},
                        {"time": 8, "label": "0:08 - Conclusion & Sign-off"}
                    ],
                    "audio_url": audio_data_url
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

        page_title = ctx_json.get("metadata", {}).get("title") or url.split("//")[-1].split("/")[0]

        # Dynamic scraping depth based on selected briefing duration
        scrape_depths = {
            "30": 3000,
            "60": 6000,
            "90": 12000  # Maximum scraped depth for 90s deep dive
        }
        max_chars = scrape_depths.get(length, 6000)
        truncated_md = markdown_content[:max_chars]

        # Language instructions
        lang_names = {
            "en": "English",
            "es": "Spanish (Español)",
            "fr": "French (Français)",
            "de": "German (Deutsch)",
            "hi": "Hindi (हिंदी)"
        }
        target_lang = lang_names.get(language, "English")

        # Length word constraints & pointwise count
        length_configs = {
            "30": ("under 35 words total (approx 30 seconds)", 3),
            "60": ("under 65 words total (approx 60 seconds)", 5),
            "90": ("under 110 words total (approx 90 seconds)", 7)
        }
        target_length, num_points = length_configs.get(length, ("under 65 words total (approx 60 seconds)", 5))

        # Persona style prompts
        persona_instructions = {
            "upbeat": "Persona: Tech Pulse Anchor (Upbeat & Energetic). Use fast-paced, enthusiastic phrasing.",
            "calm": "Persona: Deep Dive Analyst (Calm & Reflective). Use slow, clear, analytical phrasing.",
            "vintage": "Persona: 1940s Vintage Radio Newsreel. Start with dramatic radio phrasing.",
            "standard": "Persona: Standard Radio News Anchor. Deliver a crisp, balanced news briefing."
        }
        selected_persona_prompt = persona_instructions.get(persona, persona_instructions["standard"])

        # Stage 2: Groq LLM Condensation & Pointwise Breakdown Extraction
        prompt = (
            f"{selected_persona_prompt}\n"
            f"Language Constraint: Write in {target_lang}.\n"
            f"Length Constraint: Keep spoken script {target_length}.\n"
            "STRICT OUTPUT FORMAT:\n"
            "Provide the response in two sections separated by '---POINTWISE---':\n"
            "Section 1: Spoken broadcast script (plain spoken English/target language, no markdown syntax).\n"
            f"Section 2: Exactly {num_points} key pointwise breakdown sections extracted from scraped page as JSON array:\n"
            "[{\"title\": \"Point Title\", \"details\": \"Detailed scraped insight line\"}]\n\n"
            f"Scraped Web Content (Depth: {max_chars} chars):\n{truncated_md}"
        )

        groq_models = ["openai/gpt-oss-20b", "qwen/qwen3.6-27b", "openai/gpt-oss-120b", "groq/compound-mini"]
        raw_llm_output = None
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
                    timeout=20
                )
                if groq_resp.status_code == 200:
                    raw_llm_output = groq_resp.json()["choices"][0]["message"]["content"].strip()
                    break
                else:
                    try:
                        groq_err_msg = groq_resp.json().get("error", {}).get("message") or groq_resp.text[:200]
                    except Exception:
                        groq_err_msg = groq_resp.text[:200]
            except requests.RequestException as e:
                groq_err_msg = str(e)

        if not raw_llm_output:
            return jsonify({"detail": f"Groq LLM error: {groq_err_msg}"}), 502

        # Parse script and pointwise data
        if "---POINTWISE---" in raw_llm_output:
            parts = raw_llm_output.split("---POINTWISE---")
            script = parts[0].strip()
            pointwise_raw = parts[1].strip()
        elif "---CHAPTERS---" in raw_llm_output:
            parts = raw_llm_output.split("---CHAPTERS---")
            script = parts[0].strip()
            pointwise_raw = parts[1].strip()
        else:
            script = raw_llm_output
            pointwise_raw = ""

        # Parse pointwise items & compute chapter timestamps
        dur = 30 if length == "30" else (90 if length == "90" else 60)
        pointwise_list = []
        chapters = []

        try:
            parsed_points = json.loads(pointwise_raw)
            step_sec = dur / max(len(parsed_points), 1)

            for idx, item in enumerate(parsed_points):
                t_sec = int(idx * step_sec)
                m = t_sec // 60
                s = t_sec % 60
                ts_str = f"{m}:{s:02d}"

                p_title = item.get("title") or f"Point {idx+1}"
                p_details = item.get("details") or item.get("title") or ""

                pointwise_list.append({
                    "step": idx + 1,
                    "time": t_sec,
                    "timestamp": ts_str,
                    "title": p_title,
                    "details": p_details
                })

                chapters.append({
                    "time": t_sec,
                    "label": f"{ts_str} - {p_title}"
                })
        except Exception:
            # Fallback pointwise items
            pointwise_list = [
                {"step": 1, "time": 0, "timestamp": "0:00", "title": "Overview & Context", "details": "Primary introduction extracted from the top section of the page."},
                {"step": 2, "time": int(dur * 0.4), "timestamp": f"{int(dur*0.4)//60}:{int(dur*0.4)%60:02d}", "title": "Core Technical Findings", "details": "Key analytical data points and technical findings."},
                {"step": 3, "time": int(dur * 0.8), "timestamp": f"{int(dur*0.8)//60}:{int(dur*0.8)%60:02d}", "title": "Conclusions & Implications", "details": "Final summary takeaways and strategic conclusions."}
            ]
            chapters = [
                {"time": 0, "label": "0:00 - Overview & Context"},
                {"time": int(dur * 0.4), "label": f"{int(dur*0.4)//60}:{int(dur*0.4)%60:02d} - Core Technical Findings"},
                {"time": int(dur * 0.8), "label": f"{int(dur*0.8)//60}:{int(dur*0.8)%60:02d} - Conclusions & Implications"}
            ]


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

        # Convert audio bytes directly to Base64 Data URL (100% stateless)
        audio_b64 = base64.b64encode(fish_resp.content).decode("utf-8")
        audio_data_url = f"data:audio/mp3;base64,{audio_b64}"

        res_payload = {
            "title": page_title,
            "url": url,
            "persona": persona,
            "language": language,
            "length": length,
            "script": script,
            "chapters": chapters,
            "pointwise_data": pointwise_list,
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


