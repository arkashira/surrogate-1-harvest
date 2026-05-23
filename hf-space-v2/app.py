"""Surrogate-1 v2 — Gradio Space for Qwen2.5-Coder-7B + LoRA inference.

Runs on HF ZeroGPU (A10G) when promoted via PRO. Falls back to CPU
inference if no GPU is allocated, but expect 60+ s per response in that
case — only useful for smoke-testing.

Key design:
  * `@spaces.GPU` decorates the inference function so ZeroGPU spawns a
    GPU process per request, releases it when done. Avoids hogging the
    free 25k min/mo budget.
  * Adapter is loaded LAZILY inside the GPU function — base model is
    pre-loaded via the `preload_from_hub` README directive so cold-start
    is just LoRA weights (~50 MB) instead of full 7.6B (~15 GB).
  * Streaming output via Gradio's generator API so users see tokens
    arrive as the model produces them.
"""
from __future__ import annotations

import os
import threading
from typing import Iterator

import gradio as gr
import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TextIteratorStreamer,
)

# ZeroGPU runtime is provided by `spaces` package on HF Spaces. We import
# defensively so the module also runs locally without it (CPU fallback).
try:
    import spaces  # type: ignore
    HAS_ZERO_GPU = True
except ImportError:
    HAS_ZERO_GPU = False
    class _DummyDecorator:
        def __call__(self, fn): return fn
        def GPU(self, *args, **kwargs):
            def deco(fn): return fn
            return deco
    spaces = _DummyDecorator()  # type: ignore


BASE_MODEL = os.environ.get("BASE_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
ADAPTER_REPO = os.environ.get("ADAPTER_REPO", "axentx/surrogate-1-coder-7b-lora-v2")
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

SYSTEM_PROMPT_DEFAULT = (
    "You are Surrogate-1, a coding + DevOps assistant trained on 1M+ "
    "real-world coding/devops/SRE pain points harvested by the axentx "
    "fleet. Be concise, give working code, link to docs only when needed."
)


# Lazy-loaded model cache — populated on first GPU call, kept warm by HF
# Spaces' container lifetime (Spaces cycle when idle for hours).
_model = None
_tokenizer = None
_lock = threading.Lock()


def _load_model_and_tokenizer():
    global _model, _tokenizer
    with _lock:
        if _model is not None:
            return _model, _tokenizer

        tok = AutoTokenizer.from_pretrained(
            BASE_MODEL, trust_remote_code=True, token=HF_TOKEN,
        )
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token

        # 4-bit quant — fits the 7.6B model in ~5 GB on A10G with room
        # for KV cache + adapter weights.
        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        ) if torch.cuda.is_available() else None

        kwargs = dict(
            trust_remote_code=True,
            torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
            token=HF_TOKEN,
        )
        if bnb is not None:
            kwargs["quantization_config"] = bnb

        m = AutoModelForCausalLM.from_pretrained(BASE_MODEL, **kwargs)

        # Apply LoRA adapter. peft.PeftModel wraps the base; resulting
        # forward pass adds the adapter delta. Free to swap adapters at
        # runtime if we ship multiple variants later.
        try:
            from peft import PeftModel
            m = PeftModel.from_pretrained(m, ADAPTER_REPO, token=HF_TOKEN)
            print(f"[v2] loaded adapter {ADAPTER_REPO}", flush=True)
        except Exception as e:
            print(f"[v2] adapter load failed ({e}); serving base model only",
                  flush=True)

        m.eval()
        _model = m
        _tokenizer = tok
        return m, tok


@spaces.GPU(duration=60) if HAS_ZERO_GPU else (lambda f: f)
def chat_stream(
    user_msg: str,
    history: list,
    system_prompt: str = SYSTEM_PROMPT_DEFAULT,
    max_new_tokens: int = 1024,
    temperature: float = 0.3,
    top_p: float = 0.9,
) -> Iterator[str]:
    """Yields the assistant response one token at a time."""
    model, tok = _load_model_and_tokenizer()

    # Build chat-template-formatted prompt. Qwen2.5-Coder uses ChatML.
    messages = [{"role": "system", "content": system_prompt}]
    for u, a in (history or []):
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": user_msg})

    prompt = tok.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
    )
    inputs = tok(prompt, return_tensors="pt").to(model.device)

    streamer = TextIteratorStreamer(
        tok, skip_prompt=True, skip_special_tokens=True,
    )
    gen_kwargs = dict(
        **inputs,
        streamer=streamer,
        max_new_tokens=max_new_tokens,
        do_sample=temperature > 0,
        temperature=temperature,
        top_p=top_p,
        pad_token_id=tok.pad_token_id,
    )
    thread = threading.Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()

    out = ""
    for chunk in streamer:
        out += chunk
        yield out


def respond(message, chat_history, system_prompt, max_tokens, temperature):
    """Gradio handler — returns updated chat_history each yield."""
    chat_history = chat_history or []
    chat_history.append([message, ""])
    for partial in chat_stream(
        user_msg=message,
        history=chat_history[:-1],
        system_prompt=system_prompt,
        max_new_tokens=int(max_tokens),
        temperature=float(temperature),
    ):
        chat_history[-1][1] = partial
        yield chat_history, ""


with gr.Blocks(title="Surrogate-1 v2", theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        f"# Surrogate-1 v2\n"
        f"Base: `{BASE_MODEL}` + adapter: `{ADAPTER_REPO}`\n\n"
        f"Trained on the streaming axentx harvest: 1M+ coding / devops / SRE / "
        f"agent / reasoning pairs from public HF datasets + internal pain mining."
    )
    with gr.Row():
        with gr.Column(scale=4):
            chatbot = gr.Chatbot(label="conversation", height=520)
            with gr.Row():
                msg = gr.Textbox(
                    placeholder="Ask me to write code, debug a stack trace, "
                                "design a Terraform module, ...",
                    show_label=False,
                    scale=10,
                )
                send = gr.Button("Send", scale=1, variant="primary")
        with gr.Column(scale=1):
            system_prompt = gr.Textbox(
                label="system prompt",
                value=SYSTEM_PROMPT_DEFAULT,
                lines=4,
            )
            max_tokens = gr.Slider(64, 4096, value=1024, step=64,
                                    label="max new tokens")
            temperature = gr.Slider(0.0, 1.5, value=0.3, step=0.1,
                                    label="temperature")
            clear = gr.Button("Clear chat")

    send.click(
        respond,
        inputs=[msg, chatbot, system_prompt, max_tokens, temperature],
        outputs=[chatbot, msg],
    )
    msg.submit(
        respond,
        inputs=[msg, chatbot, system_prompt, max_tokens, temperature],
        outputs=[chatbot, msg],
    )
    clear.click(lambda: ([], ""), None, [chatbot, msg])

    gr.Examples(
        examples=[
            "Write a FastAPI endpoint that streams server-sent events.",
            "Debug this: TypeError: Cannot read property 'map' of undefined.",
            "Terraform module for an autoscaling EKS node group with spot "
            "instances and a managed bottlerocket AMI.",
            "How do I prevent N+1 queries when paginating a Django ORM list?",
        ],
        inputs=msg,
    )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "7860")),
        show_error=True,
    )
