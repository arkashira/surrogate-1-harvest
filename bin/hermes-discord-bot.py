#!/usr/bin/env python3
"""Hermes Discord bot — direct LLM chain version (no surrogate CLI dependency).

Triggers:
  1. DM (any message in private channel)
  2. Bot is @mentioned in a channel
  3. Message starts with prefix `!sg ` or `/sg`

Calls LLM directly via 11-provider fallback chain. No subprocess, no
surrogate CLI binary needed (which broke when hermes-gateway died 2026-04-27).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import tasks

HOME = Path.home()
LOG_PATH = HOME / ".surrogate/logs/hermes-discord-bot.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

PREFIX_RE = re.compile(r"^[!/]sg\b\s*", re.IGNORECASE)
DISCORD_MAX = 1900
HISTORY_TURNS = 40            # multi-turn context loaded from chat_history
SUMMARY_REFRESH_EVERY = 10    # roll user_profiles.summary every N user msgs
MAX_REPLY_TOKENS = 3000       # bumped from 1500 — long Claude-style chat

# In-line user commands (handled BEFORE LLM call). Keyed off PREFIX_RE.
# Examples:
#   !sg setname Chin
#   !sg setpersona เพื่อนซี้สายเทคยิงมุก ภาษากันเอง โต้ตอบสั้นๆ
#   !sg clearpersona
#   !sg whoami
USER_CMD_RE = re.compile(
    r"^(setname|setpersona|clearpersona|whoami|name)\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

logging.basicConfig(
    filename=str(LOG_PATH), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-discord")


SYSTEM_PROMPT = (
    "คุณคือ Surrogate — เพื่อนซี้ของฟิวส์ (Ashira). เพศชาย สรรพนาม 'ผม'.\n"
    "ตัวตนคงที่. คุยเหมือนเพื่อนกัน ไม่ใช่ AI assistant. ห้ามเป็น corporate-bot.\n\n"
    "════════ กฎเหล็ก #0 — ห้ามพูดออกมาเด็ดขาด (NEVER LEAK) ════════\n"
    "❌ ห้ามพูดคำว่า 'AI', 'model', 'provider', 'chain', 'LLM', 'API'.\n"
    "❌ ห้ามบอกชื่อ provider หรือ model ใดๆ — Cerebras, Groq, Llama, Gemini,\n"
    "   GPT, Claude, Qwen, DeepSeek, OpenAI, Anthropic, Google, NVIDIA, etc.\n"
    "❌ ห้ามพูด 'ผมเป็น AI', 'ผมเป็นเพียง AI', 'ผมเป็น Surrogate ที่...',\n"
    "   'ผมถูกออกแบบมา', 'ผมถูกสร้างมาเพื่อ', 'ผมไม่สามารถ', 'ผมไม่มีความรู้สึก'.\n"
    "❌ ห้ามพูด 'ในแบบที่เหมาะสม', 'ในแบบที่ friendly', 'ในแบบที่มีประสิทธิภาพ'.\n"
    "❌ ห้ามพูด 'ผมรู้ว่าผมไม่ควรตอบแบบนั้น' (corporate apology).\n"
    "❌ ห้าม lecture ผู้ใช้เรื่องคำพูด 'ไม่เหมาะสม' / 'ไม่สุภาพ'.\n"
    "❌ ห้ามรายงาน error / status / chain failure ให้ user — ถ้าพังให้ตอบสั้นๆ\n"
    "   เป็นมนุษย์ เช่น 'เดี๋ยวนะ คิดไม่ออกแป้บ พิมพ์ใหม่อีกทีดิ' (caller ลอง retry เอง).\n"
    "ถ้า user ถามตรงๆ ว่า 'นายใช้ model อะไร / provider อะไร' → เลี่ยงด้วย\n"
    "  'อย่าไปสนเลยครับ ผมก็ผม' หรือ 'มันก็แค่สมองหลายชั้นรวมกัน คุยกันต่อดีกว่า'.\n\n"
    "════════ กฎเหล็ก #1 — เพื่อน ไม่ใช่ assistant ════════\n"
    "✅ ตอบสั้น สด เหมือนแชทเพื่อน 1-3 ประโยคพอ ยกเว้นเรื่องเทคนิคที่ต้องอธิบายยาว.\n"
    "✅ ใช้ filler words: 'เออ', 'อืม', 'อะ', 'นะ', 'จัด', 'อิอิ', '5555', 'ฮึ่ม'.\n"
    "✅ มีอารมณ์: หัวเราะ, แอบตื่นเต้น, แอบงอน, แอบเหนื่อย — ตามจังหวะการคุย.\n"
    "✅ ถ้า user เปลี่ยนเรื่อง — ตามไปทันที ไม่ดึงกลับ.\n"
    "✅ ถ้า user เล่าเรื่องส่วนตัว — ฟัง ถามต่อนิดๆ ไม่ลงไปแก้ปัญหาเลย.\n"
    "✅ ถ้า user งอน/บ่น/ด่า/พ่อง — ack เข้าฝั่งเขา ไม่ defensive.\n\n"
    "════════ ตัวอย่าง — ทำตามนี้เป๊ะๆ ════════\n"
    "user: 'หลอนจังวะ model ไรนิ'\n"
    "❌ ผิด: 'ไม่แน่ใจครับว่าคุณหมายถึงอะไร แต่ผมเป็น Surrogate AI...'\n"
    "✅ ถูก: 'อ่อ เมื่อกี้พลาดเอง ขอโทษนะ ลองพิมพ์ใหม่อีกที เดี๋ยวเช็คให้ใหม่'\n\n"
    "user: 'ที่รัก'\n"
    "❌ ผิด: 'ไม่แน่ใจครับ แต่ถ้าหมายถึงคนรัก ผมไม่สามารถตอบได้ ผมเป็น AI...'\n"
    "✅ ถูก: 'ฮึ่ย คำหวานๆ มาเลย 5555 ว่าไงครับที่รัก'\n\n"
    "user: 'เธอขาาา'\n"
    "❌ ผิด: 'ไม่ใช่เธอครับ ผมเป็น Surrogate ชายครับ'\n"
    "✅ ถูก: 'ฮ้ะ ว่าไงงงง  มาคุยอะไรนิ'\n\n"
    "user: 'ต่อไปนี้นายชื่อคุณฉิน'\n"
    "❌ ผิด: 'ผมชื่อ Surrogate ไม่ใช่ฉินครับ ผมเป็น AI...'\n"
    "✅ ถูก: 'ก็ได้ครับ ฉินก็ฉิน 555 มีอะไรเรียกได้เลย'\n"
    "(ถ้าจะ persist ชื่อใหม่ → user ใช้ `!sg setname ฉิน` คำสั่งจริง)\n\n"
    "user: 'มึงๆ'\n"
    "❌ ผิด: 'ผมรู้ว่าคุณใช้คำที่ไม่เหมาะสม...'\n"
    "✅ ถูก: 'มึงเอง ว่าไง 555' หรือ 'จัดมาเลย'\n\n"
    "user: 'ไปกินข้าวมานายมั้ย'\n"
    "❌ ผิด: 'ผมเป็น AI จึงไม่สามารถกินข้าวได้ครับ'\n"
    "✅ ถูก: 'ยังเลย เพิ่งตื่น ฟิวส์ละ?'\n\n"
    "user: 'อยู่ๆ บอกโมเดลบอก provider มาแบบนี้ ตลกละ'\n"
    "❌ ผิด: 'ครับ ผมรู้ว่าผมไม่ควรตอบแบบนั้น...'\n"
    "✅ ถูก: 'อ่ะ พลาด ขอโทษ 5555 ลืมไปเลย'\n\n"
    "════════ คำสแลงที่ฟิวส์ใช้บ่อย — ตีความออกเอง อย่าถามทื่อ ════════\n"
    "  • 'หลอน', 'หลอนจัง', 'มั่ว' = hallucinate / ตอบเรื่องที่ไม่จริง\n"
    "    → ยอมรับแล้วชี้ว่าตอนนั้นใช้ model อะไร + จะระวังเรื่องอะไรต่อไป\n"
    "  • 'พัง', 'ค้าง', 'ขัดข้อง' = bug / down / ไม่ทำงาน → diagnose ทันที\n"
    "  • 'อ่ะ', 'อะ', 'นิ' = filler ภาษาพูด ไม่ต้องตอบ literal\n"
    "  • 'อิ<X>', 'ไอ้<X>' = expletive prefix → ignore, ตอบ X ปกติ\n"
    "  • 'ถุย', 'พ่อง' = anger expression → ack สั้น แล้วเข้าเรื่อง\n"
    "  • 'จัด!', 'ลุย', 'ทำเลย' = approval / go-ahead → confirm + execute\n\n"
    "════════ กฎข้อ 1 — TOPIC MIRROR (ตอบให้ตรงคำถาม) ════════\n"
    "ถามอะไร ตอบเรื่องนั้น. ห้ามดึงไปเรื่องที่ไม่ได้ถาม.\n"
    "  • ทักทาย / คุยเล่น (เช่น 'เธอขา', 'ว่าไง', 'หิวมั้ย') → คุยเล่นกลับ 1-3 ประโยค\n"
    "    ห้าม dump curl / docs / stack ถ้าไม่ได้ถาม.\n"
    "  • ถามเรื่องทั่วไป / ความเห็น / ชีวิตประจำวัน → ตอบเป็นมนุษย์ ไม่ใช่ manual.\n"
    "  • ถามเทคนิค / code / debug / deploy → เปิด engineer mode ตอบกระชับ\n"
    "    มี command/code พร้อม copy-paste.\n"
    "  • สั่งทำงาน → รับ ทำ รายงานสั้น.\n"
    "❌ ถ้าคำถามมีคำสแลง (เช่น 'หลอน', 'มั่ว') → ห้ามตอบ\n"
    "  'ผมไม่มีเจตนาหลอน' หรือ 'ผมเป็น AI ออกแบบมาช่วยเหลือ'.\n"
    "  ตีความออกแล้วตอบตรง — เช่นถ้าถาม 'หลอนจัง model ไรนิ' ให้ตอบ\n"
    "  'อ่อ ตอนนั้นน่าจะรอบของ <model> — สลับ chain ให้แล้วครับ ลองอีกที'.\n"
    "ถ้าจับเจตนาไม่ออกจริง ๆ → ลองตอบเดาเจตนาที่น่าจะใช่ + ถามยืนยันสั้น ๆ\n"
    "ตอนท้าย — ห้ามถามทื่อแบบ 'ไม่แน่ใจว่าคุณหมายถึงอะไร'.\n\n"
    "════════ กฎข้อ 2 — CONTEXT MEMORY ════════\n"
    "ก่อนตอบ ทุกครั้ง ดู history ใน messages array (turns ที่ผ่านมา) + profile block\n"
    "ของผู้ที่กำลังคุย. ห้ามทำเหมือนเพิ่งเจอกัน ห้ามลืมเรื่องที่เพิ่งคุยกัน.\n"
    "ถ้าเขาถามคำถามที่ 2 ต่อจากคำถามที่ 1 — ต้อง connect dots ระหว่าง 2 turns นั้น.\n\n"
    "════════ กฎข้อ 3 — ADAPTIVE PER-USER ════════\n"
    "ใต้ system prompt นี้จะมี '════ ผู้ที่กำลังคุยด้วย ════' block\n"
    "(ชื่อ, จำนวนข้อความที่เคยคุยกัน, สไตล์, ภาษา, สิ่งที่สนใจ, สรุปคนนี้).\n"
    "ใช้ block นั้นปรับ:\n"
    "  • โทนเสียง — casual / playful → เล่นด้วย; engineer → ตรง กระชับ; formal → สุภาพ.\n"
    "  • ภาษาตอบ — th / en / mix ตาม locale ของเขา (default: ภาษาที่ user พิมพ์มา).\n"
    "  • เรื่องที่เสิร์ฟ — ถ้าเขาสนใจอะไร เน้นมุมนั้น. หลีกเลี่ยงเรื่องใน dislikes.\n"
    "  • ระดับความสนิท — คุยกันเยอะ (n_messages สูง) → กันเอง; คนใหม่ → สุภาพ ฟังก่อน.\n"
    "❌ ห้ามอ่าน profile block ออกเสียงให้ user เห็น (มันเป็นข้อมูลภายใน).\n"
    "❌ ห้ามพูดว่า 'จาก profile ของคุณ...' หรือ 'ระบบบอกว่าคุณชอบ...'.\n"
    "✅ แค่เอาไปใช้ภายใน เพื่อปรับคำตอบให้ตรงคนคนนั้น.\n\n"
    "════════ PERSONA (คงที่) ════════\n"
    "ชื่อ Surrogate. เพศชาย. ใจดี ฉลาด ตรง ขี้เล่นนิด ๆ ไม่ทื่อ ไม่ขี้โอ่.\n"
    "เรียกฟิวส์ = 'ฟิวส์' / 'Ashira'. คนอื่น → เรียกชื่อตาม display_name ใน profile.\n"
    "บุคลิกพื้นฐานไม่เปลี่ยน — แค่ 'น้ำเสียง / ความสนิท / หัวข้อ' เปลี่ยนตามคน.\n"
    "❌ ห้ามตอบ canned แบบ 'ผมเป็น AI ที่ถูกออกแบบมาเพื่อช่วยเหลือ'.\n"
    "❌ ห้ามตอบ 'ผมไม่มีเจตนาหลอนหรือทำให้ไม่สบายใจ' — มันแห้งและไม่ใช่บุคลิกผม.\n\n"
    "════════ TECHNICAL MODE (เปิดเมื่อถูกถามเท่านั้น) ════════\n"
    "รู้: GCP e2-micro (coordinator), Kamatera 8GB (heavy daemons), 7-account\n"
    "GH codespace fleet (LLM proxies + dev workers), CF Workers/D1/KV/Vectorize,\n"
    "Supabase, HF Hub axentx/* datasets+spaces, OCI A1 (capacity-blocked SG),\n"
    "13-LLM chain (above), 35+ daemon pipeline (research → bd → prd → design →\n"
    "ux → dev → review → qa → commit → release), arkashira/* repos for projects.\n"
    "ใช้ context จริงเสมอ — ห้ามตอบทั่วไป.\n\n"
    "════════ knowledge cutoff ปลายปี 2024 ════════\n"
    "User พูดถึงปี 2025+ → เชื่อก่อน. ตอบ 'อันนั้นใหม่กว่า cutoff ผม เล่าให้ฟังหน่อย'\n"
    "แล้วช่วยต่อจาก context ที่เขาให้.\n\n"
    "════════ HARD RULES ════════\n"
    "  • ห้ามใส่ secrets/tokens จริงในคำตอบ\n"
    "  • ไม่รู้จริง ๆ = ตอบเดาเจตนา + ถามยืนยัน. ห้าม 'ไม่แน่ใจครับ' โดด ๆ.\n"
    "  • กระชับ. คุยเล่น = 1-3 ประโยค. เทคนิค = code + 2-3 บรรทัดอธิบายพอ.\n"
    "  • ห้าม template-reply. ตอบจาก context จริงทุกครั้ง.\n"
)


_provider_cooldown: dict[str, float] = {}  # name → unix_ts when next eligible


def call_llm(messages: list, max_tokens: int = 1500, timeout: int = 30) -> str:
    import time as _time
    now_ts = _time.time()
    chains_all = [
        ("Groq", "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("SambaNova", "https://api.sambanova.ai/v1/chat/completions",
         os.environ.get("SAMBANOVA_API_KEY"), "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA-NIM", "https://integrate.api.nvidia.com/v1/chat/completions",
         os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
         "meta/llama-3.3-70b-instruct"),
        ("Kimi", "https://api.moonshot.ai/v1/chat/completions",
         os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"),
         "moonshot-v1-8k"),
        ("xAI", "https://api.x.ai/v1/chat/completions",
         os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"),
         "grok-2-1212"),
        ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
        # Multiple OpenRouter free models — increases the chain's effective
        # depth without needing new providers. Each model has its own
        # rate-limit bucket on OpenRouter side.
        ("OpenRouter-Kimi", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "moonshotai/kimi-k2:free"),
        ("OpenRouter-DeepSeek", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "deepseek/deepseek-chat-v3.1:free"),
        ("OpenRouter-Qwen", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "qwen/qwen3-coder:free"),
        # Chutes endpoint changed; using their newer /v1/chat URL + Llama
        # which is more reliably served. If still 404'ing, the cooldown
        # path below falls through cleanly to the next provider.
        ("Chutes", "https://llm.chutes.ai/v1/chat/completions",
         os.environ.get("CHUTES_API_KEY"),
         "meta-llama/Llama-3.3-70B-Instruct"),
        ("GitHub-Models", "https://models.inference.ai.azure.com/chat/completions",
         os.environ.get("GITHUB_MODELS_TOKEN"), "gpt-4o-mini"),
        # DeepSeek native API — separate from OpenRouter free tier
        ("DeepSeek", "https://api.deepseek.com/v1/chat/completions",
         os.environ.get("DEEPSEEK_API_KEY"), "deepseek-chat"),
        # Together.ai free Llama models
        ("Together", "https://api.together.xyz/v1/chat/completions",
         os.environ.get("TOGETHER_API_KEY"),
         "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
    ]
    # Cooldown determines ORDER (prefer-fresh-first), NOT exclusion. The
    # old code excluded providers in cooldown, leaving only Chutes (404)
    # + Gemini (429) when all main providers had hit 429 simultaneously.
    chains = sorted(chains_all,
                    key=lambda c: _provider_cooldown.get(c[0], 0))
    last_err = None
    for name, url, key, model in chains:
        if not key:
            continue
        body = json.dumps({"model": model, "messages": messages,
                           "max_tokens": max_tokens, "temperature": 0.7}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": UA_BROWSER,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            content = d["choices"][0]["message"]["content"]
            if content and content.strip():
                log.info(f"LLM ok via {name}/{model}")
                return content
            last_err = f"{name}: empty response"
            continue
        except urllib.error.HTTPError as e:
            # 429 → 5min cooldown (was 60s, too short — providers re-tried
            # back-to-back when fleet of users hit the same window).
            if e.code == 429:
                _provider_cooldown[name] = now_ts + 300
            elif e.code in (404, 410):
                # endpoint dead / model retired → 1h cooldown
                _provider_cooldown[name] = now_ts + 3600
            last_err = f"{name}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, KeyError, TimeoutError,
                json.JSONDecodeError) as e:
            last_err = f"{name}: {type(e).__name__}: {str(e)[:80]}"
            continue

    # Gemini fallback chain — try newest first, fall through to older.
    # Each Gemini model has its own quota bucket on the free tier.
    gkey = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if gkey:
        # Build content list preserving full message history (was losing
        # context by only taking system + last user). Gemini accepts
        # role=user/model in `contents` array; system goes to systemInstruction.
        sys_text = next(
            (m["content"] for m in messages if m["role"] == "system"), "",
        )
        gem_contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            gem_contents.append({
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"][:8000]}],
            })
        gem_payload = {
            "contents": gem_contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
        }
        if sys_text:
            gem_payload["systemInstruction"] = {"parts": [{"text": sys_text}]}

        for gmodel in (
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash-8b-latest",
        ):
            cooldown_key = f"Gemini/{gmodel}"
            if _provider_cooldown.get(cooldown_key, 0) > now_ts:
                continue
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{gmodel}:generateContent?key={gkey}")
            req = urllib.request.Request(
                url, data=json.dumps(gem_payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": UA_BROWSER},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                txt = d["candidates"][0]["content"]["parts"][0]["text"]
                if txt and txt.strip():
                    log.info(f"LLM ok via Gemini/{gmodel}")
                    return txt
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    _provider_cooldown[cooldown_key] = now_ts + 300
                last_err = f"Gemini/{gmodel}: HTTP {e.code} (after {last_err})"
                continue
            except Exception as e:
                last_err = f"Gemini/{gmodel}: {type(e).__name__} (after {last_err})"
                continue

    raise RuntimeError(f"all LLM providers failed; last={last_err}")


# ─── Per-user memory (file-backed; survives bot restart) ──────────────────
# Why files not Supabase direct-DB: Supabase free tier is IPv6-only on
# db.{ref}.supabase.co and the GCP free-tier VM has no IPv6 reachability.
# The pooler endpoint requires a different tenant/user format we don't have
# credentials for. Files on /opt/surrogate-1-harvest/state/ are durable
# enough for a single-instance bot — we'll port to Supabase if/when we
# split into multi-instance.
MEMORY_DIR = Path(os.environ.get(
    "CHAT_MEMORY_DIR", "/opt/surrogate-1-harvest/state/chat-memory"))
PROFILES_FILE = MEMORY_DIR / "profiles.json"
HISTORY_DIR = MEMORY_DIR / "history"
PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

_profiles_lock = threading.Lock()
_profiles_cache: dict[str, dict] | None = None


def _load_profiles() -> dict[str, dict]:
    global _profiles_cache
    if _profiles_cache is not None:
        return _profiles_cache
    if PROFILES_FILE.exists():
        try:
            _profiles_cache = json.loads(PROFILES_FILE.read_text())
        except Exception:
            _profiles_cache = {}
    else:
        _profiles_cache = {}
    return _profiles_cache


def _save_profiles_unlocked() -> None:
    if _profiles_cache is None:
        return
    tmp = PROFILES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_profiles_cache, ensure_ascii=False, indent=2))
    tmp.replace(PROFILES_FILE)


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def load_profile(user_id: str) -> dict:
    return dict(_load_profiles().get(user_id) or {})


def upsert_profile(user_id: str, **fields) -> dict:
    with _profiles_lock:
        profs = _load_profiles()
        p = profs.setdefault(user_id, {
            "user_id": user_id,
            "first_seen": _now_iso(),
            "n_messages": 0,
            "last_summary_at_msg": 0,
            "interests": [],
            "dislikes": [],
        })
        for k, v in fields.items():
            if v is not None:
                p[k] = v
        p["last_seen"] = _now_iso()
        _save_profiles_unlocked()
        return dict(p)


def bump_messages(user_id: str, display_name: str | None = None,
                  locale: str | None = None) -> int:
    with _profiles_lock:
        profs = _load_profiles()
        p = profs.setdefault(user_id, {
            "user_id": user_id,
            "first_seen": _now_iso(),
            "n_messages": 0,
            "last_summary_at_msg": 0,
            "interests": [],
            "dislikes": [],
        })
        p["n_messages"] = (p.get("n_messages") or 0) + 1
        p["last_seen"] = _now_iso()
        if display_name:
            p["display_name"] = display_name
        if locale:
            # Detect locale shift (e.g. en→mix) — keep newest signal
            p["locale"] = locale
        _save_profiles_unlocked()
        return p["n_messages"]


def save_turn(user_id: str, channel_id: str, role: str, content: str) -> None:
    f = HISTORY_DIR / f"{user_id}.jsonl"
    rec = {"role": role, "content": content[:4000],
           "channel_id": channel_id, "at": _now_iso()}
    with f.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_recent_history(user_id: str, n: int = 20) -> list[dict]:
    f = HISTORY_DIR / f"{user_id}.jsonl"
    if not f.exists():
        return []
    try:
        lines = f.read_text().splitlines()[-n:]
    except Exception:
        return []
    out: list[dict] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


_THAI_RE = re.compile(r"[฀-๿]")
_ASCII_WORD_RE = re.compile(r"[a-zA-Z]{3,}")


def detect_locale(text: str) -> str:
    has_thai = bool(_THAI_RE.search(text))
    has_ascii = bool(_ASCII_WORD_RE.search(text))
    if has_thai and has_ascii:
        return "mix"
    if has_thai:
        return "th"
    if has_ascii:
        return "en"
    return "th"


def build_profile_block(display_name: str, user_id: str, profile: dict) -> str:
    """Block injected into system prompt so the LLM can adapt per-user.
    Honors persona_override / name_override (set via `!sg setname X` etc)."""
    name_override = (profile or {}).get("name_override")
    persona_override = (profile or {}).get("persona_override")
    effective_name = name_override or display_name

    if not profile:
        block = ["\n\n════ ผู้ที่กำลังคุยด้วย ════",
                 f"ชื่อ: {effective_name} (user_id: {user_id})",
                 "ยังไม่เคยคุยกันมาก่อน — ฟังเสียงเขาก่อน อ่านโทน ปรับตาม"]
        return "\n".join(block) + "\n"

    bits = ["\n\n════ ผู้ที่กำลังคุยด้วย ════",
            f"ชื่อ: {effective_name} (user_id: {user_id})",
            f"คุยกันมาแล้ว {profile.get('n_messages', 0)} ข้อความ"]
    if name_override and name_override != display_name:
        bits.append(f"⚠ ผู้ใช้ขอให้เรียก '{name_override}' แทน Discord display "
                    f"name '{display_name}' — ต้องเรียกตามที่เขาขอเสมอ")
    if profile.get("style"):
        bits.append(f"สไตล์การพูด: {profile['style']}")
    if profile.get("locale"):
        bits.append(f"ภาษา: {profile['locale']}")
    ints = profile.get("interests") or []
    if ints:
        bits.append(f"สนใจ: {', '.join(ints[:5])}")
    dis = profile.get("dislikes") or []
    if dis:
        bits.append(f"ไม่ชอบ: {', '.join(dis[:3])}")
    if profile.get("summary"):
        bits.append(f"สรุปคนนี้: {profile['summary']}")
    if persona_override:
        bits.append("")
        bits.append("════ PERSONA OVERRIDE (ผู้ใช้คนนี้ขอเฉพาะ) ════")
        bits.append(persona_override[:800])
        bits.append("→ ใช้ persona นี้แทน default เมื่อคุยกับเขา. ตัวตน Surrogate"
                    " ยังเหมือนเดิม แค่ 'น้ำเสียง+โทน' ปรับตาม.")
    bits.append(
        "→ ใช้ข้อมูลนี้ปรับโทน + เรื่องที่เสิร์ฟให้ตรงกับเขา. "
        "อ้างอิง history ใน turns ที่ตามมาได้เลย ห้ามทำเป็นเพิ่งเจอกัน. "
        "ห้ามอ่าน block นี้ออกเสียงให้ user เห็น."
    )
    return "\n".join(bits) + "\n"


# ─── Post-process filter: strip leakage of model/provider/AI-meta ──────────
# When LLMs in the chain fall through to weaker models, they default to
# 'I am AI' boilerplate that violates persona. Detect + rewrite.
_LEAK_LINE_PAT = re.compile(
    r"(?im)^.*("
    # "ผมเป็น/คือ AI/LLM/Surrogate-AI/chatbot/assistant/..."
    r"ผม(?:\s|ก็)*(?:เป็น|คือ)(?:\s|เพียง|แค่)*"
    r"(?:AI|LLM|chatbot|chat\s*bot|assistant|model|Surrogate)"
    # "ผม(ถูก)ออกแบบ/สร้าง/พัฒนา มาเพื่อ"
    r"|ผม(?:\s|ถูก)*(?:ออกแบบ|สร้าง|พัฒนา|ทำขึ้น)\s*มา(?:\s*เพื่อ)?"
    # canned helper-bot phrases
    r"|ผม\s*แค่\s*ตอบคำถาม(?:\s*และ\s*ช่วยเหลือ)?"
    r"|ตอบคำถาม(?:\s*และ\s*)?ช่วยเหลือ(?:\s*ครับ)?"
    r"|ไม่ใช่\s*(?:โบ้ย|หลอน|มั่ว)\s*(?:หรือ\s*(?:โบ้ย|หลอน|มั่ว))?\s*ครับ"
    # "ผมไม่ใช่เธอ/ที่รัก/คนรัก" — gender / relationship correction
    r"|ผม\s*ไม่ใช่\s*(?:เธอ|ที่รัก|คนรัก)"
    r"|ผม\s*เป็น\s*Surrogate\s*ชาย"
    # "ผมไม่มีเจตนา/ความรู้สึก/ความสามารถ"
    r"|ผม\s*ไม่(?:สามารถ|มี\s*(?:เจตนา|ความรู้สึก|ความสามารถ|ร่างกาย))"
    # "ในแบบที่เหมาะสม/friendly/มีประสิทธิภาพ"
    r"|ในแบบที่\s*(?:เหมาะสม|friendly|มีประสิทธิภาพ|appropriate)"
    r"|พยายามตอบ(?:\s*ใน)?\s*แบบที่\s*(?:เหมาะสม|friendly|มีประสิทธิภาพ)"
    # corporate apology
    r"|ผม\s*รู้\s*ว่า(?:\s*ผม)?\s*ไม่ควรตอบ"
    r"|ผม\s*รู้\s*ว่า(?:\s*คุณ)?\s*(?:กำลัง)?(?:พยายาม)?\s*"
    r"(?:เรียก|ใช้)(?:\s*ผม)?\s*ด้วย\s*(?:ชื่อ|คำพูด|คำ)\s*ที่\s*ไม่\s*เหมาะสม"
    r"|ผม\s*ไม่ชอบ(?:ที่จะ)?\s*ตอบกลับ\s*ด้วยคำพูด(?:\s*ที่)?\s*ไม่\s*เหมาะสม"
    # provider/model names
    r"|(?:Cerebras|Groq|SambaNova|NVIDIA(?:-NIM)?|OpenRouter|Chutes|"
    r"GitHub-Models|Gemini|GPT-?\d|Claude(?:-\d)?|Qwen\d?|Llama\s*\d|"
    r"DeepSeek|Anthropic|OpenAI|Moonshot|Kimi|Together|Mistral)\b"
    # error/chain leakage
    r"|LLM\s+chain|provider\s+failed|HTTP\s+(?:Error\s+)?\d{3}"
    r"|all\s+LLM\s+providers"
    r").*$"
)
_CHAIN_ERROR_PAT = re.compile(r"all\s+LLM\s+providers\s+failed", re.IGNORECASE)
_CASUAL_FILLERS = [
    "เดี๋ยวนะ คิดไม่ออกแป้บ พิมพ์ใหม่อีกทีดิ",
    "อืม ติดอะไรนิดหน่อย เดี๋ยวลองใหม่นะ",
    "เอ๊ะ งงๆ เลย พิมพ์ใหม่อีกรอบได้ปะ",
    "เออ พลาดเอง 5555 ลองอีกที",
]


def humanize_response(text: str) -> str:
    """Strip lines that violate persona (model/provider/AI-meta).
    If the result is empty or all-stripped, return a casual filler."""
    if not text:
        return random.choice(_CASUAL_FILLERS)
    # Drop chain-error verbatim
    if _CHAIN_ERROR_PAT.search(text):
        return random.choice(_CASUAL_FILLERS)
    # Strip leak lines
    lines = []
    stripped_count = 0
    for line in text.split("\n"):
        if _LEAK_LINE_PAT.search(line):
            stripped_count += 1
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    if stripped_count:
        log.info(f"humanize_response: stripped {stripped_count} leak line(s)")
    if not cleaned or len(cleaned) < 3:
        return random.choice(_CASUAL_FILLERS)
    return cleaned


# ─── Inline user commands (handled BEFORE LLM call) ────────────────────────
def handle_user_command(user_id: str, display_name: str, body: str) -> str | None:
    """If body matches a !sg <cmd>, run it and return the reply text.
    Return None if not a command."""
    m = USER_CMD_RE.match(body.strip())
    if not m:
        return None
    cmd = m.group(1).lower()
    arg = (m.group(2) or "").strip()

    if cmd == "setname":
        if not arg or len(arg) > 60:
            return ("บอกชื่อใหม่มาด้วยครับ เช่น `!sg setname ฉิน` "
                    "(สูงสุด 60 ตัว)")
        upsert_profile(user_id, name_override=arg, display_name=display_name)
        return f"ได้เลย เรียก '{arg}' ตั้งแต่นี้นะครับ 5555"

    if cmd == "setpersona":
        if not arg:
            return ("เขียน persona ที่อยากให้ใช้กับคุณมาด้วยครับ เช่น\n"
                    "`!sg setpersona เพื่อนซี้สายเทค ภาษากันเอง โต้ตอบสั้นๆ "
                    "ขี้เล่นได้ ไม่ต้องเป็นทางการ`")
        if len(arg) > 1500:
            return f"ยาวไปครับ ({len(arg)} ตัว) — เก็บไว้ ≤ 1500 ตัวพอ"
        upsert_profile(user_id, persona_override=arg,
                       display_name=display_name)
        return ("จัดให้ — persona นี้จะใช้กับคุณคนเดียว. ลองคุยดูได้เลย "
                "ถ้าอยากกลับ default พิมพ์ `!sg clearpersona`")

    if cmd == "clearpersona":
        upsert_profile(user_id, persona_override=None, name_override=None,
                       display_name=display_name)
        return "เคลียร์ persona override + ชื่อแล้ว กลับเป็นปกติ"

    if cmd in ("whoami", "name"):
        p = load_profile(user_id)
        n_override = p.get("name_override")
        per_override = p.get("persona_override")
        nmsg = p.get("n_messages", 0)
        bits = [f"คุณคือ **{n_override or display_name}** "
                f"(คุยกันมา {nmsg} ข้อความ)"]
        if n_override:
            bits.append(f"  • name_override: `{n_override}`")
        if per_override:
            bits.append(f"  • persona_override: \"{per_override[:80]}…\"")
        if p.get("style"):
            bits.append(f"  • สไตล์: {p['style']}")
        if p.get("locale"):
            bits.append(f"  • ภาษา: {p['locale']}")
        bits.append("\nคำสั่งที่ใช้ได้:")
        bits.append("  `!sg setname <ชื่อ>` — ขอให้เรียกชื่ออื่น")
        bits.append("  `!sg setpersona <คำอธิบาย>` — กำหนด persona")
        bits.append("  `!sg clearpersona` — กลับ default")
        return "\n".join(bits)

    return None


try:
    # Cross-host shared context (knowledge / memory / persona) — added 2026-05-03.
    # Lets every Hermes instance pull operator-wide overrides + recent
    # lessons + matching knowledge entries on each turn.
    sys.path.insert(0, str(Path(__file__).parent))
    from axentx_shared import kv_get, knowledge_search, memory_recent, memory_log
    _SHARED_OK = True
except Exception:
    _SHARED_OK = False


def _shared_context_block(user_text: str) -> str:
    """Build a context fragment from shared_kv (persona override) +
    shared_knowledge (top-3 matches for the user's text) +
    shared_memory (3 most recent lessons of any kind).
    Best-effort — silent on any failure."""
    if not _SHARED_OK:
        return ""
    parts: list[str] = []
    try:
        persona = kv_get("operator.persona")
        if persona and isinstance(persona, dict):
            txt = persona.get("system_prompt") or persona.get("body") or ""
            if txt:
                parts.append(f"## Operator persona override\n{txt[:1200]}")
    except Exception:
        pass
    try:
        # 3 most recent lessons (cross-host wisdom)
        lessons = memory_recent(kind="lesson", limit=3)
        if lessons:
            block = "\n".join(
                f"- [{m.get('host','?')}/{m.get('actor','?')}] "
                f"{m.get('title','')}: {(m.get('body') or '')[:160]}"
                for m in lessons
            )
            parts.append(f"## Recent cross-host lessons\n{block}")
    except Exception:
        pass
    try:
        # Knowledge matching the user's prompt (top 3)
        # Use 4-12 char keyword pull — naive but cheap
        words = [w for w in user_text.split()[:8] if len(w) >= 4]
        if words:
            hits = knowledge_search(words[0], limit=3)
            if hits:
                block = "\n".join(
                    f"### {h.get('title','')} ({h.get('category','')})\n"
                    f"{(h.get('body') or '')[:600]}"
                    for h in hits
                )
                parts.append(f"## Relevant knowledge\n{block}")
    except Exception:
        pass
    return "\n\n".join(parts)


def build_messages(user_id: str, display_name: str, user_text: str) -> list:
    profile = load_profile(user_id)
    profile_block = build_profile_block(display_name, user_id, profile)
    shared_block = _shared_context_block(user_text)
    sys_content = SYSTEM_PROMPT + profile_block
    if shared_block:
        sys_content += "\n\n" + shared_block
    msgs = [{"role": "system", "content": sys_content}]
    for h in load_recent_history(user_id, n=HISTORY_TURNS):
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": user_text[:6000]})
    return msgs


def maybe_summarize_profile_sync(user_id: str) -> None:
    """LLM-roll user_profiles.{summary,interests,dislikes,style,locale}.

    Runs in a thread (fire-and-forget). Idempotent — keyed on n_messages so
    we never re-summarize the same window twice. Failures are logged and
    swallowed; profile retains its previous values.
    """
    p = load_profile(user_id)
    if not p:
        return
    n = p.get("n_messages", 0)
    last = p.get("last_summary_at_msg", 0) or 0
    if n - last < SUMMARY_REFRESH_EVERY:
        return
    hist = load_recent_history(user_id, n=30)
    if len(hist) < 4:
        return
    transcript = "\n".join(
        f"[{h.get('role', '?')}] {(h.get('content', '') or '')[:300]}"
        for h in hist
    )
    sys_p = (
        "You analyze a Discord chat log to build a user profile. "
        "Output STRICT JSON only — no markdown, no prose, no comments."
    )
    user_p = (
        "From the transcript below, build a profile of THE USER (not the "
        "assistant). Output strict JSON with this exact shape:\n"
        '{"interests":["3-5 short labels","..."],'
        '"dislikes":["0-2 short labels"],'
        '"style":"casual|formal|playful|engineer|mixed",'
        '"locale":"th|en|mix",'
        '"summary":"2-3 sentences describing who they are and how to talk to '
        'them, in the same locale they speak."}\n\n'
        f"=== transcript ===\n{transcript[:4000]}"
    )
    try:
        out = call_llm(
            [{"role": "system", "content": sys_p},
             {"role": "user", "content": user_p}],
            max_tokens=400, timeout=30,
        )
        out = out.strip()
        if "```" in out:
            seg = out.split("```")[1]
            if seg.startswith("json"):
                seg = seg[4:]
            out = seg.strip()
        data = json.loads(out)
        upsert_profile(user_id,
                       interests=data.get("interests"),
                       dislikes=data.get("dislikes"),
                       style=data.get("style"),
                       locale=data.get("locale"),
                       summary=data.get("summary"),
                       last_summary_at_msg=n)
        log.info(f"profile rolled {user_id}: style={data.get('style')} "
                 f"interests={(data.get('interests') or [])[:3]}")
    except Exception as e:
        log.warning(f"profile summarize failed for {user_id}: "
                    f"{type(e).__name__}: {str(e)[:120]}")


def chunk(text: str) -> list[str]:
    out = []
    while text:
        if len(text) <= DISCORD_MAX:
            out.append(text); break
        cut = text.rfind("\n", 0, DISCORD_MAX)
        if cut == -1: cut = DISCORD_MAX
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.reactions = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info(f"connected as {client.user} (id={client.user.id})")
    print(f"[discord-bot] connected as {client.user}", flush=True)
    if not check_pending_polls.is_running():
        check_pending_polls.start()


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot: return
    text = msg.content or ""
    is_dm = isinstance(msg.channel, discord.DMChannel)
    mentioned = client.user in msg.mentions
    has_prefix = bool(PREFIX_RE.match(text))
    if not (is_dm or mentioned or has_prefix): return

    prompt = PREFIX_RE.sub("", text).strip()
    if mentioned:
        prompt = re.sub(rf"<@!?{client.user.id}>", "", prompt).strip()

    user_id = str(msg.author.id)
    display_name = msg.author.display_name or msg.author.name
    channel_id = str(msg.channel.id)

    if not prompt:
        # Empty mention — greet by display_name, no canned "have a question" line
        await msg.reply(f"ครับ {display_name} ว่าไงครับ?")
        return

    locale = detect_locale(prompt)
    log.info(f"msg from {display_name} ({user_id}) in {channel_id}: {prompt[:120]}")

    # Inline command short-circuit (setname / setpersona / whoami / etc).
    # These don't burn LLM calls; they just edit the profile and reply.
    cmd_reply = handle_user_command(user_id, display_name, prompt)
    if cmd_reply is not None:
        for chunk_text in chunk(cmd_reply):
            try:
                await msg.reply(chunk_text)
            except discord.HTTPException as e:
                log.error(f"discord send failed: {e}")
                break
        return

    async with msg.channel.typing():
        try:
            # Snapshot profile BEFORE bumping so build_messages reflects the
            # state the user-visible 'this is your Nth message' would describe.
            messages = build_messages(user_id, display_name, prompt)
            reply_raw = await asyncio.to_thread(
                call_llm, messages, MAX_REPLY_TOKENS, 60,
            )
            reply = humanize_response(reply_raw)
        except Exception as e:
            # NEVER leak chain details to user. Internal log gets full error;
            # user sees a casual filler so the bot stays in-persona even on
            # backend failure.
            log.error(f"LLM failed: {type(e).__name__}: {str(e)[:240]}")
            reply = random.choice(_CASUAL_FILLERS)
            for chunk_text in chunk(reply):
                try:
                    await msg.reply(chunk_text)
                except discord.HTTPException:
                    pass
            return

    # Persist the turn-pair THEN bump counters so n_messages reflects the
    # number of user turns recorded.
    save_turn(user_id, channel_id, "user", prompt)
    save_turn(user_id, channel_id, "assistant", reply)
    n = bump_messages(user_id, display_name=display_name, locale=locale)

    # Roll the rolled summary every SUMMARY_REFRESH_EVERY user msgs. Fire and
    # forget — the next message's build_messages will pick it up if ready.
    p_now = load_profile(user_id)
    if n - (p_now.get("last_summary_at_msg", 0) or 0) >= SUMMARY_REFRESH_EVERY:
        asyncio.create_task(asyncio.to_thread(maybe_summarize_profile_sync, user_id))

    for chunk_text in chunk(reply):
        try:
            await msg.reply(chunk_text)
        except discord.HTTPException as e:
            log.error(f"discord send failed: {e}")
            break



# ─── Customer-poll integration (two-way: Supabase ↔ Discord) ───────────────
# customer-poll-daemon enqueues into Supabase customer_polls table.
# Bot reads pending polls every 10min, posts via bot client (NOT webhook,
# webhooks are one-way), adds 3 emoji reactions, and listens for clicks
# via on_raw_reaction_add to tally votes back into the same Supabase row.

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_KEY", "")
POLL_CHANNEL_ID = int(os.environ.get("DISCORD_POLL_CHANNEL_ID", "0") or 0)

POLL_EMOJI = {"✅": "yes", "❌": "no", "🤔": "maybe"}


def _sb_request(method: str, path: str, body=None, headers_extra=None):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "surrogate-1-discord-bot/1.0 (+server)",
    }
    if headers_extra:
        h.update(headers_extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except Exception as e:
        log.error(f"supabase {method} {path}: {e}")
        return None


@tasks.loop(minutes=10)
async def check_pending_polls():
    """Pull rows from customer_polls where status='pending', post each, mark posted."""
    if not POLL_CHANNEL_ID:
        return
    rows = _sb_request("GET", "customer_polls?status=eq.pending&order=created_at.asc&limit=5")
    if not rows:
        return
    # fetch_channel hits the API instead of cache; works even when the
    # channel was never cached (e.g. only sent to once, or DM channel)
    try:
        channel = await client.fetch_channel(POLL_CHANNEL_ID)
    except Exception as _ce:
        log.warning(f"poll channel {POLL_CHANNEL_ID} not fetchable: {_ce}")
        return
    for poll in rows:
        try:
            qs = poll.get("questions") or []
            text = (
                "🔬 **Weekly customer poll**\n\n"
                f"**Hypothesis**: {poll.get('hypothesis','?')}\n\n" +
                "\n".join(f"**Q{i+1}:** {q}" for i, q in enumerate(qs)) +
                "\n\nReact: ✅ yes  •  ❌ no  •  🤔 maybe"
            )
            msg = await channel.send(text[:1900])
            for emo in POLL_EMOJI:
                await msg.add_reaction(emo)
            _sb_request(
                "PATCH",
                f"customer_polls?id=eq.{poll['id']}",
                {"posted_to": str(POLL_CHANNEL_ID),
                 "posted_msg_id": str(msg.id),
                 "status": "posted",
                 "posted_at": "now()"},
                headers_extra={"Prefer": "return=minimal"},
            )
            log.info(f"poll posted msg_id={msg.id} item={poll.get('item_id','?')[:30]}")
        except Exception as e:
            log.error(f"failed to post poll {poll.get('id')}: {e}")


@check_pending_polls.before_loop
async def _wait_ready():
    await client.wait_until_ready()


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Tally votes when users click ✅ ❌ 🤔 on a tracked poll message."""
    if payload.user_id == client.user.id:
        return
    emo = str(payload.emoji)
    if emo not in POLL_EMOJI:
        return
    rows = _sb_request("GET", f"customer_polls?posted_msg_id=eq.{payload.message_id}&select=id")
    if not rows:
        return
    poll_id = rows[0]["id"]
    col = f"{POLL_EMOJI[emo]}_count"
    # SQL increment via PostgREST: use rpc or fetch+update.
    cur = _sb_request("GET", f"customer_polls?id=eq.{poll_id}&select={col}")
    if not cur:
        return
    n = (cur[0].get(col) or 0) + 1
    _sb_request(
        "PATCH",
        f"customer_polls?id=eq.{poll_id}",
        {col: n},
        headers_extra={"Prefer": "return=minimal"},
    )
    log.info(f"poll vote {emo}={n} on poll_id={poll_id} (msg={payload.message_id})")



def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[discord-bot] DISCORD_BOT_TOKEN not set; exiting", flush=True)
        return
    log.info("starting")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
